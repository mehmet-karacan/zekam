"""PostgreSQL acceptance for the typed Memory Continuity ledger."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import Error as PsycopgError
from tests.integration.test_agent_residency_postgres import residency_scope as _residency_scope

from zekam.application.memory_continuity import HydrationPreparation, MemoryContinuityService
from zekam.application.memory_control import MemoryControlOperation, MemoryControlService
from zekam.application.memory_upgrade import canonical_projection_source_digest
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.execution_run import ExecutionRun
from zekam.domain.memory_compiler import (
    CompilerCandidate,
    CompilerCandidateType,
    MemoryCompilerOutput,
)
from zekam.domain.memory_contract import (
    MEMORY_INVARIANT_IDS,
    InvariantStatus,
    MemoryContractEvaluation,
    MemoryInvariantResult,
)
from zekam.domain.policy import RiskLevel
from zekam.domain.project import SourceRevisionKind
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.session_continuity import (
    CloseStatus,
    CompactionReceipt,
    CompactionStatus,
    ContextSelectionReference,
    DataClassification,
    DigestReference,
    FreshnessDimension,
    ProjectionGenerationReceipt,
    SessionCloseReceipt,
    SessionHydrationReceipt,
    SessionLifecycleEvent,
    TruthClass,
)
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
from zekam.infrastructure.postgres.memory_continuity_repository import (
    GapRecoveryRecord,
    MemoryContinuityRepository,
)
from zekam.infrastructure.postgres.memory_control_repository import (
    PostgresMemoryControlRepository,
)
from zekam.infrastructure.postgres.project_repository import SourceBindingRepository
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime.now(dt.UTC)


def _ref(name: str, truth: TruthClass = TruthClass.REPO_FACT) -> DigestReference:
    return DigestReference(f"evidence/{name}", digest(name), truth)


def _setup_runtime(realm: Any, connection: Any, tmp_path: Path) -> tuple[Any, Any, Any, UUID, UUID]:
    source = tmp_path / "memory-continuity-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(
        project_id=project.id, type=WorkType.TASK, title="Memory continuity acceptance"
    )
    policy_digest = digest("memory-continuity-policy")
    plan = graph.create_plan(
        work.id,
        source_revision="git/revision-1",
        policy_digest=policy_digest,
        steps=(PlanStep("continuity", "Continuity", EffectKind.DATABASE_WRITE),),
    )
    run = ExecutionRun.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        client_id="codex",
        session_id="session/memory-continuity",
        source_revision="git/revision-1",
        policy_digest=policy_digest,
        max_input_tokens=100,
        max_output_tokens=100,
        max_cost_micros=1000,
        deadline=NOW + dt.timedelta(hours=1),
        created_at=NOW - dt.timedelta(seconds=1),
    )
    execution = ExecutionRunRepository(connection, realm.id)
    execution.create_run(run)
    execution.activate_run(run.id, started_at=NOW)
    job_id, attempt_id = uuid4(), uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,priority,"
            " attempt_count,max_attempts,fencing_token,idempotency_key,run_id)"
            " values (%s,%s,%s,%s,%s,'continuity','mutation','running',100,1,1,1,%s,%s)",
            (job_id, realm.id, project.id, work.id, plan.id, f"job-{job_id}", run.id),
        )
        cursor.execute(
            "insert into runtime.job_attempt"
            " (id,realm_id,job_id,attempt_number,fencing_token,worker_label,started_at)"
            " values (%s,%s,%s,1,1,'continuity-worker',%s)",
            (attempt_id, realm.id, job_id, NOW + dt.timedelta(seconds=2)),
        )
    return project, work, run, job_id, attempt_id


def _hydration(realm: Any, project: Any, work: Any, run: Any) -> SessionHydrationReceipt:
    current = digest("current")
    return SessionHydrationReceipt(
        receipt_id=uuid4(),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
        plan_ref="plan/revision-1",
        checkpoint_ref="checkpoint/current",
        source_digest=digest("source"),
        policy_digest=run.policy_digest,
        migration_digest=digest("migration-0055"),
        inventory_digest=digest("inventory"),
        context_digest=digest("context"),
        required_selections=(
            ContextSelectionReference("work/current", digest("work"), 5, TruthClass.REPO_FACT),
        ),
        optional_selections=(),
        omissions=(),
        token_budget=10,
        tokens_used=5,
        freshness=(FreshnessDimension("source", current, current, True),),
        projection_refs=(),
        hydration_event_digest=digest("hydration-event"),
        created_at=NOW,
        fresh=True,
        complete=True,
    )


def _canonical_projection(
    realm: Any,
    connection: Any,
    repository: MemoryContinuityRepository,
    project: Any,
    work: Any,
) -> ProjectionGenerationReceipt:
    with connection.cursor() as cursor:
        cursor.execute(
            "select item.revision,item.state,item.record_digest,revision.revision,"
            " revision.tree_digest"
            " from work.work_item item"
            " join projects.source_binding binding"
            " on binding.realm_id=item.realm_id and binding.project_id=item.project_id"
            " join lateral (select source.revision,source.tree_digest"
            " from projects.source_revision source"
            " where source.realm_id=binding.realm_id and source.binding_id=binding.id"
            " order by source.observed_at desc,source.id desc limit 1) revision on true"
            " where item.realm_id=%s and item.project_id=%s and item.id=%s",
            (realm.id, project.id, work.id),
        )
        source = cursor.fetchone()
        assert source is not None
        cursor.execute("select max(version) from core.schema_migrations")
        migration_head = int(cursor.fetchone()[0])
    database_revision_digest = digest(
        {
            "project_id": str(project.id),
            "work_item_id": str(work.id),
            "work_revision": int(source[0]),
            "work_state": str(source[1]),
            "work_record_digest": str(source[2]),
        }
    )
    projection_source_digest = canonical_projection_source_digest(
        source_head=str(source[3]),
        source_tree_digest=str(source[4]),
        migration_head=migration_head,
        database_revision_digest=database_revision_digest,
    )
    projection_body = {
        "schema": "zekam-memory-continuity-public-projection/v1",
        "project_id": str(project.id),
        "work_item_id": str(work.id),
        "work_revision": int(source[0]),
        "work_state": str(source[1]),
        "source_head": str(source[3]),
        "source_tree_digest": str(source[4]),
        "migration_head": migration_head,
        "database_revision_digest": database_revision_digest,
        "source_digest": projection_source_digest,
        "classification": "public",
        "public_filtered": True,
        "content_included": False,
        "fresh": True,
        "read_only": True,
        "grants_authority": False,
    }
    projection = ProjectionGenerationReceipt(
        receipt_id=uuid4(),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        source_ref=f"work-item/{work.id}/revision/{int(source[0])}",
        source_digest=projection_source_digest,
        projection_ref="projection/active-work",
        projection_digest=digest(projection_body),
        generator_version="projection/v1",
        generated_at=NOW,
    )
    assert repository.store_projection_receipt(
        projection, idempotency_key=f"projection-canonical-{work.id}"
    )
    return projection


def _evaluation(realm: Any, project: Any, work: Any, run: Any) -> MemoryContractEvaluation:
    return MemoryContractEvaluation(
        evaluation_id=uuid4(),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        results=tuple(
            MemoryInvariantResult(
                invariant_id,
                InvariantStatus.PASSED,
                f"gate/{invariant_id}",
                (_ref(invariant_id),),
            )
            for invariant_id in MEMORY_INVARIANT_IDS
        ),
        source_revision=run.source_revision,
        policy_version="policy/v1",
        evaluator_version="evaluator/v1",
        evaluated_at=NOW,
    )


def test_continuity_receipts_compiler_watermark_and_snapshot(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    scope = _residency_scope.__wrapped__(realm_session, tmp_path)  # type: ignore[attr-defined]
    run = scope["run"]
    project = SimpleNamespace(id=run.project_id)
    work = SimpleNamespace(id=run.work_item_id)
    bindings = SourceBindingRepository(connection, realm.id)
    binding = bindings.for_project(project.id)[0]
    bindings.record_revision(
        binding_id=binding.id,
        kind=SourceRevisionKind.TREE_DIGEST,
        revision=run.source_revision,
        tree_digest=digest("residency-source-tree"),
        now=NOW,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "select job_id,attempt_id from runtime.execution_envelope"
            " where realm_id=%s and run_id=%s order by request_ordinal desc limit 1",
            (realm.id, run.id),
        )
        job_id, attempt_id = cursor.fetchone()
    repository = MemoryContinuityRepository(connection, realm.id)
    with connection.cursor() as cursor:
        cursor.execute(
            "select revision,state,policy_body->>'grants_authority',grants_authority"
            " from continuity.feature_policy_state where realm_id=%s"
            " and component='memory-continuity-plane' and is_current",
            (realm.id,),
        )
        assert cursor.fetchone() == (1, "shadow", "false", False)
    event = SessionLifecycleEvent(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
        event_id=uuid4(),
        event_type="hydration_required",
        sequence=1,
        previous_digest=None,
        origin="client/codex",
        causation_id="cause/one",
        correlation_id="correlation/one",
        recursion_depth=0,
        source_revision=run.source_revision,
        plan_ref=f"work-plan:{run.plan_id}",
        checkpoint_ref=f"run:{run.id}:genesis",
        context_ref="context/current",
        payload_digest=digest("event-payload"),
        metadata=(),
        classification=DataClassification.INTERNAL,
        occurred_at=NOW,
        ingested_at=NOW,
    )
    stage = repository.stage_lifecycle_delivery(
        event, idempotency_key="lifecycle-one", plan_digest=digest("plan")
    )
    replay = repository.stage_lifecycle_delivery(
        event, idempotency_key="lifecycle-one", plan_digest=digest("plan")
    )
    assert stage.created and replay == type(stage)(stage.event_id, stage.outbox_id, False)
    repository.finalize_lifecycle_delivery(
        outbox_id=stage.outbox_id,
        receipt_digest=digest("delivery-receipt"),
        status="completed",
        completed_at=NOW,
    )

    projection = _canonical_projection(realm, connection, repository, project, work)
    inventory = repository.read_hydration_inventory(
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
    )
    continuity = MemoryContinuityService(repository, AuthorizationRepository(connection, realm.id))
    hydration_plan = continuity.prepare_from_inventory(
        HydrationPreparation(
            receipt_id=uuid4(),
            realm_id=realm.id,
            project_id=project.id,
            work_item_id=work.id,
            run_id=run.id,
            session_id=run.session_id,
            client_id=run.client_id,
            token_budget=10,
            idempotency_key="hydrate-one",
            created_at=NOW,
        ),
        inventory,
    )
    hydration_actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="hydration-authorizer", now=NOW)
    )
    hydration_authorizations = AuthorizationRepository(connection, realm.id)
    hydration_authorization = hydration_authorizations.issue(
        Authorization.issue(
            realm_id=realm.id,
            actor_id=hydration_actor.id,
            work_item_id=work.id,
            plan_id=run.plan_id,
            plan_digest=hydration_plan.plan_digest,
            effect_digest=hydration_plan.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(hydration_plan.resource,),
                allowed_effects=("database-write",),
            ),
            risk="high",
            lifetime=dt.timedelta(minutes=5),
            now=NOW,
        )
    )
    applied_hydration = continuity.apply(
        hydration_plan, authorization_id=hydration_authorization.id, now=NOW
    )
    assert applied_hydration.created
    hydration = hydration_plan.receipt
    assert isinstance(hydration, SessionHydrationReceipt)
    assert not repository.store_hydration_receipt(hydration, idempotency_key="hydrate-one")
    close = SessionCloseReceipt(
        receipt_id=uuid4(),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
        job_id=job_id,
        attempt_id=attempt_id,
        envelope_digest=digest("envelope"),
        fencing_token=1,
        completed_steps=(_ref("step"),),
        changed_artifacts=(),
        verified_outcomes=(_ref("verified"),),
        pending_steps=(),
        next_safe_action=None,
        human_decisions=(),
        discovered_constraints=(),
        failure_recovery_refs=(),
        candidate_lessons=(),
        candidate_skills=(),
        checkpoint_ref=_ref("checkpoint"),
        journal_head=_ref("journal"),
        source_digest=digest("source"),
        policy_digest=run.policy_digest,
        migration_digest=digest("migration-0055"),
        context_digest=digest("context"),
        status=CloseStatus.CLOSED,
        closed_at=NOW,
    )
    assert repository.store_close_receipt(close, idempotency_key="close-one")
    compaction = CompactionReceipt(
        receipt_id=uuid4(),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
        pre_compaction_event_digest=digest("pre"),
        checkpoint_draft_digest=digest("draft"),
        outbox_ref="outbox/one",
        outbox_payload_digest=digest("outbox"),
        worker_result_digest=digest("worker"),
        checkpoint_ref="checkpoint/current",
        checkpoint_digest=digest("checkpoint"),
        post_compaction_event_digest=digest("post"),
        rehydration_receipt_digest=hydration.receipt_digest,
        status=CompactionStatus.COMPLETED,
        created_at=NOW,
        completed_at=NOW,
    )
    assert repository.store_compaction_receipt(compaction, idempotency_key="compact-one")
    evaluation = _evaluation(realm, project, work, run)
    assert repository.store_contract_evaluation(evaluation, idempotency_key="contract-one")
    assert not repository.store_projection_receipt(
        projection, idempotency_key=f"projection-canonical-{work.id}"
    )
    current_projection = repository.read_latest_projection(
        project_id=project.id,
        work_item_id=work.id,
        projection_ref=projection.projection_ref,
        expected_source_digest=projection.source_digest,
    )
    assert current_projection is not None
    assert current_projection.current and not current_projection.grants_authority
    stale_projection = repository.read_latest_projection(
        project_id=project.id,
        work_item_id=work.id,
        projection_ref=projection.projection_ref,
        expected_source_digest=digest("new-source"),
    )
    assert stale_projection is not None and not stale_projection.current

    source_ref = _ref("compiler-source")
    candidate = CompilerCandidate(
        candidate_id="candidate/one",
        logical_key="decision/one",
        content_ref="cas/candidate-one",
        content_digest=digest("candidate"),
        truth_class=TruthClass.USER_DECISION,
        classification=DataClassification.LOCAL_ONLY,
        candidate_type=CompilerCandidateType.DURABLE_DECISION,
        risk=RiskLevel.HIGH,
        source_refs=(source_ref,),
        evidence_refs=(_ref("candidate-evidence"),),
    )
    source_set_digest = digest([source_ref.as_dict()])
    claim = repository.claim_compiler_watermark(
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        idempotency_key="compiler-one",
        source_set_digest=source_set_digest,
        source_watermark="source/watermark-1",
        claimed_at=NOW,
    )
    output = MemoryCompilerOutput(
        output_id=uuid4(),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        source_set=(source_ref,),
        source_watermark="source/watermark-1",
        parser_digest=digest("parser"),
        compiler_digest=digest("compiler"),
        policy_digest=run.policy_digest,
        profile_digest=digest("profile"),
        candidates=(candidate,),
        rejected=(),
        duplicate_groups=(),
        conflict_groups=(),
        gateway_request_ref=None,
        gateway_request_digest=None,
        gateway_response_ref=None,
        gateway_response_digest=None,
        created_at=NOW,
    )
    assert repository.store_compiler_output(output, watermark_claim_id=claim.claim_id)
    assert not repository.store_compiler_output(output, watermark_claim_id=claim.claim_id)
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "update memory.compiler_candidate set state='promoted'"
            " where realm_id=%s and logical_candidate_id=%s",
            (realm.id, candidate.candidate_id),
        )
    connection.rollback()
    assert repository.record_candidate_review(
        candidate_id=candidate.candidate_id,
        compiler_identity="compiler-a",
        reviewer_identity="verifier-b",
        review_ref="review/candidate-one",
        review_digest=digest("candidate-review"),
        reviewed_at=NOW,
    )
    assert not repository.record_candidate_review(
        candidate_id=candidate.candidate_id,
        compiler_identity="compiler-a",
        reviewer_identity="verifier-b",
        review_ref="review/candidate-one",
        review_digest=digest("candidate-review"),
        reviewed_at=NOW,
    )
    with pytest.raises(PsycopgError):
        repository.promote_reviewed_candidate(
            candidate_id=candidate.candidate_id,
            promotion_ref="memory/promotion-one",
            promotion_digest=digest("candidate-promotion"),
            authorization_id=uuid4(),
            promoted_at=NOW,
        )
    connection.rollback()
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="continuity-reviewer", now=NOW)
    )
    authorizations = AuthorizationRepository(connection, realm.id)
    control = MemoryControlService(
        PostgresMemoryControlRepository(repository),
        authorizations,
    )
    promotion_plan = control.prepare(
        operation=MemoryControlOperation.CANDIDATE_PROMOTE,
        subject_id=candidate.candidate_id,
        evidence_ref="memory/promotion-one",
        evidence_digest=digest("candidate-promotion"),
        target_state="promoted",
    )
    authorization = Authorization.issue(
        realm_id=realm.id,
        actor_id=actor.id,
        work_item_id=work.id,
        plan_digest=promotion_plan.plan_digest,
        effect_digest=promotion_plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(promotion_plan.resource,), allowed_effects=("database-write",)
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )
    authorizations.issue(authorization)
    promotion_receipt = control.apply(promotion_plan, authorization_id=authorization.id, now=NOW)
    assert promotion_receipt.created
    with connection.cursor() as cursor:
        cursor.execute(
            "select consumed_by from security.authorization where realm_id=%s and id=%s",
            (realm.id, authorization.id),
        )
        assert cursor.fetchone()[0] == "memory-compiler-candidate-promotion/v1"
    assert not repository.promote_reviewed_candidate(
        candidate_id=candidate.candidate_id,
        promotion_ref="memory/promotion-one",
        promotion_digest=digest("candidate-promotion"),
        authorization_id=authorization.id,
        promoted_at=NOW,
    )

    snapshot = repository.read_session_snapshot(
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
    )
    assert snapshot.hydration_receipt_digest == hydration.receipt_digest
    assert snapshot.close_status == "closed"
    assert snapshot.compaction_status == "completed"
    assert snapshot.contract_passed and snapshot.ready_for_mutation

    gap = GapRecoveryRecord(
        id=uuid4(),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        gap_code="hydration.stale",
        gap_ref="gap/hydration-stale",
        evidence_digest=digest("gap"),
        recovery_ref="recovery/rehydrate",
        state="recovery-required",
        created_at=NOW,
    )
    assert repository.record_gap(gap)
    assert not repository.record_gap(gap)
    assert not repository.read_session_snapshot(
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
    ).ready_for_mutation
    assert repository.resolve_gap(
        gap_id=gap.id,
        recovery_receipt_ref="receipt/rehydration-repair",
        recovery_receipt_digest=digest("gap-recovery-receipt"),
        resolved_at=NOW,
    )
    assert not repository.resolve_gap(
        gap_id=gap.id,
        recovery_receipt_ref="receipt/rehydration-repair",
        recovery_receipt_digest=digest("gap-recovery-receipt"),
        resolved_at=NOW,
    )
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "update continuity.gap_recovery_reference set gap_ref='gap/forged'"
            " where realm_id=%s and id=%s",
            (realm.id, gap.id),
        )
    connection.rollback()
    assert repository.read_session_snapshot(
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
    ).ready_for_mutation

    pre_close = SessionLifecycleEvent(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
        event_id=uuid4(),
        event_type="pre_close",
        sequence=2,
        previous_digest=event.event_digest,
        origin="client/codex",
        causation_id="cause/pre-close",
        correlation_id="correlation/one",
        recursion_depth=0,
        source_revision=run.source_revision,
        plan_ref=f"work-plan:{run.plan_id}",
        checkpoint_ref=f"run:{run.id}:genesis",
        context_ref="context/current",
        payload_digest=digest("pre-close-payload"),
        metadata=(),
        classification=DataClassification.INTERNAL,
        occurred_at=NOW,
        ingested_at=NOW,
    )
    repository.stage_lifecycle_delivery(
        pre_close,
        idempotency_key="lifecycle-pre-close",
        plan_digest=digest("pre-close-plan"),
    )

    release_snapshot = repository.read_projection_release_snapshot(
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
    )
    assert release_snapshot.fresh
    assert release_snapshot.lifecycle_complete
    assert not release_snapshot.pending_lifecycle_steps
    assert release_snapshot.projection_receipt_digest == projection.receipt_digest
    assert release_snapshot.expected_projection_source_digest == projection.source_digest

    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "update continuity.session_lifecycle_event set event_type='forged'"
            " where realm_id=%s and id=%s",
            (realm.id, event.event_id),
        )
    connection.rollback()
