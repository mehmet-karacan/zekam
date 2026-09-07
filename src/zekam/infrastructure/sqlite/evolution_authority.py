"""Transactional SQLite ledger for evolution parent grants and child budgets.

The ledger targets the existing operational SQLite engine.  It receives an
already-open connection so it cannot create a parallel database or bootstrap a
live grant by itself.  ``EVOLUTION_AUTHORITY_DDL`` is consumed by the admitted
operational schema migration in a later package and by isolated tests here.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.domain.evolution_authority import (
    EVOLUTION_HANDLERS,
    EvolutionBudget,
    EvolutionRunPlan,
    ExecutionBoundary,
    StandingGrant,
    derive_child_authorization,
)
from zekam.domain.improvement_policy import ImprovementChangeClass
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    AuthorizationState,
    DataClassification,
)

EVOLUTION_AUTHORITY_DDL: str = r"""
create table evolution_grant_approval(
    receipt_digest text primary key,
    grant_digest text not null unique,
    owner_id text not null,
    realm_id text not null,
    device_id text not null,
    approver_id text not null,
    issued_at text not null,
    expires_at text not null
) strict;
create table evolution_standing_grant(
    grant_digest text primary key,
    grant_id text not null,
    revision integer not null check(revision>0),
    approval_receipt_digest text not null unique,
    body_json text not null,
    registered_at text not null,
    unique(grant_id,revision)
) strict;
create table evolution_review_decision(
    receipt_digest text primary key,
    grant_digest text not null references evolution_standing_grant(grant_digest),
    plan_digest text not null unique,
    candidate_digest text not null,
    fixture_digest text not null,
    effect_digest text not null,
    reviewer_ref text not null,
    executor_ref text not null,
    approved integer not null check(approved=1),
    reviewed_at text not null,
    expires_at text not null,
    check(reviewer_ref<>executor_ref)
) strict;
create table evolution_grant_revocation(
    grant_digest text primary key references evolution_standing_grant(grant_digest),
    reason text not null,
    revoked_at text not null
) strict;
create table evolution_child_reservation(
    reservation_id text primary key,
    idempotency_key text not null unique,
    grant_digest text not null references evolution_standing_grant(grant_digest),
    plan_digest text not null unique,
    plan_json text not null,
    authorization_digest text not null unique,
    authorization_json text not null,
    provider_calls integer not null check(provider_calls>=0),
    tokens integer not null check(tokens>=0),
    duration_seconds integer not null check(duration_seconds>0),
    cost_micros integer not null check(cost_micros>=0),
    disk_bytes integer not null check(disk_bytes>0),
    concurrency integer not null check(concurrency>0),
    reserved_at text not null
) strict;
create table evolution_child_effect_claim(
    reservation_id text primary key references evolution_child_reservation(reservation_id),
    authorization_digest text not null unique,
    effect_digest text not null unique,
    claimed_at text not null
) strict;
create table evolution_runtime_attestation(
    attestation_digest text primary key,
    grant_digest text not null references evolution_standing_grant(grant_digest),
    plan_digest text not null unique,
    authorization_digest text not null unique,
    task_scope_digest text not null,
    policy_digest text not null,
    verifier_digest text not null,
    validator_digest text not null,
    source_lineage_digest text not null,
    protected_manifest_digest text not null,
    dependency_manifest_digest text not null,
    model_ref text not null,
    provider_ref text not null,
    execution_boundary text not null check(execution_boundary in('local','remote')),
    attestor_ref text not null,
    executor_ref text not null,
    observed_at text not null,
    expires_at text not null,
    check(attestor_ref<>executor_ref)
) strict;
create table evolution_terminal_readback(
    readback_digest text primary key,
    authorization_digest text not null unique,
    plan_digest text not null unique,
    status text not null check(status in('completed','failed','unknown')),
    evidence_digest text not null,
    usage_json text not null,
    verifier_ref text not null,
    executor_ref text not null,
    verified_at text not null,
    check(verifier_ref<>executor_ref)
) strict;
create table evolution_child_terminal(
    reservation_id text primary key references evolution_child_effect_claim(reservation_id),
    status text not null check(status in('completed','failed','unknown')),
    evidence_digest text not null,
    provider_calls integer not null check(provider_calls>=0),
    tokens integer not null check(tokens>=0),
    duration_seconds integer not null check(duration_seconds>=0),
    cost_micros integer not null check(cost_micros>=0),
    disk_bytes integer not null check(disk_bytes>=0),
    finished_at text not null
) strict;
"""
for _table in (
    "evolution_grant_approval",
    "evolution_standing_grant",
    "evolution_grant_revocation",
    "evolution_review_decision",
    "evolution_child_reservation",
    "evolution_child_effect_claim",
    "evolution_runtime_attestation",
    "evolution_terminal_readback",
    "evolution_child_terminal",
):
    EVOLUTION_AUTHORITY_DDL += (
        f"create trigger {_table}_no_update before update on {_table} "
        "begin select raise(abort,'append-only'); end;\n"
        f"create trigger {_table}_no_delete before delete on {_table} "
        "begin select raise(abort,'append-only'); end;\n"
    )


@dataclass(frozen=True, slots=True)
class ChildReservation:
    reservation_id: str
    authorization: Authorization
    replayed: bool


class EvolutionApprovalAuthority(Protocol):
    """Trusted outer authority; model output cannot implement this boundary."""

    def verify_grant_approval(
        self, grant: StandingGrant, approval_receipt_digest: str, *, now: dt.datetime
    ) -> bool: ...

    def verify_review(self, plan: EvolutionRunPlan, *, now: dt.datetime) -> bool: ...

    def verify_effect_current(
        self,
        grant: StandingGrant,
        plan: EvolutionRunPlan,
        child: Authorization,
        *,
        now: dt.datetime,
    ) -> bool: ...

    def verify_terminal_readback(
        self,
        plan: EvolutionRunPlan,
        child: Authorization,
        status: str,
        evidence_digest: str,
        usage: EvolutionBudget,
        *,
        now: dt.datetime,
    ) -> bool: ...


class SQLiteEvolutionApprovalAuthority:
    """Verify immutable approval, review, drift and readback receipts in operational SQLite."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def verify_grant_approval(
        self, grant: StandingGrant, approval_receipt_digest: str, *, now: dt.datetime
    ) -> bool:
        row = self._db.execute(
            "select grant_digest,owner_id,realm_id,device_id,approver_id,issued_at,expires_at "
            "from evolution_grant_approval where receipt_digest=?",
            (approval_receipt_digest,),
        ).fetchone()
        return bool(
            row is not None
            and tuple(map(str, row[:5]))
            == (
                grant.grant_digest,
                str(grant.owner_id),
                str(grant.realm_id),
                grant.device_id,
                str(grant.owner_id),
            )
            and dt.datetime.fromisoformat(str(row[5]))
            <= now
            < dt.datetime.fromisoformat(str(row[6]))
        )

    def verify_review(self, plan: EvolutionRunPlan, *, now: dt.datetime) -> bool:
        if plan.review_receipt_digest is None:
            return False
        row = self._db.execute(
            "select grant_digest,plan_digest,candidate_digest,fixture_digest,effect_digest,"
            "reviewer_ref,executor_ref,approved,reviewed_at,expires_at "
            "from evolution_review_decision "
            "where receipt_digest=?",
            (plan.review_receipt_digest,),
        ).fetchone()
        return bool(
            row is not None
            and tuple(map(str, row[:7]))
            == (
                plan.parent_grant_digest,
                plan.plan_digest,
                plan.candidate_digest,
                plan.fixture_digest,
                plan.effect_digest,
                plan.verifier_ref,
                plan.executor_ref,
            )
            and int(row[7]) == 1
            and plan.requested_at <= dt.datetime.fromisoformat(str(row[8])) <= now
            and now < dt.datetime.fromisoformat(str(row[9]))
        )

    def verify_effect_current(
        self,
        grant: StandingGrant,
        plan: EvolutionRunPlan,
        child: Authorization,
        *,
        now: dt.datetime,
    ) -> bool:
        row = self._db.execute(
            "select grant_digest,task_scope_digest,policy_digest,verifier_digest,"
            "validator_digest,source_lineage_digest,protected_manifest_digest,"
            "dependency_manifest_digest,model_ref,provider_ref,execution_boundary,"
            "attestor_ref,executor_ref,observed_at,expires_at "
            "from evolution_runtime_attestation where plan_digest=? and authorization_digest=?",
            (plan.plan_digest, child.authorization_digest),
        ).fetchone()
        return bool(
            row is not None
            and tuple(map(str, row[:11]))
            == (
                grant.grant_digest,
                plan.task_scope_digest,
                plan.policy_digest,
                plan.verifier_digest,
                plan.validator_digest,
                plan.source_lineage_digest,
                plan.protected_manifest_digest,
                plan.dependency_manifest_digest,
                plan.model_ref,
                plan.provider_ref,
                plan.execution_boundary.value,
            )
            and str(row[11]) == plan.verifier_ref
            and str(row[12]) == plan.executor_ref
            and max(plan.requested_at, child.issued_at)
            <= dt.datetime.fromisoformat(str(row[13]))
            <= now
            < dt.datetime.fromisoformat(str(row[14]))
        )

    def verify_terminal_readback(
        self,
        plan: EvolutionRunPlan,
        child: Authorization,
        status: str,
        evidence_digest: str,
        usage: EvolutionBudget,
        *,
        now: dt.datetime,
    ) -> bool:
        row = self._db.execute(
            "select status,evidence_digest,usage_json,verifier_ref,executor_ref,verified_at "
            "from evolution_terminal_readback where authorization_digest=? and plan_digest=?",
            (child.authorization_digest, plan.plan_digest),
        ).fetchone()
        claim = self._db.execute(
            "select c.claimed_at from evolution_child_effect_claim c "
            "join evolution_child_reservation r on r.reservation_id=c.reservation_id "
            "where r.authorization_digest=?",
            (child.authorization_digest,),
        ).fetchone()
        return bool(
            row is not None
            and claim is not None
            and tuple(map(str, row[:5]))
            == (
                status,
                evidence_digest,
                canonical_json(usage.body()),
                plan.verifier_ref,
                plan.executor_ref,
            )
            and dt.datetime.fromisoformat(str(claim[0]))
            <= dt.datetime.fromisoformat(str(row[5]))
            <= now
        )


def _authorization(raw: str) -> Authorization:
    try:
        body = json.loads(raw)
        scope_body = body["scope"]
        scope = AuthorizationScope(
            allowed_resources=tuple(scope_body["allowed_resources"]),
            allowed_read_resources=tuple(scope_body.get("allowed_read_resources", ())),
            allowed_write_resources=tuple(scope_body.get("allowed_write_resources", ())),
            allowed_effects=tuple(scope_body["allowed_effects"]),
            provider_refs=tuple(scope_body["provider_refs"]),
            secret_ref_ids=tuple(UUID(value) for value in scope_body["secret_ref_ids"]),
            data_classifications=tuple(
                DataClassification(value) for value in scope_body["data_classifications"]
            ),
        )
        return Authorization(
            id=UUID(body["id"]),
            realm_id=UUID(body["realm_id"]),
            actor_id=UUID(body["actor_id"]),
            plan_digest=body["plan_digest"],
            effect_digest=body["effect_digest"],
            scope=scope,
            risk=body["risk"],
            issued_at=dt.datetime.fromisoformat(body["issued_at"]),
            expires_at=dt.datetime.fromisoformat(body["expires_at"]),
            state=AuthorizationState(body["state"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PolicyViolation("Stored child authorization corrupt") from exc


def _evolution_run_plan(raw: str) -> EvolutionRunPlan:
    try:
        body = json.loads(raw)
        budget = EvolutionBudget(**body["budget"])
        plan = EvolutionRunPlan(
            parent_grant_digest=body["parent_grant_digest"],
            project_id=UUID(body["project_id"]),
            logical_source=body["logical_source"],
            operation=body["operation"],
            handler_version=body["handler_version"],
            change_class=ImprovementChangeClass(body["change_class"]),
            readable_resources=tuple(body["readable_resources"]),
            writable_resources=tuple(body["writable_resources"]),
            model_ref=body["model_ref"],
            provider_ref=body["provider_ref"],
            data_classifications=tuple(
                DataClassification(value) for value in body["data_classifications"]
            ),
            network_scope=body["network_scope"],
            execution_boundary=ExecutionBoundary(body["execution_boundary"]),
            task_scope_digest=body["task_scope_digest"],
            policy_digest=body["policy_digest"],
            verifier_digest=body["verifier_digest"],
            validator_digest=body["validator_digest"],
            source_lineage_digest=body["source_lineage_digest"],
            protected_manifest_digest=body["protected_manifest_digest"],
            dependency_manifest_digest=body["dependency_manifest_digest"],
            input_digest=body["input_digest"],
            candidate_digest=body["candidate_digest"],
            fixture_digest=body["fixture_digest"],
            budget_digest=body["budget_digest"],
            effect_digest=body["effect_digest"],
            budget=budget,
            executor_ref=body["executor_ref"],
            verifier_ref=body["verifier_ref"],
            review_receipt_digest=body["review_receipt_digest"],
            requested_at=dt.datetime.fromisoformat(body["requested_at"]),
            deadline=dt.datetime.fromisoformat(body["deadline"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PolicyViolation("Stored evolution run plan corrupt") from exc
    if canonical_json(plan.body()) != raw:
        raise PolicyViolation("Stored evolution run plan canonical drift")
    return plan


class SQLiteEvolutionAuthorityLedger:
    """Admission logic scoped to one existing operational DB connection."""

    def __init__(
        self, connection: sqlite3.Connection, authority: EvolutionApprovalAuthority
    ) -> None:
        self._db = connection
        self._authority = authority

    def register_grant(
        self,
        grant: StandingGrant,
        *,
        approval_receipt_digest: str,
        now: dt.datetime,
    ) -> None:
        """Append an externally approved grant revision; never activates one implicitly."""

        parse_digest(approval_receipt_digest)
        self._transaction()
        try:
            if not self._authority.verify_grant_approval(grant, approval_receipt_digest, now=now):
                raise PolicyViolation("Standing grant trusted approval receipt invalid")
            current = self._db.execute(
                "select max(revision) from evolution_standing_grant where grant_id=?",
                (str(grant.grant_id),),
            ).fetchone()
            if current is not None and current[0] is not None and int(current[0]) >= grant.revision:
                raise ConcurrencyConflict("Standing grant revision already registered")
            self._db.execute(
                "insert into evolution_standing_grant values(?,?,?,?,?,?)",
                (
                    grant.grant_digest,
                    str(grant.grant_id),
                    grant.revision,
                    approval_receipt_digest,
                    canonical_json(grant.body()),
                    now.isoformat(),
                ),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def load_claimed(self, reservation_id: str) -> tuple[EvolutionRunPlan, ChildReservation]:
        """Reconstruct one consumed child from append-only operational evidence."""

        row = self._db.execute(
            "select r.plan_digest,r.plan_json,r.authorization_digest,r.authorization_json "
            "from evolution_child_reservation r join evolution_child_effect_claim c "
            "on c.reservation_id=r.reservation_id where r.reservation_id=?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise PolicyViolation("Claimed evolution reservation missing")
        run = _evolution_run_plan(str(row[1]))
        child = _authorization(str(row[3]))
        if (
            str(row[0]) != run.plan_digest
            or str(row[2]) != child.authorization_digest
            or child.plan_digest != run.plan_digest
            or child.effect_digest != run.effect_digest
        ):
            raise PolicyViolation("Claimed evolution reservation durable drift")
        return run, ChildReservation(reservation_id, child, True)

    def revoke(self, grant_digest: str, *, reason: str, now: dt.datetime) -> None:
        parse_digest(grant_digest)
        if type(reason) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", reason):
            raise ValidationFailed("Standing grant revoke reason required")
        self._transaction()
        try:
            self._db.execute(
                "insert into evolution_grant_revocation values(?,?,?)",
                (grant_digest, reason, now.isoformat()),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def reserve_child(
        self,
        grant: StandingGrant,
        plan: EvolutionRunPlan,
        *,
        reservation_id: str,
        idempotency_key: str,
        now: dt.datetime,
    ) -> ChildReservation:
        """Check drift and remaining budget, then issue+reserve in one transaction."""

        for label, value in (
            ("reservation_id", reservation_id),
            ("idempotency_key", idempotency_key),
        ):
            if type(value) is not str or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,511}", value
            ):
                raise ValidationFailed(f"Evolution {label} invalid")
        self._transaction()
        try:
            self._assert_current_grant(grant)
            replay = self._db.execute(
                "select reservation_id,plan_digest,authorization_json "
                "from evolution_child_reservation where idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                if str(replay[1]) != plan.plan_digest:
                    raise ConcurrencyConflict("Evolution idempotency key plan conflict")
                result = ChildReservation(str(replay[0]), _authorization(str(replay[2])), True)
                self._db.commit()
                return result

            child = derive_child_authorization(grant, plan, now=now)
            if plan.review_receipt_digest is not None and not self._authority.verify_review(
                plan, now=now
            ):
                raise PolicyViolation("Evolution trusted review receipt invalid")
            self._assert_remaining_budget(grant, plan.budget)
            document = child.as_dict() | {
                "realm_id": str(child.realm_id),
                "actor_id": str(child.actor_id),
                "secret_ref_ids": [],
            }
            self._db.execute(
                "insert into evolution_child_reservation values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    reservation_id,
                    idempotency_key,
                    grant.grant_digest,
                    plan.plan_digest,
                    canonical_json(plan.body()),
                    child.authorization_digest,
                    canonical_json(document),
                    *plan.budget.body().values(),
                    now.isoformat(),
                ),
            )
            self._db.commit()
            return ChildReservation(reservation_id, child, False)
        except Exception:
            self._db.rollback()
            raise

    def claim_effect(
        self,
        grant: StandingGrant,
        plan: EvolutionRunPlan,
        *,
        effect_body: dict[str, object],
        reservation_id: str,
        authorization_digest: str,
        effect_digest: str,
        now: dt.datetime,
    ) -> None:
        """Consume the child at the immutable immediate pre-effect boundary."""

        parse_digest(authorization_digest)
        parse_digest(effect_digest)
        self._transaction()
        try:
            self._assert_current_grant(grant)
            row = self._db.execute(
                "select plan_digest,plan_json,authorization_digest,authorization_json "
                "from evolution_child_reservation "
                "where reservation_id=? and grant_digest=?",
                (reservation_id, grant.grant_digest),
            ).fetchone()
            if row is None:
                raise PolicyViolation("Evolution reservation missing")
            child = _authorization(str(row[3]))
            EVOLUTION_HANDLERS[plan.operation].validate_input(effect_body)
            if (
                str(row[0]) != plan.plan_digest
                or str(row[1]) != canonical_json(plan.body())
                or effect_body != plan.effect_body()
                or digest(effect_body) != effect_digest
                or str(row[2]) != authorization_digest
                or child.authorization_digest != authorization_digest
                or child.effect_digest != effect_digest
                or not child.is_valid_at(now)
            ):
                raise PolicyViolation("Evolution child authorization drift or expired")
            if not self._authority.verify_effect_current(grant, plan, child, now=now):
                raise PolicyViolation("Evolution current policy/provider/source drift")
            self._db.execute(
                "insert into evolution_child_effect_claim values(?,?,?,?)",
                (reservation_id, authorization_digest, effect_digest, now.isoformat()),
            )
            self._db.commit()
        except sqlite3.IntegrityError as exc:
            self._db.rollback()
            raise ConcurrencyConflict("Evolution child authorization already consumed") from exc
        except Exception:
            self._db.rollback()
            raise

    def record_terminal(
        self,
        reservation_id: str,
        plan: EvolutionRunPlan,
        child: Authorization,
        *,
        status: str,
        evidence_digest: str,
        usage: EvolutionBudget,
        now: dt.datetime,
    ) -> None:
        if status not in {"completed", "failed", "unknown"}:
            raise ValidationFailed("Evolution terminal status invalid")
        parse_digest(evidence_digest)
        self._transaction()
        try:
            reserved = self._db.execute(
                "select r.provider_calls,r.tokens,r.duration_seconds,r.cost_micros,"
                "r.disk_bytes,r.concurrency,r.plan_digest,r.plan_json,r.authorization_digest,"
                "r.reserved_at,c.claimed_at "
                "from evolution_child_reservation r join evolution_child_effect_claim c "
                "on c.reservation_id=r.reservation_id where r.reservation_id=?",
                (reservation_id,),
            ).fetchone()
            if reserved is None:
                raise PolicyViolation("Evolution claimed reservation missing")
            reserved_budget = EvolutionBudget(*map(int, reserved[:6]))
            if not reserved_budget.covers(usage):
                raise PolicyViolation("Evolution usage exceeds reservation")
            if (
                str(reserved[6]) != plan.plan_digest
                or str(reserved[7]) != canonical_json(plan.body())
                or str(reserved[8]) != child.authorization_digest
                or now < dt.datetime.fromisoformat(str(reserved[9]))
                or now < dt.datetime.fromisoformat(str(reserved[10]))
                or not self._authority.verify_terminal_readback(
                    plan,
                    child,
                    status,
                    evidence_digest,
                    usage,
                    now=now,
                )
            ):
                raise PolicyViolation("Evolution terminal independent readback invalid")
            self._db.execute(
                "insert into evolution_child_terminal values(?,?,?,?,?,?,?,?,?)",
                (
                    reservation_id,
                    status,
                    evidence_digest,
                    usage.provider_calls,
                    usage.tokens,
                    usage.duration_seconds,
                    usage.cost_micros,
                    usage.disk_bytes,
                    now.isoformat(),
                ),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def assert_effect_current(
        self,
        grant: StandingGrant,
        plan: EvolutionRunPlan,
        child: Authorization,
        *,
        now: dt.datetime,
    ) -> None:
        """Recheck revocation and exact policy at the immediate execution boundary."""

        self._transaction()
        try:
            self._assert_current_grant(grant)
            if not child.is_valid_at(now) or not self._authority.verify_effect_current(
                grant, plan, child, now=now
            ):
                raise PolicyViolation("Evolution effect authority no longer current")
            claimed = self._db.execute(
                "select 1 from evolution_child_effect_claim where authorization_digest=? ",
                (child.authorization_digest,),
            ).fetchone()
            if claimed is None:
                raise PolicyViolation("Evolution effect requires prior durable claim")
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def assert_terminal_settlement(
        self,
        reservation_id: str,
        plan: EvolutionRunPlan,
        child: Authorization,
        *,
        evidence_digest: str,
    ) -> None:
        """Bind a downstream settlement to the exact durable claimed terminal."""

        parse_digest(evidence_digest)
        row = self._db.execute(
            "select r.plan_digest,r.plan_json,r.authorization_digest,c.effect_digest,"
            "t.status,t.evidence_digest from evolution_child_reservation r "
            "join evolution_child_effect_claim c on c.reservation_id=r.reservation_id "
            "join evolution_child_terminal t on t.reservation_id=r.reservation_id "
            "where r.reservation_id=?",
            (reservation_id,),
        ).fetchone()
        if (
            row is None
            or str(row[0]) != plan.plan_digest
            or str(row[1]) != canonical_json(plan.body())
            or str(row[2]) != child.authorization_digest
            or str(row[3]) != plan.effect_digest
            or str(row[4]) != "completed"
            or str(row[5]) != evidence_digest
        ):
            raise PolicyViolation("Typed rollout requires exact durable evolution terminal")

    def assert_claimed_recovery(
        self,
        reservation_id: str,
        plan: EvolutionRunPlan,
        child: Authorization,
    ) -> None:
        """Authorize only restorative handling of one already-consumed child."""

        row = self._db.execute(
            "select r.plan_digest,r.plan_json,r.authorization_digest,c.effect_digest "
            "from evolution_child_reservation r join evolution_child_effect_claim c "
            "on c.reservation_id=r.reservation_id where r.reservation_id=?",
            (reservation_id,),
        ).fetchone()
        if (
            row is None
            or str(row[0]) != plan.plan_digest
            or str(row[1]) != canonical_json(plan.body())
            or str(row[2]) != child.authorization_digest
            or str(row[3]) != plan.effect_digest
        ):
            raise PolicyViolation("Rollout recovery requires exact consumed child")
        terminal = self._db.execute(
            "select status from evolution_child_terminal where reservation_id=?",
            (reservation_id,),
        ).fetchone()
        if terminal is not None and str(terminal[0]) != "failed":
            raise PolicyViolation("Completed or unknown evolution terminal cannot be reversed")

    def claimed_terminal(
        self,
        reservation_id: str,
        plan: EvolutionRunPlan,
        child: Authorization,
    ) -> tuple[str, str, dt.datetime] | None:
        """Read one exact claimed terminal for restart reconciliation."""

        row = self._db.execute(
            "select r.plan_digest,r.plan_json,r.authorization_digest,c.effect_digest,"
            "t.status,t.evidence_digest,t.finished_at "
            "from evolution_child_reservation r join evolution_child_effect_claim c "
            "on c.reservation_id=r.reservation_id left join evolution_child_terminal t "
            "on t.reservation_id=r.reservation_id where r.reservation_id=?",
            (reservation_id,),
        ).fetchone()
        if (
            row is None
            or str(row[0]) != plan.plan_digest
            or str(row[1]) != canonical_json(plan.body())
            or str(row[2]) != child.authorization_digest
            or str(row[3]) != plan.effect_digest
        ):
            raise PolicyViolation("Claimed evolution terminal binding drift")
        if row[4] is None:
            return None
        status = str(row[4])
        evidence = str(row[5])
        parse_digest(evidence)
        try:
            finished_at = dt.datetime.fromisoformat(str(row[6]))
        except ValueError as exc:
            raise PolicyViolation("Claimed evolution terminal time drift") from exc
        return status, evidence, finished_at

    def record_recovery_terminal(
        self,
        reservation_id: str,
        plan: EvolutionRunPlan,
        child: Authorization,
        *,
        recovery_digest: str,
        now: dt.datetime,
    ) -> None:
        """Close one consumed child after bounded no-replay recovery."""

        parse_digest(recovery_digest)
        self.assert_claimed_recovery(reservation_id, plan, child)
        self._transaction()
        try:
            existing = self._db.execute(
                "select status,evidence_digest from evolution_child_terminal "
                "where reservation_id=?",
                (reservation_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != ("failed", recovery_digest):
                    raise ConcurrencyConflict("Evolution recovery terminal drift")
                self._db.rollback()
                return
            self._db.execute(
                "insert into evolution_child_terminal values(?,?,?,?,?,?,?,?,?)",
                (reservation_id, "failed", recovery_digest, 0, 0, 0, 0, 0, now.isoformat()),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def assert_recovery_terminal(
        self,
        reservation_id: str,
        plan: EvolutionRunPlan,
        child: Authorization,
        *,
        recovery_digest: str,
    ) -> None:
        """Verify the exact failed terminal created by bounded recovery."""

        self.assert_claimed_recovery(reservation_id, plan, child)
        row = self._db.execute(
            "select status,evidence_digest from evolution_child_terminal "
            "where reservation_id=?",
            (reservation_id,),
        ).fetchone()
        if row is None or tuple(row) != ("failed", recovery_digest):
            raise PolicyViolation("Evolution recovery terminal missing or drifted")

    def _assert_remaining_budget(self, grant: StandingGrant, requested: EvolutionBudget) -> None:
        row = self._db.execute(
            "select "
            "coalesce(sum(case when t.status in('completed','failed') then t.provider_calls "
            "else r.provider_calls end),0),"
            "coalesce(sum(case when t.status in('completed','failed') then t.tokens "
            "else r.tokens end),0),"
            "coalesce(sum(case when t.status in('completed','failed') then t.duration_seconds "
            "else r.duration_seconds end),0),"
            "coalesce(sum(case when t.status in('completed','failed') then t.cost_micros "
            "else r.cost_micros end),0),"
            "coalesce(sum(case when t.status in('completed','failed') then t.disk_bytes "
            "else r.disk_bytes end),0) "
            "from evolution_child_reservation r "
            "join evolution_standing_grant g on g.grant_digest=r.grant_digest "
            "left join evolution_child_terminal t on t.reservation_id=r.reservation_id "
            "where g.grant_id=?",
            (str(grant.grant_id),),
        ).fetchone()
        assert row is not None
        totals = tuple(map(int, row))
        limits = grant.budget.body()
        requested_body = requested.body()
        for index, name in enumerate(
            ("provider_calls", "tokens", "duration_seconds", "cost_micros", "disk_bytes")
        ):
            if totals[index] + requested_body[name] > limits[name]:
                raise PolicyViolation(f"Standing grant {name} budget exhausted")
        active = self._db.execute(
            "select coalesce(sum(r.concurrency),0) from evolution_child_reservation r "
            "join evolution_standing_grant g on g.grant_digest=r.grant_digest "
            "left join evolution_child_terminal t on t.reservation_id=r.reservation_id "
            "where g.grant_id=? and (t.reservation_id is null or t.status='unknown')",
            (str(grant.grant_id),),
        ).fetchone()
        assert active is not None
        if int(active[0]) + requested.concurrency > grant.budget.concurrency:
            raise PolicyViolation("Standing grant concurrency budget exhausted")

    def _assert_current_grant(self, grant: StandingGrant) -> None:
        stored = self._db.execute(
            "select grant_digest,body_json from evolution_standing_grant "
            "where grant_id=? order by revision desc limit 1",
            (str(grant.grant_id),),
        ).fetchone()
        if stored is None or str(stored[0]) != grant.grant_digest:
            raise PolicyViolation("Standing grant missing or superseded")
        if str(stored[1]) != canonical_json(grant.body()):
            raise PolicyViolation("Standing grant stored body drift")
        revoked = self._db.execute(
            "select 1 from evolution_grant_revocation where grant_digest=?",
            (grant.grant_digest,),
        ).fetchone()
        if revoked is not None:
            raise PolicyViolation("Standing grant revoked")

    def _transaction(self) -> None:
        if self._db.in_transaction:
            raise ConcurrencyConflict("Evolution ledger nested transaction forbidden")
        self._db.execute("begin immediate")


def evolution_authority_schema_digest() -> str:
    """Stable DDL fingerprint for migration binding."""

    return digest({"ddl": EVOLUTION_AUTHORITY_DDL})
