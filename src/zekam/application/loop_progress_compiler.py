"""Deterministic bounded hydration compiler for measured loop attempts."""

from __future__ import annotations

from dataclasses import dataclass

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.loop_progress import LoopProgressCheckpoint, LoopProgressPacket


@dataclass(frozen=True, slots=True)
class LoopProgressCompilerPolicy:
    max_packet_tokens: int = 2048
    max_rejected_hypotheses: int = 8
    max_new_evidence_refs: int = 8
    max_forbidden_retries: int = 8
    max_focus_characters: int = 512
    max_reference_characters: int = 256

    def __post_init__(self) -> None:
        if self.max_packet_tokens < 128:
            raise ValidationFailed("Progress packet token budget en az 128 olmali")
        if min(
            self.max_rejected_hypotheses,
            self.max_new_evidence_refs,
            self.max_forbidden_retries,
        ) < 0:
            raise ValidationFailed("Progress packet liste limitleri negatif olamaz")
        if self.max_focus_characters < 1 or self.max_reference_characters < 1:
            raise ValidationFailed("Progress packet metin limitleri pozitif olmali")


class LoopProgressCompiler:
    """Compile only predecessor state; full history is neither accepted nor emitted."""

    def __init__(self, policy: LoopProgressCompilerPolicy | None = None) -> None:
        self.policy = policy or LoopProgressCompilerPolicy()

    def compile(self, checkpoint: LoopProgressCheckpoint) -> LoopProgressPacket:
        checkpoint.__post_init__()
        focus = _bounded_text(
            checkpoint.next_allowed_focus,
            label="next allowed focus",
            maximum=self.policy.max_focus_characters,
        )
        diagnosis_ref = _bounded_text(
            checkpoint.validator_diagnosis_ref,
            label="validator diagnosis ref",
            maximum=self.policy.max_reference_characters,
        )
        rejected = checkpoint.rejected_hypothesis_digests[
            -self.policy.max_rejected_hypotheses :
        ] if self.policy.max_rejected_hypotheses else ()
        evidence = checkpoint.new_evidence_refs[-self.policy.max_new_evidence_refs :]
        if not self.policy.max_new_evidence_refs:
            evidence = ()
        forbidden = checkpoint.forbidden_retries[-self.policy.max_forbidden_retries :]
        if not self.policy.max_forbidden_retries:
            forbidden = ()

        # Optional reference lists are shed in a deterministic order when the hard
        # token cap is tighter than their configured cardinality cap.
        while True:
            try:
                return LoopProgressPacket(
                    objective_digest=checkpoint.objective_digest,
                    source_revision=checkpoint.source_revision,
                    plan_digest=checkpoint.plan_digest,
                    policy_revision_digest=checkpoint.policy_revision_digest,
                    validator_asset_manifest_digest=checkpoint.validator_asset_manifest_digest,
                    artifact_before_digest=checkpoint.artifact_before_digest,
                    artifact_after_digest=checkpoint.artifact_after_digest,
                    predecessor_attempt_id=checkpoint.predecessor_attempt_id,
                    attempt_ordinal=checkpoint.next_attempt_ordinal,
                    previous_metric_vector=checkpoint.previous_metric_vector,
                    current_metric_vector=checkpoint.current_metric_vector,
                    metric_deltas=checkpoint.current_metric_vector.deltas,
                    accepted_hypothesis_digest=checkpoint.accepted_hypothesis_digest,
                    rejected_hypothesis_digests=rejected,
                    patch_digest=checkpoint.patch_digest,
                    failure_signature=checkpoint.failure_signature,
                    validator_diagnosis_ref=diagnosis_ref,
                    validator_diagnosis_digest=checkpoint.validator_diagnosis_digest,
                    new_evidence_refs=evidence,
                    remaining_attempts=checkpoint.remaining_attempts,
                    remaining_tokens=checkpoint.remaining_tokens,
                    remaining_cost_micros=checkpoint.remaining_cost_micros,
                    remaining_time_seconds=checkpoint.remaining_time_seconds,
                    next_allowed_focus=focus,
                    forbidden_retries=forbidden,
                    max_packet_tokens=self.policy.max_packet_tokens,
                )
            except PolicyViolation as exc:
                if "token budget" not in str(exc):
                    raise
                if evidence:
                    evidence = evidence[1:]
                    continue
                if rejected:
                    rejected = rejected[1:]
                    continue
                if forbidden:
                    forbidden = forbidden[1:]
                    continue
                if len(focus) > 32:
                    focus = focus[: max(32, len(focus) // 2)]
                    continue
                raise PolicyViolation(
                    "Progress packet zorunlu alanlari token budget'e sigmiyor"
                ) from exc


def _bounded_text(value: str, *, label: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValidationFailed(f"Progress packet {label} bos olamaz")
    if any(character in normalized for character in ("\r", "\n", "\x00")):
        raise ValidationFailed(f"Progress packet {label} raw/multiline icerik tasiyamaz")
    return normalized[:maximum]
