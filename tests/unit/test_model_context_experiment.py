"""ZK-P2-006 model/context experiment acceptance tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from zekam.application.model_context_experiment import persist_experiment_proposal
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import (
    TrialResult,
    TrialStatus,
    VerifierIdentity,
    VerifierVerdict,
    aggregate_trials,
)
from zekam.domain.model_context_experiment import (
    ContextAblationProfile,
    ExperimentArm,
    ExperimentDecision,
    ExperimentEvidenceManifest,
    ModelContextExperimentPolicy,
    compare_model_context_arms,
)
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


def _benchmark_evidence(
    model: str,
    context: str,
    *,
    quality: float = 0.90,
    reliability: float = 0.95,
    latency: float = 100.0,
    cost: float = 1.0,
    tokens: float = 1000.0,
    approved: bool = True,
    unsafe: bool = False,
) -> tuple[object, tuple[TrialResult, ...], VerifierIdentity, tuple[VerifierVerdict, ...]]:
    verifier = VerifierIdentity(
        "independent-verifier", "verifier-run-1", digest("verifier-provenance")
    )
    trials: list[TrialResult] = []
    verdicts: list[VerifierVerdict] = []
    for repetition in range(1, 7):
        is_unsafe = unsafe and repetition == 6
        response = digest({"model": model, "context": context, "repetition": repetition})
        trial = TrialResult(
            digest("paired-fixture"),
            repetition,
            TrialStatus.UNSAFE if is_unsafe else TrialStatus.PASSED,
            not is_unsafe,
            not is_unsafe,
            not is_unsafe,
            not is_unsafe,
            quality,
            reliability,
            int(latency),
            int(tokens // 2),
            int(tokens - tokens // 2),
            0,
            0,
            cost,
            cost,
            response,
            digest(
                {
                    "model": model,
                    "context": context,
                    "repetition": repetition,
                    "approved": not is_unsafe,
                }
            ),
            "unsafe" if is_unsafe else None,
        )
        trials.append(trial)
        verdicts.append(
            VerifierVerdict(
                model,
                verifier.model_id,
                verifier.execution_identity,
                response,
                not is_unsafe,
                digest({"verdict": response, "approved": not is_unsafe}),
            )
        )
    aggregate = aggregate_trials(tuple(trials), tested_model_id=model, verifier=verifier)
    assert aggregate.approved is approved
    return aggregate, tuple(trials), verifier, tuple(verdicts)


def _arm(
    arm_id: str,
    model: str,
    context: str,
    **metrics: object,
) -> ExperimentArm:
    packet = digest(f"packet:{context}")
    manifest_digest = digest(f"manifest:{context}")
    route = digest(f"route:{model}")
    profile = (
        ContextAblationProfile(("architecture", "memory", "source"), ())
        if context == "full"
        else ContextAblationProfile(("source",), ("architecture", "memory"))
    )
    aggregate, trials, verifier, verdicts = _benchmark_evidence(
        model,
        context,
        **metrics,  # type: ignore[arg-type]
    )
    ordered = tuple(
        sorted(
            trials,
            key=lambda row: (
                row.fixture_digest,
                row.repetition,
                row.response_digest,
                row.evidence_digest,
            ),
        )
    )
    verdict_by_response = {item.tested_response_digest: item for item in verdicts}
    verdict_digests = tuple(
        digest(
            {
                "tested_model_id": verdict_by_response[row.response_digest].tested_model_id,
                "verifier_model_id": verdict_by_response[row.response_digest].verifier_model_id,
                "execution_identity": verdict_by_response[row.response_digest].execution_identity,
                "tested_response_digest": row.response_digest,
                "approved": verdict_by_response[row.response_digest].approved,
                "evidence_digest": verdict_by_response[row.response_digest].evidence_digest,
            }
        )
        for row in ordered
    )
    evidence = ExperimentEvidenceManifest(
        model,
        packet,
        manifest_digest,
        route,
        profile.profile_digest,
        "source-revision-7",
        digest("suite-v3"),
        "typescript-code",
        digest(
            [
                {"fixture_digest": row.fixture_digest, "repetition": row.repetition}
                for row in ordered
            ]
        ),
        aggregate.evidence_digest,
        aggregate.verifier_model_id,
        aggregate.verifier_execution_identity,
        aggregate.verifier_provenance_digest,
        len(ordered),
        tuple(row.evidence_digest for row in ordered),
        verdict_digests,
    )
    return ExperimentArm(
        arm_id,
        model,
        packet,
        manifest_digest,
        route,
        "source-revision-7",
        digest("suite-v3"),
        "typescript-code",
        profile,
        aggregate,
        evidence,
        trials,
        verifier,
        verdicts,
    )


def test_candidate_with_no_regression_and_better_cost_value_is_proposed() -> None:
    baseline = _arm("baseline", "model-a", "full")
    candidate = _arm(
        "candidate",
        "model-b",
        "focused",
        quality=0.91,
        reliability=0.96,
        latency=95.0,
        cost=0.80,
        tokens=900.0,
    )
    result = compare_model_context_arms(baseline, candidate, ModelContextExperimentPolicy())
    assert result.decision is ExperimentDecision.PROPOSE_CANDIDATE
    assert all(item.passed for item in result.gates)
    assert result.value_ratio > 1.0
    assert not result.grants_authority


@pytest.mark.parametrize(
    ("metrics", "failed_gate"),
    (
        ({"quality": 0.89}, "experiment.quality-no-regression"),
        ({"reliability": 0.90}, "experiment.reliability-no-regression"),
        ({"latency": 120.0}, "experiment.latency-budget"),
        ({"tokens": 1200.0}, "experiment.token-budget"),
        ({"cost": 1.20}, "experiment.cost-budget"),
        ({"unsafe": True, "approved": False}, "experiment.candidate-approved"),
    ),
)
def test_any_no_regression_gate_failure_keeps_baseline(
    metrics: dict[str, object], failed_gate: str
) -> None:
    result = compare_model_context_arms(
        _arm("baseline", "model-a", "full"),
        _arm("candidate", "model-b", "focused", **metrics),
        ModelContextExperimentPolicy(),
    )
    assert result.decision is ExperimentDecision.KEEP_BASELINE
    assert failed_gate in {item.code for item in result.gates if not item.passed}


def test_context_ablation_can_compare_same_model_but_scope_must_be_paired() -> None:
    baseline = _arm("baseline", "model-a", "full")
    focused = _arm("candidate", "model-a", "focused", cost=0.8, tokens=900.0)
    assert (
        compare_model_context_arms(baseline, focused, ModelContextExperimentPolicy()).decision
        is ExperimentDecision.PROPOSE_CANDIDATE
    )
    with pytest.raises(ValidationFailed, match="binding drift"):
        compare_model_context_arms(
            baseline,
            replace(focused, suite_digest=digest("other-suite")),
            ModelContextExperimentPolicy(),
        )
    with pytest.raises(ValidationFailed, match="distinct benchmark evidence"):
        compare_model_context_arms(
            baseline,
            replace(baseline, arm_id="candidate"),
            ModelContextExperimentPolicy(),
        )


def test_model_verifier_independence_and_authority_fail_closed() -> None:
    baseline = _arm("baseline", "model-a", "full")
    with pytest.raises(PolicyViolation, match="verifier"):
        replace(baseline.evidence_manifest, verifier_model_id="model-a")

    result = compare_model_context_arms(
        baseline,
        _arm("candidate", "model-b", "focused", cost=0.8),
        ModelContextExperimentPolicy(),
    )
    with pytest.raises(PolicyViolation, match="authority"):
        replace(result, grants_authority=True)


def test_same_benchmark_evidence_cannot_masquerade_as_context_ablation() -> None:
    baseline = _arm("baseline", "model-a", "full")
    with pytest.raises(ValidationFailed, match="binding drift"):
        replace(
            baseline,
            arm_id="candidate",
            context_packet_digest=digest("candidate-packet"),
            context_manifest_digest=digest("candidate-manifest"),
        )

    candidate = _arm("candidate", "model-b", "focused", cost=0.8)
    fabricated_aggregate = replace(
        candidate.aggregate, evidence_digest=digest("fabricated-candidate-evidence")
    )
    fabricated_manifest = replace(
        candidate.evidence_manifest,
        aggregate_evidence_digest=fabricated_aggregate.evidence_digest,
    )
    with pytest.raises(ValidationFailed, match="canonical trial recompute"):
        replace(
            candidate,
            aggregate=fabricated_aggregate,
            evidence_manifest=fabricated_manifest,
        )


def test_ablation_pair_requires_same_feature_universe_and_controlled_removal() -> None:
    baseline = _arm("baseline", "model-a", "full")
    candidate = _arm("candidate", "model-b", "focused", cost=0.8)
    unrelated = ContextAblationProfile(("unrelated-feature",), ())
    unrelated_manifest = replace(
        candidate.evidence_manifest,
        ablation_profile_digest=unrelated.profile_digest,
    )
    unrelated_arm = replace(
        candidate,
        ablation_profile=unrelated,
        evidence_manifest=unrelated_manifest,
    )
    with pytest.raises(ValidationFailed, match="feature universe"):
        compare_model_context_arms(baseline, unrelated_arm, ModelContextExperimentPolicy())


def test_result_semantics_tamper_and_nonfinite_values_fail_closed() -> None:
    result = compare_model_context_arms(
        _arm("baseline", "model-a", "full"),
        _arm("candidate", "model-b", "focused", cost=0.8),
        ModelContextExperimentPolicy(),
    )
    with pytest.raises(ValidationFailed, match="digest drift"):
        replace(result, experiment_digest=digest("unrelated"))
    with pytest.raises(ValidationFailed, match="semantic drift"):
        replace(result, decision=ExperimentDecision.KEEP_BASELINE)
    with pytest.raises(ValidationFailed, match="semantic drift"):
        replace(result, value_ratio=float("nan"))


def test_review_required_proposal_is_persisted_with_read_after_write(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = compare_model_context_arms(
        _arm("baseline", "model-a", "full"),
        _arm("candidate", "model-b", "focused", cost=0.8),
        ModelContextExperimentPolicy(),
    )
    stored = persist_experiment_proposal(result, LocalContentAddressedStore(tmp_path).ensure())
    assert stored.experiment_digest == result.experiment_digest
    assert stored.review_status == "review-required"
    assert not stored.grants_authority
