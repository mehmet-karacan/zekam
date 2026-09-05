from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from zekam.application.continuity_projection import ProjectionReleaseSnapshot
from zekam.application.memory_upgrade import canonical_projection_source_digest
from zekam.application.projection_closure import (
    ProjectionAwareClosureService,
    ProjectionClosureApplyReceipt,
    ProjectionClosurePlan,
    ProjectionClosureSnapshot,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.session_continuity import (
    CloseStatus,
    DigestReference,
    SessionCloseReceipt,
    TruthClass,
)
from zekam.domain.work import AcceptanceCriterion, EvidenceRef, WorkItem, WorkState, WorkType

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.UTC)


class Cursor:
    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        return None

    def execute(self, statement: str) -> None:
        assert statement in {
            "set transaction isolation level repeatable read read only",
            "set transaction isolation level serializable",
        }


class Connection:
    @contextmanager
    def transaction(self):  # type: ignore[no-untyped-def]
        yield

    def cursor(self) -> Cursor:
        return Cursor()


class Repository:
    def __init__(self, snapshot: ProjectionClosureSnapshot) -> None:
        self.connection = Connection()
        self.snapshot = snapshot
        self.applied = False
        self.apply_calls = 0
        self.terminal_effect = False
        self.replay_receipt: ProjectionClosureApplyReceipt | None = None
        self.race_after_effect = False

    def has_terminal_effect_receipt(self, claim_id: UUID) -> bool:
        return self.terminal_effect

    def replay_completed_closure(
        self,
        receipt: SessionCloseReceipt,
        *,
        idempotency_key: str,
        plan_digest: str,
        authorization: Authorization,
        claim_id: UUID,
    ) -> ProjectionClosureApplyReceipt | None:
        assert self.terminal_effect
        assert receipt.work_item_id == self.snapshot.work_item.id
        assert idempotency_key and plan_digest == authorization.plan_digest
        return self.replay_receipt

    def read_closure_snapshot(
        self, receipt: SessionCloseReceipt, *, lock: bool = False
    ) -> ProjectionClosureSnapshot:
        assert receipt.work_item_id == self.snapshot.work_item.id
        return self.snapshot

    def apply_closure(
        self,
        plan: ProjectionClosurePlan,
        *,
        authorization: Authorization,
        claim_id: UUID,
        applied_at: dt.datetime,
    ) -> ProjectionClosureApplyReceipt:
        self.applied = True
        self.apply_calls += 1
        result = ProjectionClosureApplyReceipt(
            work_item_id=plan.completed_work.id,
            work_revision=plan.completed_work.revision,
            close_receipt_id=plan.receipt.receipt_id,
            close_receipt_digest=plan.receipt.receipt_digest,
            projection_receipt_id=plan.projection_receipt.receipt_id,
            projection_receipt_digest=plan.projection_receipt.receipt_digest,
            effect_receipt_id=claim_id,
            result_digest=plan.result_digest,
            plan_digest=plan.plan_digest,
            replayed=False,
            applied_at=applied_at,
        )
        if self.race_after_effect:
            self.terminal_effect = True
            self.replay_receipt = replace(result, replayed=True)
            raise ConcurrencyConflict("concurrent terminal commit")
        return result


class Authorizations:
    current: Authorization | None = None

    def get(self, authorization_id: UUID) -> Authorization:
        assert self.current is not None and self.current.id == authorization_id
        return self.current

    def consume(  # type: ignore[no-untyped-def]
        self, authorization_id, *, effect_digest, consumed_by, now=None
    ):
        assert self.current is not None
        assert authorization_id == self.current.id
        assert effect_digest == self.current.effect_digest
        assert consumed_by == "projection-aware-close/v1"
        return type("Consumed", (), {"consumed": True, "reason": "consumed"})()


def _ref(name: str) -> DigestReference:
    return DigestReference(f"evidence/{name}", digest(name), TruthClass.REPO_FACT)


def _fixture(*, verified: bool = True):  # type: ignore[no-untyped-def]
    realm_id, project_id, work_id, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    work = WorkItem(
        id=work_id,
        realm_id=realm_id,
        project_id=project_id,
        type=WorkType.TASK,
        state=WorkState.VERIFICATION,
        title="Projection close",
        revision=7,
        acceptance_criteria=(AcceptanceCriterion("all gates", verified=verified),),
        created_at=NOW - dt.timedelta(hours=1),
        updated_at=NOW - dt.timedelta(minutes=1),
    )
    source_tree = digest("tree")
    database_revision = digest(
        {
            "project_id": str(project_id),
            "work_item_id": str(work_id),
            "work_revision": work.revision,
            "work_state": work.state.value,
            "work_record_digest": work.record_digest,
        }
    )
    current_source = canonical_projection_source_digest(
        source_head="abc123",
        source_tree_digest=source_tree,
        migration_head=56,
        database_revision_digest=database_revision,
    )
    release = ProjectionReleaseSnapshot(
        project_id=project_id,
        work_item_id=work_id,
        work_revision=work.revision,
        work_state=work.state.value,
        work_record_digest=work.record_digest,
        source_head="abc123",
        source_tree_digest=source_tree,
        migration_head=56,
        database_revision_digest=database_revision,
        projection_ref="projection/active-work",
        projection_receipt_digest=digest("current-projection-receipt"),
        projection_digest=digest("current-projection"),
        projection_source_digest=current_source,
        lifecycle_complete=True,
        pending_lifecycle_steps=(),
        next_safe_action="complete-current-work",
    )
    checkpoint = _ref("checkpoint")
    completed = work.with_state(
        WorkState.COMPLETED,
        evidence=(EvidenceRef("closure-checkpoint", checkpoint.ref, checkpoint.digest_value),),
        now=NOW,
    )
    completed_database = digest(
        {
            "project_id": str(project_id),
            "work_item_id": str(work_id),
            "work_revision": completed.revision,
            "work_state": completed.state.value,
            "work_record_digest": completed.record_digest,
        }
    )
    completed_source = canonical_projection_source_digest(
        source_head=release.source_head,
        source_tree_digest=release.source_tree_digest,
        migration_head=release.migration_head,
        database_revision_digest=completed_database,
    )
    job_id, attempt_id = uuid4(), uuid4()
    receipt = SessionCloseReceipt(
        receipt_id=uuid4(),
        realm_id=realm_id,
        project_id=project_id,
        work_item_id=work_id,
        run_id=run_id,
        session_id="session-close",
        client_id="codex-local",
        job_id=job_id,
        attempt_id=attempt_id,
        envelope_digest=digest("envelope"),
        fencing_token=3,
        completed_steps=(_ref("step"),),
        changed_artifacts=(),
        verified_outcomes=(_ref("verification"),),
        pending_steps=(),
        next_safe_action=None,
        human_decisions=(),
        discovered_constraints=(),
        failure_recovery_refs=(),
        candidate_lessons=(),
        candidate_skills=(),
        checkpoint_ref=checkpoint,
        journal_head=_ref("journal"),
        source_digest=completed_source,
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
        context_digest=digest("context"),
        status=CloseStatus.CLOSED,
        closed_at=NOW,
    )
    pre_close_event_digest = digest("pre-close")
    pre_close_plan_digest = digest("pre-close-plan")
    snapshot = ProjectionClosureSnapshot(
        work_item=work,
        release=release,
        task_plan_id=uuid4(),
        task_plan_revision=6,
        task_plan_digest=digest("task-plan"),
        task_plan_source_revision=release.source_head,
        task_plan_policy_digest=receipt.policy_digest,
        job_id=job_id,
        attempt_id=attempt_id,
        run_id=run_id,
        lease_id=uuid4(),
        lease_worker_label="projection-worker",
        fencing_token=3,
        lease_expires_at=NOW + dt.timedelta(minutes=5),
        envelope_digest=receipt.envelope_digest,
        checkpoint_digest=checkpoint.digest_value,
        lock_digest=digest("locks"),
        pre_close_event_id=uuid4(),
        pre_close_event_digest=pre_close_event_digest,
        pre_close_sequence=4,
        pre_close_previous_digest=digest("previous"),
        pre_close_outbox_id=uuid4(),
        pre_close_outbox_plan_digest=pre_close_plan_digest,
        pre_close_outbox_payload_digest=digest(
            {
                "event_digest": pre_close_event_digest,
                "plan_digest": pre_close_plan_digest,
            }
        ),
        other_open_job_count=0,
        other_receiptless_claim_count=0,
    )
    return receipt, snapshot


def test_prepare_binds_completed_projection_and_current_pre_close_outbox() -> None:
    receipt, snapshot = _fixture()
    plan = ProjectionAwareClosureService(Repository(snapshot), Authorizations()).prepare(
        receipt, idempotency_key="projection-close:one", now=NOW
    )

    assert plan.completed_work.state is WorkState.COMPLETED
    assert plan.completed_work.revision == snapshot.work_item.revision + 1
    assert plan.projection_receipt.source_digest == receipt.source_digest
    assert plan.pre_close_event_id == snapshot.pre_close_event_id
    assert plan.pre_close_outbox_id == snapshot.pre_close_outbox_id
    assert plan.body()["requires_authorization"] is True
    plan.assert_integrity()


def test_prepare_rejects_unverified_acceptance_and_stale_fence() -> None:
    receipt, snapshot = _fixture(verified=False)
    with pytest.raises(PolicyViolation, match="acceptance criteria"):
        ProjectionAwareClosureService(Repository(snapshot), Authorizations()).prepare(
            receipt, idempotency_key="projection-close:unverified", now=NOW
        )

    current_receipt, current = _fixture()
    expired = replace(current, lease_expires_at=NOW)
    with pytest.raises(PolicyViolation, match="lease"):
        ProjectionAwareClosureService(Repository(expired), Authorizations()).prepare(
            current_receipt, idempotency_key="projection-close:expired", now=NOW
        )


def test_apply_rechecks_snapshot_and_consumes_exact_authorization() -> None:
    receipt, snapshot = _fixture()
    repository = Repository(snapshot)
    authorizations = Authorizations()
    service = ProjectionAwareClosureService(repository, authorizations)
    plan = service.prepare(receipt, idempotency_key="projection-close:apply", now=NOW)
    authorization = Authorization.issue(
        realm_id=receipt.realm_id,
        actor_id=uuid4(),
        work_item_id=receipt.work_item_id,
        plan_id=snapshot.task_plan_id,
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(plan.resource,),
            allowed_effects=("database-write",),
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )
    authorizations.current = authorization

    result = service.apply(
        plan,
        authorization_id=authorization.id,
        claim_id=uuid4(),
        now=NOW,
    )

    assert repository.applied
    assert result.work_revision == snapshot.work_item.revision + 1


def test_apply_race_returns_only_exact_terminal_replay_without_second_effect() -> None:
    receipt, snapshot = _fixture()
    repository = Repository(snapshot)
    repository.race_after_effect = True
    authorizations = Authorizations()
    service = ProjectionAwareClosureService(repository, authorizations)
    plan = service.prepare(receipt, idempotency_key="projection-close:race", now=NOW)
    authorization = Authorization.issue(
        realm_id=receipt.realm_id,
        actor_id=uuid4(),
        work_item_id=receipt.work_item_id,
        plan_id=snapshot.task_plan_id,
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

    result = service.apply(
        plan,
        authorization_id=authorization.id,
        claim_id=uuid4(),
        now=NOW,
    )

    assert result.replayed
    assert repository.apply_calls == 1


def test_transaction_bound_apply_preserves_original_failure_for_owner_rollback() -> None:
    receipt, snapshot = _fixture()
    repository = Repository(snapshot)
    repository.race_after_effect = True
    authorizations = Authorizations()
    service = ProjectionAwareClosureService(repository, authorizations)
    plan = service.prepare(receipt, idempotency_key="projection-close:bound", now=NOW)
    authorization = Authorization.issue(
        realm_id=receipt.realm_id,
        actor_id=uuid4(),
        work_item_id=receipt.work_item_id,
        plan_id=snapshot.task_plan_id,
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

    with pytest.raises(ConcurrencyConflict, match="concurrent terminal commit"):
        service.apply(
            plan,
            authorization_id=authorization.id,
            claim_id=uuid4(),
            now=NOW,
            transaction_bound=True,
        )

    assert repository.apply_calls == 1


def test_apply_preexisting_terminal_replay_bypasses_snapshot_and_effect() -> None:
    receipt, snapshot = _fixture()
    repository = Repository(snapshot)
    authorizations = Authorizations()
    service = ProjectionAwareClosureService(repository, authorizations)
    plan = service.prepare(receipt, idempotency_key="projection-close:committed", now=NOW)
    authorization = Authorization.issue(
        realm_id=receipt.realm_id,
        actor_id=uuid4(),
        work_item_id=receipt.work_item_id,
        plan_id=snapshot.task_plan_id,
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
    repository.terminal_effect = True
    repository.replay_receipt = ProjectionClosureApplyReceipt(
        work_item_id=plan.completed_work.id,
        work_revision=plan.completed_work.revision,
        close_receipt_id=plan.receipt.receipt_id,
        close_receipt_digest=plan.receipt.receipt_digest,
        projection_receipt_id=plan.projection_receipt.receipt_id,
        projection_receipt_digest=plan.projection_receipt.receipt_digest,
        effect_receipt_id=uuid4(),
        result_digest=plan.result_digest,
        plan_digest=plan.plan_digest,
        replayed=True,
        applied_at=NOW,
    )

    result = service.apply(
        plan,
        authorization_id=authorization.id,
        claim_id=uuid4(),
        now=NOW,
    )

    assert result.replayed
    assert repository.apply_calls == 0


def test_apply_rejects_source_and_projection_drift_after_plan_snapshot() -> None:
    receipt, snapshot = _fixture()
    repository = Repository(snapshot)
    authorizations = Authorizations()
    service = ProjectionAwareClosureService(repository, authorizations)
    plan = service.prepare(receipt, idempotency_key="projection-close:drift", now=NOW)
    changed_source = canonical_projection_source_digest(
        source_head="def456",
        source_tree_digest=snapshot.release.source_tree_digest,
        migration_head=snapshot.release.migration_head,
        database_revision_digest=snapshot.release.database_revision_digest,
    )
    repository.snapshot = replace(
        snapshot,
        release=replace(
            snapshot.release,
            source_head="def456",
            projection_receipt_digest=digest("replacement-projection-receipt"),
            projection_digest=digest("replacement-projection"),
            projection_source_digest=changed_source,
        ),
    )
    authorization = Authorization.issue(
        realm_id=receipt.realm_id,
        actor_id=uuid4(),
        work_item_id=receipt.work_item_id,
        plan_id=snapshot.task_plan_id,
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

    with pytest.raises(PolicyViolation, match="snapshot stale"):
        service.apply(
            plan,
            authorization_id=authorization.id,
            claim_id=uuid4(),
            now=NOW,
        )

    assert not repository.applied


@pytest.mark.parametrize("field", ("task_plan_id", "pre_close_event_id"))
def test_apply_rejects_self_consistent_crafted_plan_identity(field: str) -> None:
    receipt, snapshot = _fixture()
    repository = Repository(snapshot)
    service = ProjectionAwareClosureService(repository, Authorizations())
    plan = service.prepare(receipt, idempotency_key="projection-close:crafted", now=NOW)
    forged = (
        replace(plan, task_plan_id=uuid4(), plan_digest="")
        if field == "task_plan_id"
        else replace(plan, pre_close_event_id=uuid4(), plan_digest="")
    )
    forged = replace(forged, plan_digest=digest(forged.body()))

    with pytest.raises(PolicyViolation, match="lifecycle identity drift"):
        service.apply(
            forged,
            authorization_id=uuid4(),
            claim_id=uuid4(),
            now=NOW,
        )

    assert not repository.applied
