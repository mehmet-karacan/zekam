from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from tests.unit.test_model_context_experiment import _arm

from zekam.domain import model_context_experiment as context
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_context_experiment import (
    ContextAblationProfile,
    ModelContextExperimentPolicy,
    compare_model_context_arms,
)
from zekam.domain.optimization import ProgressState

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "changes",
    [
        {"max_quality_drop": -1.0},
        {"max_reliability_drop": float("nan")},
        {"minimum_value_ratio": 0.99},
        {"minimum_value_ratio": float("inf")},
        {"policy_version": " "},
    ],
)
def test_experiment_policy_rejects_invalid_tolerance_ratio_and_version(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationFailed):
        ModelContextExperimentPolicy(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"profile_version": " "},
        {"included_features": ("z", "a")},
        {"included_features": ("same", "same")},
        {"excluded_features": (" ",)},
        {"included_features": ("same",), "excluded_features": ("same",)},
        {"included_features": ()},
    ],
)
def test_ablation_profile_rejects_invalid_version_sets_overlap_and_empty(
    changes: dict[str, Any],
) -> None:
    values: dict[str, Any] = {"included_features": ("source",), "excluded_features": ()}
    values.update(changes)
    with pytest.raises(ValidationFailed):
        ContextAblationProfile(**values)


def test_evidence_manifest_rejects_empty_scope_low_repetitions_and_cardinality() -> None:
    manifest = _arm("baseline", "model-a", "full").evidence_manifest
    with pytest.raises(ValidationFailed, match="identity/scope"):
        replace(manifest, workload=" ")
    with pytest.raises(PolicyViolation, match="bes repetition"):
        replace(manifest, repetitions=4)
    with pytest.raises(ValidationFailed, match="repetition sayisi"):
        replace(manifest, trial_evidence_digests=manifest.trial_evidence_digests[:-1])


def test_arm_rejects_empty_identity_verdict_cardinality_and_verdict_binding() -> None:
    arm = _arm("baseline", "model-a", "full")
    with pytest.raises(ValidationFailed, match="kimlik"):
        replace(arm, arm_id=" ")
    with pytest.raises(ValidationFailed, match="cardinality"):
        replace(arm, verifier_verdicts=())
    verdicts = list(arm.verifier_verdicts)
    verdicts[0] = replace(verdicts[0], tested_model_id="different")
    with pytest.raises(ValidationFailed, match="verdict trial binding"):
        replace(arm, verifier_verdicts=tuple(verdicts))
    forged = (digest("forged"), *arm.evidence_manifest.trial_evidence_digests[1:])
    with pytest.raises(ValidationFailed, match="trial provenance"):
        replace(
            arm,
            evidence_manifest=replace(arm.evidence_manifest, trial_evidence_digests=forged),
        )


def test_pair_rejects_same_arm_scope_same_model_profile_and_uncontrolled_delta() -> None:
    baseline = _arm("baseline", "model-a", "full")
    candidate = _arm("candidate", "model-b", "focused", cost=0.8)
    with pytest.raises(ValidationFailed, match="arms ayri"):
        compare_model_context_arms(
            baseline, replace(candidate, arm_id="baseline"), ModelContextExperimentPolicy()
        )

    drifted_scope = replace(
        candidate,
        source_revision="other-source",
        evidence_manifest=replace(
            candidate.evidence_manifest,
            source_revision="other-source",
        ),
    )
    with pytest.raises(ValidationFailed, match="paired scope drift"):
        compare_model_context_arms(baseline, drifted_scope, ModelContextExperimentPolicy())

    same_model_profile_base = _arm("candidate", "model-a", "focused", cost=0.8)
    same_model_profile = replace(
        same_model_profile_base,
        ablation_profile=baseline.ablation_profile,
        evidence_manifest=replace(
            same_model_profile_base.evidence_manifest,
            ablation_profile_digest=baseline.ablation_profile.profile_digest,
        ),
    )
    with pytest.raises(ValidationFailed, match="model veya ablation"):
        compare_model_context_arms(baseline, same_model_profile, ModelContextExperimentPolicy())

    focused_baseline = _arm("focused-baseline", "model-a", "focused")
    uncontrolled = _arm("full-candidate", "model-b", "full", cost=0.8)
    with pytest.raises(ValidationFailed, match="kontrollu feature delta"):
        compare_model_context_arms(focused_baseline, uncontrolled, ModelContextExperimentPolicy())


def test_metric_guards_reject_negative_nonfinite_and_unit_interval_drift() -> None:
    metric = _arm("baseline", "model-a", "full").aggregate.quality
    with pytest.raises(ValidationFailed, match="metrikleri gecersiz"):
        context._validate_metric("quality", replace(metric, mean=-1.0))
    with pytest.raises(ValidationFailed, match=r"0\.\.1"):
        context._validate_metric("quality", replace(metric, mean=2.0, median=2.0, p95=2.0))


def test_generic_metric_and_pair_defensive_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = _arm("baseline", "model-a", "full")
    candidate = _arm("candidate", "model-b", "focused", cost=0.8)
    monkeypatch.setattr(
        context,
        "evaluate_progress",
        lambda *_args, **_kwargs: SimpleNamespace(progress_state=ProgressState.INVALID),
    )
    with pytest.raises(ValidationFailed, match="generic metric vector invalid"):
        context._comparison(baseline, candidate, ModelContextExperimentPolicy())

    monkeypatch.undo()
    drifted_manifest = replace(
        candidate.evidence_manifest,
        paired_trial_set_digest=digest("different-pair-set"),
    )
    monkeypatch.setattr(context.ExperimentArm, "__post_init__", lambda _self: None)
    drifted_candidate = replace(candidate, evidence_manifest=drifted_manifest)
    with pytest.raises(ValidationFailed, match="paired trial set drift"):
        context._validate_pair(baseline, drifted_candidate)


def test_arm_rejects_unsafe_approved_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    arm = _arm("baseline", "model-a", "full")
    forged = replace(arm.aggregate, approved=True, unsafe=True)
    monkeypatch.setattr(context, "aggregate_trials", lambda *_args, **_kwargs: forged)
    with pytest.raises(ValidationFailed, match="unsafe aggregate approved"):
        replace(arm, aggregate=forged)


def test_result_rejects_negative_values_after_semantic_recompute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = compare_model_context_arms(
        _arm("baseline", "model-a", "full"),
        _arm("candidate", "model-b", "focused", cost=0.8),
        ModelContextExperimentPolicy(),
    )
    monkeypatch.setattr(
        context,
        "_comparison",
        lambda *_args: (result.gates, -1.0, result.candidate_value, result.value_ratio),
    )
    with pytest.raises(ValidationFailed, match="value metrikleri"):
        replace(result, baseline_value=-1.0)
