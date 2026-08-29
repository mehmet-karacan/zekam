"""Bounded progress packets and rephrase-proof measured-loop stop gates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_bytes, digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.optimization import ProgressState, ProgressVector


class LoopStopReason(StrEnum):
    TARGET_REACHED = "target-reached"
    NO_PROGRESS = "no-progress"
    REPEATED_HYPOTHESIS = "repeated-hypothesis"
    REPEATED_PATCH = "repeated-patch"
    REPEATED_FAILURE_SIGNATURE = "repeated-failure-signature"
    OSCILLATION = "oscillation"
    METRIC_REGRESSION = "metric-regression"
    INVALID_MEASUREMENT = "invalid-measurement"
    VALIDATOR_DRIFT = "validator-drift"
    RISK_ESCALATION = "risk-escalation"


@dataclass(frozen=True, slots=True)
class AttemptNoveltyFingerprint:
    objective_digest: str
    artifact_digest: str
    hypothesis_digest: str
    patch_digest: str
    failure_signature: str
    action_semantics_digest: str
    novelty_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.objective_digest,
            self.artifact_digest,
            self.hypothesis_digest,
            self.patch_digest,
            self.failure_signature,
            self.action_semantics_digest,
            self.novelty_digest,
        ):
            parse_digest(value)
        if self.novelty_digest != digest(self.semantic_body()):
            raise ValidationFailed("Attempt novelty digest drift")

    def semantic_body(self) -> dict[str, str]:
        return {
            "objective_digest": self.objective_digest,
            "artifact_digest": self.artifact_digest,
            "hypothesis_digest": self.hypothesis_digest,
            "patch_digest": self.patch_digest,
            "failure_signature": self.failure_signature,
            "action_semantics_digest": self.action_semantics_digest,
        }

    @classmethod
    def build(
        cls,
        *,
        objective_digest: str,
        artifact_digest: str,
        hypothesis_digest: str,
        patch_digest: str,
        failure_signature: str,
        action_semantics_digest: str,
    ) -> AttemptNoveltyFingerprint:
        body = {
            "objective_digest": objective_digest,
            "artifact_digest": artifact_digest,
            "hypothesis_digest": hypothesis_digest,
            "patch_digest": patch_digest,
            "failure_signature": failure_signature,
            "action_semantics_digest": action_semantics_digest,
        }
        return cls(
            objective_digest,
            artifact_digest,
            hypothesis_digest,
            patch_digest,
            failure_signature,
            action_semantics_digest,
            digest(body),
        )


@dataclass(frozen=True, slots=True)
class LoopAttemptProgress:
    attempt_id: UUID
    attempt_ordinal: int
    artifact_before_digest: str
    artifact_after_digest: str
    novelty: AttemptNoveltyFingerprint
    progress_state: ProgressState
    progress_digest: str
    diagnosis_evidence_digest: str | None = None

    def __post_init__(self) -> None:
        if self.attempt_ordinal < 1:
            raise ValidationFailed("Loop attempt ordinal 1'den kucuk olamaz")
        for value in (
            self.artifact_before_digest,
            self.artifact_after_digest,
            self.progress_digest,
        ):
            parse_digest(value)
        if self.diagnosis_evidence_digest is not None:
            parse_digest(self.diagnosis_evidence_digest)


@dataclass(frozen=True, slots=True)
class LoopProgressGateDecision:
    allow_next_attempt: bool
    progress_counted: bool
    diagnostic_retry: bool
    stop_reason: LoopStopReason | None
    reason_codes: tuple[str, ...]
    decision_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Loop progress gate authority veremez")
        if self.stop_reason is None and not self.allow_next_attempt:
            raise ValidationFailed("Stopped loop stop reason ister")
        if self.stop_reason is not None and self.allow_next_attempt:
            raise ValidationFailed("Stopped loop next attempt acamaz")
        if self.progress_counted and self.diagnostic_retry:
            raise ValidationFailed("Diagnostic retry measured progress sayilamaz")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValidationFailed("Loop gate reason listesi kanonik olmali")
        parse_digest(self.decision_digest)
        if self.decision_digest != digest(self.semantic_body()):
            raise ValidationFailed("Loop progress gate decision digest drift")

    def semantic_body(self) -> dict[str, Any]:
        return {
            "allow_next_attempt": self.allow_next_attempt,
            "progress_counted": self.progress_counted,
            "diagnostic_retry": self.diagnostic_retry,
            "stop_reason": None if self.stop_reason is None else str(self.stop_reason),
            "reason_codes": list(self.reason_codes),
            "grants_authority": False,
        }


def _gate_decision(
    allow_next_attempt: bool,
    progress_counted: bool,
    diagnostic_retry: bool,
    stop_reason: LoopStopReason | None,
    reason_codes: tuple[str, ...],
) -> LoopProgressGateDecision:
    canonical_reasons = tuple(sorted(set(reason_codes)))
    body: dict[str, Any] = {
        "allow_next_attempt": allow_next_attempt,
        "progress_counted": progress_counted,
        "diagnostic_retry": diagnostic_retry,
        "stop_reason": None if stop_reason is None else str(stop_reason),
        "reason_codes": list(canonical_reasons),
        "grants_authority": False,
    }
    return LoopProgressGateDecision(
        allow_next_attempt,
        progress_counted,
        diagnostic_retry,
        stop_reason,
        canonical_reasons,
        digest(body),
    )


def evaluate_attempt_gates(
    current: LoopAttemptProgress,
    history: tuple[LoopAttemptProgress, ...],
    *,
    stall_limit: int,
    diagnostic_patience: int = 1,
) -> LoopProgressGateDecision:
    """Apply duplicate, no-op, plateau, regression and A-B-A oscillation gates."""

    if stall_limit < 1 or diagnostic_patience < 0:
        raise ValidationFailed("Loop stall/patience siniri gecersiz")
    if history:
        ordinals = tuple(item.attempt_ordinal for item in history)
        if ordinals != tuple(sorted(set(ordinals))) or current.attempt_ordinal <= ordinals[-1]:
            raise ValidationFailed("Loop attempt history ordinal sirasi gecersiz")
        if any(
            item.novelty.objective_digest != current.novelty.objective_digest
            for item in history
        ):
            raise ValidationFailed("Loop novelty history objective drift")

    if any(item.novelty.patch_digest == current.novelty.patch_digest for item in history):
        return _gate_decision(
            False, False, False, LoopStopReason.REPEATED_PATCH, ("repeated-patch",)
        )
    if any(
        (item.novelty.hypothesis_digest, item.novelty.failure_signature)
        == (current.novelty.hypothesis_digest, current.novelty.failure_signature)
        for item in history
    ):
        return _gate_decision(
            False,
            False,
            False,
            LoopStopReason.REPEATED_HYPOTHESIS,
            ("repeated-hypothesis-failure",),
        )
    if any(item.novelty.failure_signature == current.novelty.failure_signature for item in history):
        return _gate_decision(
            False,
            False,
            False,
            LoopStopReason.REPEATED_FAILURE_SIGNATURE,
            ("repeated-failure-signature",),
        )
    if current.artifact_before_digest == current.artifact_after_digest:
        return _gate_decision(False, False, False, LoopStopReason.NO_PROGRESS, ("artifact-no-op",))
    if current.progress_state is ProgressState.INVALID:
        return _gate_decision(
            False,
            False,
            False,
            LoopStopReason.INVALID_MEASUREMENT,
            ("invalid-measurement",),
        )
    if current.progress_state is ProgressState.REGRESSED:
        return _gate_decision(
            False,
            False,
            False,
            LoopStopReason.METRIC_REGRESSION,
            ("metric-regression",),
        )
    if (
        len(history) >= 2
        and current.artifact_after_digest == history[-2].artifact_after_digest
        and current.artifact_after_digest != history[-1].artifact_after_digest
    ):
        return _gate_decision(False, False, False, LoopStopReason.OSCILLATION, ("artifact-a-b-a",))

    plateau_count = 0
    for attempt in reversed((*history, current)):
        if attempt.progress_state is not ProgressState.PLATEAU:
            break
        plateau_count += 1
    if plateau_count >= stall_limit:
        return _gate_decision(
            False,
            False,
            False,
            LoopStopReason.NO_PROGRESS,
            ("plateau-limit",),
        )

    if current.progress_state is ProgressState.TARGET_REACHED:
        return _gate_decision(
            False,
            True,
            False,
            LoopStopReason.TARGET_REACHED,
            ("target-reached",),
        )
    if current.progress_state is ProgressState.IMPROVED:
        return _gate_decision(True, True, False, None, (str(current.progress_state),))

    prior_diagnoses = {
        item.diagnosis_evidence_digest
        for item in history
        if item.diagnosis_evidence_digest is not None
    }
    new_diagnosis = (
        current.diagnosis_evidence_digest is not None
        and current.diagnosis_evidence_digest not in prior_diagnoses
    )
    diagnostic_retries = sum(
        item.progress_state is ProgressState.PLATEAU
        and item.diagnosis_evidence_digest is not None
        for item in history
    )
    if new_diagnosis and diagnostic_retries < diagnostic_patience:
        return _gate_decision(
            True,
            False,
            True,
            None,
            ("new-diagnostic-evidence",),
        )
    return _gate_decision(False, False, False, LoopStopReason.NO_PROGRESS, ("plateau",))


@dataclass(frozen=True, slots=True)
class LoopProgressCheckpoint:
    objective_digest: str
    source_revision: str
    plan_digest: str
    policy_revision_digest: str
    validator_asset_manifest_digest: str
    artifact_before_digest: str
    artifact_after_digest: str
    predecessor_attempt_id: UUID
    next_attempt_ordinal: int
    previous_metric_vector: ProgressVector
    current_metric_vector: ProgressVector
    accepted_hypothesis_digest: str
    rejected_hypothesis_digests: tuple[str, ...]
    patch_digest: str
    failure_signature: str
    validator_diagnosis_ref: str
    validator_diagnosis_digest: str
    new_evidence_refs: tuple[tuple[str, str], ...]
    remaining_attempts: int
    remaining_tokens: int
    remaining_cost_micros: int
    remaining_time_seconds: int
    next_allowed_focus: str
    forbidden_retries: tuple[str, ...]

    def __post_init__(self) -> None:
        for value in (
            self.objective_digest,
            self.plan_digest,
            self.policy_revision_digest,
            self.validator_asset_manifest_digest,
            self.artifact_before_digest,
            self.artifact_after_digest,
            self.accepted_hypothesis_digest,
            self.patch_digest,
            self.failure_signature,
            self.validator_diagnosis_digest,
        ):
            parse_digest(value)
        if not self.source_revision.strip() or not self.validator_diagnosis_ref.strip():
            raise ValidationFailed("Loop checkpoint source ve diagnosis ref ister")
        if self.next_attempt_ordinal < 2:
            raise ValidationFailed("Progress checkpoint attempt 2+ icindir")
        if min(
            self.remaining_attempts,
            self.remaining_tokens,
            self.remaining_cost_micros,
            self.remaining_time_seconds,
        ) < 0:
            raise ValidationFailed("Loop remaining budget negatif olamaz")
        if not self.next_allowed_focus.strip():
            raise ValidationFailed("Loop checkpoint next allowed focus ister")
        _assert_digest_tuple(self.rejected_hypothesis_digests, "rejected hypothesis")
        _assert_ref_digest_tuple(self.new_evidence_refs)
        if self.forbidden_retries != tuple(sorted(set(self.forbidden_retries))):
            raise ValidationFailed("Forbidden retry listesi kanonik olmali")


@dataclass(frozen=True, slots=True)
class LoopProgressPacket:
    objective_digest: str
    source_revision: str
    plan_digest: str
    policy_revision_digest: str
    validator_asset_manifest_digest: str
    artifact_before_digest: str
    artifact_after_digest: str
    predecessor_attempt_id: UUID
    attempt_ordinal: int
    previous_metric_vector: ProgressVector
    current_metric_vector: ProgressVector
    metric_deltas: tuple[tuple[str, float], ...]
    accepted_hypothesis_digest: str
    rejected_hypothesis_digests: tuple[str, ...]
    patch_digest: str
    failure_signature: str
    validator_diagnosis_ref: str
    validator_diagnosis_digest: str
    new_evidence_refs: tuple[tuple[str, str], ...]
    remaining_attempts: int
    remaining_tokens: int
    remaining_cost_micros: int
    remaining_time_seconds: int
    next_allowed_focus: str
    forbidden_retries: tuple[str, ...]
    max_packet_tokens: int
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Loop progress packet authority veremez")
        checkpoint = LoopProgressCheckpoint(
            self.objective_digest,
            self.source_revision,
            self.plan_digest,
            self.policy_revision_digest,
            self.validator_asset_manifest_digest,
            self.artifact_before_digest,
            self.artifact_after_digest,
            self.predecessor_attempt_id,
            self.attempt_ordinal,
            self.previous_metric_vector,
            self.current_metric_vector,
            self.accepted_hypothesis_digest,
            self.rejected_hypothesis_digests,
            self.patch_digest,
            self.failure_signature,
            self.validator_diagnosis_ref,
            self.validator_diagnosis_digest,
            self.new_evidence_refs,
            self.remaining_attempts,
            self.remaining_tokens,
            self.remaining_cost_micros,
            self.remaining_time_seconds,
            self.next_allowed_focus,
            self.forbidden_retries,
        )
        checkpoint.__post_init__()
        if self.metric_deltas != self.current_metric_vector.deltas:
            raise ValidationFailed("Loop packet metric delta vector drift")
        if self.max_packet_tokens < 1:
            raise ValidationFailed("Loop packet token budget pozitif olmali")
        if self.estimated_tokens > self.max_packet_tokens:
            raise PolicyViolation("Loop progress packet token budget asildi")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-loop-progress-packet/v1",
            "objective_digest": self.objective_digest,
            "source_revision": self.source_revision,
            "plan_digest": self.plan_digest,
            "policy_revision_digest": self.policy_revision_digest,
            "validator_asset_manifest_digest": self.validator_asset_manifest_digest,
            "artifact_before_digest": self.artifact_before_digest,
            "artifact_after_digest": self.artifact_after_digest,
            "predecessor_attempt_id": str(self.predecessor_attempt_id),
            "attempt_ordinal": self.attempt_ordinal,
            "previous_metric_vector": self.previous_metric_vector.as_dict(),
            "current_metric_vector": self.current_metric_vector.as_dict(),
            "metric_deltas": dict(self.metric_deltas),
            "accepted_hypothesis_digest": self.accepted_hypothesis_digest,
            "rejected_hypothesis_digests": list(self.rejected_hypothesis_digests),
            "patch_digest": self.patch_digest,
            "failure_signature": self.failure_signature,
            "validator_diagnosis": {
                "ref": self.validator_diagnosis_ref,
                "digest": self.validator_diagnosis_digest,
            },
            "new_evidence_refs": [
                {"ref": reference, "digest": evidence_digest}
                for reference, evidence_digest in self.new_evidence_refs
            ],
            "remaining_budget": {
                "attempts": self.remaining_attempts,
                "tokens": self.remaining_tokens,
                "cost_micros": self.remaining_cost_micros,
                "time_seconds": self.remaining_time_seconds,
            },
            "next_allowed_focus": self.next_allowed_focus,
            "forbidden_retries": list(self.forbidden_retries),
            "max_packet_tokens": self.max_packet_tokens,
            "grants_authority": False,
        }

    @property
    def estimated_tokens(self) -> int:
        return max(1, (len(canonical_bytes(self.as_dict())) + 3) // 4)

    @property
    def packet_digest(self) -> str:
        return digest(self.as_dict())


def _assert_digest_tuple(values: tuple[str, ...], label: str) -> None:
    for value in values:
        parse_digest(value)
    if values != tuple(sorted(set(values))):
        raise ValidationFailed(f"{label} listesi kanonik olmali")


def _assert_ref_digest_tuple(values: tuple[tuple[str, str], ...]) -> None:
    refs = tuple(reference for reference, _digest in values)
    if refs != tuple(sorted(set(refs))) or any(not reference.strip() for reference in refs):
        raise ValidationFailed("Evidence refs dolu, tekil ve kanonik olmali")
    for _reference, evidence_digest in values:
        parse_digest(evidence_digest)


def require_progress_packet(
    *,
    attempt_ordinal: int,
    packet: LoopProgressPacket | None,
    objective_digest: str,
    source_revision: str,
    plan_digest: str,
    policy_revision_digest: str,
    validator_asset_manifest_digest: str,
) -> None:
    """Fail closed when attempt 2+ lacks a fresh, exactly-bound packet."""

    if attempt_ordinal < 1:
        raise ValidationFailed("Loop attempt ordinal 1'den kucuk olamaz")
    if attempt_ordinal == 1:
        if packet is not None:
            raise PolicyViolation("Ilk loop attempt progress packet tasiyamaz")
        return
    if packet is None:
        raise PolicyViolation("Loop attempt 2+ progress packet olmadan baslayamaz")
    expected = (
        attempt_ordinal,
        objective_digest,
        source_revision,
        plan_digest,
        policy_revision_digest,
        validator_asset_manifest_digest,
    )
    observed = (
        packet.attempt_ordinal,
        packet.objective_digest,
        packet.source_revision,
        packet.plan_digest,
        packet.policy_revision_digest,
        packet.validator_asset_manifest_digest,
    )
    if observed != expected:
        raise PolicyViolation("Loop progress packet stale veya baska scope'a bagli")
