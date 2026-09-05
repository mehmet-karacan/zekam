from __future__ import annotations

import importlib.util
import json
import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, tzinfo
from functools import partial
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Literal, Self

import pytest

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.local_continuity import ContinuityBinding, ContinuityTail
from zekam.application.local_continuity_v4_internal import (
    DirectEffectOutcomeRequest,
    EffectClaimRequest,
    FrozenDirectEffectOutcomeSnapshot,
    FrozenEffectClaimSnapshot,
    FrozenTurnCommitSnapshot,
    TurnCommitRequest,
)
from zekam.application.local_runtime import LocalClaimedWork
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    PolicyViolation,
)
from zekam.infrastructure.sqlite import local_runtime as runtime_module
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.local_continuity_v4_internal import (
    SQLiteDormantV4InternalProducer,
    verify_b1_b2_internal_producers,
    verify_b1_internal_producers,
)
from zekam.infrastructure.sqlite.local_continuity_v4_writer import (
    SQLiteDormantV4CloseWriter,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

_runtime_module: Any = runtime_module

ROOT = Path("/Users/mkaracan/Projeler/akilli-kasa")
SOURCE = ROOT / "src/akilli_kasa/api/saglik.py"
SLICE_A_TEST = Path(__file__).with_name("test_local_continuity_v4_session_start.py")
CLAIM_NOW = "2026-09-03T23:00:00.123456+00:00"
CLAIMED_AT = "2026-09-03T23:00:01+00:00"
COMPLETED_AT = "2026-09-03T23:00:02+00:00"
WORK = "018f0000-0000-7000-8000-000000000401"
RUN = "018f0000-0000-7000-8000-000000000402"
CONFIG = "018f0000-0000-7000-8000-000000000403"


def _fixed_runtime_datetime(moment_text: str) -> type[datetime]:
    class _FixedRuntimeDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromisoformat(moment_text).astimezone(tz or UTC)

    return _FixedRuntimeDatetime


def _load_slice_a() -> ModuleType:
    spec = importlib.util.spec_from_file_location("b1_slice_a_fixture", SLICE_A_TEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _issue[SnapshotT](kind: type[SnapshotT], **values: object) -> SnapshotT:
    value = object.__new__(kind)
    fields: dict[str, object] = kind.__dataclass_fields__  # type: ignore[attr-defined]
    for name in fields:
        object.__setattr__(value, name, values[name])
    value.__post_init__()  # type: ignore[attr-defined]
    return value


class _TurnIssuer:
    def __init__(self, binding: ContinuityBinding) -> None:
        self.binding = binding
        self.fail_recheck = False
        self.counter = 0
        self.last: FrozenTurnCommitSnapshot | None = None
        self.snapshots: dict[tuple[str, str], FrozenTurnCommitSnapshot] = {}

    def snapshot(self, request: TurnCommitRequest) -> FrozenTurnCommitSnapshot:
        key = (request.role, request.item_ref)
        if key in self.snapshots:
            self.last = self.snapshots[key]
            return self.last
        self.counter += 1
        nonce = f"{self.counter:064x}"
        previous_generation = (
            None if self.last is None else self.last.store_generation_commitment_digest
        )
        content = digest(
            {
                "schema": "zekam-turn-content-authority-envelope/v1",
                "nonce_hex": nonce,
                "binding_digest": self.binding.binding_digest,
                "role": request.role,
                "item_ref": request.item_ref,
                "internal_content_digest": digest(f"private-content:{self.counter}"),
            }
        )
        generation = digest(
            {
                "schema": "zekam-turn-generation-authority-envelope/v1",
                "nonce_hex": nonce,
                "binding_digest": self.binding.binding_digest,
                "role": request.role,
                "item_ref": request.item_ref,
                "store_generation_id": f"generation-{self.counter}",
                "previous_store_generation_id": (
                    None if self.counter == 1 else f"generation-{self.counter - 1}"
                ),
            }
        )
        committed = f"2026-09-03T12:00:0{self.counter}+00:00"
        self.last = _issue(
            FrozenTurnCommitSnapshot,
            binding_digest=self.binding.binding_digest,
            role=request.role,
            item_ref=request.item_ref,
            content_commitment_digest=content,
            store_generation_commitment_digest=generation,
            previous_store_generation_commitment_digest=previous_generation,
            issuer_receipt_id=f"turn-authority-{self.counter}",
            committed_at=committed,
        )
        self.snapshots[key] = self.last
        return self.last

    def recheck(self, snapshot: FrozenTurnCommitSnapshot) -> None:
        if self.fail_recheck or snapshot not in self.snapshots.values():
            raise PolicyViolation("injected turn issuer drift")


class _ClaimIssuer:
    def __init__(self, path: Path, binding: ContinuityBinding) -> None:
        self.path = path
        self.binding = binding
        self.last: FrozenEffectClaimSnapshot | None = None
        self.fail_recheck = False
        self.counter = 0

    def snapshot(self, request: EffectClaimRequest) -> FrozenEffectClaimSnapshot:
        self.counter += 1
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            job = db.execute("select * from local_job where id=?", (request.job_id,)).fetchone()
            lease = db.execute(
                "select * from local_lease where job_id=?", (request.job_id,)
            ).fetchone()
            locks = db.execute(
                "select resource,acquired_at from local_resource_lock "
                "where job_id=? order by resource",
                (request.job_id,),
            ).fetchall()
        assert job is not None and lease is not None
        payload = json.loads(job["payload_json"])
        commitment = digest(
            {
                "schema": "zekam-effect-plan-authority-envelope/v1",
                "nonce_hex": "11" * 32,
                "binding_digest": self.binding.binding_digest,
                "job_id": request.job_id,
                "lease_id": lease["id"],
                "fencing_token": lease["fencing_token"],
                "operation": payload["operation"],
                "internal_effect_digest": digest("private-tool-input"),
            }
        )
        claimed_at = (
            datetime.fromisoformat(CLAIMED_AT) + timedelta(seconds=2 * (self.counter - 1))
        ).isoformat()
        self.last = _issue(
            FrozenEffectClaimSnapshot,
            binding_digest=self.binding.binding_digest,
            job_id=request.job_id,
            job_state="running",
            job_payload_digest=digest(payload),
            job_updated_at=job["updated_at"],
            lease_id=lease["id"],
            lease_owner_id=lease["owner_id"],
            lease_owner_pid=lease["owner_pid"],
            lease_owner_token=lease["owner_token"],
            fencing_token=lease["fencing_token"],
            lease_heartbeat_at=lease["heartbeat_at"],
            lease_expires_at=lease["expires_at"],
            resource_locks=tuple((row["resource"], row["acquired_at"]) for row in locks),
            operation=payload["operation"],
            effect_commitment_digest=commitment,
            claimed_at=claimed_at,
        )
        return self.last

    def recheck(self, snapshot: FrozenEffectClaimSnapshot) -> None:
        if self.fail_recheck or snapshot != self.last:
            raise PolicyViolation("injected claim issuer drift")


class _OutcomeIssuer:
    def __init__(self, path: Path, binding: ContinuityBinding) -> None:
        self.path = path
        self.binding = binding
        self.last: FrozenDirectEffectOutcomeSnapshot | None = None
        self.status = "completed"
        self.fail_recheck = False

    def snapshot(self, request: DirectEffectOutcomeRequest) -> FrozenDirectEffectOutcomeSnapshot:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            claim = db.execute(
                "select * from local_effect_claim where id=?", (request.claim_id,)
            ).fetchone()
            lease = db.execute(
                "select * from local_lease where id=?", (claim["lease_id"],)
            ).fetchone()
        assert claim is not None and lease is not None
        outcome = digest(
            {
                "schema": "zekam-direct-effect-outcome-authority-envelope/v1",
                "nonce_hex": "22" * 32,
                "binding_digest": self.binding.binding_digest,
                "job_id": claim["job_id"],
                "claim_id": claim["id"],
                "fencing_token": claim["fencing_token"],
                "status": self.status,
                "internal_result_digest": digest("private-tool-result"),
            }
        )
        completed_at = (
            datetime.fromisoformat(str(claim["claimed_at"])) + timedelta(seconds=1)
        ).isoformat()
        self.last = _issue(
            FrozenDirectEffectOutcomeSnapshot,
            binding_digest=self.binding.binding_digest,
            job_id=claim["job_id"],
            claim_id=claim["id"],
            lease_id=lease["id"],
            lease_owner_id=lease["owner_id"],
            lease_owner_pid=lease["owner_pid"],
            lease_owner_token=lease["owner_token"],
            fencing_token=claim["fencing_token"],
            operation=claim["operation"],
            effect_commitment_digest=claim["effect_digest"],
            claimed_at=claim["claimed_at"],
            status=self.status,
            outcome_commitment_digest=outcome,
            completed_at=completed_at,
        )
        return self.last

    def recheck(self, snapshot: FrozenDirectEffectOutcomeSnapshot) -> None:
        if self.fail_recheck or snapshot != self.last:
            raise PolicyViolation("injected outcome issuer drift")


def _tail(path: Path, binding: ContinuityBinding) -> ContinuityTail:
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("begin")
        rows = SQLiteDormantV4CloseWriter._events(db, binding)
        return ContinuityTail(len(rows), str(rows[-1]["event_digest"]))


def _prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    Path,
    ContinuityBinding,
    SQLiteDormantV4InternalProducer,
    _TurnIssuer,
    _ClaimIssuer,
    _OutcomeIssuer,
]:
    fixture = _load_slice_a()
    monkeypatch.setattr(
        fixture.ingress_module,
        "_trusted_process_owner",
        lambda value: type(value) is fixture._Manager,
    )
    monkeypatch.setattr(
        fixture.composition_module,
        "_trusted_context_owner",
        lambda value: type(value) is fixture._ContextPort,
    )
    path = tmp_path / "operational.db"
    operational_schema.bootstrap_v4(path)
    base = fixture._binding()
    binding = ContinuityBinding(
        session_id=base.session_id,
        external_session_id=base.external_session_id,
        project_id=base.project_id,
        realm_id=base.realm_id,
        client_id=base.client_id,
        device_id=base.device_id,
        source_snapshot_id=base.source_snapshot_id,
        task_digest=base.task_digest,
        plan_digest=base.plan_digest,
        policy_digest=base.policy_digest,
        work_item_id=WORK,
        run_id=RUN,
    )
    original_context = fixture._context

    def work_scoped_context(current: ContinuityBinding) -> object:
        context = original_context(current)
        ranking = fixture.ContextRankingRequest(
            role=context.ranking_request.role,
            target_identity_refs=context.ranking_request.target_identity_refs,
            step_scope_ref=context.ranking_request.step_scope_ref,
            work_scope_ref=f"work/{WORK}",
            project_scope_ref=context.ranking_request.project_scope_ref,
            realm_scope_ref=context.ranking_request.realm_scope_ref,
            current_source_revision=context.ranking_request.current_source_revision,
            compatible_source_revisions=context.ranking_request.compatible_source_revisions,
            task_terms=context.ranking_request.task_terms,
            tokenizer_profile_digest=context.ranking_request.tokenizer_profile_digest,
        )
        compiled = fixture.compile_context_v2(
            context.selected_provenance,
            ranking_request=ranking,
            token_budget=2048,
            minimum_authority=fixture.AuthorityLevel.OBSERVED,
            now=fixture.dt.datetime.fromisoformat(fixture.NOW),
            contents=dict(context.fragments),
            ranking_snapshot_digest=digest(ranking.body()),
            candidate_set_digest=digest(context.selected_provenance[0].candidate_digest),
            recipe_id="codex0151-session-start",
            recipe_digest=digest("codex0151-session-start"),
            target_role="builder",
        )
        return fixture.LocalContext(
            compiled,
            context.fragments,
            ranking,
            context.selected_provenance,
        )

    monkeypatch.setattr(fixture, "_context", work_scoped_context)
    source = SOURCE.read_text()
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into project(id,slug,display_name,created_at) values(?,?,?,?)",
            (binding.project_id, "akilli-kasa", "Akilli Kasa", fixture.NOW),
        )
        db.execute(
            "insert into project_knowledge_realm values(?,?,?)",
            (binding.project_id, binding.realm_id, fixture.NOW),
        )
        db.execute(
            "insert into source_binding values(?,?,?,?,?,?)",
            ("source", binding.project_id, "source:akilli-kasa", "directory", 1, fixture.NOW),
        )
        db.execute(
            "insert into source_snapshot values(?,?,?,?,?,?,?)",
            (
                binding.source_snapshot_id,
                "source",
                "a" * 40,
                digest("tree"),
                digest(source),
                digest("config"),
                fixture.NOW,
            ),
        )
        db.execute(
            "insert into config_revision values(?,?,?,?,?,?)",
            (CONFIG, digest("config"), binding.task_digest, "{}", 1, fixture.NOW),
        )
        db.execute(
            "insert into work_item(id,project_id,kind,title,state,revision,evidence_digest,"
            "created_at) values(?,?,?,?,?,?,?,?)",
            (WORK, binding.project_id, "implementation", "B1", "active", 1, None, fixture.NOW),
        )
        db.execute(
            "insert into run values(?,?,?,?,?,?,?,?,?,?)",
            (
                RUN,
                WORK,
                binding.source_snapshot_id,
                CONFIG,
                "running",
                "{}",
                binding.plan_digest,
                None,
                fixture.NOW,
                fixture.NOW,
            ),
        )
        db.execute(
            "insert into session(id,client_id,device_id,project_id,work_item_id,status,opened_at) "
            "values(?,?,?,?,?,'open',?)",
            (
                binding.session_id,
                binding.client_id,
                binding.device_id,
                binding.project_id,
                WORK,
                fixture.NOW,
            ),
        )
        db.execute("insert into local_runtime_config values(1,64)")
        db.execute(
            "insert into continuity_session_binding values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                binding.session_id,
                binding.external_session_id,
                binding.project_id,
                binding.realm_id,
                WORK,
                RUN,
                binding.client_id,
                binding.device_id,
                binding.source_snapshot_id,
                binding.task_digest,
                binding.plan_digest,
                binding.policy_digest,
                binding.binding_digest,
                fixture.NOW,
            ),
        )
    manager = fixture._Manager(binding)
    context = fixture._ContextPort(binding)
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    event = fixture._event()
    ingress = fixture.SQLiteCodexV4Ingress(
        path,
        binding,
        process_manager=manager,
        context_port=context,
        spool=spool,
    )
    ingress.attach_process()
    ingress.session_start(event)
    turn = _TurnIssuer(binding)
    claim = _ClaimIssuer(path, binding)
    outcome = _OutcomeIssuer(path, binding)
    producer = SQLiteDormantV4InternalProducer(
        path,
        binding,
        turn_issuer=turn,
        claim_issuer=claim,
        outcome_issuer=outcome,
    )
    return path, binding, producer, turn, claim, outcome


def _running_work(
    path: Path,
    binding: ContinuityBinding,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str = "primary",
    runtime_now: str = CLAIM_NOW,
) -> tuple[SQLiteLocalRuntimeStore, LocalClaimedWork]:
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    original_datetime_module = _runtime_module.dt
    fixed_datetime_module = SimpleNamespace(
        datetime=_fixed_runtime_datetime(runtime_now),
        UTC=UTC,
        timedelta=timedelta,
    )
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(_runtime_module, "dt", fixed_datetime_module)
        job, created = runtime.enqueue(
            idempotency_key=f"b1-job:{suffix}:{digest(str(path))}",
            payload={
                "operation": "test.effect",
                "session_id": binding.session_id,
                "binding_digest": binding.binding_digest,
                "run_id": binding.run_id,
            },
            max_attempts=1,
            available_at="2026-09-03T00:00:00+00:00",
        )
    assert _runtime_module.dt is original_datetime_module
    assert created
    work = runtime.claim_next(
        owner_id="b1-worker",
        owner_pid=31337,
        owner_token="b1-incarnation",
        lease_seconds=3600,
        resources=("project/test",),
        supported_operations=("test.effect",),
        job_id=job.id,
        now=runtime_now,
    )
    assert work is not None
    return runtime, work


def _running_job(path: Path, binding: ContinuityBinding, monkeypatch: pytest.MonkeyPatch) -> str:
    _runtime, work = _running_work(path, binding, monkeypatch)
    return work.job.id


@pytest.mark.parametrize(
    ("created_at", "accepted"),
    ((CLAIMED_AT, True), ("2026-09-03T23:00:01.000001+00:00", False)),
)
def test_claim_job_creation_boundary_is_exact_and_failure_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    created_at: str,
    accepted: bool,
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    _runtime, work = _running_work(path, binding, monkeypatch, runtime_now=created_at)
    job_id = work.job.id
    with sqlite3.connect(path) as db:
        before = digest(tuple(db.iterdump()))
    request = EffectClaimRequest(binding, job_id, _tail(path, binding))
    if accepted:
        producer.claim_effect(request)
        return
    with pytest.raises(PolicyViolation, match="causal time drift"):
        producer.claim_effect(request)
    with sqlite3.connect(path) as db:
        assert digest(tuple(db.iterdump())) == before


def test_durable_claim_rejects_job_creation_one_microsecond_after_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    job_id = _running_job(path, binding, monkeypatch)
    producer.claim_effect(EffectClaimRequest(binding, job_id, _tail(path, binding)))
    with sqlite3.connect(path) as db:
        late = "2026-09-03T23:00:01.000001+00:00"
        db.execute("update local_job set created_at=? where id=?", (late, job_id))
        before = digest(tuple(db.iterdump()))
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match="selected job claim time drift"):
            verify_b1_internal_producers(db, binding)
        assert digest(tuple(db.iterdump())) == before


def _ready_job(
    path: Path,
    binding: ContinuityBinding,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> str:
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    job, created = runtime.enqueue(
        idempotency_key=f"b1-ready:{suffix}:{digest(str(path))}",
        payload={
            "operation": "test.effect",
            "session_id": binding.session_id,
            "binding_digest": binding.binding_digest,
            "run_id": binding.run_id,
        },
        max_attempts=1,
        available_at="2026-09-03T00:00:00+00:00",
    )
    assert created
    return job.id


def test_turn_claim_and_direct_outcome_are_atomic_replayable_and_authority_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_before = SOURCE.read_bytes()
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("B1 must not use network"),
    )
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    first_turn = producer.commit_turn(
        TurnCommitRequest(
            binding,
            "user",
            "turn/018f0000-0000-7000-8000-000000000301",
            _tail(path, binding),
        )
    )
    assert not first_turn.replay and not first_turn.body()["grants_authority"]
    replay_turn = producer.commit_turn(
        TurnCommitRequest(
            binding,
            "user",
            "turn/018f0000-0000-7000-8000-000000000301",
            ContinuityTail(1, _tail(path, binding).event_digest),
        )
    )
    assert replay_turn.replay and replay_turn.event_digest == first_turn.event_digest
    original_datetime_module = _runtime_module.dt
    job_id = _running_job(path, binding, monkeypatch)
    assert _runtime_module.dt is original_datetime_module
    claimed = producer.claim_effect(EffectClaimRequest(binding, job_id, _tail(path, binding)))
    assert claimed.event_kind == "TOOL_EFFECT_CLAIMED"
    direct = producer.record_direct_outcome(
        DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
    )
    assert direct.event_kind == "TOOL_EFFECT_COMPLETED"
    assert producer.record_direct_outcome(
        DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
    ).replay
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("begin")
        verified = verify_b1_internal_producers(db, binding)
        assert verify_b1_b2_internal_producers(db, binding) == verified
        counts = db.execute(
            "select (select count(*) from continuity_turn_commit_receipt),"
            "(select count(*) from local_effect_claim),"
            "(select count(*) from local_effect_receipt),"
            "(select count(*) from session_event)"
        ).fetchone()
        times = db.execute(
            "select j.created_at,o.created_at,d.updated_at,c.claimed_at from local_job j "
            "join local_outbox o on o.job_id=j.id and o.event_kind='job.enqueued' "
            "join local_outbox_delivery d on d.outbox_id=o.id "
            "join local_effect_claim c on c.job_id=j.id where j.id=?",
            (job_id,),
        ).fetchone()
    assert verified and tuple(counts) == (1, 1, 1, 4)
    assert tuple(times) == (CLAIM_NOW, CLAIM_NOW, CLAIM_NOW, CLAIMED_AT)
    assert SOURCE.read_bytes() == source_before


def test_second_turn_predecessor_and_stale_tail_are_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    first_request = TurnCommitRequest(
        binding,
        "user",
        "turn/018f0000-0000-7000-8000-000000000302",
        _tail(path, binding),
    )
    producer.commit_turn(first_request)
    with pytest.raises(ConcurrencyConflict, match="tail"):
        producer.commit_turn(
            TurnCommitRequest(
                binding,
                "assistant",
                "turn/018f0000-0000-7000-8000-000000000303",
                first_request.expected_tail,
            )
        )
    second = producer.commit_turn(
        TurnCommitRequest(
            binding,
            "assistant",
            "turn/018f0000-0000-7000-8000-000000000303",
            _tail(path, binding),
        )
    )
    assert second.event_kind == "ASSISTANT_TURN_COMMITTED"
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "select previous_turn_commit_digest from continuity_turn_commit_receipt "
            "order by created_at"
        ).fetchall()
    assert rows[0][0] is None and rows[1][0] is not None


def test_turn_allows_unrelated_pending_job_but_effect_operations_reject_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    _ready_job(path, binding, monkeypatch, "unrelated")
    committed = producer.commit_turn(
        TurnCommitRequest(
            binding,
            "user",
            "turn/018f0000-0000-7000-8000-000000000309",
            _tail(path, binding),
        )
    )
    assert committed.event_kind == "USER_TURN_COMMITTED"
    selected = _running_job(path, binding, monkeypatch)
    with pytest.raises(PolicyViolation, match="runtime drift"):
        producer.claim_effect(EffectClaimRequest(binding, selected, _tail(path, binding)))


def test_direct_outcome_rejects_new_same_session_nonterminal_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    selected = _running_job(path, binding, monkeypatch)
    claimed = producer.claim_effect(EffectClaimRequest(binding, selected, _tail(path, binding)))
    _ready_job(path, binding, monkeypatch, "late-ambiguous")
    with pytest.raises(PolicyViolation, match="runtime drift"):
        producer.record_direct_outcome(
            DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
        )


@pytest.mark.parametrize("issuer_name", ("turn", "claim", "outcome"))
def test_issuer_recheck_failure_rolls_back_every_b1_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issuer_name: str
) -> None:
    path, binding, producer, turn, claim, outcome = _prepared(tmp_path, monkeypatch)
    before = _tail(path, binding)
    if issuer_name == "turn":
        turn.fail_recheck = True
        call = partial(
            producer.commit_turn,
            TurnCommitRequest(
                binding,
                "user",
                "turn/018f0000-0000-7000-8000-000000000304",
                before,
            ),
        )
    else:
        job_id = _running_job(path, binding, monkeypatch)
        if issuer_name == "claim":
            claim.fail_recheck = True
            call = partial(producer.claim_effect, EffectClaimRequest(binding, job_id, before))
        else:
            claimed = producer.claim_effect(EffectClaimRequest(binding, job_id, before))
            before = _tail(path, binding)
            outcome.fail_recheck = True
            call = partial(
                producer.record_direct_outcome,
                DirectEffectOutcomeRequest(binding, claimed.producer_ref, before),
            )
    with pytest.raises(PolicyViolation, match="issuer drift"):
        call()
    assert _tail(path, binding) == before


def test_two_concurrent_same_tail_turns_have_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, claim, outcome = _prepared(tmp_path, monkeypatch)
    other = SQLiteDormantV4InternalProducer(
        path,
        binding,
        turn_issuer=_TurnIssuer(binding),
        claim_issuer=claim,
        outcome_issuer=outcome,
    )
    tail = _tail(path, binding)
    requests = (
        TurnCommitRequest(
            binding,
            "user",
            "turn/018f0000-0000-7000-8000-000000000305",
            tail,
        ),
        TurnCommitRequest(
            binding,
            "assistant",
            "turn/018f0000-0000-7000-8000-000000000306",
            tail,
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(current.commit_turn, request)
            for current, request in zip((producer, other), requests, strict=True)
        ]
    successes = [future.result() for future in futures if future.exception() is None]
    failures = [future.exception() for future in futures if future.exception() is not None]
    assert len(successes) == 1
    assert len(failures) == 1 and isinstance(failures[0], ConcurrencyConflict)


def test_default_v3_rejects_without_writes(tmp_path: Path) -> None:
    fixture = _load_slice_a()
    path = tmp_path / "v3.db"
    operational_schema.bootstrap(path)
    binding = fixture._binding()
    turn = _TurnIssuer(binding)
    with pytest.raises(ConfigurationError, match="operational-v4"):
        SQLiteDormantV4InternalProducer(
            path,
            binding,
            turn_issuer=turn,
            claim_issuer=_ClaimIssuer(path, binding),
            outcome_issuer=_OutcomeIssuer(path, binding),
        )
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from local_effect_claim").fetchone()[0] == 0


def test_partial_graph_conflicts_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    request = TurnCommitRequest(
        binding,
        "user",
        "turn/018f0000-0000-7000-8000-000000000307",
        _tail(path, binding),
    )
    with sqlite3.connect(path) as db:
        snapshot = producer.turn_issuer.snapshot(request)
        body = {
            "binding_digest": binding.binding_digest,
            "content_digest": snapshot.content_commitment_digest,
            "created_at": snapshot.committed_at,
            "item_ref": snapshot.item_ref,
            "previous_turn_commit_digest": None,
            "role": snapshot.role,
            "session_id": binding.session_id,
            "store_generation_digest": snapshot.store_generation_commitment_digest,
        }
        receipt = digest({"schema": "zekam-turn-commit-receipt/v1", "body": body})
        db.execute(
            "insert into continuity_turn_commit_receipt values(?,?,?,?,?,?,?,?,?,?)",
            (
                receipt,
                binding.session_id,
                binding.binding_digest,
                snapshot.role,
                snapshot.item_ref,
                snapshot.content_commitment_digest,
                snapshot.store_generation_commitment_digest,
                None,
                canonical_json(body),
                snapshot.committed_at,
            ),
        )
    with pytest.raises(ConcurrencyConflict, match="partial"):
        producer.commit_turn(request)


@pytest.mark.parametrize("status", ("completed", "failed"))
def test_direct_outcome_remains_valid_after_ordinary_runtime_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: Literal["completed", "failed"],
) -> None:
    path, binding, producer, _turn, _claim, outcome = _prepared(tmp_path, monkeypatch)
    runtime, work = _running_work(path, binding, monkeypatch)
    claimed = producer.claim_effect(EffectClaimRequest(binding, work.job.id, _tail(path, binding)))
    outcome.status = status
    producer.record_direct_outcome(
        DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
    )
    assert outcome.last is not None
    runtime.finish(
        work,
        state=status,
        evidence_digest=outcome.last.outcome_commitment_digest,
        now="2026-09-03T23:00:03.654321+00:00",
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert verify_b1_internal_producers(db, binding)
    assert producer.record_direct_outcome(
        DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
    ).replay


@pytest.mark.parametrize("status", ("completed", "failed"))
def test_direct_outcome_remains_valid_after_runtime_lease_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: Literal["completed", "failed"],
) -> None:
    path, binding, producer, _turn, _claim, outcome = _prepared(tmp_path, monkeypatch)
    runtime, work = _running_work(path, binding, monkeypatch)
    claimed = producer.claim_effect(EffectClaimRequest(binding, work.job.id, _tail(path, binding)))
    outcome.status = status
    producer.record_direct_outcome(
        DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
    )
    sweep = runtime.recover_orphans(
        lambda _pid: None,
        now="2026-09-03T23:00:03.654321+00:00",
    )
    assert sweep.finalized == 1
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert verify_b1_internal_producers(db, binding)


def test_two_distinct_jobs_claim_and_complete_in_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, outcome = _prepared(tmp_path, monkeypatch)
    for suffix in ("first", "second"):
        runtime, work = _running_work(path, binding, monkeypatch, suffix)
        claimed = producer.claim_effect(
            EffectClaimRequest(binding, work.job.id, _tail(path, binding))
        )
        producer.record_direct_outcome(
            DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
        )
        assert outcome.last is not None
        runtime.finish(
            work,
            state="completed",
            evidence_digest=outcome.last.outcome_commitment_digest,
            now=(
                "2026-09-03T23:00:03.123456+00:00"
                if suffix == "first"
                else "2026-09-03T23:00:04.123456+00:00"
            ),
        )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert verify_b1_internal_producers(db, binding)
        assert db.execute("select count(*) from local_effect_claim").fetchone()[0] == 2


@pytest.mark.parametrize("operation", ("turn", "claim", "outcome"))
def test_commit_unknown_reconstructs_complete_graph_without_duplicate_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    if operation == "turn":
        call = partial(
            producer.commit_turn,
            TurnCommitRequest(
                binding,
                "user",
                "turn/018f0000-0000-7000-8000-000000000308",
                _tail(path, binding),
            ),
        )
    else:
        job_id = _running_job(path, binding, monkeypatch)
        if operation == "claim":
            call = partial(
                producer.claim_effect, EffectClaimRequest(binding, job_id, _tail(path, binding))
            )
        else:
            claimed = producer.claim_effect(
                EffectClaimRequest(binding, job_id, _tail(path, binding))
            )
            call = partial(
                producer.record_direct_outcome,
                DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding)),
            )

    def commit_then_raise(db: sqlite3.Connection) -> None:
        db.commit()
        raise OSError("injected post-commit ambiguity")

    monkeypatch.setattr(
        SQLiteDormantV4InternalProducer,
        "_commit",
        staticmethod(commit_then_raise),
    )
    result = call()
    assert result.replay
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert verify_b1_internal_producers(db, binding)


@pytest.mark.parametrize("operation", ("turn", "claim", "outcome"))
def test_exception_after_event_insert_rolls_back_entire_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    if operation == "turn":
        before = (0, 0, 0, 1)

        def call() -> object:
            return producer.commit_turn(
                TurnCommitRequest(
                    binding,
                    "user",
                    "turn/018f0000-0000-7000-8000-000000000310",
                    _tail(path, binding),
                )
            )

    else:
        job_id = _running_job(path, binding, monkeypatch)
        if operation == "claim":
            before = (0, 0, 0, 1)

            def call() -> object:
                return producer.claim_effect(
                    EffectClaimRequest(binding, job_id, _tail(path, binding))
                )

        else:
            claimed = producer.claim_effect(
                EffectClaimRequest(binding, job_id, _tail(path, binding))
            )
            before = (0, 1, 0, 2)

            def call() -> object:
                return producer.record_direct_outcome(
                    DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
                )

    original = SQLiteDormantV4InternalProducer._insert_event

    def insert_then_fail(*args: object, **kwargs: object) -> str:
        original(*args, **kwargs)  # type: ignore[arg-type]
        raise OSError("injected insert boundary failure")

    monkeypatch.setattr(
        SQLiteDormantV4InternalProducer,
        "_insert_event",
        staticmethod(insert_then_fail),
    )
    with pytest.raises(OSError, match="insert boundary"):
        call()
    with sqlite3.connect(path) as db:
        counts = db.execute(
            "select (select count(*) from continuity_turn_commit_receipt),"
            "(select count(*) from local_effect_claim),"
            "(select count(*) from local_effect_receipt),"
            "(select count(*) from session_event)"
        ).fetchone()
    assert counts is not None and tuple(counts) == before


def test_semantic_verifier_rejects_event_body_tamper_beyond_sql_shape_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    producer.commit_turn(
        TurnCommitRequest(
            binding,
            "user",
            "turn/018f0000-0000-7000-8000-000000000311",
            _tail(path, binding),
        )
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        triggers = db.execute(
            "select name from sqlite_master where type='trigger' and sql like "
            "'%session_event_detail%'"
        ).fetchall()
        for trigger in triggers:
            db.execute(f'drop trigger "{trigger[0]}"')
        row = db.execute(
            "select * from session_event_detail where idempotency_key like 'turn-commit:%'"
        ).fetchone()
        assert row is not None
        body = json.loads(row["body_json"])
        body["event"]["source_refs"] = ["turn/018f0000-0000-7000-8000-000000000399"]
        db.execute(
            "update session_event_detail set body_json=? where event_id=?",
            (canonical_json(body), row["event_id"]),
        )
        with pytest.raises(PolicyViolation, match="event body"):
            verify_b1_internal_producers(db, binding)


def test_restart_replays_complete_graph_without_live_issuer_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, turn, claim, outcome = _prepared(tmp_path, monkeypatch)
    turn_request = TurnCommitRequest(
        binding,
        "user",
        "turn/018f0000-0000-7000-8000-000000000312",
        _tail(path, binding),
    )
    producer.commit_turn(turn_request)
    job_id = _running_job(path, binding, monkeypatch)
    claim_request = EffectClaimRequest(binding, job_id, _tail(path, binding))
    claimed = producer.claim_effect(claim_request)
    outcome_request = DirectEffectOutcomeRequest(
        binding, claimed.producer_ref, _tail(path, binding)
    )
    producer.record_direct_outcome(outcome_request)

    def unavailable(*_args: object, **_kwargs: object) -> object:
        pytest.fail("durable replay must not invoke a vanished live issuer")

    for issuer in (turn, claim, outcome):
        monkeypatch.setattr(issuer, "snapshot", unavailable)
        monkeypatch.setattr(issuer, "recheck", unavailable)
    reopened = SQLiteDormantV4InternalProducer(
        path,
        binding,
        turn_issuer=turn,
        claim_issuer=claim,
        outcome_issuer=outcome,
    )
    assert reopened.commit_turn(turn_request).replay
    assert reopened.claim_effect(claim_request).replay
    assert reopened.record_direct_outcome(outcome_request).replay


@pytest.mark.parametrize("status", ("delivered", "failed", "unknown"))
def test_runtime_outbox_delivery_states_remain_semantically_verifiable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: Literal["delivered", "failed", "unknown"],
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    runtime, work = _running_work(path, binding, monkeypatch)
    claimed = producer.claim_effect(EffectClaimRequest(binding, work.job.id, _tail(path, binding)))
    producer.record_direct_outcome(
        DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
    )
    moment = datetime.now(UTC).replace(microsecond=123456)
    outbox = runtime.claim_outbox(
        supported_kinds=("job.enqueued",),
        owner_id="delivery-worker",
        owner_pid=31338,
        owner_token="delivery-incarnation",
        lease_seconds=60,
        now=moment.isoformat(),
    )
    assert outbox is not None
    runtime.record_outbox_receipt(
        outbox,
        status=status,
        evidence_digest=digest(f"delivery:{status}"),
        now=(moment + timedelta(seconds=1)).isoformat(),
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert verify_b1_internal_producers(db, binding)


@pytest.mark.parametrize(
    ("target", "value", "message"),
    (
        ("job", "2026-09-03T23:00:01.500000+00:00", "causal time"),
        ("lease", CLAIMED_AT, "causal time"),
    ),
)
def test_claim_rejects_job_or_lease_causal_boundary_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    value: str,
    message: str,
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    job_id = _running_job(path, binding, monkeypatch)
    with sqlite3.connect(path) as db:
        if target == "job":
            db.execute("update local_job set updated_at=? where id=?", (value, job_id))
        else:
            db.execute("update local_lease set expires_at=? where job_id=?", (value, job_id))
    with pytest.raises(PolicyViolation, match=message):
        producer.claim_effect(EffectClaimRequest(binding, job_id, _tail(path, binding)))


def test_direct_outcome_rejects_completion_at_lease_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    job_id = _running_job(path, binding, monkeypatch)
    claimed = producer.claim_effect(EffectClaimRequest(binding, job_id, _tail(path, binding)))
    with sqlite3.connect(path) as db:
        db.execute(
            "update local_lease set expires_at=? where job_id=?",
            (COMPLETED_AT, job_id),
        )
    with pytest.raises(PolicyViolation, match="causal time"):
        producer.record_direct_outcome(
            DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
        )


@pytest.mark.parametrize("tamper", ("scope", "job-fence", "lock-fence"))
def test_claim_rejects_scope_and_fencing_graph_tamper_without_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    job_id = _running_job(path, binding, monkeypatch)
    with sqlite3.connect(path) as db:
        if tamper == "scope":
            payload = {
                "operation": "test.effect",
                "session_id": binding.session_id,
                "binding_digest": digest("foreign-binding"),
                "run_id": binding.run_id,
            }
            db.execute(
                "update local_job set payload_json=? where id=?",
                (canonical_json(payload), job_id),
            )
        elif tamper == "job-fence":
            db.execute("update local_job set fencing_counter=2 where id=?", (job_id,))
        else:
            db.execute(
                "update local_resource_lock set fencing_token=2 where job_id=?",
                (job_id,),
            )
    with pytest.raises(PolicyViolation):
        producer.claim_effect(EffectClaimRequest(binding, job_id, _tail(path, binding)))
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from local_effect_claim").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("owner_id", " "),
        ("owner_token", "x" * 513),
        ("owner_pid", 2_147_483_648),
    ),
)
def test_durable_claim_replay_rejects_unbounded_runtime_owner_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    value: object,
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    _runtime, work = _running_work(path, binding, monkeypatch)
    producer.claim_effect(EffectClaimRequest(binding, work.job.id, _tail(path, binding)))
    with sqlite3.connect(path) as db:
        db.execute(f'update local_lease set "{column}"=? where job_id=?', (value, work.job.id))
    with pytest.raises(PolicyViolation, match=r"lease|owner|runtime"):
        producer.claim_effect(EffectClaimRequest(binding, work.job.id, _tail(path, binding)))


def test_durable_claim_replay_rejects_more_than_64_resource_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    _runtime, work = _running_work(path, binding, monkeypatch)
    producer.claim_effect(EffectClaimRequest(binding, work.job.id, _tail(path, binding)))
    with sqlite3.connect(path) as db:
        lease = db.execute(
            "select id,fencing_token from local_lease where job_id=?", (work.job.id,)
        ).fetchone()
        assert lease is not None
        for number in range(64):
            db.execute(
                "insert into local_resource_lock values(?,?,?,?,?)",
                (
                    f"project/extra-{number:02d}",
                    work.job.id,
                    lease[0],
                    lease[1],
                    CLAIM_NOW,
                ),
            )
    with pytest.raises(PolicyViolation, match="lock bound"):
        producer.claim_effect(EffectClaimRequest(binding, work.job.id, _tail(path, binding)))


def test_post_claim_runtime_heartbeat_allows_replay_and_direct_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    runtime, work = _running_work(path, binding, monkeypatch)
    first = producer.claim_effect(EffectClaimRequest(binding, work.job.id, _tail(path, binding)))
    runtime.heartbeat(
        work.lease.id,
        owner_id=work.lease.owner_id,
        owner_token=work.lease.owner_token,
        fencing_token=work.lease.fencing_token,
        lease_seconds=3600,
        now="2026-09-03T23:00:02.123456+00:00",
    )
    assert producer.claim_effect(
        EffectClaimRequest(binding, work.job.id, _tail(path, binding))
    ).replay
    terminal = producer.record_direct_outcome(
        DirectEffectOutcomeRequest(binding, first.producer_ref, _tail(path, binding))
    )
    assert terminal.event_kind == "TOOL_EFFECT_COMPLETED"


@pytest.mark.parametrize("drift", ("owner", "token", "fence", "expiry"))
def test_failed_runtime_heartbeat_preserves_claim_replay_and_lease_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    runtime, work = _running_work(path, binding, monkeypatch)
    producer.claim_effect(EffectClaimRequest(binding, work.job.id, _tail(path, binding)))
    before = work.lease
    owner_id = "wrong" if drift == "owner" else before.owner_id
    owner_token = "wrong" if drift == "token" else before.owner_token
    fencing_token = before.fencing_token + 1 if drift == "fence" else before.fencing_token
    moment = before.expires_at if drift == "expiry" else "2026-09-03T23:00:02+00:00"
    with pytest.raises(ConcurrencyConflict):
        runtime.heartbeat(
            before.id,
            owner_id=owner_id,
            owner_token=owner_token,
            fencing_token=fencing_token,
            lease_seconds=3600,
            now=moment,
        )
    with sqlite3.connect(path) as db:
        after = db.execute(
            "select heartbeat_at,expires_at from local_lease where id=?", (before.id,)
        ).fetchone()
    assert after == (CLAIM_NOW, before.expires_at)
    assert producer.claim_effect(
        EffectClaimRequest(binding, work.job.id, _tail(path, binding))
    ).replay


def test_post_claim_heartbeat_does_not_hide_resource_scope_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepared(tmp_path, monkeypatch)
    runtime, work = _running_work(path, binding, monkeypatch)
    producer.claim_effect(EffectClaimRequest(binding, work.job.id, _tail(path, binding)))
    runtime.heartbeat(
        work.lease.id,
        owner_id=work.lease.owner_id,
        owner_token=work.lease.owner_token,
        fencing_token=work.lease.fencing_token,
        lease_seconds=3600,
        now="2026-09-03T23:00:02.123456+00:00",
    )
    with sqlite3.connect(path) as db:
        db.execute(
            "update local_resource_lock set fencing_token=fencing_token+1 where job_id=?",
            (work.job.id,),
        )
    with pytest.raises(PolicyViolation, match="resource lock"):
        producer.claim_effect(EffectClaimRequest(binding, work.job.id, _tail(path, binding)))
