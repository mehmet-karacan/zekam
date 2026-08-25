"""Her agent/model loop effect'inden once canonical admission kapisi."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar
from uuid import UUID

from zekam.application.agent_dispatch import AssignmentStore, CanonicalAgentDispatchService
from zekam.domain.agents import AgentAssignment, AgentInvocation
from zekam.domain.canonical import digest
from zekam.domain.clients import DispatchResult
from zekam.domain.errors import PolicyViolation
from zekam.domain.loop_policy import (
    LoopAdmission,
    LoopAttemptRequest,
    LoopTerminalState,
    LoopValidation,
)
from zekam.infrastructure.clients.adapters import ClientAdapter

T = TypeVar("T")


class LoopLedger(Protocol):
    def admit(self, request: LoopAttemptRequest) -> LoopAdmission: ...

    def complete(self, attempt_id: UUID, validation: LoopValidation) -> str: ...

    def interrupt(self, attempt_id: UUID, failure_digest: str) -> LoopTerminalState: ...

    def bind_dispatch(self, attempt_id: UUID, surface: str, dispatch_id: UUID) -> None: ...


@dataclass(frozen=True, slots=True)
class LoopExecutionResult[T]:
    value: T
    admission: LoopAdmission
    validation: LoopValidation
    terminal_state: LoopTerminalState | None


@dataclass(frozen=True, slots=True)
class BoundedLoopExecutor:
    """Admission -> effect -> validator -> terminal ledger tek production yolu."""

    ledger: LoopLedger

    def execute(
        self,
        request: LoopAttemptRequest,
        *,
        effect: Callable[[], T],
        validator: Callable[[T, LoopAdmission], LoopValidation],
        before_effect: Callable[[LoopAdmission], None] | None = None,
    ) -> LoopExecutionResult[T]:
        admission = self.ledger.admit(request)
        if not admission.admitted or admission.attempt_id is None:
            raise PolicyViolation(
                "Loop dispatch reddedildi: "
                f"{admission.terminal_state or 'unknown'} ({admission.reason})"
            )
        try:
            if before_effect is not None:
                before_effect(admission)
            value = effect()
            validation = validator(value, admission)
            if validation.validator_spec_digest != request.validator_spec_digest:
                raise PolicyViolation("Loop validator sonucu immutable spec ile uyusmuyor")
            state = self.ledger.complete(admission.attempt_id, validation)
        except Exception as exc:
            self.ledger.interrupt(
                admission.attempt_id,
                digest(
                    {
                        "attempt_id": str(admission.attempt_id),
                        "failure_category": type(exc).__name__,
                    }
                ),
            )
            raise
        terminal = None if state == "active" else LoopTerminalState(state)
        return LoopExecutionResult(value, admission, validation, terminal)


@dataclass(frozen=True, slots=True)
class LoopBoundAgentDispatchService:
    """Loop admission'i canonical assignment-first dispatch'ten ayirilamaz yapar."""

    ledger: LoopLedger
    assignment_store: AssignmentStore

    def dispatch(
        self,
        request: LoopAttemptRequest,
        *,
        assignment: AgentAssignment,
        invocation: AgentInvocation,
        adapter: ClientAdapter,
        cwd: Path,
        timeout_seconds: int,
        validator: Callable[[DispatchResult, LoopAdmission], LoopValidation],
    ) -> LoopExecutionResult[DispatchResult]:
        canonical = CanonicalAgentDispatchService(self.assignment_store)

        def bind(admission: LoopAdmission) -> None:
            if admission.attempt_id is None:
                raise PolicyViolation("Admitted loop attempt kimligi eksik")
            self.ledger.bind_dispatch(admission.attempt_id, "agent", invocation.id)

        return BoundedLoopExecutor(self.ledger).execute(
            request,
            before_effect=bind,
            effect=lambda: canonical.dispatch(
                assignment,
                invocation,
                adapter,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            ),
            validator=validator,
        )
