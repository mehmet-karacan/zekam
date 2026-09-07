from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID

import pytest

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.domain.evolution_authority import (
    EvolutionBudget,
    EvolutionRunPlan,
    ExecutionBoundary,
    StandingGrant,
    StandingGrantKind,
    StandingGrantState,
    derive_child_authorization,
    evolution_effect_digest,
)
from zekam.domain.improvement_policy import ImprovementChangeClass
from zekam.domain.security import Authorization, AuthorizationState, DataClassification
from zekam.infrastructure.sqlite.evolution_authority import (
    EVOLUTION_AUTHORITY_DDL,
    SQLiteEvolutionApprovalAuthority,
    SQLiteEvolutionAuthorityLedger,
)

NOW = dt.datetime(2026, 9, 6, 12, tzinfo=dt.UTC)
ONE = UUID("00000000-0000-0000-0000-000000000001")
TWO = UUID("00000000-0000-0000-0000-000000000002")
THREE = UUID("00000000-0000-0000-0000-000000000003")
D = digest({"fixture": "exact"})


def approval(parent: StandingGrant) -> str:
    return digest({"grant": parent.grant_digest, "approval": "fixture"})


def budget(**changes: int) -> EvolutionBudget:
    values = {
        "provider_calls": 2,
        "tokens": 1000,
        "duration_seconds": 60,
        "cost_micros": 100,
        "disk_bytes": 4096,
        "concurrency": 1,
    }
    values.update(changes)
    return EvolutionBudget(**values)


def grant(**changes: object) -> StandingGrant:
    values: dict[str, object] = {
        "grant_id": ONE,
        "revision": 1,
        "kind": StandingGrantKind.EVOLUTION,
        "owner_id": TWO,
        "actor_id": THREE,
        "device_id": "windows-dev-1",
        "realm_id": ONE,
        "project_ids": (TWO,),
        "logical_sources": ("gpu",),
        "operations": ("knowledge.compile",),
        "handler_versions": ("knowledge.compile/v1",),
        "change_classes": (ImprovementChangeClass.AUTO_SAFE,),
        "readable_resources": ("local-capture",),
        "writable_resources": ("local-report",),
        "model_refs": ("none",),
        "provider_refs": ("local-deterministic",),
        "data_classifications": (DataClassification.LOCAL_ONLY,),
        "network_scopes": (),
        "execution_boundary": ExecutionBoundary.LOCAL,
        "task_scope_digest": D,
        "policy_digest": D,
        "verifier_digest": D,
        "validator_digest": D,
        "source_lineage_digest": D,
        "protected_manifest_digest": D,
        "dependency_manifest_digest": D,
        "budget": budget(),
        "valid_from": NOW - dt.timedelta(minutes=1),
        "expires_at": NOW + dt.timedelta(hours=1),
        "review_after": NOW + dt.timedelta(minutes=30),
        "rollback_required": True,
        "notify_on_failure": True,
    }
    values.update(changes)
    return StandingGrant(**values)


def plan(parent: StandingGrant, **changes: object) -> EvolutionRunPlan:
    requested = budget(provider_calls=0, tokens=0, cost_micros=0)
    values: dict[str, object] = {
        "parent_grant_digest": parent.grant_digest,
        "project_id": TWO,
        "logical_source": "gpu",
        "operation": "knowledge.compile",
        "handler_version": "knowledge.compile/v1",
        "change_class": ImprovementChangeClass.AUTO_SAFE,
        "readable_resources": ("local-capture",),
        "writable_resources": ("local-report",),
        "model_ref": "none",
        "provider_ref": "local-deterministic",
        "data_classifications": (DataClassification.LOCAL_ONLY,),
        "network_scope": None,
        "execution_boundary": ExecutionBoundary.LOCAL,
        "task_scope_digest": D,
        "policy_digest": D,
        "verifier_digest": D,
        "validator_digest": D,
        "source_lineage_digest": D,
        "protected_manifest_digest": D,
        "dependency_manifest_digest": D,
        "input_digest": D,
        "candidate_digest": D,
        "fixture_digest": D,
        "budget_digest": digest(requested.body()),
        "budget": requested,
        "executor_ref": "worker-a",
        "verifier_ref": "verifier-b",
        "review_receipt_digest": None,
        "requested_at": NOW,
        "deadline": NOW + dt.timedelta(minutes=10),
    }
    values.update(changes)
    if "effect_digest" not in changes:
        values["effect_digest"] = evolution_effect_digest(
            operation=str(values["operation"]),
            handler_version=str(values["handler_version"]),
            input_digest=str(values["input_digest"]),
            candidate_digest=str(values["candidate_digest"]),
            fixture_digest=str(values["fixture_digest"]),
            budget_digest=str(values["budget_digest"]),
            readable_resources=values["readable_resources"],  # type: ignore[arg-type]
            writable_resources=values["writable_resources"],  # type: ignore[arg-type]
        )
    return EvolutionRunPlan(**values)


def test_exact_plan_derives_existing_one_shot_authorization() -> None:
    parent = grant()
    run = plan(parent)

    child = derive_child_authorization(parent, run, now=NOW)

    assert child.state is AuthorizationState.ISSUED
    assert child.plan_digest == run.plan_digest
    assert child.effect_digest == run.effect_digest
    assert child.realm_id == parent.realm_id
    assert child.actor_id == parent.actor_id
    assert child.scope.allowed_effects == (run.operation,)
    assert child.scope.allowed_resources == ("local-capture", "local-report")
    assert child.scope.allowed_read_resources == ("local-capture",)
    assert child.scope.allowed_write_resources == ("local-report",)
    assert child.expires_at == run.deadline


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("logical_sources", ("*",)),
        ("operations", ("knowledge.*",)),
        ("writable_resources", ("authorization",)),
        ("change_classes", (ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED,)),
        ("change_classes", (ImprovementChangeClass.PROHIBITED_AUTONOMOUS,)),
        ("operations", ("knowledge.compile", "knowledge.compile")),
    ],
)
def test_standing_grant_rejects_broad_or_protected_scope(field: str, value: object) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        grant(**{field: value})


def test_remote_and_code_maintenance_grants_are_separate_and_narrow() -> None:
    with pytest.raises(PolicyViolation, match="outbound-safe"):
        grant(
            execution_boundary=ExecutionBoundary.REMOTE,
            network_scopes=("internet",),
        )
    with pytest.raises(PolicyViolation, match="kaynak koduna"):
        grant(writable_resources=("source-file:src/zekam/example.py",))
    with pytest.raises(PolicyViolation, match="exact source files"):
        grant(kind=StandingGrantKind.CODE_MAINTENANCE)
    with pytest.raises(PolicyViolation, match="protected source dependency"):
        grant(
            kind=StandingGrantKind.CODE_MAINTENANCE,
            operations=("maintenance.reconcile",),
            handler_versions=("maintenance.reconcile/v1",),
            readable_resources=("local-registry",),
            writable_resources=("source-file:src/zekam/domain/security.py",),
        )

    maintenance = grant(
        kind=StandingGrantKind.CODE_MAINTENANCE,
        operations=("maintenance.reconcile",),
        handler_versions=("maintenance.reconcile/v1",),
        readable_resources=("local-registry",),
        writable_resources=("source-file:src/zekam/example.py",),
    )
    assert maintenance.kind is StandingGrantKind.CODE_MAINTENANCE


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", THREE),
        ("logical_source", "sky"),
        ("operation", "sources.refresh"),
        ("handler_version", "knowledge.compile/v2"),
        ("model_ref", "remote-model"),
        ("provider_ref", "remote-provider"),
        ("network_scope", "internet"),
        ("policy_digest", digest({"drift": "policy"})),
        ("verifier_digest", digest({"drift": "verifier"})),
        ("validator_digest", digest({"drift": "validator"})),
        ("source_lineage_digest", digest({"drift": "source"})),
        ("protected_manifest_digest", digest({"drift": "protected"})),
        ("dependency_manifest_digest", digest({"drift": "dependency"})),
        ("writable_resources", ("other",)),
    ],
)
def test_child_admission_rejects_scope_or_digest_drift(field: str, value: object) -> None:
    parent = grant()
    with pytest.raises(PolicyViolation):
        derive_child_authorization(parent, plan(parent, **{field: value}), now=NOW)


@pytest.mark.parametrize("state", [StandingGrantState.PAUSED, StandingGrantState.REVOKED])
def test_child_admission_rejects_non_active_parent(state: StandingGrantState) -> None:
    parent = grant(state=state)
    with pytest.raises(PolicyViolation):
        derive_child_authorization(parent, plan(parent), now=NOW)


def test_child_admission_rejects_review_due_parent() -> None:
    parent = grant(review_after=NOW)
    with pytest.raises(PolicyViolation, match="validity/review"):
        derive_child_authorization(parent, plan(parent), now=NOW)


def test_review_required_child_needs_independent_receipt() -> None:
    parent = grant(
        change_classes=(
            ImprovementChangeClass.AUTO_SAFE,
            ImprovementChangeClass.REVIEW_REQUIRED,
        )
    )
    run = plan(parent, change_class=ImprovementChangeClass.REVIEW_REQUIRED)
    with pytest.raises(PolicyViolation, match="review receipt"):
        derive_child_authorization(parent, run, now=NOW)

    reviewed = replace(run, review_receipt_digest=D)
    assert derive_child_authorization(parent, reviewed, now=NOW).risk == "REVIEW_REQUIRED"


def test_child_budget_cannot_widen_parent() -> None:
    parent = grant()
    requested = budget(tokens=1001)
    run = plan(parent, budget=requested, budget_digest=digest(requested.body()))
    with pytest.raises(PolicyViolation, match="budget"):
        derive_child_authorization(parent, run, now=NOW)


@pytest.mark.parametrize("bad", [-1, True])
def test_budget_rejects_negative_and_boolean_values(bad: int) -> None:
    with pytest.raises(ValidationFailed):
        budget(tokens=bad)


def test_plan_rejects_budget_digest_drift_and_same_reviewer() -> None:
    parent = grant()
    with pytest.raises(ValidationFailed, match="budget digest"):
        plan(parent, budget_digest=D)
    with pytest.raises(PolicyViolation, match="bagimsiz"):
        plan(parent, verifier_ref="worker-a")
    with pytest.raises(PolicyViolation, match="effect digest"):
        plan(parent, effect_digest=D)
    with pytest.raises(PolicyViolation, match="operation/handler"):
        plan(parent, handler_version="knowledge.compile/v2")


class TrustedAuthority:
    def __init__(
        self,
        *,
        approval_valid: bool = True,
        review_valid: bool = True,
        effect_current: bool = True,
        terminal_valid: bool = True,
    ) -> None:
        self.approval_valid = approval_valid
        self.review_valid = review_valid
        self.effect_current = effect_current
        self.terminal_valid = terminal_valid

    def verify_grant_approval(
        self, parent: StandingGrant, approval_receipt_digest: str, *, now: dt.datetime
    ) -> bool:
        return (
            self.approval_valid
            and parent.owner_id == TWO
            and approval_receipt_digest == approval(parent)
            and now == NOW
        )

    def verify_review(self, run: EvolutionRunPlan, *, now: dt.datetime) -> bool:
        return self.review_valid and run.review_receipt_digest == D and now == NOW

    def verify_effect_current(
        self,
        parent: StandingGrant,
        run: EvolutionRunPlan,
        child: Authorization,
        *,
        now: dt.datetime,
    ) -> bool:
        return (
            self.effect_current
            and parent.owner_id == TWO
            and child.realm_id == parent.realm_id
            and child.plan_digest == run.plan_digest
        )

    def verify_terminal_readback(
        self,
        run: EvolutionRunPlan,
        child: Authorization,
        status: str,
        evidence_digest: str,
        usage: EvolutionBudget,
        *,
        now: dt.datetime,
    ) -> bool:
        return (
            self.terminal_valid
            and child.plan_digest == run.plan_digest
            and status in {"completed", "failed", "unknown"}
            and evidence_digest == D
            and usage.duration_seconds > 0
            and now == NOW
        )


AUTHORITY = TrustedAuthority()


def ledger() -> SQLiteEvolutionAuthorityLedger:
    connection = sqlite3.connect(":memory:")
    connection.execute("pragma foreign_keys=on")
    connection.executescript(EVOLUTION_AUTHORITY_DDL)
    return SQLiteEvolutionAuthorityLedger(connection, AUTHORITY)


def test_transactional_reservation_is_idempotent_and_revoke_blocks_new_child() -> None:
    parent = grant()
    run = plan(parent)
    store = ledger()
    store.register_grant(parent, approval_receipt_digest=approval(parent), now=NOW)

    first = store.reserve_child(
        parent,
        run,
        reservation_id="reservation-1",
        idempotency_key="run-1",
        now=NOW,
    )
    replay = store.reserve_child(
        parent,
        run,
        reservation_id="ignored-on-replay",
        idempotency_key="run-1",
        now=NOW,
    )
    assert replay.replayed is True
    assert replay.reservation_id == first.reservation_id
    assert replay.authorization.id == first.authorization.id

    store.revoke(parent.grant_digest, reason="owner-request", now=NOW)
    with pytest.raises(PolicyViolation, match="revoked"):
        store.reserve_child(
            parent,
            run,
            reservation_id="ignored-after-revoke",
            idempotency_key="run-1",
            now=NOW,
        )
    second = plan(
        parent,
        candidate_digest=digest({"candidate": 2}),
    )
    with pytest.raises(PolicyViolation, match="revoked"):
        store.reserve_child(
            parent,
            second,
            reservation_id="reservation-2",
            idempotency_key="run-2",
            now=NOW,
        )


def test_trusted_authority_controls_grant_review_and_effect_boundary() -> None:
    parent = grant()
    connection = sqlite3.connect(":memory:")
    connection.execute("pragma foreign_keys=on")
    connection.executescript(EVOLUTION_AUTHORITY_DDL)
    denied = SQLiteEvolutionAuthorityLedger(connection, TrustedAuthority(approval_valid=False))
    with pytest.raises(PolicyViolation, match="approval receipt"):
        denied.register_grant(parent, approval_receipt_digest=approval(parent), now=NOW)

    trusted = TrustedAuthority(review_valid=False)
    store = SQLiteEvolutionAuthorityLedger(connection, trusted)
    store.register_grant(parent, approval_receipt_digest=approval(parent), now=NOW)
    reviewed_parent = replace(
        parent,
        change_classes=(
            ImprovementChangeClass.AUTO_SAFE,
            ImprovementChangeClass.REVIEW_REQUIRED,
        ),
        grant_id=THREE,
    )
    store.register_grant(
        reviewed_parent,
        approval_receipt_digest=approval(reviewed_parent),
        now=NOW,
    )
    reviewed = plan(
        reviewed_parent,
        change_class=ImprovementChangeClass.REVIEW_REQUIRED,
        review_receipt_digest=D,
    )
    with pytest.raises(PolicyViolation, match="trusted review"):
        store.reserve_child(
            reviewed_parent,
            reviewed,
            reservation_id="reviewed-reservation",
            idempotency_key="reviewed-run",
            now=NOW,
        )

    run = plan(parent)
    reservation = store.reserve_child(
        parent,
        run,
        reservation_id="reservation-1",
        idempotency_key="run-1",
        now=NOW,
    )
    trusted.effect_current = False
    with pytest.raises(PolicyViolation, match="current policy/provider/source drift"):
        store.claim_effect(
            parent,
            run,
            effect_body=run.effect_body(),
            reservation_id=reservation.reservation_id,
            authorization_digest=reservation.authorization.authorization_digest,
            effect_digest=reservation.authorization.effect_digest,
            now=NOW,
        )


def test_sqlite_authority_verifies_immutable_approval_and_current_attestation() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("pragma foreign_keys=on")
    connection.executescript(EVOLUTION_AUTHORITY_DDL)
    parent = grant()
    approval_digest = approval(parent)
    connection.execute(
        "insert into evolution_grant_approval values(?,?,?,?,?,?,?,?)",
        (
            approval_digest,
            parent.grant_digest,
            str(parent.owner_id),
            str(parent.realm_id),
            parent.device_id,
            str(parent.owner_id),
            (NOW - dt.timedelta(minutes=1)).isoformat(),
            (NOW + dt.timedelta(minutes=30)).isoformat(),
        ),
    )
    connection.commit()
    authority = SQLiteEvolutionApprovalAuthority(connection)
    store = SQLiteEvolutionAuthorityLedger(connection, authority)
    store.register_grant(parent, approval_receipt_digest=approval_digest, now=NOW)
    run = plan(parent)
    reservation = store.reserve_child(
        parent,
        run,
        reservation_id="reservation-1",
        idempotency_key="run-1",
        now=NOW,
    )
    connection.execute(
        "insert into evolution_runtime_attestation values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            digest({"attestation": 1}),
            parent.grant_digest,
            run.plan_digest,
            reservation.authorization.authorization_digest,
            run.task_scope_digest,
            run.policy_digest,
            run.verifier_digest,
            run.validator_digest,
            run.source_lineage_digest,
            run.protected_manifest_digest,
            run.dependency_manifest_digest,
            run.model_ref,
            run.provider_ref,
            run.execution_boundary.value,
            run.verifier_ref,
            run.executor_ref,
            NOW.isoformat(),
            (NOW + dt.timedelta(minutes=1)).isoformat(),
        ),
    )
    connection.commit()
    store.claim_effect(
        parent,
        run,
        effect_body=run.effect_body(),
        reservation_id=reservation.reservation_id,
        authorization_digest=reservation.authorization.authorization_digest,
        effect_digest=reservation.authorization.effect_digest,
        now=NOW,
    )
    connection.execute(
        "insert into evolution_terminal_readback values(?,?,?,?,?,?,?,?,?)",
        (
            digest({"readback": 1}),
            reservation.authorization.authorization_digest,
            run.plan_digest,
            "completed",
            D,
            canonical_json(run.budget.body()),
            run.verifier_ref,
            run.executor_ref,
            NOW.isoformat(),
        ),
    )
    connection.commit()
    store.record_terminal(
        reservation.reservation_id,
        run,
        reservation.authorization,
        status="completed",
        evidence_digest=D,
        usage=run.budget,
        now=NOW,
    )


def test_unknown_terminal_does_not_release_concurrency() -> None:
    parent = grant(budget=budget(tokens=5000, duration_seconds=600, disk_bytes=20000))
    store = ledger()
    store.register_grant(parent, approval_receipt_digest=approval(parent), now=NOW)
    first = plan(parent)
    reservation = store.reserve_child(
        parent,
        first,
        reservation_id="reservation-1",
        idempotency_key="run-1",
        now=NOW,
    )
    store.claim_effect(
        parent,
        first,
        effect_body=first.effect_body(),
        reservation_id=reservation.reservation_id,
        authorization_digest=reservation.authorization.authorization_digest,
        effect_digest=reservation.authorization.effect_digest,
        now=NOW,
    )
    store.record_terminal(
        "reservation-1",
        first,
        reservation.authorization,
        status="unknown",
        evidence_digest=D,
        usage=first.budget,
        now=NOW,
    )
    second = plan(
        parent,
        candidate_digest=digest({"candidate": 2}),
    )
    with pytest.raises(PolicyViolation, match="concurrency"):
        store.reserve_child(
            parent,
            second,
            reservation_id="reservation-2",
            idempotency_key="run-2",
            now=NOW,
        )


def test_verified_actual_usage_reconciles_reserved_budget() -> None:
    parent = grant(
        budget=budget(
            tokens=1000,
            duration_seconds=61,
            disk_bytes=4097,
            concurrency=1,
        )
    )
    store = ledger()
    store.register_grant(parent, approval_receipt_digest=approval(parent), now=NOW)
    first = plan(parent)
    reservation = store.reserve_child(
        parent,
        first,
        reservation_id="reservation-1",
        idempotency_key="run-1",
        now=NOW,
    )
    store.claim_effect(
        parent,
        first,
        effect_body=first.effect_body(),
        reservation_id=reservation.reservation_id,
        authorization_digest=reservation.authorization.authorization_digest,
        effect_digest=reservation.authorization.effect_digest,
        now=NOW,
    )
    store.record_terminal(
        reservation.reservation_id,
        first,
        reservation.authorization,
        status="completed",
        evidence_digest=D,
        usage=budget(
            provider_calls=0,
            tokens=0,
            duration_seconds=1,
            cost_micros=0,
            disk_bytes=1,
        ),
        now=NOW,
    )
    second = plan(parent, candidate_digest=digest({"candidate": 2}))
    assert (
        store.reserve_child(
            parent,
            second,
            reservation_id="reservation-2",
            idempotency_key="run-2",
            now=NOW,
        ).replayed
        is False
    )


def test_effect_claim_is_one_shot_and_rechecks_revocation() -> None:
    parent = grant()
    store = ledger()
    store.register_grant(parent, approval_receipt_digest=approval(parent), now=NOW)
    run = plan(parent)
    reservation = store.reserve_child(
        parent,
        run,
        reservation_id="reservation-1",
        idempotency_key="run-1",
        now=NOW,
    )
    with pytest.raises(PolicyViolation, match="expired"):
        store.claim_effect(
            parent,
            run,
            effect_body=run.effect_body(),
            reservation_id=reservation.reservation_id,
            authorization_digest=reservation.authorization.authorization_digest,
            effect_digest=reservation.authorization.effect_digest,
            now=NOW - dt.timedelta(seconds=1),
        )
    arguments = {
        "effect_body": run.effect_body(),
        "reservation_id": reservation.reservation_id,
        "authorization_digest": reservation.authorization.authorization_digest,
        "effect_digest": reservation.authorization.effect_digest,
        "now": NOW,
    }
    mismatched = dict(run.effect_body())
    mismatched["input_digest"] = digest({"other": "input"})
    with pytest.raises(PolicyViolation, match="authorization drift"):
        store.claim_effect(parent, run, **{**arguments, "effect_body": mismatched})
    store.claim_effect(parent, run, **arguments)
    with pytest.raises(ConcurrencyConflict, match="consumed"):
        store.claim_effect(parent, run, **arguments)

    other_parent = replace(parent, grant_id=THREE)
    store.register_grant(
        other_parent,
        approval_receipt_digest=approval(other_parent),
        now=NOW,
    )
    other_run = plan(other_parent)
    other = store.reserve_child(
        other_parent,
        other_run,
        reservation_id="reservation-2",
        idempotency_key="run-2",
        now=NOW,
    )
    store.revoke(other_parent.grant_digest, reason="owner-request", now=NOW)
    with pytest.raises(PolicyViolation, match="revoked"):
        store.claim_effect(
            other_parent,
            other_run,
            effect_body=other_run.effect_body(),
            reservation_id=other.reservation_id,
            authorization_digest=other.authorization.authorization_digest,
            effect_digest=other.authorization.effect_digest,
            now=NOW,
        )


def test_terminal_usage_requires_independent_readback() -> None:
    parent = grant()
    connection = sqlite3.connect(":memory:")
    connection.execute("pragma foreign_keys=on")
    connection.executescript(EVOLUTION_AUTHORITY_DDL)
    store = SQLiteEvolutionAuthorityLedger(connection, TrustedAuthority(terminal_valid=False))
    store.register_grant(parent, approval_receipt_digest=approval(parent), now=NOW)
    run = plan(parent)
    reservation = store.reserve_child(
        parent,
        run,
        reservation_id="reservation-1",
        idempotency_key="run-1",
        now=NOW,
    )
    store.claim_effect(
        parent,
        run,
        effect_body=run.effect_body(),
        reservation_id=reservation.reservation_id,
        authorization_digest=reservation.authorization.authorization_digest,
        effect_digest=reservation.authorization.effect_digest,
        now=NOW,
    )
    with pytest.raises(PolicyViolation, match="independent readback"):
        store.record_terminal(
            reservation.reservation_id,
            run,
            reservation.authorization,
            status="completed",
            evidence_digest=D,
            usage=run.budget,
            now=NOW,
        )


def test_concurrent_reservation_has_one_winner(tmp_path) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "operational-test.db"
    with sqlite3.connect(database) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.executescript(EVOLUTION_AUTHORITY_DDL)
        parent = grant(budget=budget(tokens=5000, duration_seconds=600, disk_bytes=20000))
        SQLiteEvolutionAuthorityLedger(connection, AUTHORITY).register_grant(
            parent, approval_receipt_digest=approval(parent), now=NOW
        )

    barrier = threading.Barrier(2)

    def reserve(index: int) -> str:
        connection = sqlite3.connect(database, timeout=5)
        connection.execute("pragma foreign_keys=on")
        store = SQLiteEvolutionAuthorityLedger(connection, AUTHORITY)
        run = plan(
            parent,
            candidate_digest=digest({"candidate": index}),
        )
        barrier.wait()
        try:
            store.reserve_child(
                parent,
                run,
                reservation_id=f"reservation-{index}",
                idempotency_key=f"run-{index}",
                now=NOW,
            )
        except PolicyViolation as exc:
            return str(exc)
        finally:
            connection.close()
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(reserve, (1, 2)))
    assert outcomes.count("reserved") == 1
    assert sum("concurrency" in value for value in outcomes) == 1
