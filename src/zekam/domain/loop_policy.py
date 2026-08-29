"""Butceli, evidence-delta zorunlu evrensel loop sozlesmesi."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.optimization import ProgressState


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
    objective_id: UUID | None = None
    stable_objective_digest: str | None = None
    measurement_plan_digest: str | None = None
    validator_manifest_id: UUID | None = None
    validator_asset_manifest_digest: str | None = None
    metric_specs_digest: str | None = None
    stall_limit: int | None = None
    diagnostic_patience: int | None = None
    progress_token_budget: int | None = None
    minimum_value_per_cost: float | None = None

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
        self._validate_v2_bindings()

    def _validate_v2_bindings(self) -> None:
        fields = (
            self.objective_id,
            self.stable_objective_digest,
            self.measurement_plan_digest,
            self.validator_manifest_id,
            self.validator_asset_manifest_digest,
            self.metric_specs_digest,
            self.stall_limit,
            self.diagnostic_patience,
            self.progress_token_budget,
            self.minimum_value_per_cost,
        )
        if all(value is None for value in fields):
            return
        if any(value is None for value in fields):
            raise ValidationFailed("Measured LoopPolicy v2 bindingleri exact ve tam olmali")
        assert self.stable_objective_digest is not None
        assert self.measurement_plan_digest is not None
        assert self.validator_asset_manifest_digest is not None
        assert self.metric_specs_digest is not None
        for value in (
            self.stable_objective_digest,
            self.measurement_plan_digest,
            self.validator_asset_manifest_digest,
            self.metric_specs_digest,
        ):
            parse_digest(value)
        assert self.stall_limit is not None
        assert self.diagnostic_patience is not None
        assert self.progress_token_budget is not None
        assert self.minimum_value_per_cost is not None
        if not 1 <= self.stall_limit <= self.max_attempts:
            raise ValidationFailed("Measured LoopPolicy stall limit attempt butcesini asamaz")
        if not 0 <= self.diagnostic_patience < self.max_attempts:
            raise ValidationFailed("Measured LoopPolicy diagnostic patience gecersiz")
        if not 64 <= self.progress_token_budget <= self.max_tokens:
            raise ValidationFailed("Measured LoopPolicy progress token budget gecersiz")
        if not math.isfinite(self.minimum_value_per_cost) or self.minimum_value_per_cost < 0:
            raise ValidationFailed("Measured LoopPolicy minimum value-per-cost gecersiz")

    def body(self) -> dict[str, Any]:
        body = {
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
        if self.objective_id is not None:
            body["measured_v2"] = {
                "objective_id": str(self.objective_id),
                "stable_objective_digest": self.stable_objective_digest,
                "measurement_plan_digest": self.measurement_plan_digest,
                "validator_manifest_id": str(self.validator_manifest_id),
                "validator_asset_manifest_digest": self.validator_asset_manifest_digest,
                "metric_specs_digest": self.metric_specs_digest,
                "stall_limit": self.stall_limit,
                "diagnostic_patience": self.diagnostic_patience,
                "progress_token_budget": self.progress_token_budget,
                "minimum_value_per_cost": self.minimum_value_per_cost,
            }
        return body

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
    attempt_ordinal: int = 1
    objective_digest: str | None = None
    validator_asset_manifest_digest: str | None = None
    progress_packet_digest: str | None = None
    metric_vector_digest: str | None = None
    novelty_digest: str | None = None

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
        if self.attempt_ordinal < 1:
            raise ValidationFailed("Loop attempt ordinal 1'den kucuk olamaz")
        for optional_digest in (
            self.objective_digest,
            self.validator_asset_manifest_digest,
            self.progress_packet_digest,
            self.metric_vector_digest,
            self.novelty_digest,
        ):
            if optional_digest is not None:
                parse_digest(optional_digest)
        measured = any(
            value is not None
            for value in (
                self.objective_digest,
                self.validator_asset_manifest_digest,
                self.progress_packet_digest,
                self.metric_vector_digest,
                self.novelty_digest,
            )
        )
        if measured and (
            self.objective_digest is None
            or self.validator_asset_manifest_digest is None
            or self.novelty_digest is None
        ):
            raise ValidationFailed("Measured loop attempt objective/manifest/novelty ister")
        if (
            measured
            and self.attempt_ordinal == 1
            and (
                self.predecessor_attempt_id is not None
                or self.progress_packet_digest is not None
                or self.metric_vector_digest is not None
            )
        ):
            raise ValidationFailed("Ilk measured loop attempt predecessor/progress tasiyamaz")
        if self.attempt_ordinal > 1 and (
            self.predecessor_attempt_id is None
            or self.objective_digest is None
            or self.validator_asset_manifest_digest is None
            or self.progress_packet_digest is None
            or self.metric_vector_digest is None
            or self.novelty_digest is None
        ):
            raise ValidationFailed("Loop attempt 2+ exact progress/novelty binding ister")

    @property
    def semantic_request_digest(self) -> str:
        if self.novelty_digest is not None:
            return self.novelty_digest
        return digest(
            {
                "prompt_digest": self.prompt_digest,
                "context_digest": self.context_digest,
                "action_digest": self.action_digest,
            }
        )

    @property
    def binding_digest(self) -> str:
        body: dict[str, Any] = {
            "source_revision": self.source_revision,
            "plan_digest": self.plan_digest,
            "policy_revision_digest": self.policy_revision_digest,
            "validator_spec_digest": self.validator_spec_digest,
            "predecessor_attempt_id": (
                None if self.predecessor_attempt_id is None else str(self.predecessor_attempt_id)
            ),
        }
        if self.attempt_ordinal != 1 or any(
            value is not None
            for value in (
                self.objective_digest,
                self.validator_asset_manifest_digest,
                self.progress_packet_digest,
                self.metric_vector_digest,
                self.novelty_digest,
            )
        ):
            body["measured_v2"] = {
                "attempt_ordinal": self.attempt_ordinal,
                "objective_digest": self.objective_digest,
                "validator_asset_manifest_digest": self.validator_asset_manifest_digest,
                "progress_packet_digest": self.progress_packet_digest,
                "metric_vector_digest": self.metric_vector_digest,
                "novelty_digest": self.novelty_digest,
            }
        return digest(body)

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
    metric_evidence_refs: tuple[str, ...] = ()
    metric_vector_digest: str | None = None
    progress_state: ProgressState | None = None
    progress_decision_digest: str | None = None
    progress_packet_digest: str | None = None
    producer_self_report: bool = False
    hard_guard_regressed: bool = False

    def __post_init__(self) -> None:
        parse_digest(self.validator_spec_digest)
        if min(self.actual_input_tokens, self.actual_output_tokens, self.actual_cost_micros) < 0:
            raise ValidationFailed("Loop actual usage negatif olamaz")
        measured = (
            bool(self.metric_evidence_refs)
            or self.metric_vector_digest is not None
            or self.progress_state is not None
            or self.progress_decision_digest is not None
            or self.progress_packet_digest is not None
        )
        if measured:
            if (
                not self.metric_evidence_refs
                or self.metric_vector_digest is None
                or self.progress_state is None
                or self.progress_decision_digest is None
                or self.progress_packet_digest is None
            ):
                raise ValidationFailed("Loop validation v2 metric bindingleri exact olmali")
            if self.metric_evidence_refs != tuple(sorted(set(self.metric_evidence_refs))):
                raise ValidationFailed("Loop metric evidence refs kanonik ve unique olmali")
            if any(not value.strip() for value in self.metric_evidence_refs):
                raise ValidationFailed("Loop metric evidence ref bos olamaz")
            parse_digest(self.metric_vector_digest)
            parse_digest(self.progress_decision_digest)
            parse_digest(self.progress_packet_digest)
        elif self.producer_self_report or self.hard_guard_regressed:
            raise ValidationFailed("Loop validation v2 guard bayraklari metric binding ister")

    @property
    def measured_progress(self) -> bool:
        return (
            self.progress_state in {ProgressState.IMPROVED, ProgressState.TARGET_REACHED}
            and not self.producer_self_report
            and not self.hard_guard_regressed
        )

    @property
    def terminal_state(self) -> LoopTerminalState | None:
        return {
            LoopAttemptOutcome.PASSED: LoopTerminalState.PASSED,
            LoopAttemptOutcome.BLOCKED: LoopTerminalState.BLOCKED,
            LoopAttemptOutcome.MANUAL_REVIEW: LoopTerminalState.MANUAL_REVIEW,
            LoopAttemptOutcome.RETRYABLE_FAILURE: None,
        }[self.outcome]
