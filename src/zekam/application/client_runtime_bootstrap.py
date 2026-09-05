"""Reviewed control-plane bootstrap for one pending client lifecycle delivery."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid5

from zekam.application.client_lifecycle_bridge import (
    ClientLifecycleBridge,
    LifecycleClientContract,
    LifecycleDeliveryRepository,
    LifecycleRequest,
)
from zekam.application.client_lifecycle_composition import (
    _EVENT_NAMESPACE,
    _configure_active_memory_hook_runtime,
)
from zekam.application.client_lifecycle_continuity import (
    _HYDRATING_EVENT_TYPES,
    _HYDRATION_NAMESPACE,
    _SESSION_START_HYDRATION_TOKEN_BUDGET,
)
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.execution import ExecutionHost
from zekam.application.governance import DEFAULT_POLICY_NAME, GovernanceService
from zekam.application.hook_runtime import HookRuntime
from zekam.application.legacy_repository_provider import legacy_repository
from zekam.application.lifecycle_runtime_template import template_source_revision
from zekam.application.memory_continuity import (
    HydrationPreparation,
    MemoryContinuityService,
    MemoryContinuityStore,
)
from zekam.application.memory_hooks import memory_hook_bundle
from zekam.application.memory_upgrade import canonical_projection_source_digest
from zekam.application.work_graph import WorkGraphService
from zekam.domain.agents import AgentAssignment, AssignmentRole, AssignmentStatus
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    Checkpoint,
    ContextCandidate,
    JournalEntry,
    compile_context,
)
from zekam.domain.context_fragment import (
    ContextContentKind,
    ContextFragment,
    ContextFragmentSet,
    ContextRole,
    ContextVisibility,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_environment import (
    AssignmentEnvironmentBinding,
    TurnExecutionSnapshot,
)
from zekam.domain.execution_run import (
    CheckpointDisposition,
    ContextPacket,
    ContextPacketSection,
    ExecutionEnvelope,
    ExecutionRun,
)
from zekam.domain.identifiers import new_uuid7
from zekam.domain.realm import ActorKind, LifecycleStatus
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, Job, JobKind
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.security import DataClassification as AuthorizationClassification
from zekam.domain.session_continuity import (
    DataClassification,
    ProjectionGenerationReceipt,
)
from zekam.domain.work import EffectKind, PlanStep, WorkState
from zekam.infrastructure.clients.codex_lifecycle import (
    CODEX_EVENT_MAPPING,
    CODEX_REVIEWED_VERSION,
    codex_lifecycle_descriptor,
    load_codex_contract_evidence,
)

_BOOTSTRAP_STEP_ID = "client-lifecycle-bootstrap"
_ADOPTION_STEP_ID = "client-lifecycle-legacy-adoption"
_LIFECYCLE_STEP_ID = "client-lifecycle-drain"
_CLOSE_STEP_ID = "projection-aware-close"
_CAPABILITY = "client.lifecycle.codex-bootstrap"
_BOOTSTRAP_OPERATION = "client-lifecycle-bootstrap-materialize/v1"
_BOOTSTRAP_ADAPTER_DIGEST = digest("client-lifecycle-bootstrap-materializer/v1")
_ADOPTION_OPERATION = "client-lifecycle-legacy-run-adoption/v1"
_ADOPTION_ADAPTER_DIGEST = digest("client-lifecycle-legacy-run-adoption/v1")


@dataclass(frozen=True, slots=True)
class ClientRuntimeBootstrapPlan:
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    work_revision: int
    work_record_digest: str
    actor_id: UUID
    client_id: str
    session_id: str
    entry_digest: str
    event_type: str
    source_revision: str
    policy_digest: str
    bootstrap_resource: str
    lifecycle_resource: str
    prepared_at: dt.datetime
    rebootstrap: bool
    adopt_existing: bool
    adopted_run_id: UUID | None

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-client-runtime-bootstrap-plan/v1",
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "work_revision": self.work_revision,
            "work_record_digest": self.work_record_digest,
            "actor_id": str(self.actor_id),
            "client_id": self.client_id,
            "session_id": self.session_id,
            "entry_digest": self.entry_digest,
            "event_type": self.event_type,
            "source_revision": self.source_revision,
            "policy_digest": self.policy_digest,
            "bootstrap_resource": self.bootstrap_resource,
            "lifecycle_resource": self.lifecycle_resource,
            "rebootstrap": self.rebootstrap,
            "adopt_existing": self.adopt_existing,
            "adopted_run_id": (None if self.adopted_run_id is None else str(self.adopted_run_id)),
            "adoption_resource": self.adoption_resource,
            "adoption_effect_digest": self.adoption_effect_digest,
            "strategy": "control-plane-only-then-governed-worker-tick",
            "grants_authority": False,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.body())

    @property
    def resource(self) -> str:
        """Backward-compatible name for the child lifecycle resource."""

        return self.lifecycle_resource

    @property
    def adoption_resource(self) -> str | None:
        if self.adopted_run_id is None:
            return None
        return (
            f"work:{self.project_id}:execution-run:{self.work_item_id}:"
            f"{self.adopted_run_id}:legacy-adoption"
        )

    @property
    def adoption_effect_digest(self) -> str | None:
        resource = self.adoption_resource
        if resource is None:
            return None
        return digest(
            {
                "schema": "zekam-client-runtime-legacy-adoption-effect/v1",
                "operation": _ADOPTION_OPERATION,
                "resource": resource,
                "work_item_id": str(self.work_item_id),
                "work_revision": self.work_revision,
                "work_record_digest": self.work_record_digest,
                "adopted_run_id": str(self.adopted_run_id),
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {
            "plan_digest": self.plan_digest,
            "prepared_at": self.prepared_at,
            "applied": False,
        }


@dataclass(frozen=True, slots=True)
class ClientRuntimeBootstrapResult:
    work_item_id: UUID
    task_plan_id: UUID
    run_id: UUID
    coordinator_assignment_id: UUID
    builder_assignment_id: UUID
    verifier_assignment_id: UUID
    bootstrap_assignment_id: UUID
    job_id: UUID
    adoption_job_id: UUID | None = None
    adoption_claim_id: UUID | None = None
    adoption_receipt_id: UUID | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-client-runtime-bootstrap-result/v1",
            "work_item_id": str(self.work_item_id),
            "task_plan_id": str(self.task_plan_id),
            "run_id": str(self.run_id),
            "coordinator_assignment_id": str(self.coordinator_assignment_id),
            "builder_assignment_id": str(self.builder_assignment_id),
            "verifier_assignment_id": str(self.verifier_assignment_id),
            "bootstrap_assignment_id": str(self.bootstrap_assignment_id),
            "job_id": str(self.job_id),
            "adoption_job_id": (
                None if self.adoption_job_id is None else str(self.adoption_job_id)
            ),
            "adoption_claim_id": (
                None if self.adoption_claim_id is None else str(self.adoption_claim_id)
            ),
            "adoption_receipt_id": (
                None if self.adoption_receipt_id is None else str(self.adoption_receipt_id)
            ),
            "applied": True,
            "effect_started": False,
            "next_safe_action": "bind execution envelope and session hydration authority",
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class ClientRuntimeBootstrapService:
    connection: Any
    realm: Any

    def prepare(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        actor_id: UUID,
        client_id: str,
        session_id: str,
        entry_digest: str,
        source_revision: str,
        event_type: str = "session_start",
        rebootstrap: bool = False,
        adopt_existing: bool = False,
        now: dt.datetime | None = None,
    ) -> ClientRuntimeBootstrapPlan:
        moment = now or dt.datetime.now(dt.UTC)
        graph = WorkGraphService(self.connection, self.realm, actor_id=actor_id)
        actor = legacy_repository("actor", self.connection, self.realm.id).get(actor_id)
        if actor.kind is not ActorKind.HUMAN or actor.status is not LifecycleStatus.ACTIVE:
            raise PolicyViolation("Client runtime bootstrap aktif human actor ister")
        work = graph.items.get(work_item_id)
        if rebootstrap and adopt_existing:
            raise PolicyViolation("Re-bootstrap ve legacy adoption ayni anda kullanilamaz")
        expected_state = (
            WorkState.VERIFICATION
            if adopt_existing
            else WorkState.ACTIVE
            if rebootstrap
            else WorkState.PROPOSED
        )
        if work.project_id != project_id or work.state is not expected_state:
            raise PolicyViolation("Client runtime bootstrap exact Work state ister")
        adopted_run_id = None
        if rebootstrap:
            legacy_repository(
                "lifecycle_runtime_template", self.connection, self.realm.id
            ).assert_rebootstrap_admissible(work_item_id)
        elif adopt_existing:
            snapshot = graph.snapshot(work_item_id)
            if (
                event_type != "pre_close"
                or snapshot.plan is None
                or any(not item.verified for item in work.acceptance_criteria)
            ):
                raise PolicyViolation("Legacy adoption verified pre_close Work ister")
            adopted_run_id = legacy_repository(
                "lifecycle_runtime_template", self.connection, self.realm.id
            ).assert_legacy_adoption_admissible(work_item_id, task_plan_id=snapshot.plan.id)
        if not graph.snapshot(work_item_id).is_actionable:
            raise PolicyViolation("Client runtime bootstrap Work actionable degil")
        if client_id != "codex" or not session_id.strip():
            raise PolicyViolation("Client runtime bootstrap reviewed Codex session ister")
        if not event_type.strip():
            raise PolicyViolation("Client runtime bootstrap lifecycle event type ister")
        governance = GovernanceService(self.connection, self.realm, actor_id=actor_id)
        policy = governance.policies.current(DEFAULT_POLICY_NAME)
        if policy is None:
            raise PolicyViolation("Client runtime bootstrap current policy ister")
        bootstrap_resource = f"runtime-bootstrap:{project_id}:{session_id}"
        lifecycle_resource = f"memory:{project_id}:session:{session_id}"
        return ClientRuntimeBootstrapPlan(
            realm_id=self.realm.id,
            project_id=project_id,
            work_item_id=work_item_id,
            work_revision=work.revision,
            work_record_digest=work.record_digest,
            actor_id=actor_id,
            client_id=client_id,
            session_id=session_id,
            entry_digest=entry_digest,
            event_type=event_type,
            source_revision=source_revision,
            policy_digest=policy.policy_digest,
            bootstrap_resource=bootstrap_resource,
            lifecycle_resource=lifecycle_resource,
            prepared_at=moment,
            rebootstrap=rebootstrap,
            adopt_existing=adopt_existing,
            adopted_run_id=adopted_run_id,
        )

    def apply(
        self,
        plan: ClientRuntimeBootstrapPlan,
        *,
        supplied_plan_digest: str,
        current_entry_digest: str,
        current_source_revision: str,
        now: dt.datetime | None = None,
    ) -> ClientRuntimeBootstrapResult:
        moment = now or dt.datetime.now(dt.UTC)
        if supplied_plan_digest != plan.plan_digest:
            raise PolicyViolation("Client runtime bootstrap exact plan digest ister")
        if current_entry_digest != plan.entry_digest:
            raise PolicyViolation("Client runtime bootstrap spool head drift")
        if current_source_revision != plan.source_revision:
            raise PolicyViolation("Client runtime bootstrap source revision drift")

        graph = WorkGraphService(self.connection, self.realm, actor_id=plan.actor_id)
        governance = GovernanceService(self.connection, self.realm, actor_id=plan.actor_id)
        policy = governance.policies.current(DEFAULT_POLICY_NAME)
        if policy is None or policy.policy_digest != plan.policy_digest:
            raise PolicyViolation("Client runtime bootstrap policy drift")
        work = graph.items.get(plan.work_item_id)
        expected_state = (
            WorkState.VERIFICATION
            if plan.adopt_existing
            else WorkState.ACTIVE
            if plan.rebootstrap
            else WorkState.PROPOSED
        )
        if (
            work.project_id != plan.project_id
            or work.state is not expected_state
            or work.revision != plan.work_revision
            or work.record_digest != plan.work_record_digest
            or not graph.snapshot(work.id).is_actionable
        ):
            raise PolicyViolation("Client runtime bootstrap Work state/revision drift")

        assignments = legacy_repository("agent_assignment", self.connection, self.realm.id)
        runs = legacy_repository("execution_run", self.connection, self.realm.id)
        jobs = legacy_repository("job", self.connection, self.realm.id)
        prior_plan = graph.snapshot(work.id).plan
        adoption_job_id = None
        adoption_claim_id = None
        adoption_receipt_id = None
        with self.connection.transaction():
            if plan.rebootstrap or plan.adopt_existing:
                if prior_plan is None:
                    raise PolicyViolation("Lifecycle continuation prior reviewed Plan ister")
                if plan.adopt_existing:
                    adopted_run_id = legacy_repository(
                        "lifecycle_runtime_template", self.connection, self.realm.id
                    ).assert_legacy_adoption_admissible(work.id, task_plan_id=prior_plan.id)
                    if adopted_run_id != plan.adopted_run_id:
                        raise PolicyViolation("Lifecycle legacy adoption run binding drift")
                if plan.rebootstrap:
                    assignments.complete_terminal_plan(prior_plan.id, now=moment)
                else:
                    with self.connection.cursor() as cursor:
                        cursor.execute(
                            "select count(*) from agents.assignment"
                            " where realm_id=%s and plan_id=%s",
                            (self.realm.id, prior_plan.id),
                        )
                        assignment_count = int(cursor.fetchone()[0])
                    if assignment_count:
                        assignments.complete_terminal_plan(prior_plan.id, now=moment)
            if plan.adopt_existing:
                graph.transition(
                    work.id,
                    WorkState.ACTIVE,
                    reason="verified legacy Work governed pre_close adoption",
                    now=moment,
                )
            graph.set_intent(
                work.id,
                goal="Pending Codex lifecycle deliverysini governed worker ile tamamla",
                non_goals=("hook icinde effect", "silent retry", "provider call"),
                outcomes=("terminal lifecycle receipt",),
                constraints=("claim-before-effect", "max-attempts-one"),
                now=moment,
            )
            run_id = new_uuid7(now=moment)
            close_resource = f"work:{plan.project_id}:{work.id}:projection-close:{run_id}"
            adoption_task_plan = None
            if plan.adopt_existing:
                adoption_resource = plan.adoption_resource
                if adoption_resource is None:
                    raise PolicyViolation("Lifecycle legacy adoption resource binding eksik")
                adoption_task_plan = graph.create_plan(
                    work.id,
                    source_revision=plan.source_revision,
                    policy_digest=plan.policy_digest,
                    steps=(
                        PlanStep(
                            step_id=_ADOPTION_STEP_ID,
                            title="Bos legacy run'i receipt ile terminale al",
                            effect=EffectKind.DATABASE_WRITE,
                            logical_resources=(adoption_resource,),
                            risk="high",
                        ),
                    ),
                    now=moment,
                )
            plan_steps = [
                PlanStep(
                    step_id=_BOOTSTRAP_STEP_ID,
                    title="Claim sonrasinda lifecycle child isini materialize et",
                    effect=EffectKind.DATABASE_WRITE,
                    logical_resources=(plan.bootstrap_resource,),
                    risk="high",
                ),
                PlanStep(
                    step_id=_LIFECYCLE_STEP_ID,
                    title="Pending Codex lifecycle deliverysini isle",
                    effect=EffectKind.DATABASE_WRITE,
                    logical_resources=(plan.lifecycle_resource,),
                    risk="high",
                    depends_on=(_BOOTSTRAP_STEP_ID,),
                ),
            ]
            if plan.event_type == "pre_close":
                plan_steps.append(
                    PlanStep(
                        step_id=_CLOSE_STEP_ID,
                        title="Verified Work ve staged pre-close zincirini atomik kapat",
                        effect=EffectKind.DATABASE_WRITE,
                        logical_resources=(close_resource,),
                        risk="high",
                        depends_on=(_LIFECYCLE_STEP_ID,),
                    )
                )
            task_plan = graph.create_plan(
                work.id,
                source_revision=plan.source_revision,
                policy_digest=plan.policy_digest,
                steps=tuple(plan_steps),
                now=moment,
            )
            if not plan.rebootstrap and not plan.adopt_existing:
                graph.transition(work.id, WorkState.READY, now=moment)
                graph.transition(work.id, WorkState.ACTIVE, now=moment)
            elif plan.rebootstrap:
                legacy_repository(
                    "lifecycle_runtime_template", self.connection, self.realm.id
                ).assert_rebootstrap_admissible(work.id)
            run = ExecutionRun.create(
                id=run_id,
                realm_id=self.realm.id,
                project_id=plan.project_id,
                work_item_id=work.id,
                plan_id=task_plan.id,
                client_id=plan.client_id,
                session_id=plan.session_id,
                source_revision=plan.source_revision,
                policy_digest=plan.policy_digest,
                max_input_tokens=4096,
                max_output_tokens=1024,
                max_cost_micros=1,
                deadline=moment + dt.timedelta(minutes=15),
                created_at=moment,
            )
            runs.create_run(run)
            coordinator = _assignment(
                plan=plan,
                task_plan_id=task_plan.id,
                role=AssignmentRole.COORDINATOR,
                agent_ref="client-runtime-coordinator",
                parent=None,
                now=moment,
                step_id=_BOOTSTRAP_STEP_ID,
            )
            adoption_coordinator = None
            adoption_assignment = None
            if plan.adopt_existing:
                adoption_resource = plan.adoption_resource
                if adoption_resource is None:
                    raise PolicyViolation("Lifecycle legacy adoption resource binding eksik")
                if adoption_task_plan is None:
                    raise PolicyViolation("Lifecycle legacy adoption TaskPlan eksik")
                adoption_coordinator = _assignment(
                    plan=plan,
                    task_plan_id=adoption_task_plan.id,
                    role=AssignmentRole.COORDINATOR,
                    agent_ref="client-runtime-legacy-adoption-coordinator",
                    parent=None,
                    now=moment,
                    step_id=_ADOPTION_STEP_ID,
                )
                adoption_assignment = _assignment(
                    plan=plan,
                    task_plan_id=adoption_task_plan.id,
                    role=AssignmentRole.BUILDER,
                    agent_ref="client-runtime-legacy-adoption-worker",
                    parent=adoption_coordinator.id,
                    now=moment,
                    step_id=_ADOPTION_STEP_ID,
                    resource=adoption_resource,
                )
            bootstrap_assignment = _assignment(
                plan=plan,
                task_plan_id=task_plan.id,
                role=AssignmentRole.BUILDER,
                agent_ref="client-runtime-bootstrap-worker",
                parent=coordinator.id,
                now=moment,
                step_id=_BOOTSTRAP_STEP_ID,
                resource=plan.bootstrap_resource,
            )
            builder = _assignment(
                plan=plan,
                task_plan_id=task_plan.id,
                role=AssignmentRole.BUILDER,
                agent_ref="codex-lifecycle-worker",
                parent=coordinator.id,
                now=moment,
                step_id=_LIFECYCLE_STEP_ID,
            )
            bootstrap_verifier = _assignment(
                plan=plan,
                task_plan_id=task_plan.id,
                role=AssignmentRole.VERIFIER,
                agent_ref="client-runtime-bootstrap-verifier",
                parent=coordinator.id,
                now=moment,
                step_id=_BOOTSTRAP_STEP_ID,
                resource=plan.bootstrap_resource,
            )
            verifier = _assignment(
                plan=plan,
                task_plan_id=task_plan.id,
                role=AssignmentRole.VERIFIER,
                agent_ref="client-runtime-verifier",
                parent=coordinator.id,
                now=moment,
                step_id=_LIFECYCLE_STEP_ID,
            )
            close_builder = None
            close_verifier = None
            if plan.event_type == "pre_close":
                close_builder = _assignment(
                    plan=plan,
                    task_plan_id=task_plan.id,
                    role=AssignmentRole.BUILDER,
                    agent_ref="projection-close-worker",
                    parent=coordinator.id,
                    now=moment,
                    step_id=_CLOSE_STEP_ID,
                    resource=close_resource,
                )
                close_verifier = _assignment(
                    plan=plan,
                    task_plan_id=task_plan.id,
                    role=AssignmentRole.VERIFIER,
                    agent_ref="projection-close-verifier",
                    parent=coordinator.id,
                    now=moment,
                    step_id=_CLOSE_STEP_ID,
                    resource=close_resource,
                )
            plan_assignments = [
                coordinator,
                bootstrap_assignment,
                bootstrap_verifier,
                builder,
                verifier,
            ]
            if adoption_coordinator is not None and adoption_assignment is not None:
                plan_assignments.extend((adoption_coordinator, adoption_assignment))
            if close_builder is not None and close_verifier is not None:
                plan_assignments.extend((close_builder, close_verifier))
            for assignment in plan_assignments:
                stored_id, created = assignments.create(assignment)
                if not created or stored_id != assignment.id:
                    raise PolicyViolation("Client runtime bootstrap assignment replay")
            if plan.adopt_existing:
                adoption_resource = plan.adoption_resource
                adoption_effect_digest = plan.adoption_effect_digest
                if (
                    adoption_resource is None
                    or adoption_effect_digest is None
                    or plan.adopted_run_id is None
                    or adoption_assignment is None
                    or adoption_task_plan is None
                ):
                    raise PolicyViolation("Lifecycle legacy adoption effect binding eksik")
                adoption_authorization = Authorization.issue(
                    realm_id=self.realm.id,
                    actor_id=plan.actor_id,
                    work_item_id=work.id,
                    plan_id=adoption_task_plan.id,
                    plan_digest=adoption_task_plan.plan_digest,
                    effect_digest=adoption_effect_digest,
                    scope=AuthorizationScope(
                        allowed_resources=(adoption_resource,),
                        allowed_effects=("database-write",),
                        data_classifications=(AuthorizationClassification.INTERNAL,),
                    ),
                    risk="high",
                    lifetime=dt.timedelta(minutes=15),
                    now=moment,
                )
                authorizations = legacy_repository("authorization", self.connection, self.realm.id)
                authorizations.issue(adoption_authorization)
                adoption_capability = f"client.lifecycle.legacy-adoption.{plan.plan_digest[-16:]}"
                adoption_host = ExecutionHost(
                    self.connection,
                    self.realm.id,
                    worker_label="client-lifecycle-legacy-adoption",
                )
                adoption_job, created = adoption_host.jobs.enqueue(
                    Job.create(
                        realm_id=self.realm.id,
                        project_id=plan.project_id,
                        kind=JobKind.MUTATION,
                        idempotency_key=f"legacy-run-adoption:{plan.plan_digest}",
                        resources=parse_requests(write=(adoption_resource,)),
                        required_capabilities=(adoption_capability,),
                        max_attempts=1,
                        work_item_id=work.id,
                        plan_id=adoption_task_plan.id,
                        step_id=_ADOPTION_STEP_ID,
                        assignment_id=adoption_assignment.id,
                        payload={
                            "schema": "zekam-client-runtime-legacy-adoption-job/v1",
                            "work_item_id": str(work.id),
                            "adopted_run_id": str(plan.adopted_run_id),
                            "plan_digest": plan.plan_digest,
                        },
                        now=moment,
                    )
                )
                if not created:
                    raise PolicyViolation("Lifecycle legacy adoption job replay reddedildi")
                adopted_work = adoption_host.acquire_work(
                    capabilities=(adoption_capability,), now=moment
                )
                if adopted_work is None or adopted_work.job.id != adoption_job.id:
                    raise PolicyViolation("Lifecycle legacy adoption job claim edilemedi")
                adoption_claim = adoption_host.claim_effect(
                    adopted_work,
                    operation=_ADOPTION_OPERATION,
                    effect_digest=adoption_effect_digest,
                    authorization_digest=adoption_authorization.authorization_digest,
                    authorization_id=adoption_authorization.id,
                    resources=parse_requests(write=(adoption_resource,)),
                    adapter_digest=_ADOPTION_ADAPTER_DIGEST,
                    idempotency_key=f"legacy-run-adoption:{plan.plan_digest}",
                    now=moment,
                )
                consumed = authorizations.consume(
                    adoption_authorization.id,
                    effect_digest=adoption_effect_digest,
                    consumed_by="client-runtime-bootstrap:legacy-adoption",
                    now=moment,
                )
                if not consumed.consumed:
                    raise PolicyViolation("Lifecycle legacy adoption authorization tuketilemedi")
                legacy_repository("execution_run", self.connection, self.realm.id).finish_run(
                    plan.adopted_run_id,
                    state="failed",
                    terminal_at=moment,
                )
                adoption_result_digest = digest(
                    {
                        "schema": "zekam-client-runtime-legacy-adoption-result/v1",
                        "work_item_id": str(work.id),
                        "adopted_run_id": str(plan.adopted_run_id),
                        "replacement_run_id": str(run.id),
                        "state": "failed",
                        "plan_digest": plan.plan_digest,
                    }
                )
                adoption_receipt = adoption_host.record_success(
                    adoption_claim,
                    result_digest=adoption_result_digest,
                    adapter_evidence_digest=digest(
                        {
                            "adopted_run_id": str(plan.adopted_run_id),
                            "replacement_run_id": str(run.id),
                            "work_record_digest": plan.work_record_digest,
                        }
                    ),
                    now=moment,
                )
                checkpoint = Checkpoint(
                    checkpoint_id=f"legacy-adoption-{adoption_job.id}",
                    project_id=str(plan.project_id),
                    work_item_id=str(work.id),
                    plan_revision_id=str(adoption_task_plan.id),
                    source_revision=plan.source_revision,
                    plan_steps=adoption_task_plan.execution_order,
                    completed_steps=(_ADOPTION_STEP_ID,),
                    pending_steps=(),
                    step_results=((_ADOPTION_STEP_ID, adoption_result_digest),),
                    context_manifest_digest=_planned_manifest(plan).manifest_digest,
                    journal_head_digest=digest(
                        {
                            "adoption_claim_id": str(adoption_claim.id),
                            "adoption_receipt_id": str(adoption_receipt.id),
                        }
                    ),
                    next_safe_action=_BOOTSTRAP_STEP_ID,
                    created_at=moment,
                )
                legacy_repository(
                    "context_continuity",
                    self.connection,
                    self.realm.id,
                    plan.project_id,
                    work.id,
                ).store_checkpoint(
                    checkpoint,
                    task_plan_id=adoption_task_plan.id,
                    job_id=adoption_job.id,
                )
                if not adoption_host.finish(
                    adopted_work,
                    outcome=AttemptOutcome.SUCCEEDED,
                    result_digest=adoption_result_digest,
                    now=moment,
                ):
                    raise PolicyViolation("Lifecycle legacy adoption job kapanmadi")
                assignments.complete_terminal_plan(adoption_task_plan.id, now=moment)
                adoption_job_id = adoption_job.id
                adoption_claim_id = adoption_claim.id
                adoption_receipt_id = adoption_receipt.id
            runs.activate_run(run.id, started_at=moment)
            bootstrap_effect_digest = digest(
                {
                    "schema": "zekam-client-runtime-bootstrap-effect/v1",
                    "operation": _BOOTSTRAP_OPERATION,
                    "entry_digest": plan.entry_digest,
                    "resource": plan.bootstrap_resource,
                    "work_item_id": str(work.id),
                    "task_plan_id": str(task_plan.id),
                    "run_id": str(run.id),
                }
            )
            bootstrap_authorization = Authorization.issue(
                realm_id=self.realm.id,
                actor_id=plan.actor_id,
                work_item_id=work.id,
                plan_id=task_plan.id,
                plan_digest=task_plan.plan_digest,
                effect_digest=bootstrap_effect_digest,
                scope=AuthorizationScope(
                    allowed_resources=(plan.bootstrap_resource,),
                    allowed_effects=("database-write",),
                    data_classifications=(AuthorizationClassification.INTERNAL,),
                ),
                risk="high",
                lifetime=dt.timedelta(minutes=15),
                now=moment,
            )
            legacy_repository("authorization", self.connection, self.realm.id).issue(
                bootstrap_authorization
            )
            bootstrap_payload = {
                "schema": "zekam-codex-lifecycle-bootstrap-job/v1",
                "entry_digest": plan.entry_digest,
                "authorization_id": str(bootstrap_authorization.id),
                "effect_digest": bootstrap_effect_digest,
                "child_assignment_id": str(builder.id),
                "context_created_at": plan.prepared_at.isoformat(),
                "context_manifest_digest": _planned_manifest(plan).manifest_digest,
            }
            if plan.adopt_existing:
                if (
                    adoption_task_plan is None
                    or adoption_job_id is None
                    or adoption_claim_id is None
                    or adoption_receipt_id is None
                ):
                    raise PolicyViolation("Lifecycle legacy adoption terminal evidence eksik")
                bootstrap_payload.update(
                    {
                        "adoption_plan_id": str(adoption_task_plan.id),
                        "adoption_plan_digest": adoption_task_plan.plan_digest,
                        "adoption_job_id": str(adoption_job_id),
                        "adoption_claim_id": str(adoption_claim_id),
                        "adoption_receipt_id": str(adoption_receipt_id),
                        "adoption_result_digest": adoption_result_digest,
                    }
                )
            if close_builder is not None:
                bootstrap_payload["close_assignment_id"] = str(close_builder.id)
            job, created = jobs.enqueue(
                Job.create(
                    realm_id=self.realm.id,
                    project_id=plan.project_id,
                    kind=JobKind.MUTATION,
                    idempotency_key=(
                        f"codex-lifecycle-bootstrap:{plan.entry_digest}:plan:{task_plan.id}"
                        if plan.rebootstrap or plan.adopt_existing
                        else f"codex-lifecycle-bootstrap:{plan.entry_digest}"
                    ),
                    resources=parse_requests(write=(plan.bootstrap_resource,)),
                    required_capabilities=(_CAPABILITY,),
                    max_attempts=1,
                    work_item_id=work.id,
                    plan_id=task_plan.id,
                    step_id=_BOOTSTRAP_STEP_ID,
                    assignment_id=bootstrap_assignment.id,
                    run_id=run.id,
                    payload=bootstrap_payload,
                    now=moment,
                )
            )
            if not created:
                raise PolicyViolation("Client runtime bootstrap job replay")
        return ClientRuntimeBootstrapResult(
            work.id,
            task_plan.id,
            run.id,
            coordinator.id,
            builder.id,
            verifier.id,
            bootstrap_assignment.id,
            job.id,
            adoption_job_id,
            adoption_claim_id,
            adoption_receipt_id,
        )


@dataclass(frozen=True, slots=True)
class _MaterializedChild:
    result_digest: str
    context_manifest_digest: str
    context_manifest_id: UUID
    context_packet_id: UUID
    context_packet_digest: str
    source_revision: str


@dataclass(frozen=True, slots=True)
class ClaimedLifecycleBootstrapService:
    """Materialize one claimed parent into an immutable governed lifecycle child."""

    connection: Any
    realm_id: UUID

    def materialize(
        self,
        work: Any,
        home: Path,
        now: dt.datetime | None = None,
    ) -> str:
        moment = now or dt.datetime.now(dt.UTC)
        job = work.job
        payload = dict(job.payload)
        base_keys = {
            "schema",
            "entry_digest",
            "authorization_id",
            "effect_digest",
            "child_assignment_id",
            "context_created_at",
            "context_manifest_digest",
        }
        adoption_keys = {
            "adoption_plan_id",
            "adoption_plan_digest",
            "adoption_job_id",
            "adoption_claim_id",
            "adoption_receipt_id",
            "adoption_result_digest",
        }
        expected_key_sets = {
            frozenset(base_keys),
            frozenset(base_keys | {"close_assignment_id"}),
            frozenset(base_keys | adoption_keys),
            frozenset(base_keys | {"close_assignment_id"} | adoption_keys),
        }
        if (
            frozenset(payload) not in expected_key_sets
            or payload.get("schema") != "zekam-codex-lifecycle-bootstrap-job/v1"
            or job.kind is not JobKind.MUTATION
            or job.max_attempts != 1
            or job.required_capabilities != (_CAPABILITY,)
        ):
            raise PolicyViolation("Lifecycle bootstrap claimed parent contract drift")
        if any(
            value is None
            for value in (job.work_item_id, job.plan_id, job.assignment_id, job.run_id)
        ):
            raise PolicyViolation("Lifecycle bootstrap parent canonical identity eksik")
        entry_digest = str(payload["entry_digest"])
        spool = ClientLifecycleSpool(home, client_id="codex")
        pending = spool.pending(limit=1)
        if len(pending) != 1 or pending[0].entry_digest != entry_digest:
            raise PolicyViolation("Lifecycle bootstrap exact spool head ister")
        entry = pending[0]
        try:
            context_created_at = dt.datetime.fromisoformat(str(payload["context_created_at"]))
        except ValueError as exc:
            raise PolicyViolation("Lifecycle bootstrap context timestamp drift") from exc
        if context_created_at.tzinfo is None:
            raise PolicyViolation("Lifecycle bootstrap context timestamp timezone ister")
        authorizations = legacy_repository("authorization", self.connection, self.realm_id)
        try:
            authorization_id = UUID(str(payload["authorization_id"]))
            child_assignment_id = UUID(str(payload["child_assignment_id"]))
            close_assignment_id = (
                None
                if payload.get("close_assignment_id") is None
                else UUID(str(payload["close_assignment_id"]))
            )
            adoption_ids = (
                None
                if not adoption_keys.issubset(payload)
                else (
                    UUID(str(payload["adoption_plan_id"])),
                    UUID(str(payload["adoption_job_id"])),
                    UUID(str(payload["adoption_claim_id"])),
                    UUID(str(payload["adoption_receipt_id"])),
                )
            )
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("Lifecycle bootstrap payload UUID drift") from exc
        authorization = authorizations.get(authorization_id)
        effect_digest = str(payload["effect_digest"])
        if (
            authorization.work_item_id != job.work_item_id
            or authorization.plan_id != job.plan_id
            or authorization.effect_digest != effect_digest
            or authorization.scope.allowed_resources
            != tuple(str(request.resource) for request in job.resources)
        ):
            raise PolicyViolation("Lifecycle bootstrap exact authorization drift")
        if adoption_ids is not None:
            self._assert_adoption_evidence(
                job=job,
                adoption_plan_id=adoption_ids[0],
                adoption_plan_digest=str(payload["adoption_plan_digest"]),
                adoption_job_id=adoption_ids[1],
                adoption_claim_id=adoption_ids[2],
                adoption_receipt_id=adoption_ids[3],
                adoption_result_digest=str(payload["adoption_result_digest"]),
            )
        # Defensive revalidation immediately before claim-before-effect.  The
        # worker already checked this before queue claim; this closes direct
        # service callers and rejects any intervening template drift.
        legacy_repository(
            "lifecycle_runtime_template", self.connection, self.realm_id
        ).current_for_bootstrap_job(job.id)
        host = ExecutionHost(self.connection, self.realm_id, worker_label=work.lease.worker_label)
        claim = host.claim_effect(
            work,
            operation=_BOOTSTRAP_OPERATION,
            effect_digest=effect_digest,
            authorization_digest=authorization.authorization_digest,
            resources=job.resources,
            adapter_digest=_BOOTSTRAP_ADAPTER_DIGEST,
            authorization_id=authorization.id,
            idempotency_key=f"bootstrap:{entry_digest}:job:{job.id}",
        )
        with self.connection.transaction():
            materialized = self._materialize_claimed(
                work=work,
                entry=entry,
                child_assignment_id=child_assignment_id,
                close_assignment_id=close_assignment_id,
                authorizations=authorizations,
                context_created_at=context_created_at,
                now=moment,
            )
            receipt = host.record_success(
                claim,
                result_digest=materialized.result_digest,
                adapter_evidence_digest=digest(
                    {
                        "schema": "zekam-client-runtime-bootstrap-adapter-evidence/v1",
                        "entry_digest": entry_digest,
                        "result_digest": materialized.result_digest,
                    }
                ),
                now=moment,
            )
            continuity = legacy_repository(
                "context_continuity",
                self.connection,
                self.realm_id,
                job.project_id,
                job.work_item_id,
            )
            head = continuity.journal_head()
            previous = None if head is None else head[1]
            journal = JournalEntry(
                1 if head is None else head[0] + 1,
                str(job.work_item_id),
                "step-completed",
                materialized.result_digest,
                previous,
                False,
                moment,
            )
            continuity.append_journal(journal, expected_head=previous)
            lifecycle_repository = legacy_repository(
                "client_lifecycle", self.connection, self.realm_id
            )
            facts = legacy_repository(
                "lifecycle_runtime_template", self.connection, self.realm_id
            ).projection_facts(job.project_id, job.work_item_id)
            work_plan_digest = lifecycle_repository.current_work_plan_digest(
                work_item_id=job.work_item_id,
                plan_id=job.plan_id,
            )
            active = lifecycle_repository.current_execution(
                job_id=job.id,
                attempt_id=work.attempt_id,
                lease_id=work.lease.id,
                owner_digest=work.lease.owner_digest,
                fencing_token=work.lease.fencing_token,
                claim_id=claim.id,
                authorization_id=authorization.id,
                effect_plan_digest=authorization.plan_digest,
                work_plan_digest=work_plan_digest,
                effect_digest=effect_digest,
                operation=_BOOTSTRAP_OPERATION,
                adapter_digest=_BOOTSTRAP_ADAPTER_DIGEST,
                claim_digest=claim.claim_digest,
                authorization_digest=authorization.authorization_digest,
                source_digest=str(facts[4]),
                policy_digest=self._policy(job.plan_id),
                migration_digest=str(facts[6]),
                resource=str(job.resources[0].resource),
                session_id=entry.session_id,
                now=moment,
                allow_consumed=True,
            )
            lifecycle_repository.store_job_checkpoint(
                execution=active,
                job_id=job.id,
                step_id=_BOOTSTRAP_STEP_ID,
                result_digest=str(receipt.result_digest),
                now=moment,
                require_lifecycle_admission=False,
            )
            if not host.finish(
                work,
                outcome=AttemptOutcome.SUCCEEDED,
                result_digest=materialized.result_digest,
                now=moment,
            ):
                raise PolicyViolation("Lifecycle bootstrap parent terminal finish reddedildi")
        return materialized.result_digest

    def _assert_adoption_evidence(
        self,
        *,
        job: Job,
        adoption_plan_id: UUID,
        adoption_plan_digest: str,
        adoption_job_id: UUID,
        adoption_claim_id: UUID,
        adoption_receipt_id: UUID,
        adoption_result_digest: str,
    ) -> None:
        """Require the terminal pre-run adoption chain before bootstrap claim."""

        parse_digest(adoption_plan_digest)
        parse_digest(adoption_result_digest)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select adoption.work_item_id,adoption.project_id,adoption.plan_id,"
                " adoption.state,adoption.step_id,plan.plan_digest,claim.id,receipt.id,"
                " receipt.status,receipt.result_digest"
                " from runtime.job adoption join work.task_plan plan"
                " on plan.realm_id=adoption.realm_id and plan.id=adoption.plan_id"
                " join runtime.effect_claim claim on claim.realm_id=adoption.realm_id"
                " and claim.job_id=adoption.id"
                " join runtime.effect_receipt receipt on receipt.realm_id=claim.realm_id"
                " and receipt.claim_id=claim.id where adoption.realm_id=%s"
                " and adoption.id=%s",
                (self.realm_id, adoption_job_id),
            )
            rows = cursor.fetchall()
        if (
            len(rows) != 1
            or job.work_item_id is None
            or UUID(str(rows[0][0])) != job.work_item_id
            or UUID(str(rows[0][1])) != job.project_id
            or UUID(str(rows[0][2])) != adoption_plan_id
            or str(rows[0][3]) != "completed"
            or str(rows[0][4]) != _ADOPTION_STEP_ID
            or str(rows[0][5]) != adoption_plan_digest
            or UUID(str(rows[0][6])) != adoption_claim_id
            or UUID(str(rows[0][7])) != adoption_receipt_id
            or str(rows[0][8]) != "completed"
            or str(rows[0][9]) != adoption_result_digest
        ):
            raise PolicyViolation("Lifecycle bootstrap adoption terminal evidence drift")

    def bind_child_envelope(
        self,
        work: Any,
        now: dt.datetime | None = None,
    ) -> None:
        """Bind the claimed child to the exact context/template materialized by its parent."""

        moment = now or dt.datetime.now(dt.UTC)
        job = work.job
        payload = dict(job.payload)
        payload_keys = set(payload)
        allowed_payload_keys = {
            frozenset({"schema", "authorization_id", "lifecycle_plan_body"}),
            frozenset(
                {
                    "schema",
                    "authorization_id",
                    "hydration_authorization_id",
                    "lifecycle_plan_body",
                }
            ),
        }
        if (
            frozenset(payload_keys) not in allowed_payload_keys
            or payload.get("schema") != "zekam-codex-lifecycle-job/v1"
            or job.kind is not JobKind.MUTATION
            or job.required_capabilities != ("client.lifecycle.codex-drain",)
            or any(
                value is None
                for value in (job.assignment_id, job.run_id, job.work_item_id, job.plan_id)
            )
        ):
            raise PolicyViolation("Lifecycle child immutable materialization payload drift")
        try:
            authorization_id = UUID(str(payload["authorization_id"]))
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("Lifecycle child materialized UUID drift") from exc
        assert job.assignment_id is not None and job.run_id is not None
        repository = legacy_repository("lifecycle_runtime_template", self.connection, self.realm_id)
        run = repository.run_bindings(job.run_id)
        parent_job_id, manifest_id, manifest_digest, packet_id, packet_digest = (
            repository.bootstrap_context(job.run_id)
        )
        source_revision, policy_digest = str(run[0]), str(run[1])
        template = repository.current(
            job.project_id,
            template_source_revision(source_revision),
            policy_digest,
        )
        execution = legacy_repository("execution_run", self.connection, self.realm_id)
        execution.bind_assignment_environment(
            AssignmentEnvironmentBinding.create(
                realm_id=self.realm_id,
                assignment_id=job.assignment_id,
                execution_environment_snapshot_digest=(
                    template.execution_environment_snapshot_digest
                ),
                bound_at=moment,
            )
        )
        turn = TurnExecutionSnapshot.create(
            realm_id=self.realm_id,
            assignment_id=job.assignment_id,
            run_id=job.run_id,
            attempt_id=work.attempt_id,
            client_session_id=str(run[6]),
            turn_id=f"lifecycle-{job.id}",
            model_id=template.model_id,
            provider_id=template.provider_ref,
            route_decision_digest=template.route_decision_digest,
            reasoning_profile_digest=digest("codex-lifecycle-reasoning/v1"),
            execution_environment_snapshot_digest=(template.execution_environment_snapshot_digest),
            context_manifest_digest=manifest_digest,
            exposed_tool_set_digest=template.compiled_tool_set_digest,
            hook_set_digest=template.hook_set_digest,
            config_effective_digest=template.config_effective_digest,
            created_at=moment,
        )
        execution.create_turn_snapshot(turn)
        authorization = legacy_repository("authorization", self.connection, self.realm_id).get(
            authorization_id
        )
        execution.create_envelope(
            ExecutionEnvelope.create(
                realm_id=self.realm_id,
                run_id=job.run_id,
                job_id=job.id,
                attempt_id=work.attempt_id,
                lease_id=work.lease.id,
                fencing_token=work.lease.fencing_token,
                request_ordinal=1,
                idempotency_key=f"codex-lifecycle-envelope:{job.id}",
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
                context_manifest_digest=manifest_digest,
                context_packet_id=packet_id,
                context_packet_digest=packet_digest,
                turn_execution_snapshot_id=turn.id,
                turn_execution_snapshot_digest=turn.turn_snapshot_digest,
                checkpoint_id=None,
                checkpoint_digest=None,
                checkpoint_disposition=CheckpointDisposition.NOT_APPLICABLE_GENESIS,
                source_revision=source_revision,
                policy_digest=policy_digest,
                authorization_scope_digest=digest(authorization.scope.body()),
                output_schema_digest=digest("codex-lifecycle-output/v1"),
                payload_digest=digest(
                    {
                        "parent_job_id": str(parent_job_id),
                        "child_job_id": str(job.id),
                    }
                ),
                max_input_tokens=int(run[2]),
                max_output_tokens=int(run[3]),
                max_cost_micros=int(run[4]),
                deadline=run[5],
                created_at=moment,
            )
        )

    def _materialize_claimed(
        self,
        *,
        work: Any,
        entry: Any,
        child_assignment_id: UUID,
        close_assignment_id: UUID | None,
        authorizations: Any,
        context_created_at: dt.datetime,
        now: dt.datetime,
    ) -> _MaterializedChild:
        job = work.job
        assert job.work_item_id is not None and job.plan_id is not None and job.run_id is not None
        template_repo = legacy_repository(
            "lifecycle_runtime_template", self.connection, self.realm_id
        )
        facts = template_repo.projection_facts(job.project_id, job.work_item_id)
        template_source_revision, source_digest = str(facts[3]), str(facts[4])
        run_bindings = template_repo.run_bindings(job.run_id)
        source_revision = str(run_bindings[0])
        template = template_repo.current(
            job.project_id, template_source_revision, self._policy(job.plan_id)
        )
        candidate, manifest = _materialized_manifest(
            entry_digest=entry.entry_digest,
            work_item_id=job.work_item_id,
            source_revision=source_revision,
            created_at=context_created_at,
        )
        if manifest.manifest_digest != str(job.payload["context_manifest_digest"]):
            raise PolicyViolation("Lifecycle bootstrap planned context manifest drift")
        context_repo = legacy_repository(
            "context_continuity", self.connection, self.realm_id, job.project_id, job.work_item_id
        )
        manifest_id = context_repo.store_manifest(manifest)
        fragment = ContextFragment(
            fragment_id=f"fragment-{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
            content_kind=ContextContentKind.WORK_CONTEXT,
            role=ContextRole.SYSTEM,
            order=0,
            visibility=ContextVisibility.MODEL,
            authority=candidate.authority,
            source_ref=candidate.source_ref,
            source_revision=source_revision,
            content_digest=candidate.content_digest,
            token_count=1,
            required=True,
        )
        context_repo.store_fragment_set(
            ContextFragmentSet(manifest.manifest_digest, (fragment,)), created_at=now
        )
        packet = ContextPacket.create(
            realm_id=self.realm_id,
            project_id=job.project_id,
            work_item_id=job.work_item_id,
            manifest_id=manifest_id,
            manifest_digest=manifest.manifest_digest,
            sections=(ContextPacketSection(candidate.candidate_id, candidate.content_digest, 1),),
            created_at=now,
        )
        legacy_repository("execution_run", self.connection, self.realm_id).create_packet(packet)
        self._store_projection(job=job, facts=facts, source_digest=source_digest, now=now)
        execution = legacy_repository("execution_run", self.connection, self.realm_id)
        legacy_repository("hook_runtime", self.connection, self.realm_id).start_session(
            session_ref=entry.session_id,
            reuse_existing=True,
        )
        execution.bind_assignment_environment(
            AssignmentEnvironmentBinding.create(
                realm_id=self.realm_id,
                assignment_id=job.assignment_id,
                execution_environment_snapshot_digest=(
                    template.execution_environment_snapshot_digest
                ),
                bound_at=now,
            )
        )
        turn = TurnExecutionSnapshot.create(
            realm_id=self.realm_id,
            assignment_id=job.assignment_id,
            run_id=job.run_id,
            attempt_id=work.attempt_id,
            client_session_id=entry.session_id,
            turn_id=f"bootstrap-{entry.delivery_id}",
            model_id=template.model_id,
            provider_id=template.provider_ref,
            route_decision_digest=template.route_decision_digest,
            reasoning_profile_digest=digest("client-runtime-bootstrap-reasoning/v1"),
            execution_environment_snapshot_digest=(template.execution_environment_snapshot_digest),
            context_manifest_digest=manifest.manifest_digest,
            exposed_tool_set_digest=template.compiled_tool_set_digest,
            hook_set_digest=template.hook_set_digest,
            config_effective_digest=template.config_effective_digest,
            created_at=now,
        )
        self._assert_turn_bindings(work=work, turn=turn, now=now)
        execution.create_turn_snapshot(turn)
        parent_authorization = authorizations.get(UUID(str(job.payload["authorization_id"])))
        envelope = ExecutionEnvelope.create(
            realm_id=self.realm_id,
            run_id=job.run_id,
            job_id=job.id,
            attempt_id=work.attempt_id,
            lease_id=work.lease.id,
            fencing_token=work.lease.fencing_token,
            request_ordinal=1,
            idempotency_key=f"bootstrap-envelope:{entry.entry_digest}:job:{job.id}",
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
            source_revision=source_revision,
            policy_digest=template.policy_digest,
            authorization_scope_digest=digest(parent_authorization.scope.body()),
            output_schema_digest=digest("client-runtime-bootstrap-output/v1"),
            payload_digest=entry.observation_digest,
            max_input_tokens=int(run_bindings[2]),
            max_output_tokens=int(run_bindings[3]),
            max_cost_micros=int(run_bindings[4]),
            deadline=run_bindings[5],
            created_at=now,
        )
        execution.create_envelope(envelope)
        child_job_id = new_uuid7(now=now)
        lifecycle_plan, hydration_plan = self._prepare_child_plans(
            job=job,
            child_job_id=child_job_id,
            entry=entry,
            source_revision=source_revision,
            source_digest=source_digest,
            migration_digest=str(facts[6]),
            policy_digest=template.policy_digest,
            packet=packet,
            now=now,
        )
        lifecycle_authorization = Authorization.issue(
            realm_id=self.realm_id,
            actor_id=parent_authorization.actor_id,
            work_item_id=job.work_item_id,
            plan_id=job.plan_id,
            plan_digest=lifecycle_plan.plan_digest,
            effect_digest=lifecycle_plan.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(lifecycle_plan.resource,),
                allowed_effects=("database-write",),
                data_classifications=(AuthorizationClassification.INTERNAL,),
            ),
            risk="high",
            lifetime=dt.timedelta(minutes=10),
            now=now,
        )
        authorizations.issue(lifecycle_authorization)
        hydration_authorization = None
        if hydration_plan is not None:
            hydration_authorization = Authorization.issue(
                realm_id=self.realm_id,
                actor_id=parent_authorization.actor_id,
                work_item_id=job.work_item_id,
                plan_id=job.plan_id,
                plan_digest=hydration_plan.plan_digest,
                effect_digest=hydration_plan.effect_digest,
                scope=AuthorizationScope(
                    allowed_resources=(hydration_plan.resource,),
                    allowed_effects=("database-write",),
                ),
                risk="high",
                lifetime=dt.timedelta(minutes=10),
                now=now,
            )
            authorizations.issue(hydration_authorization)
        child_payload = {
            "schema": "zekam-codex-lifecycle-job/v1",
            "authorization_id": str(lifecycle_authorization.id),
            "lifecycle_plan_body": lifecycle_plan.body(),
        }
        if hydration_authorization is not None:
            child_payload["hydration_authorization_id"] = str(hydration_authorization.id)
        child, child_created = legacy_repository("job", self.connection, self.realm_id).enqueue(
            replace(
                Job.create(
                    realm_id=self.realm_id,
                    project_id=job.project_id,
                    kind=JobKind.MUTATION,
                    idempotency_key=(f"codex-lifecycle:{entry.delivery_id}:parent:{job.id}"),
                    resources=parse_requests(
                        write=(f"memory:{job.project_id}:session:{entry.session_id}",)
                    ),
                    required_capabilities=("client.lifecycle.codex-drain",),
                    max_attempts=1,
                    work_item_id=job.work_item_id,
                    plan_id=job.plan_id,
                    step_id=_LIFECYCLE_STEP_ID,
                    assignment_id=child_assignment_id,
                    run_id=job.run_id,
                    payload=child_payload,
                    now=now,
                ),
                id=child_job_id,
            )
        )
        if not child_created or child.id != child_job_id:
            raise PolicyViolation("Lifecycle child job replay reddedildi")
        close_job_id = None
        if entry.internal_event_type == "pre_close":
            if close_assignment_id is None:
                raise PolicyViolation("Pre-close projection close assignment ister")
            close_resource = (
                f"work:{job.project_id}:{job.work_item_id}:projection-close:{job.run_id}"
            )
            close_job_id = new_uuid7(now=now)
            close_job, close_created = legacy_repository(
                "job", self.connection, self.realm_id
            ).enqueue(
                replace(
                    Job.create(
                        realm_id=self.realm_id,
                        project_id=job.project_id,
                        kind=JobKind.MUTATION,
                        idempotency_key=(f"projection-close:{entry.delivery_id}:parent:{job.id}"),
                        resources=parse_requests(write=(close_resource,)),
                        required_capabilities=("client.lifecycle.projection-close",),
                        max_attempts=1,
                        work_item_id=job.work_item_id,
                        plan_id=job.plan_id,
                        step_id=_CLOSE_STEP_ID,
                        assignment_id=close_assignment_id,
                        run_id=job.run_id,
                        payload={
                            "schema": "zekam-projection-close-job/v1",
                            "source_authorization_id": str(parent_authorization.id),
                            "lifecycle_job_id": str(child.id),
                            "entry_digest": entry.entry_digest,
                        },
                        now=now,
                    ),
                    id=close_job_id,
                )
            )
            if not close_created or close_job.id != close_job_id:
                raise PolicyViolation("Projection close child job replay reddedildi")
        elif close_assignment_id is not None:
            raise PolicyViolation("Non-close lifecycle close assignment tasiyamaz")
        result_body = {
            "schema": "zekam-client-runtime-bootstrap-materialized/v1",
            "parent_job_id": str(job.id),
            "entry_digest": entry.entry_digest,
            "manifest_id": str(manifest_id),
            "manifest_digest": manifest.manifest_digest,
            "packet_id": str(packet.id),
            "packet_digest": packet.packet_digest,
            "child_assignment_id": str(child_assignment_id),
            "child_job_id": str(child.id),
            "close_job_id": None if close_job_id is None else str(close_job_id),
            "lifecycle_authorization_id": str(lifecycle_authorization.id),
            "hydration_authorization_id": (
                None if hydration_authorization is None else str(hydration_authorization.id)
            ),
            "template_digest": digest(
                {name: getattr(template, name) for name in template.__dataclass_fields__}
            ),
            "authorizer_count": len(authorizations.list_active(now=now)),
        }
        return _MaterializedChild(
            result_digest=digest(result_body),
            context_manifest_digest=manifest.manifest_digest,
            context_manifest_id=manifest_id,
            context_packet_id=packet.id,
            context_packet_digest=packet.packet_digest,
            source_revision=source_revision,
        )

    def _assert_turn_bindings(
        self, *, work: Any, turn: TurnExecutionSnapshot, now: dt.datetime
    ) -> None:
        """Expose the exact fail-closed turn prerequisites before the DB trigger."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select a.status,a.context_manifest_digest=%s,r.state,ja.outcome,"
                " row(j.project_id,j.work_item_id,j.run_id,j.assignment_id)="
                " row(a.project_id,a.work_item_id,%s,%s),"
                " env.config_effective_digest=%s,env.expires_at>%s,"
                " exists(select 1 from agents.assignment_environment_binding b"
                " where b.realm_id=%s and b.assignment_id=%s"
                " and b.execution_environment_snapshot_digest=%s),"
                " exists(select 1 from runtime.environment_probe_evidence p"
                " join runtime.execution_environment_snapshot current_env"
                " on current_env.realm_id=p.realm_id"
                " and current_env.snapshot_digest=p.current_snapshot_digest"
                " where p.realm_id=%s and p.sticky_snapshot_digest=%s"
                " and cardinality(p.drift_dimensions)=0 and p.checked_at<=%s"
                " and p.checked_at>=%s-interval '5 minutes' and current_env.expires_at>%s)"
                " from agents.assignment a join runtime.execution_run r"
                " on r.realm_id=a.realm_id and r.id=%s"
                " join runtime.job_attempt ja on ja.realm_id=a.realm_id and ja.id=%s"
                " join runtime.job j on j.realm_id=ja.realm_id and j.id=ja.job_id"
                " join runtime.execution_environment_snapshot env on env.realm_id=a.realm_id"
                " and env.snapshot_digest=%s where a.realm_id=%s and a.id=%s",
                (
                    turn.context_manifest_digest,
                    turn.run_id,
                    turn.assignment_id,
                    turn.config_effective_digest,
                    now,
                    self.realm_id,
                    turn.assignment_id,
                    turn.execution_environment_snapshot_digest,
                    self.realm_id,
                    turn.execution_environment_snapshot_digest,
                    now,
                    now,
                    now,
                    turn.run_id,
                    work.attempt_id,
                    turn.execution_environment_snapshot_digest,
                    self.realm_id,
                    turn.assignment_id,
                ),
            )
            row = cursor.fetchone()
        if row is None or row != (
            "active",
            True,
            "active",
            None,
            True,
            True,
            True,
            True,
            True,
        ):
            raise PolicyViolation(f"Lifecycle bootstrap turn binding drift: {row!r}")

    def _prepare_child_plans(
        self,
        *,
        job: Job,
        child_job_id: UUID,
        entry: Any,
        source_revision: str,
        source_digest: str,
        migration_digest: str,
        policy_digest: str,
        packet: ContextPacket,
        now: dt.datetime,
    ) -> tuple[Any, Any | None]:
        if job.work_item_id is None or job.plan_id is None or job.run_id is None:
            raise PolicyViolation("Lifecycle child plan parent identity eksik")
        runtime = HookRuntime(max_workers=1)
        repository = legacy_repository("client_lifecycle", self.connection, self.realm_id)
        _configure_active_memory_hook_runtime(
            runtime, memory_hook_bundle(self.realm_id), repository, now=now
        )
        continuity = legacy_repository("memory_continuity", self.connection, self.realm_id)
        authorizations = legacy_repository("authorization", self.connection, self.realm_id)
        bridge = ClientLifecycleBridge(
            runtime,
            cast(LifecycleDeliveryRepository, continuity),
            authorizations,
            legacy_repository("hook_runtime", self.connection, self.realm_id),
        )
        evidence = load_codex_contract_evidence(
            Path(__file__).resolve().parents[3]
            / "config"
            / "client-lifecycle"
            / "codex-0.150.1.json"
        )
        contract = LifecycleClientContract.verified(
            descriptor=codex_lifecycle_descriptor(
                "codex", installed_version=CODEX_REVIEWED_VERSION
            ),
            installed_version=CODEX_REVIEWED_VERSION,
            event_mapping=CODEX_EVENT_MAPPING,
            contract_evidence_digest=str(evidence["file_digest"]),
        )
        request = LifecycleRequest(
            realm_id=self.realm_id,
            project_id=job.project_id,
            work_item_id=job.work_item_id,
            run_id=job.run_id,
            session_id=entry.session_id,
            client_id=entry.client_id,
            event_id=uuid5(_EVENT_NAMESPACE, entry.entry_digest),
            external_event_type=entry.external_event_type,
            sequence=entry.sequence,
            previous_digest=repository.previous_continuity_digest(
                client_id=entry.client_id,
                session_id=entry.session_id,
                sequence=entry.sequence,
            ),
            origin=f"client:{entry.client_id}",
            causation_id=f"delivery:{entry.delivery_id}",
            correlation_id=f"job:{child_job_id}",
            recursion_depth=0,
            max_recursion_depth=3,
            source_revision=source_revision,
            work_plan_ref=f"work-plan:{job.plan_id}",
            checkpoint_ref=None,
            context_ref=f"context-packet:{packet.id}",
            metadata=(),
            classification=DataClassification.INTERNAL,
            payload=entry.observation,
            idempotency_key=entry.delivery_id,
            occurred_at=entry.occurred_at,
            ingested_at=entry.occurred_at,
        )
        lifecycle_plan = bridge.prepare(
            request,
            contract,
            runtime.start_session(),
            source_digest=source_digest,
            policy_digest=policy_digest,
            migration_digest=migration_digest,
        )
        if entry.internal_event_type not in _HYDRATING_EVENT_TYPES:
            return lifecycle_plan, None
        inventory = continuity.preview_hydration_inventory(
            project_id=job.project_id,
            work_item_id=job.work_item_id,
            run_id=job.run_id,
            session_id=entry.session_id,
            client_id=entry.client_id,
            event_type=entry.internal_event_type,
            event_body=lifecycle_plan.event.body(),
            event_digest=lifecycle_plan.event.event_digest,
        )
        hydration_plan = MemoryContinuityService(
            cast(MemoryContinuityStore, continuity), authorizations
        ).prepare_from_inventory(
            HydrationPreparation(
                receipt_id=uuid5(
                    _HYDRATION_NAMESPACE,
                    f"{self.realm_id}:{lifecycle_plan.event.event_digest}",
                ),
                realm_id=self.realm_id,
                project_id=job.project_id,
                work_item_id=job.work_item_id,
                run_id=job.run_id,
                session_id=entry.session_id,
                client_id=entry.client_id,
                token_budget=_SESSION_START_HYDRATION_TOKEN_BUDGET,
                idempotency_key=(
                    f"{entry.internal_event_type.replace('_', '-')}:"
                    f"{lifecycle_plan.event.event_id}:hydration"
                ),
                created_at=entry.occurred_at,
            ),
            inventory,
        )
        return lifecycle_plan, hydration_plan

    def _policy(self, plan_id: UUID) -> str:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select policy_digest from work.task_plan where realm_id=%s and id=%s",
                (self.realm_id, plan_id),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Lifecycle bootstrap TaskPlan policy exact degil")
        return str(rows[0][0])

    def _store_projection(
        self, *, job: Job, facts: tuple[Any, ...], source_digest: str, now: dt.datetime
    ) -> None:
        revision, state, record_digest, source_revision, _, migration_head, _ = facts
        if job.work_item_id is None:
            raise PolicyViolation("Lifecycle bootstrap projection Work identity eksik")
        database_revision_digest = digest(
            {
                "project_id": str(job.project_id),
                "work_item_id": str(job.work_item_id),
                "work_revision": int(revision),
                "work_state": str(state),
                "work_record_digest": str(record_digest),
            }
        )
        projection_source_digest = canonical_projection_source_digest(
            source_head=str(source_revision),
            source_tree_digest=source_digest,
            migration_head=int(migration_head),
            database_revision_digest=database_revision_digest,
        )
        body = {
            "schema": "zekam-memory-continuity-public-projection/v1",
            "project_id": str(job.project_id),
            "work_item_id": str(job.work_item_id),
            "work_revision": int(revision),
            "work_state": str(state),
            "source_head": str(source_revision),
            "source_tree_digest": source_digest,
            "migration_head": int(migration_head),
            "database_revision_digest": database_revision_digest,
            "source_digest": projection_source_digest,
            "classification": "public",
            "public_filtered": True,
            "content_included": False,
            "fresh": True,
            "read_only": True,
            "grants_authority": False,
        }
        receipt = ProjectionGenerationReceipt(
            receipt_id=new_uuid7(now=now),
            realm_id=self.realm_id,
            project_id=job.project_id,
            work_item_id=job.work_item_id,
            source_ref=f"work-item/{job.work_item_id}/revision/{revision}",
            source_digest=projection_source_digest,
            projection_ref="projection/active-work",
            projection_digest=digest(body),
            generator_version="memory-continuity-shadow/v1",
            generated_at=now,
        )
        legacy_repository(
            "memory_continuity", self.connection, self.realm_id
        ).store_projection_receipt(
            receipt, idempotency_key=f"bootstrap:{job.id}:active-work-projection"
        )


def _assignment(
    *,
    plan: ClientRuntimeBootstrapPlan,
    task_plan_id: UUID,
    role: AssignmentRole,
    agent_ref: str,
    parent: UUID | None,
    now: dt.datetime,
    step_id: str,
    write: bool = True,
    resource: str | None = None,
) -> AgentAssignment:
    assignment_id = new_uuid7(now=now)
    assigned_resource = resource or plan.lifecycle_resource
    write_resources = (assigned_resource,) if role is AssignmentRole.BUILDER and write else ()
    read_resources = (assigned_resource,) if role is AssignmentRole.VERIFIER else ()
    draft = AgentAssignment(
        id=assignment_id,
        realm_id=plan.realm_id,
        project_id=plan.project_id,
        work_item_id=plan.work_item_id,
        plan_id=task_plan_id,
        step_id=step_id,
        parent_assignment_id=parent,
        role=role,
        agent_ref=agent_ref,
        risk="high" if role is AssignmentRole.BUILDER else "low",
        instruction_digest=digest({"role": role.value, "entry": plan.entry_digest}),
        context_manifest_digest=_planned_manifest(plan).manifest_digest,
        assignment_digest=digest("placeholder"),
        status=AssignmentStatus.ACTIVE,
        read_resources=read_resources,
        write_resources=write_resources,
        created_at=now,
    )
    return AgentAssignment(
        **{
            **{name: getattr(draft, name) for name in draft.__dataclass_fields__},
            "assignment_digest": digest(draft.identity_body()),
        }
    )


def _materialized_manifest(
    *,
    entry_digest: str,
    work_item_id: UUID,
    source_revision: str,
    created_at: dt.datetime,
) -> tuple[ContextCandidate, Any]:
    candidate = ContextCandidate(
        candidate_id=f"bootstrap-{entry_digest.removeprefix('sha256:')[:24]}",
        authority=AuthorityLevel.CANONICAL,
        observed_at=created_at,
        source_revision=source_revision,
        content_digest=entry_digest,
        token_count=1,
        required=True,
        source_ref=f"work/{work_item_id}",
    )
    return candidate, compile_context(
        (candidate,),
        token_budget=64,
        minimum_authority=AuthorityLevel.CANONICAL,
        now=created_at,
    )


def _planned_manifest(plan: ClientRuntimeBootstrapPlan) -> Any:
    return _materialized_manifest(
        entry_digest=plan.entry_digest,
        work_item_id=plan.work_item_id,
        source_revision=plan.source_revision,
        created_at=plan.prepared_at,
    )[1]
