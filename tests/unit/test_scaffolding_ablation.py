"""Scaffolding paired ablation and deprecation-candidate tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from zekam.application.scaffolding_ablation import ScaffoldingAblationService
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_context_experiment import ContextAblationProfile
from zekam.domain.scaffolding_ablation import (
    ScaffoldingAblationPair,
    ScaffoldingAblationPolicy,
    ScaffoldingArmEvidence,
    ScaffoldingDeprecationRollbackPlan,
    ScaffoldingDisposition,
    ScaffoldingMetrics,
)


def _arm(arm_id: str, *, candidate: bool, quality: float = 0.9) -> ScaffoldingArmEvidence:
    profile = (
        ContextAblationProfile(("core",), ("critic",))
        if candidate
        else ContextAblationProfile(("core", "critic"), ())
    )
    return ScaffoldingArmEvidence(
        arm_id=arm_id,
        profile=profile,
        tested_model_id="local-model-a",
        execution_profile_digest=digest("same-execution-profile"),
        objective_digest=digest("stable-objective"),
        metric_vector_digest=digest("directed-metrics"),
        validator_asset_manifest_digest=digest("immutable-validator-assets"),
        fixture_set_digest=digest("same-fixtures"),
        paired_trial_set_digest=digest("same-five-pairs"),
        source_revision="revision-1",
        repetitions=5,
        metrics=ScaffoldingMetrics(
            quality=quality,
            reliability=0.95,
            latency_ms=80.0 if candidate else 100.0,
            token_count=800.0 if candidate else 1000.0,
            cost_micros=800.0 if candidate else 1000.0,
        ),
        evidence_manifest_digest=digest(f"evidence-{arm_id}"),
    )


def _rollback() -> ScaffoldingDeprecationRollbackPlan:
    return ScaffoldingDeprecationRollbackPlan(
        feature="critic",
        restore_action_digest=digest("restore-critic-layer"),
        source_revision="revision-1",
        review_ref="review:critic-ablation",
    )


def test_equal_quality_lower_overhead_is_only_deprecation_candidate() -> None:
    pair = ScaffoldingAblationPair(
        _arm("baseline", candidate=False), _arm("cut", candidate=True), "critic"
    )
    result = ScaffoldingAblationService.evaluate(
        pair=pair,
        policy=ScaffoldingAblationPolicy(),
        rollback_plan=_rollback(),
    )
    assert result.disposition is ScaffoldingDisposition.DEPRECATION_CANDIDATE
    assert result.review_status == "review-required"
    assert result.auto_delete is False
    assert result.grants_authority is False
    with pytest.raises(PolicyViolation, match="review-required"):
        replace(result, auto_delete=True)


def test_quality_regression_keeps_scaffolding() -> None:
    pair = ScaffoldingAblationPair(
        _arm("baseline", candidate=False),
        _arm("cut", candidate=True, quality=0.7),
        "critic",
    )
    result = ScaffoldingAblationService.evaluate(
        pair=pair,
        policy=ScaffoldingAblationPolicy(),
        rollback_plan=_rollback(),
    )
    assert result.disposition is ScaffoldingDisposition.KEEP_SCAFFOLDING
    assert "scaffolding.quality-no-regression" in {
        gate.code for gate in result.gates if not gate.passed
    }


def test_paired_controls_and_exact_feature_removal_fail_closed() -> None:
    baseline = _arm("baseline", candidate=False)
    candidate = _arm("cut", candidate=True)
    with pytest.raises(PolicyViolation, match="paired control drift"):
        ScaffoldingAblationPair(
            baseline,
            replace(candidate, objective_digest=digest("changed-objective")),
            "critic",
        )
    with pytest.raises(ValidationFailed, match="exact tek feature"):
        ScaffoldingAblationPair(baseline, candidate, "retry-hint")
