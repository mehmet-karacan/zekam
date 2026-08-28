"""PostgreSQL acceptance for exact non-continuity maintenance completion."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import Error as PsycopgError

from zekam.application.control_plane_completion import (
    CONTROL_PLANE_COMPLETION_ADAPTER_DIGEST,
    CONTROL_PLANE_COMPLETION_CONSUMER,
    CONTROL_PLANE_COMPLETION_OPERATION,
    ControlPlaneCompletionRequest,
    ControlPlaneCompletionService,
    control_plane_completion_resource,
)
from zekam.application.execution import ExecutionHost
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import Checkpoint
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, EffectClaim, EffectReceipt, Job, JobKind
from zekam.domain.security import Authorization, AuthorizationScope, DataClassification
from zekam.domain.work import (
    AcceptanceCriterion,
    EffectKind,
    EvidenceRef,
    PlanStep,
    WorkState,
    WorkType,
)
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.control_plane_completion_repository import (
    PostgresControlPlaneCompletionRepository,
)
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository
from zekam.infrastructure.postgres.runtime_repository import EffectLedger
from zekam.infrastructure.postgres.work_repository import WorkItemRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


class _ForgeryProbeRepository(PostgresControlPlaneCompletionRepository):
    """Exercise SECURITY DEFINER negative paths before the valid admission."""

    def _admit(
        self,
        request: ControlPlaneCompletionRequest,
        *,
        work_revision: int,
        work_record_digest: str,
        plan_digest: str,
        authorization_id: UUID,
        claim_id: UUID,
        effect_receipt_id: UUID,
    ) -> UUID:
        with pytest.raises(RuntimeError, match="rollback forged completion probe"):
            with self.connection.transaction():
                with self.connection.cursor() as cursor:
                    cursor.execute(
                        "select claim.effect_digest,claim.authorization_digest,"
                        " claim.resources,claim.execution_identity,claim.fencing_token,"
                        " claim.adapter_digest,claim.claimed_at,effect.result_digest,"
                        " effect.adapter_evidence_digest,effect.completed_at"
                        " from runtime.effect_claim claim"
                        " join runtime.effect_receipt effect"
                        " on effect.realm_id=claim.realm_id and effect.claim_id=claim.id"
                        " where claim.realm_id=%s and claim.id=%s and effect.id=%s",
                        (self.realm_id, claim_id, effect_receipt_id),
                    )
                    row = cursor.fetchone()
                assert row is not None
                forged_claim = EffectClaim.create(
                    realm_id=self.realm_id,
                    job_id=request.job_id,
                    attempt_id=request.attempt_id,
                    operation=CONTROL_PLANE_COMPLETION_OPERATION,
                    effect_digest=str(row[0]),
                    authorization_digest=str(row[1]),
                    idempotency_key=digest("forged-internal-completion-key"),
                    resources=parse_requests(
                        write=tuple(item["resource"] for item in row[2])
                    ),
                    execution_identity=str(row[3]),
                    fencing_token=int(row[4]),
                    adapter_digest=str(row[5]),
                    now=row[6],
                )
                ledger = EffectLedger(self.connection, self.realm_id)
                ledger.claim(forged_claim, authorization_id=authorization_id)
                forged_receipt = EffectReceipt.completed(
                    realm_id=self.realm_id,
                    claim=forged_claim,
                    result_digest=str(row[7]),
                    adapter_evidence_digest=str(row[8]),
                    now=row[9],
                )
                ledger.receipt(forged_receipt)
                parameters = self._admission_parameters(
                    request,
                    work_revision=work_revision,
                    work_record_digest=work_record_digest,
                    plan_digest=plan_digest,
                    authorization_id=authorization_id,
                    claim_id=forged_claim.id,
                    effect_receipt_id=forged_receipt.id,
                )
                with pytest.raises(
                    PsycopgError,
                    match="control-plane completion exact terminal chain missing",
                ) as rejected:
                    with self.connection.transaction():
                        self._execute_admission(parameters)
                assert rejected.value.sqlstate == "23514"
                raise RuntimeError("rollback forged completion probe")

        forged_source = replace(
            request,
            source_claim_digest=digest({"forged_source_claim": str(request.source_claim_id)}),
        )
        forged_evidence = replace(
            request,
            evidence=(
                EvidenceRef(
                    kind="runtime-receipt",
                    reference=str(request.source_effect_receipt_id),
                    digest_value=digest("forged-source-evidence"),
                ),
            ),
        )
        missing_source = replace(
            request,
            source_claim_id=uuid4(),
            source_claim_digest=digest("missing-source-claim"),
        )
        for forged in (forged_source, missing_source, forged_evidence):
            parameters = self._admission_parameters(
                forged,
                work_revision=work_revision,
                work_record_digest=work_record_digest,
                plan_digest=plan_digest,
                authorization_id=authorization_id,
                claim_id=claim_id,
                effect_receipt_id=effect_receipt_id,
            )
            with pytest.raises(PsycopgError):
                with self.connection.transaction():
                    self._execute_admission(parameters)
        return super()._admit(
            request,
            work_revision=work_revision,
            work_record_digest=work_record_digest,
            plan_digest=plan_digest,
            authorization_id=authorization_id,
            claim_id=claim_id,
            effect_receipt_id=effect_receipt_id,
        )


def test_incomplete_multistep_checkpoint_cannot_admit_completion(
    realm_session: tuple[Any, Any], tmp_path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "control-plane-incomplete-checkpoint-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    actor = Actor.create(
        realm=realm,
        kind=ActorKind.HUMAN,
        slug="control-plane-incomplete-checkpoint-actor",
    )
    ActorRepository(connection, realm.id).add(actor)
    graph = WorkGraphService(connection, realm, actor_id=actor.id)
    work = graph.create_item(
        project_id=project.id,
        type=WorkType.MAINTENANCE,
        title="Reject incomplete multi-step completion checkpoint",
        acceptance_criteria=(AcceptanceCriterion("Terminal chain verified"),),
    )
    work = graph.transition(work.id, WorkState.READY)
    work = graph.transition(work.id, WorkState.ACTIVE)
    prerequisite_step = "verify-prerequisite"
    effect_step = "maintenance-effect"
    primary_resource = f"project:{project.id}:maintenance"
    plan = graph.create_plan(
        work.id,
        source_revision="test/control-plane-incomplete-checkpoint",
        policy_digest=digest("control-plane-incomplete-checkpoint-policy"),
        steps=(
            PlanStep(prerequisite_step, "Verify prerequisite", EffectKind.NONE),
            PlanStep(
                effect_step,
                "Run exact maintenance effect",
                EffectKind.DATABASE_WRITE,
                logical_resources=(primary_resource,),
                depends_on=(prerequisite_step,),
                risk="high",
            ),
        ),
    )
    capability = "test.control-plane-incomplete-checkpoint"
    host = ExecutionHost(connection, realm.id, worker_label="control-plane-worker")
    job, created = host.jobs.enqueue(
        Job.create(
            realm_id=realm.id,
            project_id=project.id,
            kind=JobKind.MUTATION,
            idempotency_key=f"control-plane-incomplete:{work.id}",
            resources=parse_requests(write=(primary_resource,)),
            required_capabilities=(capability,),
            max_attempts=1,
            work_item_id=work.id,
            plan_id=plan.id,
            step_id=effect_step,
        )
    )
    assert created
    claimed = host.acquire_work(capabilities=(capability,))
    assert claimed is not None and claimed.job.id == job.id
    result_digest = digest({"maintenance": "completed", "work_item_id": str(work.id)})
    source_authorization = Authorization.issue(
        realm_id=realm.id,
        actor_id=actor.id,
        work_item_id=work.id,
        plan_id=plan.id,
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(primary_resource,),
            allowed_effects=(EffectKind.DATABASE_WRITE.value,),
            data_classifications=(DataClassification.LOCAL_ONLY,),
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=15),
    )
    authorizations = AuthorizationRepository(connection, realm.id)
    authorizations.issue(source_authorization)
    source_claim = host.claim_effect(
        claimed,
        operation=effect_step,
        effect_digest=plan.effect_digest,
        authorization_digest=source_authorization.authorization_digest,
        authorization_id=source_authorization.id,
        idempotency_key=f"source-incomplete:{work.id}",
        resources=parse_requests(write=(primary_resource,)),
        adapter_digest=digest({"adapter": "test-maintenance", "revision": 1}),
    )
    consumed = authorizations.consume(
        source_authorization.id,
        effect_digest=plan.effect_digest,
        consumed_by="test:maintenance-effect",
    )
    assert consumed.consumed
    source_receipt = host.record_success(
        source_claim,
        result_digest=result_digest,
        adapter_evidence_digest=digest({"verified_result": result_digest}),
    )
    prerequisite_digest = digest({"prerequisite": "verified"})
    incomplete = Checkpoint(
        checkpoint_id=f"control-plane-incomplete-{job.id}",
        project_id=str(project.id),
        work_item_id=str(work.id),
        plan_revision_id=str(plan.id),
        source_revision=plan.source_revision,
        plan_steps=plan.execution_order,
        completed_steps=(prerequisite_step,),
        pending_steps=(effect_step,),
        step_results=((prerequisite_step, prerequisite_digest),),
        context_manifest_digest=plan.plan_digest,
        journal_head_digest=digest({"terminal_result": result_digest}),
        next_safe_action=effect_step,
        created_at=dt.datetime.now(dt.UTC),
    )
    checkpoint_id = ContextContinuityRepository(
        connection, realm.id, project.id, work.id
    ).store_checkpoint(incomplete, task_plan_id=plan.id, job_id=job.id)
    assert host.finish(
        claimed,
        outcome=AttemptOutcome.SUCCEEDED,
        result_digest=result_digest,
    )
    work = graph.update_details(
        work.id,
        acceptance_criteria=(AcceptanceCriterion("Terminal chain verified", True),),
        reason="terminal maintenance result verified",
    )
    graph.transition(work.id, WorkState.VERIFICATION)
    request = ControlPlaneCompletionRequest(
        project_id=project.id,
        work_item_id=work.id,
        task_plan_id=plan.id,
        job_id=job.id,
        attempt_id=claimed.attempt_id,
        checkpoint_id=checkpoint_id,
        source_authorization_id=source_authorization.id,
        source_authorization_digest=source_authorization.authorization_digest,
        source_claim_id=source_claim.id,
        source_claim_digest=source_claim.claim_digest,
        source_effect_receipt_id=source_receipt.id,
        source_operation=effect_step,
        source_consumed_by="test:maintenance-effect",
        source_effect_digest=plan.effect_digest,
        source_adapter_digest=source_claim.adapter_digest,
        source_adapter_evidence_digest=source_receipt.adapter_evidence_digest
        or result_digest,
        source_resources=(primary_resource,),
        source_effects=(EffectKind.DATABASE_WRITE.value,),
        source_data_classifications=("local-only",),
        evidence=(
            EvidenceRef(
                kind="runtime-receipt",
                reference=str(source_receipt.id),
                digest_value=result_digest,
            ),
        ),
    )
    with pytest.raises(
        PsycopgError,
        match="control-plane completion exact terminal chain missing",
    ) as rejected:
        ControlPlaneCompletionService(
            PostgresControlPlaneCompletionRepository(connection, realm.id)
        ).complete(request)
    assert rejected.value.sqlstate == "23514"


def test_exact_terminal_maintenance_chain_is_admitted_and_completed(
    realm_session: tuple[Any, Any], tmp_path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "control-plane-completion-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    actor = Actor.create(
        realm=realm,
        kind=ActorKind.HUMAN,
        slug="control-plane-completion-actor",
    )
    ActorRepository(connection, realm.id).add(actor)

    graph = WorkGraphService(connection, realm, actor_id=actor.id)
    work = graph.create_item(
        project_id=project.id,
        type=WorkType.MAINTENANCE,
        title="Exact control-plane completion",
        acceptance_criteria=(AcceptanceCriterion("Terminal chain verified"),),
    )
    work = graph.transition(work.id, WorkState.READY)
    work = graph.transition(work.id, WorkState.ACTIVE)
    prerequisite_step = "verify-prerequisite"
    step_id = "maintenance-effect"
    plan = graph.create_plan(
        work.id,
        source_revision="test/control-plane-completion",
        policy_digest=digest("control-plane-completion-policy"),
        steps=(
            PlanStep(
                step_id=prerequisite_step,
                title="Verify prerequisite",
                effect=EffectKind.NONE,
            ),
            PlanStep(
                step_id=step_id,
                title="Run exact maintenance effect",
                effect=EffectKind.DATABASE_WRITE,
                logical_resources=(f"project:{project.id}:maintenance",),
                depends_on=(prerequisite_step,),
                risk="high",
            ),
        ),
    )
    capability = "test.control-plane-completion"
    primary_resource = f"project:{project.id}:maintenance"
    host = ExecutionHost(connection, realm.id, worker_label="control-plane-worker")
    job, created = host.jobs.enqueue(
        Job.create(
            realm_id=realm.id,
            project_id=project.id,
            kind=JobKind.MUTATION,
            idempotency_key=f"control-plane-completion:{work.id}",
            resources=parse_requests(write=(primary_resource,)),
            required_capabilities=(capability,),
            max_attempts=1,
            work_item_id=work.id,
            plan_id=plan.id,
            step_id=step_id,
        )
    )
    assert created
    claimed = host.acquire_work(capabilities=(capability,))
    assert claimed is not None
    assert claimed.job.id == job.id

    result_digest = digest({"maintenance": "completed", "work_item_id": str(work.id)})
    source_authorization = Authorization.issue(
        realm_id=realm.id,
        actor_id=actor.id,
        work_item_id=work.id,
        plan_id=plan.id,
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(primary_resource,),
            allowed_effects=(EffectKind.DATABASE_WRITE.value,),
            data_classifications=(DataClassification.LOCAL_ONLY,),
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=15),
    )
    authorizations = AuthorizationRepository(connection, realm.id)
    authorizations.issue(source_authorization)
    source_claim = host.claim_effect(
        claimed,
        operation=step_id,
        effect_digest=plan.effect_digest,
        authorization_digest=source_authorization.authorization_digest,
        authorization_id=source_authorization.id,
        idempotency_key=f"source:{work.id}",
        resources=parse_requests(write=(primary_resource,)),
        adapter_digest=digest({"adapter": "test-maintenance", "revision": 1}),
    )
    consumed = authorizations.consume(
        source_authorization.id,
        effect_digest=plan.effect_digest,
        consumed_by="test:maintenance-effect",
    )
    assert consumed.consumed
    source_receipt = host.record_success(
        source_claim,
        result_digest=result_digest,
        adapter_evidence_digest=digest({"verified_result": result_digest}),
    )
    checkpoint = Checkpoint(
        checkpoint_id=f"control-plane-{job.id}",
        project_id=str(project.id),
        work_item_id=str(work.id),
        plan_revision_id=str(plan.id),
        source_revision=plan.source_revision,
        plan_steps=plan.execution_order,
        completed_steps=plan.execution_order,
        pending_steps=(),
        step_results=tuple(
            (plan_step_id, result_digest) for plan_step_id in plan.execution_order
        ),
        context_manifest_digest=plan.plan_digest,
        journal_head_digest=digest({"terminal_result": result_digest}),
        next_safe_action="control-plane-completion",
        created_at=dt.datetime.now(dt.UTC),
    )
    checkpoint_id = ContextContinuityRepository(
        connection,
        realm.id,
        project.id,
        work.id,
    ).store_checkpoint(checkpoint, task_plan_id=plan.id, job_id=job.id)
    assert host.finish(
        claimed,
        outcome=AttemptOutcome.SUCCEEDED,
        result_digest=result_digest,
    )
    work = graph.update_details(
        work.id,
        acceptance_criteria=(AcceptanceCriterion("Terminal chain verified", True),),
        reason="terminal maintenance result verified",
    )
    work = graph.transition(work.id, WorkState.VERIFICATION)

    completion_request = ControlPlaneCompletionRequest(
        project_id=project.id,
        work_item_id=work.id,
        task_plan_id=plan.id,
        job_id=job.id,
        attempt_id=claimed.attempt_id,
        checkpoint_id=checkpoint_id,
        source_authorization_id=source_authorization.id,
        source_authorization_digest=source_authorization.authorization_digest,
        source_claim_id=source_claim.id,
        source_claim_digest=source_claim.claim_digest,
        source_effect_receipt_id=source_receipt.id,
        source_operation=step_id,
        source_consumed_by="test:maintenance-effect",
        source_effect_digest=plan.effect_digest,
        source_adapter_digest=source_claim.adapter_digest,
        source_adapter_evidence_digest=source_receipt.adapter_evidence_digest
        or result_digest,
        source_resources=(primary_resource,),
        source_effects=(EffectKind.DATABASE_WRITE.value,),
        source_data_classifications=("local-only",),
        evidence=(
            EvidenceRef(
                kind="runtime-receipt",
                reference=str(source_receipt.id),
                digest_value=result_digest,
            ),
        ),
    )
    service = ControlPlaneCompletionService(
        _ForgeryProbeRepository(connection, realm.id)
    )
    result = service.complete(completion_request)
    later_plan = graph.create_plan(
        work.id,
        source_revision="test/post-completion-plan",
        policy_digest=digest("post-completion-policy"),
        steps=(PlanStep("later", "Later plan", EffectKind.NONE),),
    )
    assert later_plan.id != plan.id
    replay = service.complete(completion_request)

    completed = WorkItemRepository(connection, realm.id).get(work.id)
    assert completed.state is WorkState.COMPLETED
    assert completed.revision == result.work_revision
    assert completed.record_digest == result.work_record_digest
    assert result.checkpoint_id == checkpoint_id
    assert result.result_digest == result_digest
    assert result.request_digest == completion_request.request_digest
    assert result.evidence_digest == completion_request.evidence_digest
    assert result.grants_authority is False
    assert replay == result

    with connection.cursor() as cursor:
        cursor.execute(
            "select admission.mode,admission.consumed_at is not null,"
            " claim.operation,claim.adapter_digest,claim.resources,"
            " authorization.consumed_by,effect.status,effect.result_digest,"
            " effect.adapter_evidence_digest,checkpoint.checkpoint_digest,"
            " authorization.actor_id,claim.idempotency_key,"
            " admission.source_authorization_id,admission.source_claim_id,"
            " admission.source_effect_receipt_id,admission.request_digest,"
            " admission.evidence_digest,admission.completion_evidence"
            " from work.completion_admission admission"
            " join runtime.effect_claim claim on claim.realm_id=admission.realm_id"
            "  and claim.id=admission.claim_id"
            " join security.authorization authorization"
            "  on authorization.realm_id=admission.realm_id"
            "  and authorization.id=admission.authorization_id"
            " join runtime.effect_receipt effect on effect.realm_id=admission.realm_id"
            "  and effect.id=admission.effect_receipt_id"
            " join work.checkpoint checkpoint on checkpoint.realm_id=admission.realm_id"
            "  and checkpoint.id=admission.checkpoint_id"
            " where admission.realm_id=%s and admission.id=%s",
            (realm.id, result.admission_id),
        )
        row = cursor.fetchone()
    assert row is not None
    assert row[0] == "control-plane"
    assert row[1] is True
    assert row[2] == CONTROL_PLANE_COMPLETION_OPERATION
    assert row[3] == CONTROL_PLANE_COMPLETION_ADAPTER_DIGEST
    assert row[4] == [
        {
            "resource": control_plane_completion_resource(project.id, work.id),
            "mode": "write",
        }
    ]
    assert row[5] == CONTROL_PLANE_COMPLETION_CONSUMER
    assert row[6] == "completed"
    assert row[7] == result_digest
    assert row[8] == digest(
        {
            "schema": "zekam-control-plane-completion-adapter-evidence/v2",
            "work_item_id": str(work.id),
            "completed_work_record_digest": completed.record_digest,
            "checkpoint_digest": row[9],
            "plan_digest": plan.plan_digest,
            "operation": CONTROL_PLANE_COMPLETION_OPERATION,
            "source_authorization_id": str(source_authorization.id),
            "source_claim_id": str(source_claim.id),
            "source_effect_receipt_id": str(source_receipt.id),
            "request_digest": completion_request.request_digest,
            "evidence_digest": completion_request.evidence_digest,
        }
    )
    assert row[10] == actor.id
    assert row[11] == completion_request.request_digest
    assert row[12:15] == (
        source_authorization.id,
        source_claim.id,
        source_receipt.id,
    )
    assert row[15] == completion_request.request_digest
    assert row[16] == completion_request.evidence_digest
    assert row[17] == [item.as_dict() for item in completion_request.evidence]
