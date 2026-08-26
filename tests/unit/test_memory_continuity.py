from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from zekam.application.memory_continuity import (
    HydrationPreparation,
    MemoryContinuityService,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.session_continuity import (
    CloseStatus,
    ContextOmissionReference,
    ContextSelectionReference,
    DigestReference,
    FreshnessDimension,
    SessionCloseReceipt,
    SessionHydrationReceipt,
    TruthClass,
)

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 8, 26, 11, 0, tzinfo=dt.UTC)


class Connection:
    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield


class Repository:
    def __init__(self) -> None:
        self.connection = Connection()
        self.stored: list[str] = []
        self.snapshot = SimpleNamespace(
            hydration_receipt_digest=None,
            hydration_fresh=False,
            hydration_complete=False,
            open_gaps=(),
            ready_for_mutation=False,
        )

    def store_hydration_receipt(
        self, receipt: SessionHydrationReceipt, *, idempotency_key: str
    ) -> bool:
        self.stored.append(f"hydration:{idempotency_key}")
        return True

    def store_close_receipt(self, receipt, *, idempotency_key):  # type: ignore[no-untyped-def]
        self.stored.append(f"close:{idempotency_key}")
        return True

    def store_compaction_receipt(self, receipt, *, idempotency_key):  # type: ignore[no-untyped-def]
        self.stored.append(f"compaction:{idempotency_key}")
        return True

    def read_session_snapshot(self, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["session_id"] == "session-hydration"
        return self.snapshot


@dataclass(frozen=True)
class Consumed:
    consumed: bool
    reason: str = "consumed"


class Authorizations:
    def __init__(self) -> None:
        self.current: Authorization | None = None
        self.consumed = 0

    def get(self, authorization_id: UUID) -> Authorization:
        assert self.current is not None and authorization_id == self.current.id
        return self.current

    def consume(self, authorization_id, *, effect_digest, consumed_by, now=None):  # type: ignore[no-untyped-def]
        assert self.current is not None
        assert authorization_id == self.current.id
        assert effect_digest == self.current.effect_digest
        assert consumed_by == "memory-continuity/v1"
        self.consumed += 1
        return Consumed(True)


def _selection(ref: str, tokens: int) -> ContextSelectionReference:
    return ContextSelectionReference(ref, digest(ref), tokens, TruthClass.REPO_FACT)


def _request(*, budget: int = 8) -> HydrationPreparation:
    current = digest("current")
    return HydrationPreparation(
        receipt_id=uuid4(),
        realm_id=uuid4(),
        project_id=uuid4(),
        work_item_id=uuid4(),
        run_id=uuid4(),
        session_id="session-hydration",
        client_id="opencode-local",
        plan_ref="work-plan:revision-3",
        checkpoint_ref="checkpoint:one",
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
        inventory_digest=digest("inventory"),
        context_digest=digest("context"),
        required_candidates=(_selection("required:work", 5),),
        optional_candidates=(
            _selection("optional:b", 3),
            _selection("optional:a", 2),
        ),
        known_omissions=(),
        token_budget=budget,
        freshness=(FreshnessDimension("source", current, current, True),),
        projection_refs=(),
        hydration_event_digest=digest("hydration-event"),
        idempotency_key="hydration:session-hydration:1",
        created_at=NOW,
    )


def test_hydration_prepare_is_deterministic_required_first_and_budget_bounded() -> None:
    service = MemoryContinuityService(Repository(), Authorizations())
    request = _request()

    first = service.prepare_hydration(request)
    second = service.prepare_hydration(request)

    assert first.plan_digest == second.plan_digest
    receipt = first.receipt
    assert isinstance(receipt, SessionHydrationReceipt)
    assert tuple(item.ref for item in receipt.required_selections) == ("required:work",)
    assert tuple(item.ref for item in receipt.optional_selections) == ("optional:a",)
    assert receipt.omissions == (
        ContextOmissionReference("optional:b", "token-budget", required=False),
    )
    assert receipt.tokens_used == 7 and receipt.complete and receipt.fresh


def test_hydration_required_set_cannot_be_silently_truncated() -> None:
    service = MemoryContinuityService(Repository(), Authorizations())
    with pytest.raises(PolicyViolation, match="Required continuity set"):
        service.prepare_hydration(_request(budget=4))


def test_apply_revalidates_drift_and_consumes_exact_authorization() -> None:
    repository = Repository()
    authorizations = Authorizations()
    service = MemoryContinuityService(repository, authorizations)
    plan = service.prepare_hydration(_request())
    authorization = Authorization.issue(
        realm_id=plan.receipt.realm_id,
        actor_id=uuid4(),
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(plan.resource,), allowed_effects=("database-write",)
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )
    authorizations.current = authorization

    applied = service.apply(
        plan,
        authorization_id=authorization.id,
        current_source_digest=plan.source_digest,
        current_policy_digest=plan.policy_digest,
        current_migration_digest=plan.migration_digest,
        current_context_digest=plan.context_digest,
        now=NOW,
    )

    assert repository.stored == ["hydration:hydration:session-hydration:1"]
    assert authorizations.consumed == 1
    assert applied.receipt_digest == plan.receipt_digest
    assert applied.result_digest.startswith("sha256:")

    with pytest.raises(PolicyViolation, match="binding drift"):
        service.apply(
            plan,
            authorization_id=authorization.id,
            current_source_digest=digest("changed-source"),
            current_policy_digest=plan.policy_digest,
            current_migration_digest=plan.migration_digest,
            current_context_digest=plan.context_digest,
            now=NOW,
        )


def test_apply_rejects_authorization_scope_mismatch_before_store() -> None:
    repository = Repository()
    authorizations = Authorizations()
    service = MemoryContinuityService(repository, authorizations)
    plan = service.prepare_hydration(_request())
    wrong = Authorization.issue(
        realm_id=plan.receipt.realm_id,
        actor_id=uuid4(),
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=("continuity:hydration:other",),
            allowed_effects=("database-write",),
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )
    authorizations.current = wrong
    with pytest.raises(AuthorizationRequired, match="exact authorization"):
        service.apply(
            plan,
            authorization_id=wrong.id,
            current_source_digest=plan.source_digest,
            current_policy_digest=plan.policy_digest,
            current_migration_digest=plan.migration_digest,
            current_context_digest=plan.context_digest,
            now=NOW,
        )
    assert repository.stored == [] and authorizations.consumed == 0


def test_mutating_admission_requires_fresh_complete_hydration_and_no_open_gap() -> None:
    repository = Repository()
    service = MemoryContinuityService(repository, Authorizations())
    identity = {
        "project_id": uuid4(),
        "work_item_id": uuid4(),
        "run_id": uuid4(),
        "session_id": "session-hydration",
        "client_id": "opencode-local",
    }
    with pytest.raises(PolicyViolation, match="hydration-missing"):
        service.assert_mutating_admission(**identity)

    repository.snapshot = SimpleNamespace(
        hydration_receipt_digest=digest("hydration"),
        hydration_fresh=True,
        hydration_complete=True,
        open_gaps=("gap:recovery-required",),
        ready_for_mutation=False,
    )
    with pytest.raises(PolicyViolation, match="open-gaps:1"):
        service.assert_mutating_admission(**identity)

    repository.snapshot = SimpleNamespace(
        hydration_receipt_digest=digest("hydration"),
        hydration_fresh=True,
        hydration_complete=True,
        open_gaps=(),
        ready_for_mutation=True,
    )
    assert service.assert_mutating_admission(**identity) is repository.snapshot


def test_close_prepare_apply_routes_immutable_receipt_through_same_authority_gate() -> None:
    repository = Repository()
    authorizations = Authorizations()
    service = MemoryContinuityService(repository, authorizations)
    realm_id = uuid4()
    next_action = DigestReference("action:none", digest("no-next-action"), TruthClass.USER_DECISION)
    checkpoint = DigestReference(
        "checkpoint:final", digest("checkpoint-final"), TruthClass.REPO_FACT
    )
    journal = DigestReference("journal:head", digest("journal-head"), TruthClass.REPO_FACT)
    receipt = SessionCloseReceipt(
        receipt_id=uuid4(),
        realm_id=realm_id,
        project_id=uuid4(),
        work_item_id=uuid4(),
        run_id=uuid4(),
        session_id="session-close",
        client_id="opencode-local",
        job_id=uuid4(),
        attempt_id=uuid4(),
        envelope_digest=digest("envelope"),
        fencing_token=1,
        completed_steps=(),
        changed_artifacts=(),
        verified_outcomes=(),
        pending_steps=(),
        next_safe_action=next_action,
        human_decisions=(),
        discovered_constraints=(),
        failure_recovery_refs=(),
        candidate_lessons=(),
        candidate_skills=(),
        checkpoint_ref=checkpoint,
        journal_head=journal,
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
        context_digest=digest("context"),
        status=CloseStatus.CLOSED,
        closed_at=NOW,
    )
    plan = service.prepare_close(receipt, idempotency_key="close:session-close:1")
    authorization = Authorization.issue(
        realm_id=realm_id,
        actor_id=uuid4(),
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(plan.resource,), allowed_effects=("database-write",)
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )
    authorizations.current = authorization

    applied = service.apply(
        plan,
        authorization_id=authorization.id,
        current_source_digest=plan.source_digest,
        current_policy_digest=plan.policy_digest,
        current_migration_digest=plan.migration_digest,
        current_context_digest=plan.context_digest,
        now=NOW,
    )

    assert repository.stored == ["close:close:session-close:1"]
    assert applied.receipt_id == receipt.receipt_id
