"""Radar pattern/gap cards and authority-free candidate decisions.

These types bridge bounded engineering radar campaigns to Zekam's existing
improvement/evaluation/rollout contracts without granting authority or
producing fake benchmarks.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.optimization import MetricSpec

_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")


class RadarCandidateKind(StrEnum):
    PATTERN = "pattern"
    GAP = "gap"
    ALREADY_SATISFIED = "already-satisfied"
    DUPLICATE = "duplicate"
    DEFERRED = "deferred-dependency"


class RadarCandidateDecision(StrEnum):
    PRESENT = "present"
    PARTIAL = "partial"
    GAP_DEMONSTRATED = "gap-demonstrated"
    DIFFERENT_FIT = "different-fit"
    NOT_APPLICABLE = "not-applicable"
    UNKNOWN = "unknown"
    ALREADY_SATISFIED = "already-satisfied"
    DUPLICATE = "duplicate"
    EVIDENCE_INSUFFICIENT = "evidence-insufficient"
    DEFERRED_DEPENDENCY = "deferred-dependency"
    REJECTED_RISK = "rejected-risk"


class EvidenceLevel(StrEnum):
    SOURCE_REVIEWED = "source-reviewed"
    TESTS_REVIEWED = "tests-reviewed"
    RUNTIME_MEASURED = "runtime-measured"
    DOCUMENTATION_REVIEWED = "documentation-reviewed"
    METADATA_ONLY = "metadata-only"


def _assert_relative(value: str, label: str) -> None:
    if PureWindowsPath(value).is_absolute() or value.startswith("/") or "\\" in value:
        raise PolicyViolation(f"{label} absolute path tasiyamaz")
    if ".." in PurePosixPath(value).parts:
        raise PolicyViolation(f"{label} traversal tasiyamaz")


def _safe(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_REF.fullmatch(value):
        raise ValidationFailed(f"{label} canonical ref olmali")
    return value


def _reject_secret(value: str, label: str) -> None:
    if re.search(
        r"(?i)(?:bearer\s+|password|credential|api[-_]?key|\bsk-[A-Za-z0-9]{8,})",
        value,
    ):
        raise PolicyViolation(f"{label} secret benzeri icerik tasiyamaz")


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Immutable upstream source identity for a radar card."""

    repository_id: int
    owner: str
    name: str
    commit_sha: str
    path: str
    range: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.repository_id, int) or self.repository_id <= 0:
            raise ValidationFailed("repository_id pozitif integer olmali")
        for label, value in (
            ("owner", self.owner),
            ("name", self.name),
            ("commit_sha", self.commit_sha),
        ):
            if not value or not value.strip():
                raise ValidationFailed(f"{label} bos olamaz")
        _assert_relative(self.path, "path")
        parse_digest(self.evidence_digest)
        _reject_secret(self.path, "path")

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "owner": self.owner,
            "name": self.name,
            "commit_sha": self.commit_sha,
            "path": self.path,
            "range": self.range,
            "evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True, slots=True)
class RadarPatternCard:
    """Source-verified external pattern observed during a radar campaign."""

    card_id: str
    campaign_id: str
    kind: RadarCandidateKind
    problem: str
    source: SourceProvenance
    observed_behavior: str
    inference_or_assumption: str
    test_evidence_level: EvidenceLevel
    license_reuse_constraint: str
    cost_dependency_limit: str
    not_applicable_conditions: str
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("radar pattern card authority veremez")
        if self.kind is not RadarCandidateKind.PATTERN:
            raise ValidationFailed("RadarPatternCard kind 'pattern' olmali")
        for label, value in (
            ("card_id", self.card_id),
            ("campaign_id", self.campaign_id),
            ("problem", self.problem),
            ("observed_behavior", self.observed_behavior),
            ("license_reuse_constraint", self.license_reuse_constraint),
            ("cost_dependency_limit", self.cost_dependency_limit),
        ):
            if not value or not str(value).strip():
                raise ValidationFailed(f"{label} bos olamaz")
            _reject_secret(str(value), label)
        if self.created_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-radar-pattern-card/v1",
            "card_id": self.card_id,
            "campaign_id": self.campaign_id,
            "kind": str(self.kind),
            "problem": self.problem,
            "source": self.source.as_dict(),
            "observed_behavior": self.observed_behavior,
            "inference_or_assumption": self.inference_or_assumption,
            "test_evidence_level": str(self.test_evidence_level),
            "license_reuse_constraint": self.license_reuse_constraint,
            "cost_dependency_limit": self.cost_dependency_limit,
            "not_applicable_conditions": self.not_applicable_conditions,
            "created_at": self.created_at.isoformat(),
            "grants_authority": False,
        }

    @property
    def card_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class RadarGapCard:
    """Zekam baseline comparison card produced by a radar analysis."""

    card_id: str
    campaign_id: str
    kind: RadarCandidateKind
    current_baseline: str
    source_binding: str
    production_call_path: str
    test_measurement_evidence: str
    covered_areas: str
    uncovered_areas: str
    result: RadarCandidateDecision
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("radar gap card authority veremez")
        if self.kind is not RadarCandidateKind.GAP:
            raise ValidationFailed("RadarGapCard kind 'gap' olmali")
        if self.result not in {
            RadarCandidateDecision.PRESENT,
            RadarCandidateDecision.PARTIAL,
            RadarCandidateDecision.GAP_DEMONSTRATED,
            RadarCandidateDecision.DIFFERENT_FIT,
            RadarCandidateDecision.NOT_APPLICABLE,
            RadarCandidateDecision.UNKNOWN,
        }:
            raise ValidationFailed("gap card result degeri gecersiz")
        for label, value in (
            ("card_id", self.card_id),
            ("campaign_id", self.campaign_id),
            ("current_baseline", self.current_baseline),
            ("source_binding", self.source_binding),
            ("production_call_path", self.production_call_path),
            ("test_measurement_evidence", self.test_measurement_evidence),
            ("covered_areas", self.covered_areas),
            ("uncovered_areas", self.uncovered_areas),
        ):
            if not value or not str(value).strip():
                raise ValidationFailed(f"{label} bos olamaz")
            _reject_secret(str(value), label)
        if self.created_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-radar-gap-card/v1",
            "card_id": self.card_id,
            "campaign_id": self.campaign_id,
            "kind": str(self.kind),
            "current_baseline": self.current_baseline,
            "source_binding": self.source_binding,
            "production_call_path": self.production_call_path,
            "test_measurement_evidence": self.test_measurement_evidence,
            "covered_areas": self.covered_areas,
            "uncovered_areas": self.uncovered_areas,
            "result": str(self.result),
            "created_at": self.created_at.isoformat(),
            "grants_authority": False,
        }

    @property
    def card_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class RadarCandidateSelection:
    """Authority-free automatic selection decision for one candidate."""

    selection_id: str
    campaign_id: str
    card_id: str
    decision: RadarCandidateDecision
    problem: str
    local_evidence: str
    upstream_evidence: str
    smallest_actionable_solution: str
    affected_logical_resources: tuple[str, ...]
    expected_benefit: str
    risk: str
    maintenance_burden: str
    dependencies: str
    acceptance_test: str
    rollback: str
    existing_decision: str | None = None
    existing_campaign_id: str | None = None
    metric_specs: tuple[MetricSpec, ...] | None = None
    change_class_hint: str | None = None
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("radar candidate selection authority veremez")
        required_non_empty = (
            ("selection_id", self.selection_id),
            ("campaign_id", self.campaign_id),
            ("card_id", self.card_id),
            ("problem", self.problem),
            ("local_evidence", self.local_evidence),
            ("upstream_evidence", self.upstream_evidence),
            ("smallest_actionable_solution", self.smallest_actionable_solution),
            ("expected_benefit", self.expected_benefit),
            ("risk", self.risk),
            ("maintenance_burden", self.maintenance_burden),
            ("acceptance_test", self.acceptance_test),
            ("rollback", self.rollback),
        )
        for label, value in required_non_empty:
            if not value or not str(value).strip():
                raise ValidationFailed(f"{label} bos olamaz")
            _reject_secret(str(value), label)
        _reject_secret(self.dependencies, "dependencies")
        if not self.affected_logical_resources:
            raise ValidationFailed("affected_logical_resources bos olamaz")
        if tuple(sorted(set(self.affected_logical_resources))) != self.affected_logical_resources:
            raise ValidationFailed("affected_logical_resources sirali ve benzersiz olmali")
        for resource in self.affected_logical_resources:
            _safe(resource, "affected_logical_resources")
        if self.existing_decision is not None:
            _reject_secret(self.existing_decision, "existing_decision")
        if self.existing_campaign_id is not None:
            _reject_secret(self.existing_campaign_id, "existing_campaign_id")
        if self.metric_specs is not None:
            if not isinstance(self.metric_specs, tuple):
                raise ValidationFailed("metric_specs tuple olmali")
            if any(type(item) is not MetricSpec for item in self.metric_specs):
                raise ValidationFailed("metric_specs elemanlari MetricSpec olmali")
        if self.change_class_hint is not None:
            if not isinstance(self.change_class_hint, str):
                raise ValidationFailed("change_class_hint string olmali")
            _reject_secret(self.change_class_hint, "change_class_hint")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": "zekam-radar-candidate-selection/v1",
            "selection_id": self.selection_id,
            "campaign_id": self.campaign_id,
            "card_id": self.card_id,
            "decision": str(self.decision),
            "problem": self.problem,
            "local_evidence": self.local_evidence,
            "upstream_evidence": self.upstream_evidence,
            "smallest_actionable_solution": self.smallest_actionable_solution,
            "affected_logical_resources": list(self.affected_logical_resources),
            "expected_benefit": self.expected_benefit,
            "risk": self.risk,
            "maintenance_burden": self.maintenance_burden,
            "dependencies": self.dependencies,
            "acceptance_test": self.acceptance_test,
            "rollback": self.rollback,
            "created_at": self.created_at.isoformat(),
            "grants_authority": False,
        }
        if self.existing_decision is not None:
            result["existing_decision"] = self.existing_decision
        if self.existing_campaign_id is not None:
            result["existing_campaign_id"] = self.existing_campaign_id
        if self.metric_specs is not None:
            result["metric_specs"] = [spec.as_dict() for spec in self.metric_specs]
        if self.change_class_hint is not None:
            result["change_class_hint"] = self.change_class_hint
        return result

    @property
    def selection_digest(self) -> str:
        """Stable identity digest of the candidate, excluding campaign-specific fields.

        Two selections that represent the same candidate in different campaigns
        produce the same digest so cross-campaign duplicate detection can link
        them to prior provenance.
        """

        identity = {
            "schema": "zekam-radar-candidate-selection/v1",
            "card_id": self.card_id,
            "decision": str(self.decision),
            "problem": self.problem,
            "local_evidence": self.local_evidence,
            "upstream_evidence": self.upstream_evidence,
            "smallest_actionable_solution": self.smallest_actionable_solution,
            "affected_logical_resources": list(self.affected_logical_resources),
            "expected_benefit": self.expected_benefit,
            "risk": self.risk,
            "maintenance_burden": self.maintenance_burden,
            "dependencies": self.dependencies,
            "acceptance_test": self.acceptance_test,
            "rollback": self.rollback,
            "metric_specs": (
                [spec.as_dict() for spec in self.metric_specs]
                if self.metric_specs is not None
                else None
            ),
            "change_class_hint": self.change_class_hint,
        }
        return digest(identity)


# Evidence levels that are strong enough to claim a source-reviewed promotion.
_PROMOTION_ELIGIBLE_LEVELS: frozenset[EvidenceLevel] = frozenset(
    {
        EvidenceLevel.SOURCE_REVIEWED,
        EvidenceLevel.TESTS_REVIEWED,
        EvidenceLevel.RUNTIME_MEASURED,
    }
)

# License constraint markers that block a reuse-approved claim.
_REUSE_BLOCKING_MARKERS: tuple[str, ...] = (
    "unknown",
    "source-available",
    "path-override",
)


def source_reviewed_promotion_allowed(level: EvidenceLevel) -> bool:
    """Whether a ``documentation/missing`` claim may be promoted to an
    actionable source-reviewed candidate.

    Only real code/test/runtime evidence enables promotion. A README-only or
    failing-code-only observation must not turn into a promotable candidate.
    """
    return level in _PROMOTION_ELIGIBLE_LEVELS


def require_source_reviewed_promotion(level: EvidenceLevel) -> None:
    """Refuse a source-reviewed promotion when evidence is not strong enough."""
    if not source_reviewed_promotion_allowed(level):
        raise PolicyViolation(
            "source-reviewed promotion yanliz SOURCE_REVIEWED/TESTS_REVIEWED/"
            "RUNTIME_MEASURED seviyesinde mumkundur"
        )


def license_reuse_allowed(license_reuse_constraint: str) -> bool:
    """Whether a reuse-approved claim is permitted for the given license rule.

    ``unknown``, ``source-available`` or ``path-override`` constraints cannot
    authorize upstream code/script reuse.
    """
    lowered = license_reuse_constraint.lower()
    return not any(marker in lowered for marker in _REUSE_BLOCKING_MARKERS)


def require_license_reuse_allowed(license_reuse_constraint: str) -> None:
    """Refuse a reuse-approved claim under a blocked license constraint."""
    if not license_reuse_allowed(license_reuse_constraint):
        raise PolicyViolation(
            "reuse-approved yanliz net izin veren lisans kisitinda uretilebilir;"
            " unknown/source-available/path-override reddedilir"
        )


def require_real_measurement_binding(
    *,
    has_real_failure: bool,
    has_real_baseline: bool,
    has_real_eval: bool,
) -> None:
    """Refuse a measured-improvement record without a real evidence binding.

    A speculative external proposal that has no real failure card, no baseline
    aggregate and no evaluation dataset contract cannot claim a measured
    improvement; doing so would fabricate a benchmark reward or a fake receipt.
    """
    missing = [
        label
        for label, present in (
            ("failure", has_real_failure),
            ("baseline", has_real_baseline),
            ("eval", has_real_eval),
        )
        if not present
    ]
    if missing:
        raise PolicyViolation(
            "measured improvement kaydi gercek failure/baseline/eval bagi olmadan "
            f"uretilmez; eksik: {', '.join(missing)}"
        )


def decide_automatic(
    *,
    card_id: str,
    campaign_id: str,
    problem: str,
    local_evidence: str,
    upstream_evidence: str,
    smallest_actionable_solution: str,
    affected_logical_resources: tuple[str, ...],
    expected_benefit: str,
    risk: str,
    maintenance_burden: str,
    dependencies: str,
    acceptance_test: str,
    rollback: str,
    is_code_or_schema_change: bool = False,
    has_duplicate: bool = False,
    already_satisfied: bool = False,
    not_applicable: bool = False,
    existing_decision: str | None = None,
    existing_campaign_id: str | None = None,
) -> RadarCandidateSelection:
    """Produce an authority-free automatic decision without AUTO_SAFE.

    Already-satisfied and not-applicable candidates short-circuit to their own
    decisions; they neither add a module nor a measured claim. Code/schema/
    security proposals are always ``rejected-risk`` because they must go
    through the existing improvement/evaluation/rollout boundary. A previously
    seen candidate is marked ``duplicate`` instead so the prior decision and
    campaign provenance are preserved.
    """

    if already_satisfied:
        decision = RadarCandidateDecision.ALREADY_SATISFIED
    elif not_applicable:
        decision = RadarCandidateDecision.NOT_APPLICABLE
    elif has_duplicate:
        decision = RadarCandidateDecision.DUPLICATE
    elif is_code_or_schema_change:
        decision = RadarCandidateDecision.REJECTED_RISK
    elif not upstream_evidence.strip() or upstream_evidence.strip() == "metadata-only":
        decision = RadarCandidateDecision.EVIDENCE_INSUFFICIENT
    elif dependencies.strip():
        decision = RadarCandidateDecision.DEFERRED_DEPENDENCY
    else:
        decision = RadarCandidateDecision.EVIDENCE_INSUFFICIENT
    return RadarCandidateSelection(
        selection_id=f"radar-sel:{digest(card_id + campaign_id)[7:19]}",
        campaign_id=campaign_id,
        card_id=card_id,
        decision=decision,
        problem=problem,
        local_evidence=local_evidence,
        upstream_evidence=upstream_evidence,
        smallest_actionable_solution=smallest_actionable_solution,
        affected_logical_resources=affected_logical_resources,
        expected_benefit=expected_benefit,
        risk=risk,
        maintenance_burden=maintenance_burden,
        dependencies=dependencies,
        acceptance_test=acceptance_test,
        rollback=rollback,
        existing_decision=existing_decision,
        existing_campaign_id=existing_campaign_id,
    )
