"""Transcript-free handoff'tan canonical child dispatch'e production yolu."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zekam.application.agent_dispatch import AssignmentStore, CanonicalAgentDispatchService
from zekam.application.context_continuity_service import ContextContinuityService
from zekam.domain.agents import AgentAssignment, AgentInvocation
from zekam.domain.clients import DispatchResult
from zekam.domain.context_continuity import Checkpoint, ContinuitySnapshot, FinalizedHandoff
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.clients.adapters import ClientLifecycleAdapter


@dataclass(frozen=True, slots=True)
class CrossClientResumeDispatchService:
    continuity: ContextContinuityService
    assignment_store: AssignmentStore

    def dispatch(
        self,
        *,
        handoff: FinalizedHandoff,
        snapshot: ContinuitySnapshot,
        checkpoint: Checkpoint,
        current_source_revision: str,
        assignment: AgentAssignment,
        invocation: AgentInvocation,
        adapter: ClientLifecycleAdapter,
        cwd: Path,
        timeout_seconds: int,
    ) -> DispatchResult:
        instructions = self.continuity.resume(
            handoff=handoff,
            snapshot=snapshot,
            checkpoint=checkpoint,
            current_source_revision=current_source_revision,
            target_client=adapter.descriptor,
        )
        if str(assignment.project_id) != snapshot.project_id:
            raise PolicyViolation("Cross-client assignment project binding uyusmuyor")
        if str(assignment.work_item_id) != snapshot.work_item_id:
            raise PolicyViolation("Cross-client assignment work binding uyusmuyor")
        if assignment.context_manifest_digest != snapshot.context_manifest_digest:
            raise PolicyViolation("Cross-client assignment context manifest drift")
        if assignment.step_id is None or assignment.step_id not in checkpoint.pending_steps:
            raise PolicyViolation("Cross-client assignment next safe step binding uyusmuyor")
        if invocation.client_id != instructions.client:
            raise PolicyViolation("Cross-client invocation target client drift")
        return CanonicalAgentDispatchService(self.assignment_store).dispatch(
            assignment,
            invocation,
            adapter,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
