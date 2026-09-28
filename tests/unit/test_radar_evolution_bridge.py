"""WP-05: RadarEvolutionBridge maps radar selections to improvement ledger."""

from __future__ import annotations

import pytest

from zekam.application.radar_evolution_bridge import RadarEvolutionBridge
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.improvement_policy import ImprovementChangeClass
from zekam.domain.radar_candidate import (
    RadarCandidateDecision,
    RadarCandidateSelection,
    SourceProvenance,
)

pytestmark = pytest.mark.unit


def _source() -> SourceProvenance:
    return SourceProvenance(
        repository_id=42,
        owner="openai",
        name="codex",
        commit_sha="abc123" * 6,
        path="README.md",
        range="1-10",
        evidence_digest=digest("readme sample"),
    )


def _gap_selection(
    *,
    resources: tuple[str, ...] = ("local-index",),
    decision: RadarCandidateDecision = RadarCandidateDecision.GAP_DEMONSTRATED,
) -> RadarCandidateSelection:
    """Return a minimal gap-derived selection for bridge tests."""

    return RadarCandidateSelection(
        selection_id="radar-sel:test-001",
        campaign_id="camp-1",
        card_id="gap-1",
        decision=decision,
        problem="coordinator text replaces child evidence",
        local_evidence="unit tests lack child payload binding",
        upstream_evidence="source-reviewed",
        smallest_actionable_solution="bind real child payload digest to findings",
        affected_logical_resources=resources,
        expected_benefit="high",
        risk="medium",
        maintenance_burden="low",
        dependencies="",
        acceptance_test="child payload digest appears in final report",
        rollback="revert fan-in change",
    )


def test_bridge_returns_improvement_candidate_with_review_required() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection(resources=("local-index",))
    candidate = bridge.to_improvement_candidate(
        selection,
        local_failure_card_digest=digest("failure"),
        local_baseline_aggregate_digest=digest("baseline"),
        evaluation_dataset_contract_digest=digest("dataset-contract"),
    )
    assert candidate.change_class is ImprovementChangeClass.REVIEW_REQUIRED
    assert candidate.allowed_resources == ("local-index",)
    assert candidate.objective == selection.smallest_actionable_solution
    assert candidate.observed_problem == selection.problem
    assert candidate.rollback_plan == selection.rollback
    assert candidate.failure_card_digest == digest("failure")
    assert candidate.baseline_aggregate_digest == digest("baseline")
    assert candidate.patch_digest == selection.selection_digest
    assert candidate.proposer_ref == "radar-evolution-bridge"
    assert candidate.source_revision == selection.selection_id
    assert candidate.body()["schema"] == "zekam-local-improvement-candidate/v1"
    assert candidate.candidate_digest.startswith("sha256:")


def test_bridge_escalates_to_human_approval_for_schema_resource() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection(resources=("local-index", "schema"))
    candidate = bridge.to_improvement_candidate(
        selection,
        local_failure_card_digest=digest("failure"),
        local_baseline_aggregate_digest=digest("baseline"),
        evaluation_dataset_contract_digest=digest("dataset-contract"),
    )
    assert candidate.change_class is ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED


def test_bridge_escalates_rejected_risk_to_human_approval() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection(
        resources=("local-index",), decision=RadarCandidateDecision.REJECTED_RISK
    )
    candidate = bridge.to_improvement_candidate(
        selection,
        local_failure_card_digest=digest("failure"),
        local_baseline_aggregate_digest=digest("baseline"),
        evaluation_dataset_contract_digest=digest("dataset-contract"),
    )
    assert candidate.change_class is ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED


def test_bridge_never_produces_auto_safe() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection(resources=("local-cache",))
    candidate = bridge.to_improvement_candidate(
        selection,
        local_failure_card_digest=digest("failure"),
        local_baseline_aggregate_digest=digest("baseline"),
        evaluation_dataset_contract_digest=digest("dataset-contract"),
    )
    assert candidate.change_class is not ImprovementChangeClass.AUTO_SAFE
    assert candidate.change_class in {
        ImprovementChangeClass.REVIEW_REQUIRED,
        ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED,
    }


def test_bridge_rejects_missing_failure_card_digest() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection()
    with pytest.raises(ValidationFailed):
        bridge.to_improvement_candidate(
            selection,
            local_failure_card_digest=None,
            local_baseline_aggregate_digest=digest("baseline"),
            evaluation_dataset_contract_digest=digest("dataset-contract"),
        )


def test_bridge_rejects_empty_failure_card_digest() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection()
    with pytest.raises(ValidationFailed):
        bridge.to_improvement_candidate(
            selection,
            local_failure_card_digest="   ",
            local_baseline_aggregate_digest=digest("baseline"),
            evaluation_dataset_contract_digest=digest("dataset-contract"),
        )


def test_bridge_rejects_missing_baseline_digest() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection()
    with pytest.raises(ValidationFailed):
        bridge.to_improvement_candidate(
            selection,
            local_failure_card_digest=digest("failure"),
            local_baseline_aggregate_digest=None,
            evaluation_dataset_contract_digest=digest("dataset-contract"),
        )


def test_bridge_rejects_invalid_failure_card_digest() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection()
    with pytest.raises(ValidationFailed):
        bridge.to_improvement_candidate(
            selection,
            local_failure_card_digest="not-a-digest",
            local_baseline_aggregate_digest=digest("baseline"),
            evaluation_dataset_contract_digest=digest("dataset-contract"),
        )


def test_bridge_rejects_invalid_dataset_contract_digest() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection()
    with pytest.raises(ValidationFailed):
        bridge.to_improvement_candidate(
            selection,
            local_failure_card_digest=digest("failure"),
            local_baseline_aggregate_digest=digest("baseline"),
            evaluation_dataset_contract_digest="bad-digest",
        )


def test_bridge_rejects_non_selection_input() -> None:
    bridge = RadarEvolutionBridge()
    with pytest.raises(ValidationFailed):
        bridge.to_improvement_candidate(
            "not-a-selection",  # type: ignore[arg-type]
            local_failure_card_digest=digest("failure"),
            local_baseline_aggregate_digest=digest("baseline"),
            evaluation_dataset_contract_digest=digest("dataset-contract"),
        )


def test_bridge_rejects_unknown_resource_for_any_change_class() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection(resources=("unknown-resource",))
    with pytest.raises(PolicyViolation):
        bridge.to_improvement_candidate(
            selection,
            local_failure_card_digest=digest("failure"),
            local_baseline_aggregate_digest=digest("baseline"),
            evaluation_dataset_contract_digest=digest("dataset-contract"),
        )


def test_bridge_candidate_body_is_schema_compliant() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection(resources=("local-index",))
    body = bridge.candidate_body(
        selection,
        local_failure_card_digest=digest("failure"),
        local_baseline_aggregate_digest=digest("baseline"),
        evaluation_dataset_contract_digest=digest("dataset-contract"),
    )
    assert body["schema"] == "zekam-local-improvement-candidate/v1"
    assert body["change_class"] == "REVIEW_REQUIRED"
    assert body["allowed_resources"] == ["local-index"]
    assert body["rollback_plan"] == selection.rollback
    assert body["max_provider_calls"] == 0
    assert body["proposer_ref"] == "radar-evolution-bridge"
    # No fake approved/auto_safe flags leak from radar into improvement body.
    assert "approved" not in body
    assert "auto_safe" not in body


def test_bridge_preserves_selection_problem_acceptance_and_rollback() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection(resources=("local-index",))
    candidate = bridge.to_improvement_candidate(
        selection,
        local_failure_card_digest=digest("failure"),
        local_baseline_aggregate_digest=digest("baseline"),
        evaluation_dataset_contract_digest=digest("dataset-contract"),
    )
    assert candidate.observed_problem == selection.problem
    assert candidate.rollback_plan == selection.rollback
    assert candidate.body()["objective"] == selection.smallest_actionable_solution


def test_bridge_hypothesis_summarizes_evidence() -> None:
    bridge = RadarEvolutionBridge()
    selection = _gap_selection(resources=("local-index",))
    candidate = bridge.to_improvement_candidate(
        selection,
        local_failure_card_digest=digest("failure"),
        local_baseline_aggregate_digest=digest("baseline"),
        evaluation_dataset_contract_digest=digest("dataset-contract"),
    )
    assert selection.local_evidence in candidate.hypothesis
    assert selection.upstream_evidence in candidate.hypothesis
