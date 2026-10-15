"""Transcript-free handoff'tan canonical child dispatch'e production yolu."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zekam.application.agent_dispatch import AssignmentStore, CanonicalAgentDispatchService
from zekam.application.canonical_job_runtime import run_canonical_job, validate_bound_source
from zekam.application.context_continuity_service import ContextContinuityService
from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.agents import AgentAssignment, AgentInvocation
from zekam.domain.canonical import digest
from zekam.domain.clients import DispatchRequest, DispatchResult, parse_result
from zekam.domain.context_continuity import Checkpoint, ContinuitySnapshot, FinalizedHandoff
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.clients.adapters import ClientLifecycleAdapter


@dataclass(frozen=True, slots=True)
class CrossClientResumeDispatchService:
    continuity: ContextContinuityService
    assignment_store: AssignmentStore

    def _validate_resume(
        self,
        *,
        handoff: FinalizedHandoff,
        snapshot: ContinuitySnapshot,
        checkpoint: Checkpoint,
        current_source_revision: str,
        current_source_fingerprint: str | None,
        current_test_fingerprint: str | None,
        current_config_fingerprint: str | None,
        assignment: AgentAssignment,
        invocation: AgentInvocation,
        adapter: ClientLifecycleAdapter,
    ) -> None:
        if str(assignment.project_id) != snapshot.project_id:
            raise PolicyViolation("Cross-client assignment project binding uyusmuyor")
        if str(assignment.work_item_id) != snapshot.work_item_id:
            raise PolicyViolation("Cross-client assignment work binding uyusmuyor")
        if assignment.plan_id is None or str(assignment.plan_id) != checkpoint.plan_revision_id:
            raise PolicyViolation("Cross-client assignment plan binding uyusmuyor")
        if assignment.context_manifest_digest != snapshot.context_manifest_digest:
            raise PolicyViolation("Cross-client assignment context manifest drift")
        if assignment.step_id is None or assignment.step_id not in checkpoint.pending_steps:
            raise PolicyViolation("Cross-client assignment next safe step binding uyusmuyor")
        instructions = self.continuity.resume(
            handoff=handoff,
            snapshot=snapshot,
            checkpoint=checkpoint,
            current_source_revision=current_source_revision,
            current_source_fingerprint=current_source_fingerprint,
            current_test_fingerprint=current_test_fingerprint,
            current_config_fingerprint=current_config_fingerprint,
            target_client=adapter.descriptor,
        )
        if invocation.client_id != instructions.client:
            raise PolicyViolation("Cross-client invocation target client drift")

    @staticmethod
    def _input_digest(
        *,
        handoff: FinalizedHandoff,
        snapshot: ContinuitySnapshot,
        checkpoint: Checkpoint,
        current_source_revision: str,
        current_source_fingerprint: str | None,
        current_test_fingerprint: str | None,
        current_config_fingerprint: str | None,
        assignment: AgentAssignment,
        invocation: AgentInvocation,
        adapter: ClientLifecycleAdapter,
        timeout_seconds: int,
    ) -> str:
        return digest(
            {
                "schema": "zekam-cross-client-resume-input/v1",
                "handoff_digest": handoff.handoff_digest,
                "snapshot_digest": snapshot.snapshot_digest,
                "checkpoint_digest": checkpoint.checkpoint_digest,
                "source_revision": current_source_revision,
                "source_fingerprint": current_source_fingerprint,
                "test_fingerprint": current_test_fingerprint,
                "config_fingerprint": current_config_fingerprint,
                "assignment_digest": assignment.assignment_digest,
                "invocation_digest": invocation.invocation_digest,
                "adapter_digest": adapter.descriptor.descriptor_digest,
                "timeout_seconds": timeout_seconds,
                "grants_authority": False,
            }
        )

    @staticmethod
    def _request(
        assignment: AgentAssignment, invocation: AgentInvocation, timeout_seconds: int
    ) -> DispatchRequest:
        return DispatchRequest(
            assignment_id=assignment.id,
            invocation_id=invocation.id,
            client_id=invocation.client_id,
            role=assignment.role.value,
            instruction_digest=assignment.instruction_digest,
            context_manifest_digest=assignment.context_manifest_digest,
            timeout_seconds=timeout_seconds,
        )

    def dispatch(
        self,
        *,
        handoff: FinalizedHandoff,
        snapshot: ContinuitySnapshot,
        checkpoint: Checkpoint,
        current_source_revision: str,
        current_source_fingerprint: str | None = None,
        current_test_fingerprint: str | None = None,
        current_config_fingerprint: str | None = None,
        assignment: AgentAssignment,
        invocation: AgentInvocation,
        adapter: ClientLifecycleAdapter,
        cwd: Path,
        timeout_seconds: int,
    ) -> DispatchResult:
        self._validate_resume(
            handoff=handoff,
            snapshot=snapshot,
            checkpoint=checkpoint,
            current_source_revision=current_source_revision,
            current_source_fingerprint=current_source_fingerprint,
            current_test_fingerprint=current_test_fingerprint,
            current_config_fingerprint=current_config_fingerprint,
            assignment=assignment,
            invocation=invocation,
            adapter=adapter,
        )
        return CanonicalAgentDispatchService(self.assignment_store).dispatch(
            assignment,
            invocation,
            adapter,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )

    def dispatch_bound(
        self,
        *,
        home: Path,
        binding: LocalJobBinding,
        handoff: FinalizedHandoff,
        snapshot: ContinuitySnapshot,
        checkpoint: Checkpoint,
        current_source_revision: str,
        current_source_fingerprint: str | None = None,
        current_test_fingerprint: str | None = None,
        current_config_fingerprint: str | None = None,
        assignment: AgentAssignment,
        invocation: AgentInvocation,
        adapter: ClientLifecycleAdapter,
        cwd: Path,
        timeout_seconds: int,
        execution_key: str = "default",
    ) -> DispatchResult:
        """Re-acquire a canonical local lease before cross-client effect."""

        if type(binding) is not LocalJobBinding:
            raise PolicyViolation("Cross-client exact canonical binding required")
        binding.__post_init__()
        if (
            binding.project_id != snapshot.project_id
            or binding.work_item_id != snapshot.work_item_id
            or binding.project_id != str(assignment.project_id)
            or binding.work_item_id != str(assignment.work_item_id)
        ):
            raise PolicyViolation("Cross-client canonical project/work binding mismatch")
        request = self._request(assignment, invocation, timeout_seconds)
        input_digest = self._input_digest(
            handoff=handoff,
            snapshot=snapshot,
            checkpoint=checkpoint,
            current_source_revision=current_source_revision,
            current_source_fingerprint=current_source_fingerprint,
            current_test_fingerprint=current_test_fingerprint,
            current_config_fingerprint=current_config_fingerprint,
            assignment=assignment,
            invocation=invocation,
            adapter=adapter,
            timeout_seconds=timeout_seconds,
        )

        def validate_inputs() -> None:
            self._validate_resume(
                handoff=handoff,
                snapshot=snapshot,
                checkpoint=checkpoint,
                current_source_revision=current_source_revision,
                current_source_fingerprint=current_source_fingerprint,
                current_test_fingerprint=current_test_fingerprint,
                current_config_fingerprint=current_config_fingerprint,
                assignment=assignment,
                invocation=invocation,
                adapter=adapter,
            )
            validate_bound_source(home, binding, source_revision=current_source_revision)

        result = run_canonical_job(
            home,
            binding=binding,
            operation="continuity.resume",
            input_digest=input_digest,
            execution_key=execution_key,
            validate_inputs=validate_inputs,
            execute=lambda _cancel: CanonicalAgentDispatchService(
                self.assignment_store
            )
            .dispatch(
                assignment,
                invocation,
                adapter,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )
            .as_dict(),
        )
        return parse_result(adapter.descriptor, request, result)
