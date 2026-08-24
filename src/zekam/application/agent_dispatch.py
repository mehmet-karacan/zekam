"""Canonical assignment-first child dispatch orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from zekam.domain.agents import AgentAssignment, AgentInvocation
from zekam.domain.clients import (
    DispatchRequest,
    DispatchResult,
    _issue_canonical_dispatch_permit,
)
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.clients.adapters import ClientAdapter


class AssignmentStore(Protocol):
    def create(self, assignment: AgentAssignment) -> tuple[UUID, bool]: ...

    def record_invocation(self, invocation: AgentInvocation) -> tuple[UUID, bool]: ...

    def store_result(
        self, *, assignment_id: UUID, invocation_id: UUID, envelope_digest: str
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CanonicalAgentDispatchService:
    """Persist assignment/invocation before effect and bind its terminal receipt."""

    store: AssignmentStore

    def dispatch(
        self,
        assignment: AgentAssignment,
        invocation: AgentInvocation,
        adapter: ClientAdapter,
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> DispatchResult:
        assignment.assert_digest()
        invocation.assert_digest()
        if not assignment.is_child:
            raise PolicyViolation("Yalniz canonical child assignment dispatch edilebilir")
        if invocation.assignment_id != assignment.id:
            raise PolicyViolation("Invocation baska assignment'a bagli")
        if invocation.client_id != adapter.descriptor.client_id:
            raise PolicyViolation("Invocation baska istemciye bagli")

        stored_assignment_id, _ = self.store.create(assignment)
        if stored_assignment_id != assignment.id:
            raise PolicyViolation("Idempotent assignment kimligi uyusmuyor")
        stored_invocation_id, _ = self.store.record_invocation(invocation)
        if stored_invocation_id != invocation.id:
            raise PolicyViolation("Idempotent invocation kimligi uyusmuyor")

        request = DispatchRequest(
                assignment_id=assignment.id,
                invocation_id=invocation.id,
                client_id=invocation.client_id,
                role=assignment.role.value,
                instruction_digest=assignment.instruction_digest,
                context_manifest_digest=assignment.context_manifest_digest,
                timeout_seconds=timeout_seconds,
            )
        result = adapter.dispatch(
            request,
            cwd=cwd,
            permit=_issue_canonical_dispatch_permit(assignment.id, invocation.id),
        )
        self.store.store_result(
            assignment_id=assignment.id,
            invocation_id=invocation.id,
            envelope_digest=result.result_digest,
        )
        return result
