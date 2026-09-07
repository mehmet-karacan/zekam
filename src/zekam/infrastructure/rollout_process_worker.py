# ruff: noqa: E501
"""Spawned rollout workers backed by a transactional local selector journal."""

from __future__ import annotations

import datetime as dt
import json
import multiprocessing as mp
import sqlite3
from contextlib import closing, suppress
from dataclasses import replace
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from zekam.application.local_attestation import Ed25519ReceiptSigner, Ed25519ReceiptVerifier
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.domain.evolution_rollout import (
    RolloutObservation,
    RolloutPlan,
    RolloutStage,
    RolloutStatus,
    RolloutVerification,
    RolloutWorkerIdentity,
)
from zekam.infrastructure.local_file_security import (
    private_directory,
    private_regular,
    restrict_private_file,
)
from zekam.infrastructure.process_identity import process_incarnation_token

_VERIFIER_TOKEN = object()
_STATE_FILE = "rollout-state.sqlite3"
_STATE_SCHEMA = r"""
pragma foreign_keys=on;
create table rollout_state_schema(singleton integer primary key check(singleton=1),version integer not null);
insert into rollout_state_schema values(1,1);
create table rollout_artifact(artifact_digest text primary key,implementation text not null,body_json text not null,check(implementation in('identity-v1','salted-v1'))) strict;
create table active_selector(pointer_name text primary key,artifact_digest text not null,revision integer not null check(revision>0),body_json text not null) strict;
create table rollout_intent(plan_digest text primary key,body_json text not null) strict;
create table rollout_evidence(plan_digest text primary key,stage text not null,status text not null,before_digest text not null,after_digest text not null,observation_count integer not null,production_effect_count integer not null,started_at text not null,finished_at text not null,body_json text not null,check(stage in('shadow','canary','activation','rollback')),check(status in('completed','failed','recovery-required'))) strict;
create table canary_usage(plan_digest text not null references rollout_evidence,input_digest text not null,candidate_artifact_digest text not null,outcome_digest text not null,body_json text not null,primary key(plan_digest,input_digest)) without rowid, strict;
create table pointer_transition(plan_digest text primary key references rollout_evidence,pointer_name text not null,before_digest text not null,after_digest text not null,selector_revision integer not null,body_json text not null) strict;
create table recovery_transition(plan_digest text primary key references rollout_evidence,pointer_name text not null,before_digest text not null,after_digest text not null,selector_revision integer not null,recovered_at text not null,body_json text not null) strict;
create trigger rollout_evidence_no_update before update on rollout_evidence begin select raise(abort,'append-only'); end;
create trigger rollout_evidence_no_delete before delete on rollout_evidence begin select raise(abort,'append-only'); end;
create trigger rollout_intent_no_update before update on rollout_intent begin select raise(abort,'append-only'); end;
create trigger rollout_intent_no_delete before delete on rollout_intent begin select raise(abort,'append-only'); end;
create trigger canary_usage_no_update before update on canary_usage begin select raise(abort,'append-only'); end;
create trigger canary_usage_no_delete before delete on canary_usage begin select raise(abort,'append-only'); end;
create trigger pointer_transition_no_update before update on pointer_transition begin select raise(abort,'append-only'); end;
create trigger pointer_transition_no_delete before delete on pointer_transition begin select raise(abort,'append-only'); end;
create trigger recovery_transition_no_update before update on recovery_transition begin select raise(abort,'append-only'); end;
create trigger recovery_transition_no_delete before delete on recovery_transition begin select raise(abort,'append-only'); end;
create trigger rollout_artifact_no_update before update on rollout_artifact begin select raise(abort,'append-only'); end;
create trigger rollout_artifact_no_delete before delete on rollout_artifact begin select raise(abort,'append-only'); end;
"""


def _schema_digest(db: sqlite3.Connection) -> str:
    rows = db.execute(
        "select type,name,sql from sqlite_master where type in('table','trigger') "
        "and name not like 'sqlite_%' order by type,name"
    ).fetchall()
    return digest([tuple(map(str, row)) for row in rows])


with closing(sqlite3.connect(":memory:")) as _schema_db:
    _schema_db.executescript(_STATE_SCHEMA)
    ROLLOUT_STATE_SCHEMA_DIGEST = _schema_digest(_schema_db)


def _state_path(root: Path) -> Path:
    return root / _STATE_FILE


def rollout_resource_manifest(root: Path, pointer_name: str) -> str:
    """Bind authority to one exact private selector store and its schema."""

    if not root.is_absolute() or not pointer_name:
        raise ValidationFailed("Rollout resource manifest requires exact local identity")
    with closing(_connect_state(root)) as db:
        row = db.execute(
            "select artifact_digest,revision from active_selector where pointer_name=?",
            (pointer_name,),
        ).fetchone()
    if row is None:
        raise PolicyViolation("Rollout resource manifest selector missing")
    return digest(
        {
            "schema": "zekam-rollout-resource-manifest/v1",
            "state_schema_digest": ROLLOUT_STATE_SCHEMA_DIGEST,
            "root": str(root.resolve()).casefold(),
            "pointer_name": pointer_name,
        }
    )


def load_rollout_plan(root: Path, plan_digest: str) -> RolloutPlan:
    """Reconstruct an executed plan only from its append-only durable intent."""

    parse_digest(plan_digest)
    with closing(_connect_state(root)) as db:
        row = db.execute(
            "select body_json from rollout_intent where plan_digest=?", (plan_digest,)
        ).fetchone()
    if row is None:
        raise PolicyViolation("Durable rollout intent missing")
    try:
        raw_body = json.loads(str(row[0]))
        body = dict(raw_body)
        if (
            body.pop("schema") != "zekam-local-rollout-plan/v1"
            or body.pop("workload_classification") != "local-deterministic-fixture"
            or body.pop("production_traffic") is not False
        ):
            raise PolicyViolation("Durable rollout intent classification drift")
        plan = RolloutPlan(
            candidate_digest=body["candidate_digest"],
            evaluation_digest=body["evaluation_digest"],
            stage=RolloutStage(body["stage"]),
            logical_resource=body["logical_resource"],
            pointer_name=body["pointer_name"],
            expected_before_digest=body["expected_before_digest"],
            candidate_artifact_digest=body["candidate_artifact_digest"],
            last_known_good_digest=body["last_known_good_digest"],
            fixture_digest=body["fixture_digest"],
            harness_digest=body["harness_digest"],
            resource_manifest_digest=body["resource_manifest_digest"],
            observation_inputs=tuple(body["observation_inputs"]),
            minimum_observations=body["minimum_observations"],
            grant_digest=body["grant_digest"],
            authorization_digest=body["authorization_digest"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PolicyViolation("Durable rollout intent corrupt") from exc
    if raw_body != plan.body() or plan.plan_digest != plan_digest:
        raise PolicyViolation("Durable rollout intent digest drift")
    return plan


def _connect_state(root: Path) -> sqlite3.Connection:
    path = _state_path(root)
    if not private_directory(root) or not private_regular(path):
        raise PolicyViolation("Rollout state identity invalid")
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("pragma foreign_keys=on")
    db.execute("pragma busy_timeout=5000")
    if (
        db.execute("select version from rollout_state_schema").fetchone()[0] != 1
        or _schema_digest(db) != ROLLOUT_STATE_SCHEMA_DIGEST
        or db.execute("pragma foreign_key_check").fetchone() is not None
    ):
        db.close()
        raise PolicyViolation("Rollout state schema drift")
    selectors = db.execute(
        "select pointer_name,artifact_digest,revision,body_json from active_selector"
    ).fetchall()
    for row in selectors:
        expected = {
            "schema": "zekam-rollout-active-selector/v1",
            "pointer_name": str(row[0]),
            "artifact_digest": str(row[1]),
            "revision": int(row[2]),
        }
        if str(row[3]) != canonical_json(expected):
            db.close()
            raise PolicyViolation("Rollout active selector body drift")
    return db


def bootstrap_rollout_fixture(root: Path, pointer_name: str, initial_digest: str) -> None:
    """Create one private deterministic selector; never overwrite existing state."""

    parse_digest(initial_digest)
    if not pointer_name or "/" in pointer_name or "\\" in pointer_name:
        raise ValidationFailed("Rollout fixture exact pointer name required")
    if not private_directory(root):
        raise PolicyViolation("Rollout fixture private root required")
    path = _state_path(root)
    if path.exists() or path.is_symlink():
        raise ConcurrencyConflict("Rollout fixture state already exists")
    body = {
        "schema": "zekam-rollout-active-selector/v1",
        "pointer_name": pointer_name,
        "artifact_digest": initial_digest,
        "revision": 1,
    }
    with closing(sqlite3.connect(path)) as db:
        db.executescript(_STATE_SCHEMA)
        artifact = {
            "schema": "zekam-rollout-artifact/v1",
            "artifact_digest": initial_digest,
            "implementation": "identity-v1",
        }
        db.execute(
            "insert into rollout_artifact values(?,?,?)",
            (initial_digest, "identity-v1", canonical_json(artifact)),
        )
        db.execute(
            "insert into active_selector values(?,?,?,?)",
            (pointer_name, initial_digest, 1, canonical_json(body)),
        )
        db.commit()
    restrict_private_file(path)
    with closing(_connect_state(root)) as db:
        if db.execute("pragma integrity_check").fetchone()[0] != "ok":
            raise PolicyViolation("Rollout fixture bootstrap readback failed")


def register_rollout_artifact(
    root: Path, artifact_digest: str, implementation: str
) -> None:
    """Register one executable fixture artifact before a rollout plan is authorized."""

    parse_digest(artifact_digest)
    if implementation not in {"identity-v1", "salted-v1"}:
        raise ValidationFailed("Rollout fixture implementation unsupported")
    body = {
        "schema": "zekam-rollout-artifact/v1",
        "artifact_digest": artifact_digest,
        "implementation": implementation,
    }
    with closing(_connect_state(root)) as db:
        db.execute("begin immediate")
        existing = db.execute(
            "select implementation,body_json from rollout_artifact where artifact_digest=?",
            (artifact_digest,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (implementation, canonical_json(body)):
                raise ConcurrencyConflict("Rollout fixture artifact drift")
            db.rollback()
            return
        db.execute(
            "insert into rollout_artifact values(?,?,?)",
            (artifact_digest, implementation, canonical_json(body)),
        )
        db.commit()


def rollout_selector(root: Path, pointer_name: str) -> str:
    with closing(_connect_state(root)) as db:
        row = db.execute(
            "select artifact_digest from active_selector where pointer_name=?",
            (pointer_name,),
        ).fetchone()
    if row is None:
        raise PolicyViolation("Rollout selector missing")
    return str(row[0])


def rollout_recovery_status(
    root: Path, settled_plan_digests: tuple[str, ...]
) -> dict[str, object]:
    """Expose durable worker evidence not yet settled in the improvement ledger."""

    for value in settled_plan_digests:
        parse_digest(value)
    with closing(_connect_state(root)) as db:
        rows = db.execute(
            "select e.plan_digest,e.stage,e.status,e.production_effect_count "
            "from rollout_evidence e left join recovery_transition r "
            "on r.plan_digest=e.plan_digest where r.plan_digest is null "
            "order by e.finished_at,e.plan_digest"
        ).fetchall()
    settled = set(settled_plan_digests)
    pending = tuple(
        {
            "plan_digest": str(row[0]),
            "stage": str(row[1]),
            "status": str(row[2]),
            "production_effect_count": int(row[3]),
        }
        for row in rows
        if str(row[0]) not in settled
    )
    return {
        "schema": "zekam-rollout-recovery-status/v1",
        "state": "recovery-required" if pending else "clean",
        "pending": pending,
        "blind_replay_allowed": False,
    }


def recover_unsettled_activation(root: Path, plan: RolloutPlan) -> str:
    """Settle an interrupted stage; reverse pointer effects without blind replay."""

    if plan.resource_manifest_digest != rollout_resource_manifest(root, plan.pointer_name):
        raise PolicyViolation("Rollout recovery resource manifest drift")
    recovered_at = dt.datetime.now(dt.UTC)
    with closing(_connect_state(root)) as db:
        db.execute("begin immediate")
        evidence = db.execute(
            "select status,after_digest from rollout_evidence where plan_digest=?",
            (plan.plan_digest,),
        ).fetchone()
        selector = db.execute(
            "select artifact_digest,revision from active_selector where pointer_name=?",
            (plan.pointer_name,),
        ).fetchone()
        existing = db.execute(
            "select body_json from recovery_transition where plan_digest=?",
            (plan.plan_digest,),
        ).fetchone()
        if existing is not None:
            db.rollback()
            return digest(json.loads(str(existing[0])))
        if evidence is None or selector is None or str(evidence[0]) not in {
            RolloutStatus.COMPLETED.value,
            RolloutStatus.RECOVERY_REQUIRED.value,
        }:
            raise PolicyViolation("Rollout recovery selector/evidence drift")
        reverse_activation = plan.stage is RolloutStage.ACTIVATION
        expected_after = (
            plan.last_known_good_digest
            if plan.stage is RolloutStage.ROLLBACK
            else plan.candidate_artifact_digest
            if plan.stage is RolloutStage.ACTIVATION
            else plan.expected_before_digest
        )
        if str(selector[0]) != expected_after:
            raise PolicyViolation("Rollout recovery preserves later selector edits")
        recovery_after = plan.expected_before_digest if reverse_activation else expected_after
        revision = int(selector[1]) + (1 if reverse_activation else 0)
        selector_body = {
            "schema": "zekam-rollout-active-selector/v1",
            "pointer_name": plan.pointer_name,
            "artifact_digest": recovery_after,
            "revision": revision,
        }
        if reverse_activation:
            changed = db.execute(
                "update active_selector set artifact_digest=?,revision=?,body_json=? "
                "where pointer_name=? and artifact_digest=? and revision=?",
                (
                    recovery_after,
                    revision,
                    canonical_json(selector_body),
                    plan.pointer_name,
                    expected_after,
                    int(selector[1]),
                ),
            ).rowcount
            if changed != 1:
                raise ConcurrencyConflict("Rollout recovery compare-and-swap drift")
        body = {
            "schema": "zekam-rollout-recovery-transition/v1",
            "plan_digest": plan.plan_digest,
            "pointer_name": plan.pointer_name,
            "before_digest": expected_after,
            "after_digest": recovery_after,
            "selector_revision": revision,
            "recovered_at": recovered_at,
            "blind_replay_allowed": False,
        }
        db.execute(
            "insert into recovery_transition values(?,?,?,?,?,?,?)",
            (
                plan.plan_digest,
                plan.pointer_name,
                expected_after,
                recovery_after,
                revision,
                recovered_at.isoformat(),
                canonical_json(body),
            ),
        )
        db.commit()
    return digest(body)


def _execute_fixture_artifact(
    implementation: str, input_digest: str, harness_digest: str
) -> str:
    if implementation == "identity-v1":
        return digest({"input": input_digest, "harness": harness_digest})
    if implementation == "salted-v1":
        return digest(
            {"input": input_digest, "harness": harness_digest, "salt": "candidate-v1"}
        )
    raise PolicyViolation("Rollout artifact implementation drift")


def _outcomes(db: sqlite3.Connection, plan: RolloutPlan) -> list[dict[str, str]]:
    implementations: dict[str, str] = {}
    for artifact_digest in (
        plan.last_known_good_digest,
        plan.candidate_artifact_digest,
    ):
        row = db.execute(
            "select implementation,body_json from rollout_artifact where artifact_digest=?",
            (artifact_digest,),
        ).fetchone()
        if row is None:
            raise PolicyViolation("Rollout executable fixture artifact missing")
        artifact = json.loads(str(row[1]))
        if artifact.get("artifact_digest") != artifact_digest:
            raise PolicyViolation("Rollout executable fixture artifact drift")
        implementations[artifact_digest] = str(row[0])
    return [
        {
            "input_digest": value,
            "baseline_outcome": _execute_fixture_artifact(
                implementations[plan.last_known_good_digest], value, plan.harness_digest
            ),
            "candidate_outcome": _execute_fixture_artifact(
                implementations[plan.candidate_artifact_digest], value, plan.harness_digest
            ),
        }
        for value in plan.observation_inputs
    ]


def _insert_evidence(
    db: sqlite3.Connection,
    plan: RolloutPlan,
    status: RolloutStatus,
    before: str,
    after: str,
    outcomes: list[dict[str, str]],
    effect_count: int,
    started: dt.datetime,
    finished: dt.datetime,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": "zekam-local-rollout-evidence/v2",
        "plan_digest": plan.plan_digest,
        "stage": plan.stage.value,
        "status": status.value,
        "before_digest": before,
        "after_digest": after,
        "outcomes": outcomes,
        "workload_classification": "local-deterministic-fixture",
        "production_traffic": False,
        "real_candidate_usage_count": (
            len(outcomes) if plan.stage is RolloutStage.CANARY else 0
        ),
    }
    db.execute(
        "insert into rollout_evidence values(?,?,?,?,?,?,?,?,?,?)",
        (
            plan.plan_digest,
            plan.stage.value,
            status.value,
            before,
            after,
            len(outcomes),
            effect_count,
            started.isoformat(),
            finished.isoformat(),
            canonical_json(body),
        ),
    )
    return body


def _run_plan(
    root: Path, plan: RolloutPlan, identity: RolloutWorkerIdentity
) -> RolloutObservation:
    plan.__post_init__()
    if plan.resource_manifest_digest != rollout_resource_manifest(root, plan.pointer_name):
        raise PolicyViolation("Rollout resource manifest drift")
    started = dt.datetime.now(dt.UTC)
    with closing(_connect_state(root)) as db:
        db.execute("begin immediate")
        intent_json = canonical_json(plan.body())
        existing_intent = db.execute(
            "select body_json from rollout_intent where plan_digest=?", (plan.plan_digest,)
        ).fetchone()
        if existing_intent is not None:
            if str(existing_intent[0]) != intent_json:
                raise PolicyViolation("Rollout durable intent replay drift")
            raise ConcurrencyConflict("Rollout plan already executed")
        db.execute(
            "insert into rollout_intent values(?,?)", (plan.plan_digest, intent_json)
        )
        row = db.execute(
            "select artifact_digest,revision from active_selector where pointer_name=?",
            (plan.pointer_name,),
        ).fetchone()
        if row is None:
            raise PolicyViolation("Rollout active selector missing")
        before = str(row["artifact_digest"])
        if before != plan.expected_before_digest:
            status = RolloutStatus.RECOVERY_REQUIRED
            after = before
            effect_count = 0
            computed: list[dict[str, str]] = []
        else:
            status = RolloutStatus.COMPLETED
            computed = _outcomes(db, plan)
            target = (
                plan.last_known_good_digest
                if plan.stage is RolloutStage.ROLLBACK
                else plan.candidate_artifact_digest
            )
            if plan.stage in {RolloutStage.ACTIVATION, RolloutStage.ROLLBACK}:
                new_revision = int(row["revision"]) + 1
                selector = {
                    "schema": "zekam-rollout-active-selector/v1",
                    "pointer_name": plan.pointer_name,
                    "artifact_digest": target,
                    "revision": new_revision,
                }
                changed = db.execute(
                    "update active_selector set artifact_digest=?,revision=?,body_json=? "
                    "where pointer_name=? and artifact_digest=? and revision=?",
                    (
                        target,
                        new_revision,
                        canonical_json(selector),
                        plan.pointer_name,
                        plan.expected_before_digest,
                        int(row["revision"]),
                    ),
                ).rowcount
                if changed != 1:
                    raise PolicyViolation("Rollout selector compare-and-swap drift")
                after = target
                effect_count = 1
            else:
                after = before
                effect_count = 0
        finished = dt.datetime.now(dt.UTC)
        evidence = _insert_evidence(
            db,
            plan,
            status,
            before,
            after,
            computed,
            effect_count,
            started,
            finished,
        )
        if status is RolloutStatus.COMPLETED and plan.stage is RolloutStage.CANARY:
            for item in computed:
                usage = {
                    "schema": "zekam-rollout-canary-usage/v1",
                    "plan_digest": plan.plan_digest,
                    "input_digest": item["input_digest"],
                    "candidate_artifact_digest": plan.candidate_artifact_digest,
                    "outcome_digest": item["candidate_outcome"],
                }
                db.execute(
                    "insert into canary_usage values(?,?,?,?,?)",
                    (
                        plan.plan_digest,
                        item["input_digest"],
                        plan.candidate_artifact_digest,
                        item["candidate_outcome"],
                        canonical_json(usage),
                    ),
                )
        if status is RolloutStatus.COMPLETED and plan.stage in {
            RolloutStage.ACTIVATION,
            RolloutStage.ROLLBACK,
        }:
            transition = {
                "schema": "zekam-rollout-pointer-transition/v1",
                "plan_digest": plan.plan_digest,
                "pointer_name": plan.pointer_name,
                "before_digest": before,
                "after_digest": after,
                "selector_revision": int(row["revision"]) + 1,
            }
            db.execute(
                "insert into pointer_transition values(?,?,?,?,?,?)",
                (
                    plan.plan_digest,
                    plan.pointer_name,
                    before,
                    after,
                    transition["selector_revision"],
                    canonical_json(transition),
                ),
            )
        readback = db.execute(
            "select artifact_digest from active_selector where pointer_name=?",
            (plan.pointer_name,),
        ).fetchone()
        if readback is None or str(readback[0]) != after:
            raise PolicyViolation("Rollout selector transactional readback drift")
        db.commit()
    return RolloutObservation(
        plan.plan_digest,
        plan.stage,
        status,
        before,
        after,
        digest(evidence),
        len(computed),
        effect_count,
        identity,
        started,
        finished,
        "ed25519:placeholder",
    )


def _verify_plan(
    root: Path,
    plan: RolloutPlan,
    observation: RolloutObservation,
    executor_key: bytes,
    identity: RolloutWorkerIdentity,
) -> RolloutVerification:
    verified_at = dt.datetime.now(dt.UTC)
    try:
        if plan.resource_manifest_digest != rollout_resource_manifest(root, plan.pointer_name):
            raise PolicyViolation("Rollout verifier resource manifest drift")
        executor = Ed25519ReceiptVerifier(Ed25519PublicKey.from_public_bytes(executor_key))
        with closing(_connect_state(root)) as db:
            db.execute("pragma query_only=on")
            expected_outcomes = _outcomes(db, plan)
            evidence_row = db.execute(
                "select * from rollout_evidence where plan_digest=?", (plan.plan_digest,)
            ).fetchone()
            selector = db.execute(
                "select artifact_digest from active_selector where pointer_name=?",
                (plan.pointer_name,),
            ).fetchone()
            canary_rows = db.execute(
                "select input_digest,candidate_artifact_digest,outcome_digest "
                "from canary_usage where plan_digest=? order by input_digest",
                (plan.plan_digest,),
            ).fetchall()
            transition = db.execute(
                "select pointer_name,before_digest,after_digest,selector_revision "
                "from pointer_transition where plan_digest=?",
                (plan.plan_digest,),
            ).fetchone()
        if evidence_row is None or selector is None:
            raise PolicyViolation("Rollout verifier evidence missing")
        evidence = json.loads(str(evidence_row["body_json"]))
        expected_after = (
            plan.last_known_good_digest
            if plan.stage is RolloutStage.ROLLBACK
            else plan.candidate_artifact_digest
            if plan.stage is RolloutStage.ACTIVATION
            else plan.expected_before_digest
        )
        expected_canary = [
            (
                item["input_digest"],
                plan.candidate_artifact_digest,
                item["candidate_outcome"],
            )
            for item in expected_outcomes
        ]
        canary_exact = (
            [tuple(map(str, row)) for row in canary_rows] == expected_canary
            if plan.stage is RolloutStage.CANARY
            else not canary_rows
        )
        transition_exact = (
            transition is not None
            and tuple(map(str, transition[:3]))
            == (plan.pointer_name, plan.expected_before_digest, expected_after)
            if plan.stage in {RolloutStage.ACTIVATION, RolloutStage.ROLLBACK}
            else transition is None
        )
        accepted = bool(
            observation.plan_digest == plan.plan_digest
            and observation.stage is plan.stage
            and observation.status is RolloutStatus.COMPLETED
            and observation.observation_count == len(plan.observation_inputs)
            and observation.observation_count >= plan.minimum_observations
            and observation.before_digest == plan.expected_before_digest
            and observation.after_digest == expected_after
            and str(selector[0]) == expected_after
            and evidence["outcomes"] == expected_outcomes
            and evidence["real_candidate_usage_count"]
            == (len(expected_outcomes) if plan.stage is RolloutStage.CANARY else 0)
            and digest(evidence) == observation.evidence_digest
            and canary_exact
            and transition_exact
            and executor.matches_receipt_digest(
                observation.receipt_body(), observation.execution_receipt
            )
        )
        status = RolloutStatus.COMPLETED if accepted else RolloutStatus.FAILED
        readback = digest(
            {
                "evidence": evidence,
                "selector": str(selector[0]),
                "canary_usage": [tuple(map(str, row)) for row in canary_rows],
                "transition": None if transition is None else tuple(map(str, transition)),
            }
        )
    except (OSError, sqlite3.Error, ValueError, ValidationFailed, PolicyViolation):
        accepted = False
        status = RolloutStatus.FAILED
        readback = digest("rollout-readback-failed")
    return RolloutVerification(
        observation.observation_digest,
        plan.plan_digest,
        accepted,
        status,
        readback,
        identity,
        verified_at,
        "ed25519:placeholder",
    )


def _worker_main(
    channel: Connection,
    root: Path,
    assignment_id: UUID,
    implementation_digest: str,
    challenge_digest: str,
    role: str,
) -> None:
    key = Ed25519PrivateKey.generate()
    signer = Ed25519ReceiptSigner(key)
    pid = mp.current_process().pid
    if pid is None:
        raise RuntimeError("Rollout worker PID unavailable")
    token = process_incarnation_token(pid)
    if token is None:
        raise RuntimeError("Rollout worker incarnation unavailable")
    draft_identity = RolloutWorkerIdentity(
        assignment_id,
        role,
        pid,
        token,
        implementation_digest,
        challenge_digest,
        "ed25519:placeholder",
    )
    identity = replace(
        draft_identity,
        boundary_receipt=signer.seal_digest(draft_identity.boundary_body()),
    )
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    channel.send((identity, public_key))
    try:
        while True:
            message = channel.recv()
            if message == "exit":
                return
            command = message[0] if type(message) is tuple and message else None
            if command == "execute" and role == "executor":
                observation = _run_plan(root, message[1], identity)
                channel.send(
                    replace(
                        observation,
                        execution_receipt=signer.seal_digest(observation.receipt_body()),
                    )
                )
            elif command == "verify" and role == "verifier":
                verification = _verify_plan(root, message[1], message[2], message[3], identity)
                channel.send(
                    replace(
                        verification,
                        verification_receipt=signer.seal_digest(verification.receipt_body()),
                    )
                )
            else:
                channel.send(None)
    finally:
        channel.close()


class RolloutWorkerVerifier:
    def __init__(
        self,
        token: object,
        process: BaseProcess,
        identity: RolloutWorkerIdentity,
        public_key: bytes,
    ) -> None:
        if token is not _VERIFIER_TOKEN:
            raise PolicyViolation("Rollout verifier requires spawned composition")
        self._process = process
        self.identity = identity
        self.public_key = public_key
        self._delegate = Ed25519ReceiptVerifier(Ed25519PublicKey.from_public_bytes(public_key))

    def matches(self, body: dict[str, Any], receipt: str) -> bool:
        return (
            self._process.is_alive()
            and self._process.pid == self.identity.process_id
            and process_incarnation_token(self.identity.process_id)
            == self.identity.process_start_token
            and self._delegate.matches_receipt_digest(body, receipt)
        )


class RolloutProcessWorker:
    def __init__(
        self,
        root: Path,
        assignment_id: UUID,
        implementation_digest: str,
        challenge_digest: str,
        role: str,
    ) -> None:
        if role not in {"executor", "verifier"} or not root.is_absolute():
            raise ValidationFailed("Rollout exact worker composition required")
        with closing(_connect_state(root)):
            pass
        self._root = root
        context = mp.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=_worker_main,
            args=(child, root, assignment_id, implementation_digest, challenge_digest, role),
        )
        process.start()
        child.close()
        identity, public_key = parent.recv()
        self._channel = parent
        self._process = process
        self.role = role
        self.identity: RolloutWorkerIdentity = identity
        self.verifier = RolloutWorkerVerifier(
            _VERIFIER_TOKEN, process, identity, public_key
        )
        if not self.verifier.matches(identity.boundary_body(), identity.boundary_receipt):
            self.close()
            raise PolicyViolation("Rollout worker startup receipt invalid")

    def execute(self, plan: RolloutPlan) -> RolloutObservation:
        if self.role != "executor":
            raise PolicyViolation("Rollout execute requires executor worker")
        self._channel.send(("execute", plan))
        result = self._channel.recv()
        if type(result) is not RolloutObservation:
            raise PolicyViolation("Rollout executor rejected plan")
        return result

    def recovery_status(
        self, settled_plan_digests: tuple[str, ...]
    ) -> dict[str, object]:
        return rollout_recovery_status(self._root, settled_plan_digests)

    def load_rollout_plan(self, plan_digest: str) -> RolloutPlan:
        return load_rollout_plan(self._root, plan_digest)

    def recover_unsettled_activation(self, plan: RolloutPlan) -> str:
        if self.role != "executor":
            raise PolicyViolation("Rollout recovery requires executor composition")
        return recover_unsettled_activation(self._root, plan)

    def verify(
        self,
        plan: RolloutPlan,
        observation: RolloutObservation,
        executor_verifier: RolloutWorkerVerifier,
    ) -> RolloutVerification:
        if self.role != "verifier" or executor_verifier.identity.role != "executor":
            raise PolicyViolation("Rollout verification requires independent worker roles")
        self._channel.send(("verify", plan, observation, executor_verifier.public_key))
        result = self._channel.recv()
        if type(result) is not RolloutVerification:
            raise PolicyViolation("Rollout verifier rejected observation")
        return result

    def close(self) -> None:
        if self._process.exitcode is None:
            with suppress(BrokenPipeError, EOFError, OSError):
                self._channel.send("exit")
            self._process.join(timeout=10)
        self._channel.close()
        if self._process.exitcode != 0:
            raise PolicyViolation("Rollout worker terminal failure")
