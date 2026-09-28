"""Bridge from authority-free radar selections to measured improvement ledger.

WP-05: Radar candidates must not pretend to be measured improvements.  This
bridge only *prepares* an ``ImprovementCandidate`` contract when real local
failure and baseline evidence digests are supplied.  It never invents
benchmarks, datasets, approved flags, or AUTO_SAFE change classes.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

from zekam.domain.canonical import parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.improvement_policy import ImprovementChangeClass
from zekam.domain.optimization import (
    MetricAggregation,
    MetricDirection,
    MetricRole,
    MetricSpec,
)
from zekam.domain.radar_candidate import (
    RadarCandidateDecision,
    RadarCandidateSelection,
)
from zekam.infrastructure.sqlite.local_improvement import ImprovementCandidate

_HUMAN_APPROVAL_RESOURCES = frozenset(
    {
        "external-effect",
        "retention",
        "root-instruction",
        "schema",
        "security-policy",
    }
)

_REVIEW_REQUIRED_RESOURCES = frozenset(
    {
        "local-cache",
        "local-index",
        "local-projection",
        "local-registry",
        "local-report",
        "prompt-candidate",
        "relation-proposal",
        "routing-candidate",
        "skill-draft",
    }
)

_APPROVED_RESOURCES = _REVIEW_REQUIRED_RESOURCES | _HUMAN_APPROVAL_RESOURCES

_PRIMARY_METRIC = MetricSpec(
    metric_id="quality.mean",
    name="quality",
    unit="score",
    direction=MetricDirection.MAXIMIZE,
    role=MetricRole.PRIMARY,
    source_kind="benchmark",
    target_value=1.0,
    minimum_meaningful_delta=0.05,
    regression_tolerance=0.01,
    aggregation=MetricAggregation.MEAN,
)


class RadarEvolutionBridge:
    """Convert a radar candidate selection into a measured-improvement contract.

    The bridge is deliberately conservative: it never produces ``AUTO_SAFE``
    candidates and refuses to proceed without independently evidenced failure
    and baseline digests.
    """

    def to_improvement_candidate(
        self,
        selection: RadarCandidateSelection,
        local_failure_card_digest: str | None,
        local_baseline_aggregate_digest: str | None,
        evaluation_dataset_contract_digest: str,
    ) -> ImprovementCandidate:
        """Return an ``ImprovementCandidate`` bound to real local evidence.

        ``evaluation_dataset_contract_digest`` is forwarded to the improvement
        ledger at proposal time; it is not embedded in the candidate body.
        """
        if type(selection) is not RadarCandidateSelection:
            raise ValidationFailed("RadarEvolutionBridge exact RadarCandidateSelection ister")
        failure_card = self._require_digest(local_failure_card_digest, "local_failure_card_digest")
        baseline_aggregate = self._require_digest(
            local_baseline_aggregate_digest, "local_baseline_aggregate_digest"
        )
        parse_digest(evaluation_dataset_contract_digest)

        change_class = self._resolve_change_class(selection, hint=selection.change_class_hint)
        allowed_resources = self._allowed_resources(selection, change_class)
        metric_specs = (
            selection.metric_specs if selection.metric_specs is not None else (_PRIMARY_METRIC,)
        )
        regression_guards = tuple(spec.metric_id for spec in metric_specs)

        return ImprovementCandidate(
            candidate_id=uuid4(),
            objective=selection.smallest_actionable_solution,
            observed_problem=selection.problem,
            failure_card_digest=failure_card,
            baseline_aggregate_digest=baseline_aggregate,
            hypothesis=self._hypothesis(selection),
            patch_digest=selection.selection_digest,
            change_class=change_class,
            allowed_resources=allowed_resources,
            metric_specs=metric_specs,
            regression_guards=regression_guards,
            evaluation_plan_digest=evaluation_dataset_contract_digest,
            max_iterations=5,
            max_provider_calls=0,
            max_tokens=100_000,
            max_cost_micros=1_000_000,
            wall_clock_seconds=3_600,
            rollback_plan=selection.rollback,
            proposer_ref="radar-evolution-bridge",
            source_revision=selection.selection_id,
            created_at=dt.datetime.now(dt.UTC),
        )

    @staticmethod
    def _require_digest(value: str | None, label: str) -> str:
        if value is None or not isinstance(value, str) or not value.strip():
            raise ValidationFailed(f"{label} gerekli bir digest olmali")
        parse_digest(value)
        return value

    @staticmethod
    def _resolve_change_class(
        selection: RadarCandidateSelection, *, hint: str | None
    ) -> ImprovementChangeClass:
        """Pick the smallest safe change class; AUTO_SAFE is forbidden."""

        if selection.decision is RadarCandidateDecision.REJECTED_RISK:
            return ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED
        if hint is not None:
            hinted = ImprovementChangeClass(hint)
            if hinted is ImprovementChangeClass.AUTO_SAFE:
                raise PolicyViolation("Radar selection AUTO_SAFE change class yasak")
            return hinted
        if any(
            resource in _HUMAN_APPROVAL_RESOURCES
            for resource in selection.affected_logical_resources
        ):
            return ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED
        if all(
            resource in _REVIEW_REQUIRED_RESOURCES
            for resource in selection.affected_logical_resources
        ):
            return ImprovementChangeClass.REVIEW_REQUIRED
        # Any resource outside the review set forces human approval.
        return ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED

    @staticmethod
    def _allowed_resources(
        selection: RadarCandidateSelection, change_class: ImprovementChangeClass
    ) -> tuple[str, ...]:
        """Return resources that are legal for the resolved change class."""

        allowed = selection.affected_logical_resources
        permitted = (
            _REVIEW_REQUIRED_RESOURCES
            if change_class is ImprovementChangeClass.REVIEW_REQUIRED
            else _APPROVED_RESOURCES
        )
        illegal = set(allowed) - permitted
        if illegal:
            raise PolicyViolation(
                f"Radar selection resources disallowed for {change_class.value}: {sorted(illegal)}"
            )
        return allowed

    @staticmethod
    def _hypothesis(selection: RadarCandidateSelection) -> str:
        """Summarize local and upstream evidence without inventing measurements."""

        return (
            f"Local: {selection.local_evidence.strip()} | "
            f"Upstream: {selection.upstream_evidence.strip()}"
        )

    def candidate_body(
        self,
        selection: RadarCandidateSelection,
        local_failure_card_digest: str | None,
        local_baseline_aggregate_digest: str | None,
        evaluation_dataset_contract_digest: str,
    ) -> dict[str, Any]:
        """Convenience: return the canonical candidate body dictionary."""

        candidate = self.to_improvement_candidate(
            selection,
            local_failure_card_digest,
            local_baseline_aggregate_digest,
            evaluation_dataset_contract_digest,
        )
        return candidate.body()
