"""Provider-free preparation of the current Codex lifecycle runtime template."""

from __future__ import annotations

import datetime as dt
import platform
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from zekam.application.control_plane_completion import (
    ControlPlaneCompletionRequest,
    ControlPlaneCompletionService,
)
from zekam.application.execution import ExecutionHost
from zekam.application.governance import DEFAULT_POLICY_NAME, EffectRequest, GovernanceService
from zekam.application.layered_model_routing import prepare_project_context
from zekam.application.model_registry import load_inventory
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.provider_configuration import load_provider_bindings
from zekam.application.work_graph import WorkGraphService
from zekam.domain.agents import AgentAssignment, AssignmentRole, AssignmentStatus
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    Checkpoint,
    ContextCandidate,
    JournalEntry,
    compile_context,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_environment import (
    AssignmentEnvironmentBinding,
    ExecutionEnvironmentSnapshot,
    ShellSnapshot,
    TurnExecutionSnapshot,
    detect_environment_drift,
    reprobe_snapshot,
)
from zekam.domain.execution_run import (
    CheckpointDisposition,
    ContextPacket,
    ContextPacketSection,
    ExecutionEnvelope,
    ExecutionRun,
    ProviderBindingSnapshot,
)
from zekam.domain.identifiers import new_uuid7
from zekam.domain.model_inventory import Modality
from zekam.domain.model_routing import (
    AgentRole,
    ExecutionTargetSnapshot,
    RoleRoutingPolicy,
    RoutingLayer,
)
from zekam.domain.realm import ActorKind, LifecycleStatus
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, Job, JobKind
from zekam.domain.security import DataClassification
from zekam.domain.work import (
    AcceptanceCriterion,
    EffectKind,
    EvidenceRef,
    PlanStep,
    WorkState,
    WorkType,
)
from zekam.infrastructure.postgres.agent_assignment_repository import AgentAssignmentRepository
from zekam.infrastructure.postgres.client_lifecycle_repository import (
    ActiveLifecycleExecution,
    ClientLifecycleRepository,
)
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.control_plane_completion_repository import (
    PostgresControlPlaneCompletionRepository,
)
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
from zekam.infrastructure.postgres.hook_runtime_repository import HookRuntimeRepository
from zekam.infrastructure.postgres.lifecycle_runtime_template_repository import (
    LifecycleRuntimeTemplateRepository,
)
from zekam.infrastructure.postgres.model_repository import ModelInventoryRepository
from zekam.infrastructure.postgres.model_routing_repository import ModelRoutingRepository
from zekam.infrastructure.postgres.runtime_repository import JobRepository
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository


@dataclass(frozen=True, slots=True)
class LifecycleTemplatePreparePlan:
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    work_revision: int
    actor_id: UUID
    source_revision: str
    policy_digest: str
    prepared_at: dt.datetime
    expires_at: dt.datetime

    def authority_body(self) -> dict[str, Any]:
        """Return the stable authority binding used across separate CLI processes."""

        return {
            "schema": "zekam-lifecycle-template-prepare-plan/v1",
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "work_revision": self.work_revision,
            "actor_id": str(self.actor_id),
            "source_revision": self.source_revision,
            "policy_digest": self.policy_digest,
            "client_id": "codex",
            "slot": "lifecycle",
            "provider_calls": 0,
            "network_calls": 0,
            "grants_authority": False,
        }

    def body(self) -> dict[str, Any]:
        return self.authority_body() | {
            "prepared_at": self.prepared_at,
            "expires_at": self.expires_at,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.authority_body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"plan_digest": self.plan_digest, "applied": False}


@dataclass(frozen=True, slots=True)
class LifecycleRuntimeTemplatePrepareService:
    connection: Any
    realm: Any

    def prepare(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        actor_id: UUID,
        source_revision: str,
        now: dt.datetime | None = None,
    ) -> LifecycleTemplatePreparePlan:
        moment = now or dt.datetime.now(dt.UTC)
        actor = ActorRepository(self.connection, self.realm.id).get(actor_id)
        if actor.kind is not ActorKind.HUMAN or actor.status is not LifecycleStatus.ACTIVE:
            raise PolicyViolation("Lifecycle template prepare aktif human actor ister")
        work = WorkGraphService(self.connection, self.realm, actor_id=actor_id).items.get(
            work_item_id
        )
        if work.project_id != project_id or work.state not in {
            WorkState.PROPOSED,
            WorkState.ACTIVE,
        }:
            raise PolicyViolation("Lifecycle template prepare exact current Work ister")
        policy = GovernanceService(self.connection, self.realm).policies.current(
            DEFAULT_POLICY_NAME
        )
        if policy is None:
            raise PolicyViolation("Lifecycle template prepare current policy ister")
        canonical = LifecycleRuntimeTemplateRepository(
            self.connection, self.realm.id
        ).current_source_revision(project_id)
        if canonical != source_revision:
            raise PolicyViolation("Lifecycle template prepare canonical source drift")
        return LifecycleTemplatePreparePlan(
            realm_id=self.realm.id,
            project_id=project_id,
            work_item_id=work_item_id,
            work_revision=work.revision,
            actor_id=actor_id,
            source_revision=source_revision,
            policy_digest=policy.policy_digest,
            prepared_at=moment,
            expires_at=moment + dt.timedelta(minutes=30),
        )

    def apply(
        self,
        plan: LifecycleTemplatePreparePlan,
        *,
        supplied_plan_digest: str,
    ) -> dict[str, Any]:
        if supplied_plan_digest != plan.plan_digest:
            raise PolicyViolation("Lifecycle template prepare exact plan digest ister")
        if dt.datetime.now(dt.UTC) > plan.expires_at:
            raise PolicyViolation("Lifecycle template prepare plan suresi dolmus")
        current = self.prepare(
            project_id=plan.project_id,
            work_item_id=plan.work_item_id,
            actor_id=plan.actor_id,
            source_revision=plan.source_revision,
            now=plan.prepared_at,
        )
        if current.plan_digest != plan.plan_digest:
            raise PolicyViolation("Lifecycle template prepare plan drift")

        graph = WorkGraphService(self.connection, self.realm, actor_id=plan.actor_id)
        governance = GovernanceService(self.connection, self.realm, actor_id=plan.actor_id)
        resource = (
            f"db-object:lifecycle-template:{plan.project_id}:"
            f"{plan.plan_digest.removeprefix('sha256:')}"
        )
        with self.connection.transaction():
            prep_work = graph.create_item(
                project_id=plan.project_id,
                type=WorkType.MAINTENANCE,
                title="Codex lifecycle runtime template prerequisites",
                summary=f"Target Work {plan.work_item_id} icin provider-free current template",
                acceptance_criteria=(AcceptanceCriterion("terminal exact runtime receipt"),),
            )
            graph.set_intent(
                prep_work.id,
                goal="Current lifecycle runtime template prerequisites materialize et",
                non_goals=("provider call", "network call", "target Work mutation"),
                outcomes=("current exact template",),
                constraints=("claim-before-effect", "max-attempts-one"),
            )
            task_plan = graph.create_plan(
                prep_work.id,
                source_revision=plan.source_revision,
                policy_digest=plan.policy_digest,
                steps=(
                    PlanStep(
                        step_id="lifecycle-template-prepare",
                        title="Provider-free lifecycle template materialize et",
                        effect=EffectKind.DATABASE_WRITE,
                        logical_resources=(resource,),
                        risk="high",
                    ),
                ),
            )
            graph.transition(prep_work.id, WorkState.READY)
            graph.transition(prep_work.id, WorkState.ACTIVE)
            request = EffectRequest(
                action="lifecycle-template-prepare-v1",
                effects=(EffectKind.DATABASE_WRITE,),
                resources=(resource,),
                data_classifications=(DataClassification.LOCAL_ONLY,),
                reversible=True,
                touches_external_system=False,
                required_capabilities=(),
            )
            authorization = governance.issue_authorization(
                request=request,
                actor_id=plan.actor_id,
                plan=task_plan,
                lifetime=dt.timedelta(minutes=30),
            )
            run = ExecutionRun.create(
                id=new_uuid7(now=plan.prepared_at),
                realm_id=self.realm.id,
                project_id=plan.project_id,
                work_item_id=prep_work.id,
                plan_id=task_plan.id,
                client_id="codex",
                session_id=f"lifecycle-template-{plan.work_item_id}",
                source_revision=plan.source_revision,
                policy_digest=plan.policy_digest,
                max_input_tokens=1,
                max_output_tokens=1,
                max_cost_micros=1,
                deadline=plan.expires_at,
                created_at=plan.prepared_at,
            )
            runs = ExecutionRunRepository(self.connection, self.realm.id)
            runs.create_run(run)
            runs.activate_run(run.id, started_at=plan.prepared_at)
            _, planned_manifest = _prepare_manifest(plan, prep_work.id)
            coordinator_id = new_uuid7(now=plan.prepared_at)
            coordinator_body: dict[str, object] = {
                "id": str(coordinator_id),
                "realm_id": str(self.realm.id),
                "project_id": str(plan.project_id),
                "work_item_id": str(prep_work.id),
                "plan_id": str(task_plan.id),
                "step_id": "lifecycle-template-prepare",
                "parent_assignment_id": None,
                "role": AssignmentRole.COORDINATOR.value,
                "agent_ref": "lifecycle-template-coordinator",
                "risk": "high",
                "instruction_digest": plan.plan_digest,
                "context_manifest_digest": planned_manifest.manifest_digest,
                "read_resources": [],
                "write_resources": [],
            }
            coordinator = AgentAssignment(
                id=coordinator_id,
                realm_id=self.realm.id,
                project_id=plan.project_id,
                work_item_id=prep_work.id,
                plan_id=task_plan.id,
                step_id="lifecycle-template-prepare",
                role=AssignmentRole.COORDINATOR,
                agent_ref="lifecycle-template-coordinator",
                status=AssignmentStatus.ACTIVE,
                risk="high",
                instruction_digest=plan.plan_digest,
                context_manifest_digest=planned_manifest.manifest_digest,
                assignment_digest=digest(coordinator_body),
                created_at=plan.prepared_at,
            )
            assignment_id = new_uuid7(now=plan.prepared_at)
            assignment_body = {
                "id": str(assignment_id),
                "realm_id": str(self.realm.id),
                "project_id": str(plan.project_id),
                "work_item_id": str(prep_work.id),
                "plan_id": str(task_plan.id),
                "step_id": "lifecycle-template-prepare",
                "parent_assignment_id": str(coordinator_id),
                "role": AssignmentRole.BUILDER.value,
                "agent_ref": "lifecycle-template-worker",
                "risk": "high",
                "instruction_digest": plan.plan_digest,
                "context_manifest_digest": planned_manifest.manifest_digest,
                "read_resources": [],
                "write_resources": [resource],
            }
            assignment = AgentAssignment(
                id=assignment_id,
                realm_id=self.realm.id,
                project_id=plan.project_id,
                work_item_id=prep_work.id,
                parent_assignment_id=coordinator_id,
                plan_id=task_plan.id,
                step_id="lifecycle-template-prepare",
                role=AssignmentRole.BUILDER,
                agent_ref="lifecycle-template-worker",
                status=AssignmentStatus.ACTIVE,
                risk="high",
                instruction_digest=plan.plan_digest,
                context_manifest_digest=planned_manifest.manifest_digest,
                write_resources=(resource,),
                assignment_digest=digest(assignment_body),
                created_at=plan.prepared_at,
            )
            verifier_id = new_uuid7(now=plan.prepared_at)
            verifier_draft = AgentAssignment(
                id=verifier_id,
                realm_id=self.realm.id,
                project_id=plan.project_id,
                work_item_id=prep_work.id,
                parent_assignment_id=coordinator_id,
                plan_id=task_plan.id,
                step_id="lifecycle-template-prepare",
                role=AssignmentRole.VERIFIER,
                agent_ref="lifecycle-template-verifier",
                status=AssignmentStatus.ACTIVE,
                risk="low",
                instruction_digest=plan.plan_digest,
                context_manifest_digest=planned_manifest.manifest_digest,
                read_resources=(resource,),
                assignment_digest=digest("placeholder"),
                created_at=plan.prepared_at,
            )
            verifier = AgentAssignment(
                **{
                    **{
                        name: getattr(verifier_draft, name)
                        for name in verifier_draft.__dataclass_fields__
                    },
                    "assignment_digest": digest(verifier_draft.identity_body()),
                }
            )
            assignment_repository = AgentAssignmentRepository(self.connection, self.realm.id)
            assignment_repository.create(coordinator)
            assignment_repository.create(assignment)
            assignment_repository.create(verifier)
            job, created = JobRepository(self.connection, self.realm.id).enqueue(
                Job.create(
                    realm_id=self.realm.id,
                    project_id=plan.project_id,
                    kind=JobKind.MUTATION,
                    idempotency_key=f"lifecycle-template:{plan.plan_digest}:{prep_work.id}",
                    resources=parse_requests(write=(resource,)),
                    required_capabilities=("client.lifecycle.template-prepare",),
                    max_attempts=1,
                    work_item_id=prep_work.id,
                    plan_id=task_plan.id,
                    step_id="lifecycle-template-prepare",
                    assignment_id=assignment.id,
                    run_id=run.id,
                    payload={
                        "schema": "zekam-lifecycle-template-prepare-job/v1",
                        "target_work_item_id": str(plan.work_item_id),
                        "target_work_revision": plan.work_revision,
                        "actor_id": str(plan.actor_id),
                        "source_revision": plan.source_revision,
                        "policy_digest": plan.policy_digest,
                        "prepared_at": plan.prepared_at.isoformat(),
                        "expires_at": plan.expires_at.isoformat(),
                        "plan_digest": plan.plan_digest,
                        "authorization_id": str(authorization.id),
                        "effect_digest": request.effect_digest,
                        "run_id": str(run.id),
                    },
                    now=plan.prepared_at,
                )
            )
            if not created:
                raise PolicyViolation("Lifecycle template prepare job replay")
        return {
            "schema": "zekam-lifecycle-template-prepare-enqueued/v1",
            "applied": True,
            "effect_started": False,
            "preparatory_work_id": str(prep_work.id),
            "task_plan_id": str(task_plan.id),
            "run_id": str(run.id),
            "assignment_id": str(assignment.id),
            "job_id": str(job.id),
            "authorization_id": str(authorization.id),
            "plan_digest": plan.plan_digest,
            "provider_calls": 0,
            "network_calls": 0,
            "grants_authority": False,
        }


def run_lifecycle_template_prepare_once(
    connection: Any,
    realm: Any,
    *,
    worker_label: str = "lifecycle-template-worker",
    now: dt.datetime | None = None,
) -> dict[str, Any] | None:
    """Consume one exact governed template job; provider and network effects stay zero."""

    moment = now or dt.datetime.now(dt.UTC)
    host = ExecutionHost(connection, realm.id, worker_label=worker_label)
    claimed = host.acquire_work(
        capabilities=("client.lifecycle.template-prepare",), now=moment
    )
    if claimed is None:
        return None
    job = claimed.job
    payload = dict(job.payload)
    expected = {
        "schema",
        "target_work_item_id",
        "target_work_revision",
        "actor_id",
        "source_revision",
        "policy_digest",
        "prepared_at",
        "expires_at",
        "plan_digest",
        "authorization_id",
        "effect_digest",
        "run_id",
    }
    if (
        set(payload) != expected
        or payload.get("schema") != "zekam-lifecycle-template-prepare-job/v1"
        or job.max_attempts != 1
        or job.kind is not JobKind.MUTATION
        or any(value is None for value in (job.work_item_id, job.plan_id, job.run_id))
    ):
        raise PolicyViolation("Lifecycle template claimed job contract drift")
    assert job.work_item_id is not None and job.plan_id is not None and job.run_id is not None
    prep_work_id = job.work_item_id
    task_plan_id = job.plan_id
    plan = LifecycleTemplatePreparePlan(
        realm_id=realm.id,
        project_id=job.project_id,
        work_item_id=UUID(str(payload["target_work_item_id"])),
        work_revision=int(payload["target_work_revision"]),
        actor_id=UUID(str(payload["actor_id"])),
        source_revision=str(payload["source_revision"]),
        policy_digest=str(payload["policy_digest"]),
        prepared_at=dt.datetime.fromisoformat(str(payload["prepared_at"])),
        expires_at=dt.datetime.fromisoformat(str(payload["expires_at"])),
    )
    if plan.plan_digest != str(payload["plan_digest"]):
        raise PolicyViolation("Lifecycle template claimed plan digest drift")
    service = LifecycleRuntimeTemplatePrepareService(connection, realm)
    service.prepare(
        project_id=plan.project_id,
        work_item_id=plan.work_item_id,
        actor_id=plan.actor_id,
        source_revision=plan.source_revision,
        now=plan.prepared_at,
    )
    authorization = AuthorizationRepository(connection, realm.id).get(
        UUID(str(payload["authorization_id"]))
    )
    resource = (
        f"db-object:lifecycle-template:{plan.project_id}:"
        f"{plan.plan_digest.removeprefix('sha256:')}"
    )
    request = EffectRequest(
        action="lifecycle-template-prepare-v1",
        effects=(EffectKind.DATABASE_WRITE,),
        resources=(resource,),
        data_classifications=(DataClassification.LOCAL_ONLY,),
        reversible=True,
        touches_external_system=False,
        required_capabilities=(),
    )
    if (
        authorization.work_item_id != prep_work_id
        or authorization.plan_id != task_plan_id
        or authorization.effect_digest != request.effect_digest
        or authorization.effect_digest != str(payload["effect_digest"])
    ):
        raise PolicyViolation("Lifecycle template exact authorization drift")
    adapter_digest = digest("lifecycle-template-postgres-materializer/v1")
    claim = host.claim_effect(
        claimed,
        operation="lifecycle-template-prepare",
        effect_digest=request.effect_digest,
        authorization_digest=authorization.authorization_digest,
        authorization_id=authorization.id,
        idempotency_key=f"{plan.plan_digest}:{job.id}",
        resources=parse_requests(write=(resource,)),
        adapter_digest=adapter_digest,
    )
    GovernanceService(connection, realm, actor_id=plan.actor_id).require_authorized(
        request,
        authorization=authorization,
        consumed_by="worker:lifecycle-template-prepare",
    )
    result = materialize_lifecycle_template(connection, realm, plan)
    runtime = _bind_prepare_runtime(
        connection=connection,
        realm=realm,
        claimed=claimed,
        plan=plan,
        authorization=authorization,
        result=result,
        now=moment,
    )
    result = result | {
        "context_manifest_id": str(runtime[0]),
        "context_packet_id": str(runtime[1]),
        "execution_envelope_id": str(runtime[2].envelope_id),
    }
    result_digest = digest(result)
    adapter_evidence_digest = digest(
        {
            "schema": "zekam-lifecycle-template-adapter-evidence/v1",
            "plan_digest": plan.plan_digest,
            "result_digest": result_digest,
            "provider_calls": 0,
            "network_calls": 0,
        }
    )
    terminal_moment = dt.datetime.now(dt.UTC)
    receipt = host.record_success(
        claim,
        result_digest=result_digest,
        adapter_evidence_digest=adapter_evidence_digest,
        now=terminal_moment,
    )
    checkpoint = Checkpoint(
        checkpoint_id=f"lifecycle-template-{job.id}",
        project_id=str(job.project_id),
        work_item_id=str(prep_work_id),
        plan_revision_id=str(task_plan_id),
        source_revision=plan.source_revision,
        plan_steps=("lifecycle-template-prepare",),
        completed_steps=("lifecycle-template-prepare",),
        pending_steps=(),
        step_results=(("lifecycle-template-prepare", result_digest),),
        context_manifest_digest=plan.plan_digest,
        journal_head_digest=adapter_evidence_digest,
        next_safe_action="target-client-runtime-bootstrap",
        created_at=terminal_moment,
    )
    checkpoint_id = ContextContinuityRepository(
        connection, realm.id, job.project_id, prep_work_id
    ).store_checkpoint(checkpoint, task_plan_id=task_plan_id, job_id=job.id)
    lifecycle_repository = ClientLifecycleRepository(connection, realm.id)
    active = runtime[2]
    lifecycle_repository.store_job_checkpoint(
        execution=ActiveLifecycleExecution(
            active.project_id,
            active.work_item_id,
            active.plan_id,
            active.run_id,
            active.attempt_id,
            active.assignment_id,
            active.lease_id,
            active.fencing_token,
            active.envelope_id,
            active.envelope_digest,
            active.source_revision,
            active.source_digest,
            active.policy_digest,
            active.migration_digest,
            active.context_manifest_digest,
            active.journal_head_digest,
            active.work_plan_digest,
        ),
        job_id=job.id,
        step_id="lifecycle-template-prepare",
        result_digest=result_digest,
        now=terminal_moment,
        require_lifecycle_admission=False,
    )
    if not host.finish(
        claimed,
        outcome=AttemptOutcome.SUCCEEDED,
        result_digest=result_digest,
        now=terminal_moment,
    ):
        raise PolicyViolation("Lifecycle template terminal job finish reddedildi")
    ExecutionRunRepository(connection, realm.id).finish_run(
        UUID(str(payload["run_id"])), state="completed", terminal_at=terminal_moment
    )
    AgentAssignmentRepository(connection, realm.id).complete_terminal_plan(
        task_plan_id, now=terminal_moment
    )
    graph = WorkGraphService(connection, realm, actor_id=plan.actor_id)
    prep_work = graph.items.get(prep_work_id)
    graph.update_details(
        prep_work.id,
        acceptance_criteria=tuple(
            AcceptanceCriterion(item.text, verified=True)
            for item in prep_work.acceptance_criteria
        ),
        reason="Lifecycle template terminal receipt verified",
        now=terminal_moment,
    )
    graph.transition(prep_work.id, WorkState.VERIFICATION, now=terminal_moment)
    completion = ControlPlaneCompletionService(
        PostgresControlPlaneCompletionRepository(connection, realm.id)
    ).complete(
        ControlPlaneCompletionRequest(
            project_id=job.project_id,
            work_item_id=prep_work_id,
            task_plan_id=task_plan_id,
            job_id=job.id,
            attempt_id=claimed.attempt_id,
            checkpoint_id=checkpoint_id,
            source_authorization_id=authorization.id,
            source_authorization_digest=authorization.authorization_digest,
            source_claim_id=claim.id,
            source_claim_digest=claim.claim_digest,
            source_effect_receipt_id=receipt.id,
            source_operation="lifecycle-template-prepare",
            source_consumed_by="worker:lifecycle-template-prepare",
            source_effect_digest=request.effect_digest,
            source_adapter_digest=adapter_digest,
            source_adapter_evidence_digest=adapter_evidence_digest,
            source_resources=(resource,),
            source_effects=(EffectKind.DATABASE_WRITE.value,),
            source_data_classifications=(DataClassification.LOCAL_ONLY.value,),
            evidence=(
                EvidenceRef(
                    kind="runtime-receipt",
                    reference=str(receipt.id),
                    digest_value=result_digest,
                ),
            ),
        )
    )
    return result | {
        "preparatory_work_id": str(prep_work_id),
        "job_id": str(job.id),
        "claim_id": str(claim.id),
        "receipt_id": str(receipt.id),
        "checkpoint_id": str(checkpoint_id),
        "completion_receipt_id": str(completion.effect_receipt_id),
    }


def _prepare_manifest(
    plan: LifecycleTemplatePreparePlan, prep_work_id: UUID
) -> tuple[ContextCandidate, Any]:
    candidate = ContextCandidate(
        candidate_id=f"template-{plan.plan_digest.removeprefix('sha256:')[:24]}",
        authority=AuthorityLevel.CANONICAL,
        observed_at=plan.prepared_at,
        source_revision=plan.source_revision,
        content_digest=plan.plan_digest,
        token_count=1,
        required=True,
        source_ref=f"work/{prep_work_id}",
    )
    return candidate, compile_context(
        (candidate,),
        token_budget=64,
        minimum_authority=AuthorityLevel.CANONICAL,
        now=plan.prepared_at,
    )


def _bind_prepare_runtime(
    *,
    connection: Any,
    realm: Any,
    claimed: Any,
    plan: LifecycleTemplatePreparePlan,
    authorization: Any,
    result: dict[str, Any],
    now: dt.datetime,
    journal_created_at: dt.datetime | None = None,
) -> tuple[UUID, UUID, ActiveLifecycleExecution]:
    """Bind the claimed provider-free preparation to immutable execution evidence."""

    job = claimed.job
    assert job.work_item_id is not None and job.plan_id is not None and job.run_id is not None
    assert job.assignment_id is not None
    template_repo = LifecycleRuntimeTemplateRepository(connection, realm.id)
    template = template_repo.current(plan.project_id, plan.source_revision, plan.policy_digest)
    candidate, manifest = _prepare_manifest(plan, job.work_item_id)
    context = ContextContinuityRepository(
        connection, realm.id, job.project_id, job.work_item_id
    )
    manifest_id = context.store_manifest(manifest)
    packet = ContextPacket.create(
        realm_id=realm.id,
        project_id=job.project_id,
        work_item_id=job.work_item_id,
        manifest_id=manifest_id,
        manifest_digest=manifest.manifest_digest,
        sections=(ContextPacketSection(candidate.candidate_id, candidate.content_digest, 1),),
        created_at=now,
    )
    runs = ExecutionRunRepository(connection, realm.id)
    runs.create_packet(packet)
    session_ref = f"lifecycle-template-{job.work_item_id}-{job.run_id}-{job.id}"
    HookRuntimeRepository(connection, realm.id).start_session(session_ref=session_ref)
    runs.bind_assignment_environment(
        AssignmentEnvironmentBinding.create(
            realm_id=realm.id,
            assignment_id=job.assignment_id,
            execution_environment_snapshot_digest=template.execution_environment_snapshot_digest,
            bound_at=now,
        )
    )
    turn = TurnExecutionSnapshot.create(
        realm_id=realm.id,
        assignment_id=job.assignment_id,
        run_id=job.run_id,
        attempt_id=claimed.attempt_id,
        client_session_id=session_ref,
        turn_id=f"template-{job.id}",
        model_id=template.model_id,
        provider_id=template.provider_ref,
        route_decision_digest=template.route_decision_digest,
        reasoning_profile_digest=digest("lifecycle-template-provider-free/v1"),
        execution_environment_snapshot_digest=template.execution_environment_snapshot_digest,
        context_manifest_digest=manifest.manifest_digest,
        exposed_tool_set_digest=template.compiled_tool_set_digest,
        hook_set_digest=template.hook_set_digest,
        config_effective_digest=template.config_effective_digest,
        created_at=now,
    )
    runs.create_turn_snapshot(turn)
    envelope = ExecutionEnvelope.create(
        realm_id=realm.id,
        run_id=job.run_id,
        job_id=job.id,
        attempt_id=claimed.attempt_id,
        lease_id=claimed.lease.id,
        fencing_token=claimed.lease.fencing_token,
        request_ordinal=1,
        idempotency_key=f"lifecycle-template-envelope:{job.id}",
        assignment_id=job.assignment_id,
        role="builder",
        route_decision_id=template.route_decision_id,
        route_decision_digest=template.route_decision_digest,
        route_expires_at=template.route_expires_at,
        model_id=template.model_id,
        provider_binding_id=template.provider_binding_id,
        provider_binding_digest=template.provider_binding_digest,
        provider_ref=template.provider_ref,
        context_manifest_id=manifest_id,
        context_manifest_digest=manifest.manifest_digest,
        context_packet_id=packet.id,
        context_packet_digest=packet.packet_digest,
        turn_execution_snapshot_id=turn.id,
        turn_execution_snapshot_digest=turn.turn_snapshot_digest,
        checkpoint_id=None,
        checkpoint_digest=None,
        checkpoint_disposition=CheckpointDisposition.NOT_APPLICABLE_GENESIS,
        source_revision=plan.source_revision,
        policy_digest=plan.policy_digest,
        authorization_scope_digest=digest(authorization.scope.body()),
        output_schema_digest=digest("lifecycle-template-prepare-output/v1"),
        payload_digest=digest(result),
        max_input_tokens=1,
        max_output_tokens=1,
        max_cost_micros=1,
        deadline=plan.expires_at,
        created_at=now,
    )
    runs.create_envelope(envelope)
    facts = template_repo.projection_facts(job.project_id, job.work_item_id)
    lifecycle = ClientLifecycleRepository(connection, realm.id)
    head = context.journal_head()
    previous = None if head is None else head[1]
    journal = JournalEntry(
        1 if head is None else head[0] + 1,
        str(job.work_item_id),
        "step-completed",
        digest(result),
        previous,
        False,
        journal_created_at or now,
    )
    context.append_journal(journal, expected_head=previous)
    with connection.cursor() as cursor:
        cursor.execute(
            "select entry_digest from work.work_journal_entry where realm_id=%s"
            " and work_item_id=%s order by sequence desc,id desc limit 1",
            (realm.id, job.work_item_id),
        )
        row = cursor.fetchone()
    if row is None:
        raise PolicyViolation("Lifecycle template Work journal head eksik")
    active = ActiveLifecycleExecution(
        job.project_id,
        job.work_item_id,
        job.plan_id,
        job.run_id,
        claimed.attempt_id,
        job.assignment_id,
        claimed.lease.id,
        claimed.lease.fencing_token,
        envelope.id,
        envelope.envelope_digest,
        plan.source_revision,
        str(facts[4]),
        plan.policy_digest,
        str(facts[6]),
        manifest.manifest_digest,
        str(row[0]),
        lifecycle.current_work_plan_digest(work_item_id=job.work_item_id, plan_id=job.plan_id),
    )
    return manifest_id, packet.id, active


def materialize_lifecycle_template(
    connection: Any, realm: Any, plan: LifecycleTemplatePreparePlan
) -> dict[str, Any]:
        """Materialize only after the caller acquired job and effect claim."""

        self = LifecycleRuntimeTemplatePrepareService(connection, realm)

        inventory = load_inventory()
        binding = load_provider_bindings().for_modality(Modality.CODE)
        inventory_records = {
            item.model_id: item for item in inventory.records if item.enabled
        }
        if binding.model_id not in inventory_records:
            raise PolicyViolation("Lifecycle template provider binding current inventory disinda")
        integration = ProjectIntegrationService(self.connection, self.realm)
        prepared = prepare_project_context(
            integration,
            plan.project_id,
            inventory_digest=inventory.snapshot_digest,
            policy_digest=plan.policy_digest,
        )
        if prepared.context.source_revision != plan.source_revision:
            raise PolicyViolation("Lifecycle template routing context source drift")

        target = ExecutionTargetSnapshot(
            client_id="codex",
            slot="lifecycle",
            execution_mode="native-sequential",
            model_selectable=True,
            structured_result=False,
            cancellation=False,
            max_concurrency=1,
            cost_evidence_digest=digest("codex-lifecycle-provider-free-cost-unknown/v1"),
            capability_digest=digest("codex-lifecycle-local-control-plane/v1"),
            captured_at=plan.prepared_at,
            expires_at=plan.expires_at,
        )
        role_policy = RoleRoutingPolicy(
            role=AgentRole.IMPLEMENTER,
            target_layer=RoutingLayer.PROJECT,
            required_layers=(
                RoutingLayer.GENERAL,
                RoutingLayer.WORKLOAD,
                RoutingLayer.PROJECT,
            ),
            top_k=1,
            fallback_model_ids=(),
            max_cost=0,
            max_latency_ms=0,
            independent_from_roles=(),
            policy_digest=plan.policy_digest,
        )
        routing = ModelRoutingRepository(self.connection, self.realm.id)
        execution = ExecutionRunRepository(self.connection, self.realm.id)
        with self.connection.transaction():
            ModelInventoryRepository(self.connection, self.realm.id).upsert(
                inventory_records[binding.model_id]
            )
            context_id, context_inserted = routing.store_project_context(prepared.context)
            target_id, target_inserted = routing.store_execution_target(target)
            role_policy_id = routing.store_role_policy(
                role_policy, effective_from=plan.prepared_at
            )
            route_digest = digest(
                {
                    "schema": "zekam-codex-lifecycle-route/v1",
                    "project_id": str(plan.project_id),
                    "context_digest": prepared.context.context_digest,
                    "target_digest": target.snapshot_digest,
                    "model_id": binding.model_id,
                    "policy_digest": plan.policy_digest,
                }
            )
            route_id = uuid4()
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "insert into models.model_route_decision"
                    " (id,realm_id,role_policy_id,execution_target_id,project_id,"
                    " project_context_id,role,target_layer,workload,technology,"
                    " inventory_digest,routing_policy_digest,policy_digest,"
                    " execution_target_digest,excluded_model_ids,excluded_execution_identities,"
                    " status,primary_model_id,fallback_model_id,evidence_digest,"
                    " authority_granted,decided_at) values"
                    " (%s,%s,%s,%s,%s,%s,'implementer','project','client-lifecycle','python',"
                    " %s,%s,%s,%s,'{}'::text[],'{}'::text[],'selected',%s,null,%s,false,%s)"
                    " on conflict (realm_id,evidence_digest) do nothing returning id",
                    (
                        route_id,
                        self.realm.id,
                        role_policy_id,
                        target_id,
                        plan.project_id,
                        context_id,
                        inventory.snapshot_digest,
                        plan.policy_digest,
                        plan.policy_digest,
                        target.snapshot_digest,
                        binding.model_id,
                        route_digest,
                        plan.prepared_at,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        "select id from models.model_route_decision"
                        " where realm_id=%s and evidence_digest=%s",
                        (self.realm.id, route_digest),
                    )
                    row = cursor.fetchone()
                if row is None:
                    raise PolicyViolation("Lifecycle template route replay kayboldu")
                route_id = UUID(str(row[0]))
                cursor.execute(
                    "select compiled.config_effective_digest"
                    " from hooks.current_generation current"
                    " join hooks.compiled_set compiled on compiled.realm_id=current.realm_id"
                    " and compiled.id=current.compiled_set_id where current.realm_id=%s",
                    (self.realm.id,),
                )
                hook_row = cursor.fetchone()
                cursor.execute(
                    "select permission_profile_digest from tools.compiled_set"
                    " where realm_id=%s and role='builder'"
                    " order by created_at desc,id desc limit 1",
                    (self.realm.id,),
                )
                tool_row = cursor.fetchone()
            if hook_row is None or tool_row is None:
                raise PolicyViolation("Lifecycle template current hook/tool generation ister")
            config_digest = str(hook_row[0])
            permission_profile_digest = str(tool_row[0])
            provider = ProviderBindingSnapshot.create(
                realm_id=self.realm.id,
                model_id=binding.model_id,
                provider_ref=binding.provider_ref,
                endpoint_ref=binding.endpoint_ref,
                operation=binding.operation,
                captured_at=plan.prepared_at,
                expires_at=plan.expires_at,
            )
            provider_id, provider_inserted = execution.create_provider_binding(provider)
            sticky = ExecutionEnvironmentSnapshot.create(
                realm_id=self.realm.id,
                environment_id="codex-lifecycle-local",
                execution_identity="codex:lifecycle",
                provider="local-control-plane",
                platform=platform.system().casefold(),
                executor_protocol_version="zekam-client-lifecycle/v1",
                cwd_locator="workspace:zekam",
                workspace_roots=("workspace:zekam",),
                shell=ShellSnapshot(
                    kind="powershell",
                    binary_digest=digest("powershell-runtime"),
                    startup_profile_digest=digest("profile-not-loaded"),
                ),
                permission_profile_id="current-builder-tool-set",
                permission_profile_digest=permission_profile_digest,
                filesystem_policy_digest=digest("exact-source-root-only"),
                network_policy_digest=digest("remote-calls-default-deny"),
                tool_runtime_digest=digest("zekam-client-lifecycle-tools/v1"),
                capability_digest=target.capability_digest,
                config_effective_digest=config_digest,
                source_revision=plan.source_revision,
                captured_at=plan.prepared_at,
                expires_at=plan.expires_at,
            )
            sticky_id, sticky_inserted = execution.create_environment_snapshot(sticky)
            current_env = reprobe_snapshot(
                sticky,
                captured_at=plan.prepared_at + dt.timedelta(microseconds=1),
                expires_at=plan.expires_at,
            )
            current_id, current_inserted = execution.create_environment_snapshot(current_env)
            probe_id, probe_inserted = execution.record_environment_probe(
                detect_environment_drift(sticky, current_env, checked_at=current_env.captured_at)
            )

        template = LifecycleRuntimeTemplateRepository(
            self.connection, self.realm.id
        ).current(plan.project_id, plan.source_revision, plan.policy_digest)
        return {
            "schema": "zekam-lifecycle-template-prepare-result/v1",
            "applied": True,
            "plan_digest": plan.plan_digest,
            "routing_context_snapshot_id": str(context_id),
            "route_decision_id": str(route_id),
            "execution_target_id": str(target_id),
            "provider_binding_id": str(provider_id),
            "sticky_environment_snapshot_id": str(sticky_id),
            "current_environment_snapshot_id": str(current_id),
            "environment_probe_id": str(probe_id),
            "template_digest": digest(
                {
                    "routing_context": template.routing_context_digest,
                    "route": template.route_decision_digest,
                    "target": template.execution_target_digest,
                    "provider": template.provider_binding_digest,
                    "environment": template.execution_environment_snapshot_digest,
                    "hooks": template.hook_set_digest,
                    "tools": template.compiled_tool_set_digest,
                }
            ),
            "inserted": {
                "context": context_inserted,
                "target": target_inserted,
                "provider": provider_inserted,
                "sticky_environment": sticky_inserted,
                "current_environment": current_inserted,
                "probe": probe_inserted,
            },
            "provider_calls": 0,
            "network_calls": 0,
            "grants_authority": False,
        }
