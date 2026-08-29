"""Authority, claim and receipt envelope for explicit doctor repair effects."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.application.composition import ApplicationContext
from zekam.application.control_plane_completion import (
    ControlPlaneCompletionRequest,
    ControlPlaneCompletionResult,
    ControlPlaneCompletionService,
)
from zekam.application.doctor_repair import (
    DoctorRepairPlan,
    apply_git_fast_forward,
)
from zekam.application.execution import ExecutionHost
from zekam.application.governance import DEFAULT_POLICY_NAME, EffectRequest, GovernanceService
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.realm_context import RealmContext
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import Checkpoint
from zekam.domain.errors import PolicyViolation
from zekam.domain.policy import GateOutcome
from zekam.domain.realm import ActorKind, LifecycleStatus
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, FailureCategory, Job, JobKind
from zekam.domain.security import DataClassification
from zekam.domain.work import (
    AcceptanceCriterion,
    EffectKind,
    EvidenceRef,
    PlanStep,
    WorkState,
    WorkType,
)
from zekam.infrastructure.postgres import migrations, routine_integrity
from zekam.infrastructure.postgres.connection import configure_session, reset_role
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.control_plane_completion_repository import (
    PostgresControlPlaneCompletionRepository,
)
from zekam.infrastructure.postgres.core_repository import ActorRepository


@dataclass(frozen=True, slots=True)
class DoctorRepairRuntimeResult:
    step: str
    result: dict[str, Any]
    work_id: UUID
    task_plan_id: UUID
    authorization_id: UUID
    job_id: UUID
    claim_id: UUID
    receipt_id: UUID
    checkpoint_id: UUID
    result_digest: str
    completion: ControlPlaneCompletionResult

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-doctor-repair-result/v1",
            "step": self.step,
            "result": self.result,
            "work_id": str(self.work_id),
            "task_plan_id": str(self.task_plan_id),
            "authorization_id": str(self.authorization_id),
            "job_id": str(self.job_id),
            "claim_id": str(self.claim_id),
            "receipt_id": str(self.receipt_id),
            "checkpoint_id": str(self.checkpoint_id),
            "result_digest": self.result_digest,
            "completion": self.completion.as_dict(),
            "grants_authority": False,
        }


def apply_doctor_repair_with_runtime(
    realm_context: RealmContext,
    context: ApplicationContext,
    *,
    repair_plan: DoctorRepairPlan,
    plan_digest: str,
    actor_id: UUID,
    project_id: UUID,
) -> DoctorRepairRuntimeResult:
    """Apply exactly one safe next repair step inside the canonical runtime envelope."""

    if repair_plan.plan_digest != plan_digest:
        raise PolicyViolation("Doctor repair plan digest exact degil")
    step = repair_plan.next_step
    if step is None:
        raise PolicyViolation("Doctor repair planinda uygulanacak adim yok")
    if repair_plan.blocked_reasons:
        raise PolicyViolation("Doctor repair bloke: " + ",".join(repair_plan.blocked_reasons))
    _assert_actor_and_project(realm_context, context, actor_id=actor_id, project_id=project_id)

    governance = GovernanceService(realm_context.connection, realm_context.realm, actor_id=actor_id)
    policy = governance.policies.current(DEFAULT_POLICY_NAME)
    if policy is None:
        raise PolicyViolation("Doctor repair current policy ister")
    resources, request = _effect_request(repair_plan, project_id=project_id, step=step)
    graph = WorkGraphService(realm_context.connection, realm_context.realm, actor_id=actor_id)
    work = graph.create_item(
        project_id=project_id,
        type=WorkType.MAINTENANCE,
        title=f"Doctor {step} repair",
        summary="Exact doctor repair planini authority ve terminal receipt ile uygula",
        acceptance_criteria=(
            AcceptanceCriterion("Plan digest effect oncesi yeniden dogrulanir"),
            AcceptanceCriterion("Effect claim-before-effect sirasiyla calisir"),
            AcceptanceCriterion("Terminal receipt ve effect sonrasi verification yazilir"),
        ),
    )
    graph.set_intent(
        work.id,
        goal=f"Doctor bulgusunu exact {step} ile guvenli sekilde gidermek",
        non_goals=("force/reset/rebase", "caller-supplied-sql", "silent-retry"),
        outcomes=("verified-terminal-receipt",),
        constraints=("explicit-uygula", "exact-plan-digest"),
    )
    work = graph.transition(work.id, WorkState.READY)
    work = graph.transition(work.id, WorkState.ACTIVE)
    source_revision = f"doctor-repair:{plan_digest}"
    plan_steps: tuple[PlanStep, ...]
    if step == "git-fast-forward":
        plan_steps = (
            PlanStep(
                step_id=step,
                title=f"Exact {step} network {plan_digest}",
                effect=EffectKind.NETWORK_CALL,
                logical_resources=resources,
                risk="high",
            ),
            PlanStep(
                step_id=f"{step}-process",
                title=f"Exact {step} process {plan_digest}",
                effect=EffectKind.PROCESS_RUN,
                logical_resources=resources,
                depends_on=(step,),
                risk="high",
            ),
            PlanStep(
                step_id=f"{step}-file",
                title=f"Exact {step} file {plan_digest}",
                effect=EffectKind.FILE_WRITE,
                logical_resources=resources,
                depends_on=(f"{step}-process",),
                risk="high",
            ),
        )
    else:
        plan_steps = (
            PlanStep(
                step_id=step,
                title=f"Exact {step} {plan_digest}",
                effect=EffectKind.DATABASE_WRITE,
                logical_resources=resources,
                risk="high",
            ),
        )
    with realm_context.connection.transaction():
        task_plan = graph.create_plan(
            work.id,
            source_revision=source_revision,
            policy_digest=policy.policy_digest,
            steps=plan_steps,
        )
        authorization = governance.issue_authorization(
            request=request,
            actor_id=actor_id,
            plan=task_plan,
            lifetime=dt.timedelta(minutes=15),
        )

    verdict = governance.evaluate(request, authorization=authorization)
    if not verdict.allowed:
        governance.revoke_authorization(authorization.id, f"doctor-{step}-policy-preflight-denied")
        graph.transition(
            work.id,
            WorkState.CANCELLED,
            reason=f"doctor {step} policy preflight denied",
        )
        denial = verdict.gates.first_denial
        reason = "denied" if denial is None else denial.reason
        raise PolicyViolation(f"Doctor repair policy reddi: {reason}")
    if any(
        decision.outcome is GateOutcome.DENY for decision in verdict.gates.decisions
    ):  # pragma: no cover - ``allowed`` bunu kapsar
        raise PolicyViolation("Doctor repair governance verdict tutarsiz")

    capability = f"doctor.repair.{plan_digest[-16:]}"
    host = ExecutionHost(
        realm_context.connection,
        realm_context.realm_id,
        worker_label=f"doctor-{step}",
    )
    job, created = host.jobs.enqueue(
        Job.create(
            realm_id=realm_context.realm_id,
            project_id=project_id,
            kind=JobKind.MUTATION,
            idempotency_key=f"doctor:{step}:{plan_digest}",
            resources=parse_requests(write=resources),
            required_capabilities=(capability,),
            max_attempts=1,
            work_item_id=work.id,
            plan_id=task_plan.id,
            step_id=step,
        )
    )
    if not created:
        governance.revoke_authorization(authorization.id, f"doctor-{step}-runtime-replay")
        graph.transition(work.id, WorkState.CANCELLED, reason="doctor runtime replay")
        raise PolicyViolation("Doctor repair runtime replay reddedildi")
    claimed = host.acquire_work(capabilities=(capability,))
    if claimed is None or claimed.job.id != job.id:
        governance.revoke_authorization(authorization.id, f"doctor-{step}-acquire-failed")
        host.jobs.mark_recovery_required(job.id, f"doctor-{step}-acquire-failed")
        graph.transition(work.id, WorkState.CANCELLED, reason="doctor job claim alinamadi")
        raise PolicyViolation("Doctor repair runtime job claim edilemedi")
    claim = host.claim_effect(
        claimed,
        operation=step,
        effect_digest=request.effect_digest,
        authorization_digest=authorization.authorization_digest,
        authorization_id=authorization.id,
        idempotency_key=plan_digest,
        resources=parse_requests(write=resources),
        adapter_digest=digest({"adapter": f"doctor-{step}/v1", "plan_digest": plan_digest}),
    )
    effect_started = False
    receipt_known = False
    terminalization_started = False
    try:
        governance.require_authorized(
            request,
            authorization=authorization,
            consumed_by=f"cli:doctor:{step}",
        )
        effect_started = True
        result = _apply_step(
            realm_context,
            context,
            repair_plan=repair_plan,
            step=step,
        )
        result_digest = digest(result)
        receipt = host.record_success(
            claim,
            result_digest=result_digest,
            adapter_evidence_digest=digest(
                {"plan_digest": plan_digest, "verified_result": result_digest}
            ),
        )
        receipt_known = True
        checkpoint = Checkpoint(
            checkpoint_id=f"doctor-{step}-{job.id}",
            project_id=str(project_id),
            work_item_id=str(work.id),
            plan_revision_id=str(task_plan.id),
            source_revision=source_revision,
            plan_steps=task_plan.execution_order,
            completed_steps=task_plan.execution_order,
            pending_steps=(),
            step_results=tuple(
                (plan_step_id, result_digest) for plan_step_id in task_plan.execution_order
            ),
            context_manifest_digest=plan_digest,
            journal_head_digest=receipt.adapter_evidence_digest or result_digest,
            next_safe_action="doctor-rerun",
            created_at=dt.datetime.now(dt.UTC),
        )
        stored_checkpoint = ContextContinuityRepository(
            realm_context.connection,
            realm_context.realm_id,
            project_id,
            work.id,
        ).store_checkpoint(checkpoint, task_plan_id=task_plan.id, job_id=job.id)
        terminal_succeeded = host.finish(
            claimed, outcome=AttemptOutcome.SUCCEEDED, result_digest=result_digest
        )
        if not terminal_succeeded:
            raise PolicyViolation("Doctor terminal attempt kapatilamadi")
        terminalization_started = True
        current_work = graph.items.get(work.id)
        current_work = graph.update_details(
            current_work.id,
            acceptance_criteria=tuple(
                AcceptanceCriterion(item.text, verified=True)
                for item in current_work.acceptance_criteria
            ),
            reason="doctor terminal result acceptance verified",
        )
        current_work = graph.transition(current_work.id, WorkState.VERIFICATION)
        completion_service = ControlPlaneCompletionService(
            PostgresControlPlaneCompletionRepository(
                realm_context.connection, realm_context.realm_id
            )
        )
        completion_request = ControlPlaneCompletionRequest(
            project_id=project_id,
            work_item_id=current_work.id,
            task_plan_id=task_plan.id,
            job_id=job.id,
            attempt_id=claimed.attempt_id,
            checkpoint_id=stored_checkpoint,
            source_authorization_id=authorization.id,
            source_authorization_digest=authorization.authorization_digest,
            source_claim_id=claim.id,
            source_claim_digest=claim.claim_digest,
            source_effect_receipt_id=receipt.id,
            source_operation=step,
            source_consumed_by=f"cli:doctor:{step}",
            source_effect_digest=request.effect_digest,
            source_adapter_digest=claim.adapter_digest,
            source_adapter_evidence_digest=receipt.adapter_evidence_digest or result_digest,
            source_resources=tuple(sorted(resources)),
            source_effects=tuple(sorted(item.value for item in request.effects)),
            source_data_classifications=tuple(
                sorted(item.value for item in request.data_classifications)
            ),
            evidence=(
                EvidenceRef(
                    kind="runtime-receipt",
                    reference=str(receipt.id),
                    digest_value=result_digest,
                ),
            ),
        )
        try:
            completion = completion_service.complete(completion_request)
        except Exception as completion_error:
            try:
                completion = completion_service.readback(completion_request)
            except Exception:
                raise completion_error from None
    except Exception as exc:
        if effect_started:
            if not terminalization_started:
                host.jobs.mark_recovery_required(
                    job.id,
                    (
                        f"doctor-{step}-success-receipt-recovery"
                        if receipt_known
                        else f"doctor-{step}-effect-uncertain"
                    ),
                )
            raise
        if host.ledger.receipt_for_claim(claim.id) is None:
            host.record_failure(
                claim,
                category=(
                    FailureCategory.POLICY
                    if isinstance(exc, PolicyViolation)
                    else FailureCategory.ADAPTER
                ),
                failure_digest=digest(
                    {"error_type": type(exc).__name__, "plan_digest": plan_digest}
                ),
            )
        host.finish(claimed, outcome=AttemptOutcome.FAILED)
        current_work = graph.items.get(work.id)
        if current_work.state is not WorkState.CANCELLED:
            graph.transition(
                current_work.id,
                WorkState.CANCELLED,
                reason=f"doctor {step} failed with terminal receipt",
            )
        raise
    return DoctorRepairRuntimeResult(
        step=step,
        result=result,
        work_id=work.id,
        task_plan_id=task_plan.id,
        authorization_id=authorization.id,
        job_id=job.id,
        claim_id=claim.id,
        receipt_id=receipt.id,
        checkpoint_id=stored_checkpoint,
        result_digest=result_digest,
        completion=completion,
    )


def _effect_request(
    repair_plan: DoctorRepairPlan, *, project_id: UUID, step: str
) -> tuple[tuple[str, ...], EffectRequest]:
    if step == "git-fast-forward":
        remote = repair_plan.git.state.remote
        branch = repair_plan.git.state.remote_branch
        if remote is None or branch is None:
            raise PolicyViolation("Git repair exact remote/branch ister")
        resources: tuple[str, ...] = (
            f"project:{project_id}:source",
            f"git-ref:{repair_plan.git.state.branch}",
            f"git-remote:{remote}:{branch}",
        )
        return resources, EffectRequest(
            action=step,
            effects=(
                EffectKind.NETWORK_CALL,
                EffectKind.PROCESS_RUN,
                EffectKind.FILE_WRITE,
            ),
            resources=resources,
            data_classifications=(DataClassification.LOCAL_ONLY,),
            reversible=True,
            touches_external_system=True,
            required_capabilities=("git.read", "process.run"),
        )
    if step == "postgres-migration-upgrade":
        migration_plan = repair_plan.migrations
        if migration_plan is None or migration_plan.next_migration is None:
            raise PolicyViolation("Migration repair plani yok")
        target = migration_plan.next_migration
        resources = (
            f"project:{project_id}:database",
            f"db-object:migration:{target.version}:{target.checksum}",
        )
        return resources, EffectRequest(
            action=step,
            effects=(EffectKind.DATABASE_WRITE,),
            resources=resources,
            data_classifications=(DataClassification.LOCAL_ONLY,),
            reversible=target.has_down,
            required_capabilities=("database.write",),
        )
    routines = repair_plan.routines
    if routines is None:
        raise PolicyViolation("Routine repair plani yok")
    resources = (
        f"project:{project_id}:database",
        f"db-object:routines:head-{routines.status.migration_head}",
    )
    return resources, EffectRequest(
        action=step,
        effects=(EffectKind.DATABASE_WRITE,),
        resources=resources,
        data_classifications=(DataClassification.LOCAL_ONLY,),
        reversible=True,
        required_capabilities=("database.write",),
    )


def _apply_step(
    realm_context: RealmContext,
    context: ApplicationContext,
    *,
    repair_plan: DoctorRepairPlan,
    step: str,
) -> dict[str, Any]:
    if step == "git-fast-forward":
        return apply_git_fast_forward(
            context.core_path,
            plan=repair_plan.git,
            plan_digest=repair_plan.git.plan_digest,
        ).as_dict()
    if step == "postgres-migration-upgrade":
        migration_plan = repair_plan.migrations
        if migration_plan is None or migration_plan.next_migration is None:
            raise PolicyViolation("Migration repair plani yok")
        directory = context.core_path / "migrations"
        before = migrations.status(realm_context.connection, directory)
        before_binding = (
            before.head,
            tuple((item.version, item.name, item.checksum) for item in before.applied),
            tuple((item.version, item.name, item.checksum) for item in before.pending),
            tuple((item.kind.value, item.version, item.detail) for item in before.drift),
        )
        planned_binding = (
            migration_plan.status.head,
            tuple(
                (item.version, item.name, item.checksum)
                for item in migration_plan.status.applied
            ),
            tuple(
                (item.version, item.name, item.checksum)
                for item in migration_plan.status.pending
            ),
            tuple(
                (item.kind.value, item.version, item.detail)
                for item in migration_plan.status.drift
            ),
        )
        if before_binding != planned_binding:
            raise PolicyViolation("Migration repair plani veritabani durumu degistigi icin stale")
        target = migration_plan.next_migration
        reset_role(realm_context.connection)
        try:
            applied = migrations.upgrade(
                realm_context.connection,
                directory,
                target=target.version,
            )
            after = migrations.status(realm_context.connection, directory)
        finally:
            configure_session(
                realm_context.connection,
                realm_id=realm_context.realm_id,
            )
        if len(applied) != 1 or applied[0].version != target.version:
            raise PolicyViolation("Doctor exact tek migration uygulamadi")
        recorded = next((item for item in after.applied if item.version == target.version), None)
        if recorded is None or recorded.checksum != target.checksum or after.drift:
            raise PolicyViolation("Migration effect sonrasi checksum/head dogrulamasi basarisiz")
        return {
            "schema": "zekam-doctor-migration-repair-result/v1",
            "previous_head": before.head,
            "head": after.head,
            "version": target.version,
            "name": target.name,
            "checksum": target.checksum,
            "remaining_pending": [item.label for item in after.pending],
            "verified": True,
        }
    routines = repair_plan.routines
    if routines is None:
        raise PolicyViolation("Routine repair plani yok")
    reset_role(realm_context.connection)
    try:
        return routine_integrity.repair_missing_routines(
            realm_context.connection,
            plan_digest=routines.plan_digest,
            directory=context.core_path / "migrations",
        ).as_dict()
    finally:
        configure_session(
            realm_context.connection,
            realm_id=realm_context.realm_id,
        )


def _assert_actor_and_project(
    realm_context: RealmContext,
    context: ApplicationContext,
    *,
    actor_id: UUID,
    project_id: UUID,
) -> None:
    actor = ActorRepository(realm_context.connection, realm_context.realm_id).get(actor_id)
    if actor.kind is not ActorKind.HUMAN or actor.status is not LifecycleStatus.ACTIVE:
        raise PolicyViolation("Doctor repair actor aktif human olmali")
    integration = ProjectIntegrationService(realm_context.connection, realm_context.realm)
    project = integration.projects.get(project_id)
    if project.status is not LifecycleStatus.ACTIVE:
        raise PolicyViolation("Doctor repair project aktif olmali")
    if integration.resolve_source_root(project_id).resolve() != context.core_path.resolve():
        raise PolicyViolation("Doctor repair yalniz exact Zekam source rootunda calisir")
