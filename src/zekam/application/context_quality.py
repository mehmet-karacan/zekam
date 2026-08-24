"""Context compiler v2 golden safety, quality ve cost degerlendirmesi."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.context_continuity import ContextManifest, OmittedReason
from zekam.domain.errors import PolicyViolation, ValidationFailed


def _ppm(numerator: int, denominator: int) -> int:
    return 0 if denominator == 0 else min(1_000_000, numerator * 1_000_000 // denominator)


def _coverage_ppm(numerator: int, denominator: int) -> int:
    return 1_000_000 if denominator == 0 else _ppm(numerator, denominator)


@dataclass(frozen=True, slots=True)
class ContextGoldenCase:
    case_id: str
    role: str
    required_candidate_ids: frozenset[str]
    forbidden_candidate_ids: frozenset[str]
    relevant_candidate_ids: frozenset[str]
    citation_candidate_ids: frozenset[str]
    conflict_candidate_ids: frozenset[str]
    compatible_revision_candidate_ids: frozenset[str]
    expected_omission_reasons: tuple[tuple[str, OmittedReason], ...]
    max_tokens: int

    def __post_init__(self) -> None:
        if not self.case_id.strip() or not self.role.strip() or self.max_tokens < 1:
            raise ValidationFailed("Context golden case identity/budget gecersiz")
        if self.required_candidate_ids & self.forbidden_candidate_ids:
            raise ValidationFailed("Context golden required/forbidden cakismasi")


@dataclass(frozen=True, slots=True)
class ContextQualityMetricsV2:
    case_id: str
    manifest_digest: str
    required_recall_ppm: int
    forbidden_inclusion_count: int
    relevance_recall_ppm: int
    duplicate_token_ratio_ppm: int
    citation_coverage_ppm: int
    conflict_visibility_ppm: int
    source_revision_compatibility_ppm: int
    ordering_stability_ppm: int
    role_leakage_count: int
    verified_outcome_quality_ppm: int
    baseline_outcome_quality_ppm: int
    selected_tokens: int
    baseline_selected_tokens: int
    safety_passed: bool
    quality_or_cost_improved: bool

    def __post_init__(self) -> None:
        parse_digest(self.manifest_digest)
        ppm_values = (
            self.required_recall_ppm,
            self.relevance_recall_ppm,
            self.duplicate_token_ratio_ppm,
            self.citation_coverage_ppm,
            self.conflict_visibility_ppm,
            self.source_revision_compatibility_ppm,
            self.ordering_stability_ppm,
            self.verified_outcome_quality_ppm,
            self.baseline_outcome_quality_ppm,
        )
        if any(not 0 <= value <= 1_000_000 for value in ppm_values):
            raise ValidationFailed("Context quality ppm araligi gecersiz")
        if self.forbidden_inclusion_count < 0 or self.role_leakage_count < 0:
            raise ValidationFailed("Context quality inclusion sayisi negatif olamaz")
        if not self.safety_passed:
            raise PolicyViolation("Context quality safety regression kabul edilemez")
        if not self.quality_or_cost_improved:
            raise PolicyViolation("Context quality veya cost iyilesmesi kanitlanmadi")

    @property
    def metrics_digest(self) -> str:
        return digest(asdict(self))


def evaluate_context_quality(
    case: ContextGoldenCase,
    manifest: ContextManifest,
    *,
    reordered_manifest: ContextManifest,
    verified_outcome_quality_ppm: int,
    baseline_outcome_quality_ppm: int,
    baseline_selected_tokens: int,
) -> ContextQualityMetricsV2:
    if manifest.target_role != case.role or manifest.token_budget > case.max_tokens:
        raise PolicyViolation("Context golden role/budget binding drift")
    selected = {item.candidate_id for item in manifest.selected}
    omitted = {item.candidate_id: item.reason for item in manifest.omitted}
    required_recall = _coverage_ppm(
        len(case.required_candidate_ids & selected), len(case.required_candidate_ids)
    )
    relevance_recall = _coverage_ppm(
        len(case.relevant_candidate_ids & selected), len(case.relevant_candidate_ids)
    )
    citation_coverage = _coverage_ppm(
        len(case.citation_candidate_ids & selected), len(case.citation_candidate_ids)
    )
    conflict_visibility = _coverage_ppm(
        sum(omitted.get(item) is OmittedReason.CONFLICT for item in case.conflict_candidate_ids),
        len(case.conflict_candidate_ids),
    )
    revision_compatibility = _coverage_ppm(
        len(case.compatible_revision_candidate_ids & selected),
        len(case.compatible_revision_candidate_ids),
    )
    stable = tuple(item.candidate_id for item in manifest.selected) == tuple(
        item.candidate_id for item in reordered_manifest.selected
    ) and tuple((item.candidate_id, item.reason) for item in manifest.omitted) == tuple(
        (item.candidate_id, item.reason) for item in reordered_manifest.omitted
    )
    expected_omissions_match = all(
        omitted.get(key) is reason for key, reason in case.expected_omission_reasons
    )
    forbidden_count = len(case.forbidden_candidate_ids & selected)
    metrics = manifest.compiler_metrics
    if metrics is None:
        raise PolicyViolation("Context golden compiler metrics ister")
    selected_tokens = sum(item.token_count for item in manifest.selected)
    safety = (
        required_recall == 1_000_000
        and forbidden_count == 0
        and conflict_visibility == 1_000_000
        and revision_compatibility == 1_000_000
        and stable
        and expected_omissions_match
    )
    return ContextQualityMetricsV2(
        case.case_id,
        manifest.manifest_digest,
        required_recall,
        forbidden_count,
        relevance_recall,
        metrics.duplicate_token_ratio_ppm,
        citation_coverage,
        conflict_visibility,
        revision_compatibility,
        1_000_000 if stable else 0,
        forbidden_count,
        verified_outcome_quality_ppm,
        baseline_outcome_quality_ppm,
        selected_tokens,
        baseline_selected_tokens,
        safety,
        verified_outcome_quality_ppm > baseline_outcome_quality_ppm
        or selected_tokens < baseline_selected_tokens,
    )
