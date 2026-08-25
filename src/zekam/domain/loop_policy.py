"""Butceli, evidence-delta zorunlu evrensel loop sozlesmesi."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class LoopDeltaKind(StrEnum):
    DIFFERENT_PATCH = "different-patch-digest"
    NEW_EVIDENCE = "new-evidence"
    NEW_FAILURE_DIAGNOSIS = "new-failure-diagnosis"
    REVISED_PLAN = "revised-plan"


class LoopEffectClass(StrEnum):
    READ_ONLY = "read-only"
    MODEL_CALL = "model-call"
    TOOL_CALL = "tool-call"
    FILE_WRITE = "file-write"
    DEPLOY = "deploy"
    MIGRATION_APPLY = "migration-apply"
    EXTERNAL_MESSAGE = "external-message"


class LoopTerminalState(StrEnum):
    PASSED = "passed"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget-exhausted"
    MANUAL_REVIEW = "manual-review"


class LoopAttemptOutcome(StrEnum):
    RETRYABLE_FAILURE = "retryable-failure"
    PASSED = "passed"
    BLOCKED = "blocked"
    MANUAL_REVIEW = "manual-review"


@dataclass(frozen=True, slots=True)
class LoopPolicy:
    id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    plan_id: UUID
    step_id: str
    assignment_id: UUID
    context_manifest_id: UUID
    validator_assignment_id: UUID
    max_attempts: int
    max_tokens: int
    max_cost_micros: int
    deadline: dt.datetime
    validator_spec_digest: str
    required_delta: tuple[LoopDeltaKind, ...]
    forbidden_effects: tuple[LoopEffectClass, ...]
    terminal_states: tuple[LoopTerminalState, ...]
    source_revision: str
    context_manifest_digest: str
    plan_digest: str
    policy_revision_digest: str
    canonical_effect_kind: str
    created_at: dt.datetime
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Loop policy authority veremez")
        if not 1 <= self.max_attempts <= 100:
            raise ValidationFailed("Loop max attempts 1..100 araliginda olmali")
        if self.max_tokens < 1 or self.max_cost_micros < 1:
            raise ValidationFailed("Loop token ve cost butcesi pozitif olmali")
        if self.created_at.tzinfo is None or self.deadline.tzinfo is None:
            raise ValidationFailed("Loop zamanlari timezone-aware olmali")
        if self.deadline <= self.created_at:
            raise ValidationFailed("Loop deadline created_at sonrasinda olmali")
        if not self.step_id.strip() or not self.source_revision.strip():
            raise ValidationFailed("Loop step ve source revision bos olamaz")
        if self.canonical_effect_kind not in {
            "none",
            "file-write",
            "database-write",
            "network-call",
            "provider-call",
            "git-commit",
            "git-push",
            "process-run",
        }:
            raise ValidationFailed("Loop canonical effect kind desteklenmiyor")
        for value in (
            self.validator_spec_digest,
            self.context_manifest_digest,
            self.plan_digest,
            self.policy_revision_digest,
        ):
            parse_digest(value)
        if tuple(sorted(set(self.required_delta), key=str)) != self.required_delta:
            raise ValidationFailed("Loop required delta listesi kanonik olmali")
        if not self.required_delta:
            raise ValidationFailed("Loop en az bir evidence delta turu ister")
        if tuple(sorted(set(self.forbidden_effects), key=str)) != self.forbidden_effects:
            raise ValidationFailed("Loop forbidden effect listesi kanonik olmali")
        expected_terminal = tuple(sorted(LoopTerminalState, key=str))
        if self.terminal_states != expected_terminal:
            raise ValidationFailed("Loop terminal state seti exact ve kapali olmali")

    def body(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "plan_id": str(self.plan_id),
            "step_id": self.step_id,
            "assignment_id": str(self.assignment_id),
            "context_manifest_id": str(self.context_manifest_id),
            "validator_assignment_id": str(self.validator_assignment_id),
            "max_attempts": self.max_attempts,
            "max_tokens": self.max_tokens,
            "max_cost_micros": self.max_cost_micros,
            "deadline": self.deadline,
            "validator_spec_digest": self.validator_spec_digest,
            "required_delta": [str(item) for item in self.required_delta],
            "forbidden_effects": [str(item) for item in self.forbidden_effects],
            "terminal_states": [str(item) for item in self.terminal_states],
            "source_revision": self.source_revision,
            "context_manifest_digest": self.context_manifest_digest,
            "plan_digest": self.plan_digest,
            "policy_revision_digest": self.policy_revision_digest,
            "canonical_effect_kind": self.canonical_effect_kind,
            "created_at": self.created_at,
            "grants_authority": False,
        }

    @property
    def policy_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class LoopAttemptRequest:
    loop_id: UUID
    prompt_digest: str
    context_digest: str
    action_digest: str
    source_revision: str
    plan_digest: str
    policy_revision_digest: str
    validator_spec_digest: str
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_cost_micros: int
    predecessor_attempt_id: UUID | None = None
    delta_evidence_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        for value in (
            self.prompt_digest,
            self.context_digest,
            self.action_digest,
            self.plan_digest,
            self.policy_revision_digest,
            self.validator_spec_digest,
        ):
            parse_digest(value)
        if not self.source_revision.strip():
            raise ValidationFailed("Loop attempt source revision bos olamaz")
        if (
            min(
                self.reserved_input_tokens,
                self.reserved_output_tokens,
                self.reserved_cost_micros,
            )
            < 0
        ):
            raise ValidationFailed("Loop reservation negatif olamaz")
        if self.reserved_input_tokens + self.reserved_output_tokens < 1:
            raise ValidationFailed("Loop attempt pozitif token reservation ister")
        if tuple(sorted(set(self.delta_evidence_ids), key=str)) != self.delta_evidence_ids:
            raise ValidationFailed("Loop evidence kimlikleri kanonik ve unique olmali")

    @property
    def semantic_request_digest(self) -> str:
        return digest(
            {
                "prompt_digest": self.prompt_digest,
                "context_digest": self.context_digest,
                "action_digest": self.action_digest,
            }
        )

    @property
    def binding_digest(self) -> str:
        return digest(
            {
                "source_revision": self.source_revision,
                "plan_digest": self.plan_digest,
                "policy_revision_digest": self.policy_revision_digest,
                "validator_spec_digest": self.validator_spec_digest,
                "predecessor_attempt_id": (
                    None
                    if self.predecessor_attempt_id is None
                    else str(self.predecessor_attempt_id)
                ),
            }
        )

    @property
    def delta_digest(self) -> str:
        return digest([str(item) for item in self.delta_evidence_ids])


@dataclass(frozen=True, slots=True)
class LoopAdmission:
    admitted: bool
    loop_id: UUID
    attempt_id: UUID | None
    ordinal: int | None
    terminal_state: LoopTerminalState | None
    reason: str
    decision_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Loop admission authority veremez")
        parse_digest(self.decision_digest)
        if self.admitted != (self.attempt_id is not None and self.ordinal is not None):
            raise ValidationFailed("Loop admission attempt binding tutarsiz")
        if self.admitted and self.terminal_state is not None:
            raise ValidationFailed("Admitted loop terminal state tasiyamaz")
        if not self.admitted and self.terminal_state is None:
            raise ValidationFailed("Reddedilen loop terminal state ister")


@dataclass(frozen=True, slots=True)
class LoopValidation:
    outcome: LoopAttemptOutcome
    validator_spec_digest: str
    actual_input_tokens: int
    actual_output_tokens: int
    actual_cost_micros: int
    result_invocation_id: UUID
    verifier_invocation_id: UUID
    effect_receipt_id: UUID | None = None

    def __post_init__(self) -> None:
        parse_digest(self.validator_spec_digest)
        if min(self.actual_input_tokens, self.actual_output_tokens, self.actual_cost_micros) < 0:
            raise ValidationFailed("Loop actual usage negatif olamaz")

    @property
    def terminal_state(self) -> LoopTerminalState | None:
        return {
            LoopAttemptOutcome.PASSED: LoopTerminalState.PASSED,
            LoopAttemptOutcome.BLOCKED: LoopTerminalState.BLOCKED,
            LoopAttemptOutcome.MANUAL_REVIEW: LoopTerminalState.MANUAL_REVIEW,
            LoopAttemptOutcome.RETRYABLE_FAILURE: None,
        }[self.outcome]
