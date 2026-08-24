"""Mutation boundary for exact, authorization-bound resume dispatch."""

from __future__ import annotations

import datetime as dt
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from zekam.application.agent_dispatch import CanonicalAgentDispatchService
from zekam.application.environment_snapshot_service import EnvironmentEffectGuard
from zekam.application.execution import ExecutionHost
from zekam.application.governance import EffectRequest, GovernanceService
from zekam.application.resume_coordinator import ResumeCoordinator
from zekam.domain.agents import AgentInvocation, AssignmentStatus
from zekam.domain.canonical import digest
from zekam.domain.checkpoint_v2 import SandboxBindingV2, SandboxDisposition
from zekam.domain.clients import DispatchRequest
from zekam.domain.errors import PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.resources import parse_requests
from zekam.domain.resume import ResumeDisposition
from zekam.domain.resume_apply import (
    ResumeApplyEvent,
    ResumeApplyPhase,
    ResumeApplyRequest,
    ResumeApplyResult,
    ResumeApplyState,
)
from zekam.domain.runtime import AttemptOutcome, FailureCategory
from zekam.domain.work import EffectKind
from zekam.infrastructure.clients.adapters import ClientAdapter
from zekam.infrastructure.postgres.agent_assignment_repository import (
    AgentAssignmentRepository,
)
from zekam.infrastructure.postgres.resume_apply_repository import ResumeApplyRepository
from zekam.infrastructure.postgres.resume_repository import ResumeRepository
from zekam.infrastructure.postgres.runtime_repository import JobRepository


def _result(plan_digest: str, event: ResumeApplyEvent) -> ResumeApplyResult:
    return ResumeApplyResult(
        apply_id=event.apply_id,
        state=event.state,
        plan_digest=plan_digest,
        event_digest=event.event_digest,
        attempt_id=event.attempt_id,
        lease_id=event.lease_id,
        fencing_token=event.fencing_token,
        claim_id=event.claim_id,
        receipt_id=event.receipt_id,
        result_digest=event.result_digest,
        reprepare_required=event.state is ResumeApplyState.RECOVERY_REQUIRED,
    )


class SandboxBindingGuard(Protocol):
    def assert_checkpoint_binding(self, binding: SandboxBindingV2) -> None: ...

    def hold_checkpoint_binding(
        self, binding: SandboxBindingV2
    ) -> AbstractContextManager[None]: ...


@dataclass(frozen=True, slots=True)
class ResumeApplyService:
    """Revalidate, consume one-shot authority, reacquire and dispatch exactly once."""

    connection: Any
    governance: GovernanceService
    environment_guard: EnvironmentEffectGuard | None = None
    sandbox_binding_guard: SandboxBindingGuard | None = None

    def apply(
        self,
        request: ResumeApplyRequest,
        adapter: ClientAdapter,
        *,
        cwd: Path,
        timeout_seconds: int,
        now: dt.datetime | None = None,
    ) -> ResumeApplyResult:
        moment = now or dt.datetime.now(dt.UTC)
        plan = request.plan
        if moment.tzinfo is None:
            raise PolicyViolation("Resume apply zamani timezone-aware olmali")
        if not 1 <= timeout_seconds <= 3600:
            raise PolicyViolation("Resume apply timeout 1..3600 olmali")
        if moment >= plan.valid_until:
            raise PolicyViolation("Resume apply plan gecerlilik penceresi doldu")
        if adapter.descriptor.client_id != plan.target_client_id:
            raise PolicyViolation("Resume apply target client drift")
        if plan.disposition is not ResumeDisposition.SAFE_CONTINUE:
            raise PolicyViolation("Resume apply yalniz fresh safe-continue plan uygular")

        effect = EffectRequest(
            action="resume.apply.next-step",
            effects=(EffectKind.DATABASE_WRITE, EffectKind.PROCESS_RUN),
            resources=tuple(
                sorted(set(plan.logical_read_resources + plan.logical_write_resources))
            ),
            required_capabilities=("database.write", "process.run"),
        )
        repository = ResumeApplyRepository(self.connection, plan.realm_id)
        assignments = AgentAssignmentRepository(self.connection, plan.realm_id)
        host = ExecutionHost(self.connection, plan.realm_id, worker_label=request.worker_label)

        with self.connection.transaction():
            repository.lock_work(plan.work_item_id)
            replay_id = repository.find_exact(
                plan,
                actor_id=request.actor_id,
                authorization_id=request.authorization_id,
                effect_digest=effect.effect_digest,
            )
            if replay_id is not None:
                replay = repository.latest_event(replay_id)
                if replay is None:
                    raise PolicyViolation("Resume apply replay event kaniti eksik")
                if replay.state in {ResumeApplyState.CLAIMED, ResumeApplyState.DISPATCHED}:
                    if repository.lease_is_live(replay, now=moment):
                        return _result(plan.plan_digest, replay)
                    replay = repository.recover_interrupted(replay, now=moment)
                return _result(plan.plan_digest, replay)
            fresh = ResumeCoordinator(
                ResumeRepository(self.connection, plan.realm_id, manage_transaction=False)
            ).prepare(
                plan.work_item_id,
                client_id=plan.target_client_id,
                observed_at=plan.observed_at,
            )
            if fresh.plan_digest != request.supplied_plan_digest:
                raise PolicyViolation("Resume apply exact plan revalidation drift")
            if plan.sandbox.disposition is not SandboxDisposition.NOT_APPLICABLE:
                if self.sandbox_binding_guard is None:
                    raise PolicyViolation("Resume apply sandbox live binding guard ister")
                self.sandbox_binding_guard.assert_checkpoint_binding(plan.sandbox)
            if self.environment_guard is None:
                raise PolicyViolation("Resume apply live environment force probe ister")
            self.environment_guard.assert_envelope_current(
                plan.runtime.execution_envelope_id, now=moment
            )

            apply_id, created = repository.create(
                plan,
                actor_id=request.actor_id,
                authorization_id=request.authorization_id,
                effect_digest=effect.effect_digest,
                now=moment,
            )
            if not created:
                replay = repository.latest_event(apply_id)
                if replay is None:
                    raise PolicyViolation("Resume apply replay event kaniti eksik")
                return _result(plan.plan_digest, replay)

            authorization = self.governance.authorizations.get(request.authorization_id)
            if authorization.actor_id != request.actor_id:
                raise PolicyViolation("Resume apply authorization actor drift")
            if authorization.plan_id is None:
                raise PolicyViolation("Resume apply authorization exact task plan ister")
            consumed = self.governance.require_authorized(
                effect,
                authorization=authorization,
                consumed_by=request.worker_label,
                now=moment,
            )
            claimed = JobRepository(self.connection, plan.realm_id).claim_exact(
                plan.runtime.job_id,
                project_id=plan.project_id,
                work_item_id=plan.work_item_id,
                plan_id=authorization.plan_id,
                step_id=str(plan.next_step_id),
                assignment_id=plan.runtime.assignment_id,
                run_id=plan.runtime.run_id,
                capabilities=request.capabilities,
                worker_label=request.worker_label,
                lease_seconds=request.lease_seconds,
                now=moment,
            )
            assignment = assignments.get(plan.runtime.assignment_id)
            if (
                assignment.project_id != plan.project_id
                or assignment.work_item_id != plan.work_item_id
                or assignment.plan_id != claimed.job.plan_id
                or assignment.step_id != plan.next_step_id
                or assignment.status not in {AssignmentStatus.READY, AssignmentStatus.ACTIVE}
                or not assignment.is_child
            ):
                raise PolicyViolation("Resume apply assignment scope/state drift")
            if plan.sandbox.disposition is not SandboxDisposition.NOT_APPLICABLE:
                if self.sandbox_binding_guard is None:
                    raise PolicyViolation("Resume apply sandbox live binding guard ister")
                self.sandbox_binding_guard.assert_checkpoint_binding(plan.sandbox)

            invocation_id = new_uuid7(now=moment)
            execution_identity = (
                f"resume:{apply_id}:{claimed.attempt_id}:{claimed.lease.fencing_token}"
            )
            invocation = AgentInvocation(
                id=invocation_id,
                realm_id=plan.realm_id,
                assignment_id=assignment.id,
                client_id=plan.target_client_id,
                execution_identity=execution_identity,
                invocation_digest=digest(
                    {
                        "id": str(invocation_id),
                        "realm_id": str(plan.realm_id),
                        "assignment_id": str(assignment.id),
                        "client_id": plan.target_client_id,
                        "execution_identity": execution_identity,
                    }
                ),
                created_at=moment,
            )
            assignments.record_invocation(invocation)
            dispatch_request = DispatchRequest(
                assignment_id=assignment.id,
                invocation_id=invocation.id,
                client_id=invocation.client_id,
                role=assignment.role.value,
                instruction_digest=assignment.instruction_digest,
                context_manifest_digest=assignment.context_manifest_digest,
                timeout_seconds=timeout_seconds,
            )
            envelope = repository.clone_envelope(
                plan,
                apply_id=apply_id,
                attempt_id=claimed.attempt_id,
                lease_id=claimed.lease.id,
                fencing_token=claimed.lease.fencing_token,
                authorization_scope_digest=digest(consumed.scope.body()),
                payload_digest=digest(dispatch_request.as_dict()),
                now=moment,
            )
            claim = host.claim_effect(
                claimed,
                operation="resume.dispatch-next-step",
                effect_digest=effect.effect_digest,
                authorization_digest=consumed.authorization_digest,
                resources=parse_requests(plan.logical_read_resources, plan.logical_write_resources),
                adapter_digest=adapter.descriptor.descriptor_digest,
                authorization_id=consumed.id,
                idempotency_key=f"resume:{apply_id}:dispatch",
                now=moment,
            )
            claimed_event = ResumeApplyEvent(
                apply_id=apply_id,
                sequence=1,
                phase=ResumeApplyPhase.CLAIM,
                state=ResumeApplyState.CLAIMED,
                reason_code="resume.exact-claim-created",
                occurred_at=moment,
                attempt_id=claimed.attempt_id,
                lease_id=claimed.lease.id,
                fencing_token=claimed.lease.fencing_token,
                claim_id=claim.id,
            )
            repository.append_event(claimed_event)

        dispatched_at = dt.datetime.now(dt.UTC)
        dispatched_event = ResumeApplyEvent(
            apply_id=apply_id,
            sequence=2,
            phase=ResumeApplyPhase.DISPATCH,
            state=ResumeApplyState.DISPATCHED,
            reason_code="resume.adapter-dispatch-started",
            occurred_at=dispatched_at,
            attempt_id=claimed.attempt_id,
            lease_id=claimed.lease.id,
            fencing_token=claimed.lease.fencing_token,
            claim_id=claim.id,
            previous_digest=claimed_event.event_digest,
        )
        with self.connection.transaction():
            repository.append_event(dispatched_event)

        guard_context: AbstractContextManager[None] = nullcontext()
        live_sandbox_guard = self.sandbox_binding_guard
        if plan.sandbox.disposition is not SandboxDisposition.NOT_APPLICABLE:
            if live_sandbox_guard is None:
                raise PolicyViolation("Resume apply sandbox live binding guard ister")
            guard_context = live_sandbox_guard.hold_checkpoint_binding(plan.sandbox)
        try:
            with guard_context:
                if plan.sandbox.disposition is not SandboxDisposition.NOT_APPLICABLE:
                    if live_sandbox_guard is None:
                        raise PolicyViolation("Resume apply sandbox live binding guard ister")
                    live_sandbox_guard.assert_checkpoint_binding(plan.sandbox)
                dispatch = CanonicalAgentDispatchService(assignments).dispatch(
                    assignment,
                    invocation,
                    adapter,
                    cwd=cwd,
                    timeout_seconds=timeout_seconds,
                )
        except Exception as dispatch_error:
            recovery_event = ResumeApplyEvent(
                apply_id=apply_id,
                sequence=3,
                phase=ResumeApplyPhase.DISPATCH,
                state=ResumeApplyState.RECOVERY_REQUIRED,
                reason_code="resume.adapter-outcome-unknown",
                occurred_at=dt.datetime.now(dt.UTC),
                attempt_id=claimed.attempt_id,
                lease_id=claimed.lease.id,
                fencing_token=claimed.lease.fencing_token,
                claim_id=claim.id,
                previous_digest=dispatched_event.event_digest,
            )
            with self.connection.transaction():
                repository.append_event(recovery_event)
                if not host.finish(
                    claimed,
                    outcome=AttemptOutcome.RECOVERY_REQUIRED,
                    failure_category=FailureCategory.ADAPTER,
                    now=recovery_event.occurred_at,
                ):
                    raise PolicyViolation(
                        "Resume apply recovery transition fence drift"
                    ) from dispatch_error
            return _result(plan.plan_digest, recovery_event)

        terminal_at = dt.datetime.now(dt.UTC)
        if dispatch.is_success:
            receipt = host.record_success(
                claim,
                result_digest=dispatch.result_digest,
                adapter_evidence_digest=dispatch.result_digest,
                now=terminal_at,
            )
            terminal_state = ResumeApplyState.COMPLETED
            reason_code = "resume.dispatch-receipt-completed"
            result_digest = dispatch.result_digest
        else:
            receipt = host.record_failure(
                claim,
                category=FailureCategory.ADAPTER,
                failure_digest=dispatch.result_digest,
                now=terminal_at,
            )
            terminal_state = ResumeApplyState.FAILED
            reason_code = "resume.dispatch-receipt-failed"
            result_digest = None

        terminal_event = ResumeApplyEvent(
            apply_id=apply_id,
            sequence=3,
            phase=ResumeApplyPhase.TERMINAL,
            state=terminal_state,
            reason_code=reason_code,
            occurred_at=terminal_at,
            attempt_id=claimed.attempt_id,
            lease_id=claimed.lease.id,
            fencing_token=claimed.lease.fencing_token,
            claim_id=claim.id,
            receipt_id=receipt.id,
            result_digest=result_digest,
            previous_digest=dispatched_event.event_digest,
        )
        with self.connection.transaction():
            if terminal_state is ResumeApplyState.COMPLETED:
                try:
                    repository.store_result_checkpoint(
                        plan,
                        envelope=envelope,
                        attempt_id=claimed.attempt_id,
                        lease_id=claimed.lease.id,
                        fencing_token=claimed.lease.fencing_token,
                        receipt_id=receipt.id,
                        result_digest=dispatch.result_digest,
                        now=terminal_at,
                    )
                except PolicyViolation as checkpoint_error:
                    recovery_event = ResumeApplyEvent(
                        apply_id=apply_id,
                        sequence=3,
                        phase=ResumeApplyPhase.DISPATCH,
                        state=ResumeApplyState.RECOVERY_REQUIRED,
                        reason_code="resume.post-result-checkpoint-gate-failed",
                        occurred_at=terminal_at,
                        attempt_id=claimed.attempt_id,
                        lease_id=claimed.lease.id,
                        fencing_token=claimed.lease.fencing_token,
                        claim_id=claim.id,
                        previous_digest=dispatched_event.event_digest,
                    )
                    repository.append_event(recovery_event)
                    if not host.finish(
                        claimed,
                        outcome=AttemptOutcome.RECOVERY_REQUIRED,
                        failure_category=FailureCategory.POLICY,
                        now=terminal_at,
                    ):
                        raise PolicyViolation(
                            "Resume checkpoint recovery fence drift"
                        ) from checkpoint_error
                    return _result(plan.plan_digest, recovery_event)
            repository.append_event(terminal_event)
            if terminal_state is ResumeApplyState.COMPLETED:
                finished = host.finish(
                    claimed,
                    outcome=AttemptOutcome.SUCCEEDED,
                    result_digest=dispatch.result_digest,
                    now=terminal_at,
                )
            else:
                finished = host.finish(
                    claimed,
                    outcome=AttemptOutcome.FAILED,
                    failure_category=FailureCategory.ADAPTER,
                    now=terminal_at,
                )
            if not finished:
                raise PolicyViolation("Resume apply terminal transition fence drift")
        return _result(plan.plan_digest, terminal_event)
