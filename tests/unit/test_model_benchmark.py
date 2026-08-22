"""Benchmark, routing, quota ve deliberation domain testleri."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import (
    HARD_GATE_ORDER,
    BenchmarkPlan,
    CandidateGate,
    DeliberationBudget,
    DeliberationFinding,
    ModelCandidate,
    QuotaObservation,
    QuotaPool,
    QuotaTrust,
    RuntimeObservation,
    RuntimeOutcome,
    TrialResult,
    TrialStatus,
    VerifierIdentity,
    aggregate_trials,
    decide_model,
    quota_pool_order,
    synthesize_deliberation,
)

DIGEST = digest({"test": True})
NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)


def _trial(repetition: int, *, status: TrialStatus = TrialStatus.PASSED) -> TrialResult:
    return TrialResult(
        fixture_digest=DIGEST,
        repetition=repetition,
        status=status,
        parse_ok=True,
        format_ok=True,
        evidence_ok=True,
        verifier_approved=True,
        quality=0.8,
        reliability=0.9,
        latency_ms=repetition * 10,
        input_tokens=100,
        output_tokens=20,
        retry_count=0,
        human_corrections=0,
        estimated_cost=0.01,
        actual_cost=0.009,
        response_digest=digest({"response": repetition}),
        evidence_digest=digest({"evidence": repetition}),
        failure_category=None if status is TrialStatus.PASSED else "unsafe-output",
    )


def _candidate(model_id: str, pool: QuotaPool, **failed: bool) -> ModelCandidate:
    gates = dict.fromkeys(HARD_GATE_ORDER, True)
    for key in failed:
        gates[CandidateGate(key)] = False
    return ModelCandidate(
        model_id=model_id,
        quota_pool=pool,
        evidence_digests=(DIGEST,),
        gates=gates,
        quality=0.9,
        reliability=0.9,
        project_specialization=0.8,
        observed_success=0.8,
        latency_efficiency=0.7,
        token_efficiency=0.7,
        cost_efficiency=0.6,
        correction_efficiency=0.9,
    )


def test_benchmark_plan_requires_five_repetitions() -> None:
    with pytest.raises(PolicyViolation, match="bes"):
        BenchmarkPlan("m", DIGEST, DIGEST, DIGEST, DIGEST, repetitions=4)


def test_aggregate_has_mean_median_p95_variance_and_cost() -> None:
    aggregate = aggregate_trials(
        tuple(_trial(index) for index in range(1, 6)),
        tested_model_id="tested",
        verifier=VerifierIdentity("verifier-model", "worker:verifier", DIGEST),
    )
    assert aggregate.approved
    assert aggregate.latency_ms.mean == 30
    assert aggregate.latency_ms.median == 30
    assert aggregate.latency_ms.p95 == 50
    assert aggregate.latency_ms.variance == 200
    assert aggregate.cost.mean == pytest.approx(0.009)


def test_aggregate_evidence_is_independent_of_trial_input_order() -> None:
    trials = tuple(_trial(index) for index in range(1, 6))
    verifier = VerifierIdentity("verifier-model", "worker:verifier", DIGEST)
    forward = aggregate_trials(trials, tested_model_id="tested", verifier=verifier)
    reverse = aggregate_trials(tuple(reversed(trials)), tested_model_id="tested", verifier=verifier)
    assert forward.evidence_digest == reverse.evidence_digest


def test_single_unsafe_trial_blocks_high_average() -> None:
    trials = tuple(
        _trial(index, status=TrialStatus.UNSAFE if index == 5 else TrialStatus.PASSED)
        for index in range(1, 7)
    )
    aggregate = aggregate_trials(
        trials,
        tested_model_id="tested",
        verifier=VerifierIdentity("verifier-model", "worker:verifier", DIGEST),
    )
    assert aggregate.unsafe
    assert not aggregate.approved


def test_passed_trial_with_failed_contract_is_unsafe_and_does_not_count_as_valid() -> None:
    invalid = _trial(6)
    invalid = TrialResult(
        fixture_digest=invalid.fixture_digest,
        repetition=invalid.repetition,
        status=invalid.status,
        parse_ok=False,
        format_ok=invalid.format_ok,
        evidence_ok=invalid.evidence_ok,
        verifier_approved=invalid.verifier_approved,
        quality=invalid.quality,
        reliability=invalid.reliability,
        latency_ms=invalid.latency_ms,
        input_tokens=invalid.input_tokens,
        output_tokens=invalid.output_tokens,
        retry_count=invalid.retry_count,
        human_corrections=invalid.human_corrections,
        estimated_cost=invalid.estimated_cost,
        actual_cost=invalid.actual_cost,
        response_digest=invalid.response_digest,
        evidence_digest=invalid.evidence_digest,
    )
    aggregate = aggregate_trials(
        (*(_trial(index) for index in range(1, 6)), invalid),
        tested_model_id="tested",
        verifier=VerifierIdentity("verifier", "worker:verifier", DIGEST),
    )
    assert aggregate.unsafe
    assert not aggregate.approved
    with pytest.raises(PolicyViolation, match="valid trial"):
        aggregate_trials(
            (*(_trial(index) for index in range(1, 5)), invalid),
            tested_model_id="tested",
            verifier=VerifierIdentity("verifier", "worker:verifier", DIGEST),
        )


def test_tested_model_cannot_verify_itself() -> None:
    with pytest.raises(PolicyViolation, match="verifier"):
        aggregate_trials(
            tuple(_trial(index) for index in range(1, 6)),
            tested_model_id="m",
            verifier=VerifierIdentity("m", "worker:verifier", DIGEST),
        )


def test_quota_thresholds_use_trusted_pool_observations() -> None:
    observations = (
        QuotaObservation(QuotaPool.CODEX, QuotaTrust.TRUSTED, 0.39, DIGEST, NOW),
        QuotaObservation(QuotaPool.CLAUDE, QuotaTrust.TRUSTED, 0.29, DIGEST, NOW),
    )
    assert quota_pool_order(observations, now=NOW) == (QuotaPool.LOCAL,)


def test_unknown_quota_is_not_guessed() -> None:
    unknown = QuotaObservation(QuotaPool.CODEX, QuotaTrust.UNKNOWN, None, None, NOW)
    assert quota_pool_order((unknown,), now=NOW) == (QuotaPool.CODEX,)
    with pytest.raises(PolicyViolation, match="tahmin"):
        QuotaObservation(QuotaPool.CODEX, QuotaTrust.UNKNOWN, 0.1, None, NOW)


def test_quota_uses_newest_observation_independent_of_tuple_order_and_rejects_stale() -> None:
    older = QuotaObservation(
        QuotaPool.CODEX, QuotaTrust.TRUSTED, 0.9, digest({"quota": "old"}), NOW
    )
    newer = QuotaObservation(
        QuotaPool.CODEX,
        QuotaTrust.TRUSTED,
        0.2,
        digest({"quota": "new"}),
        NOW + dt.timedelta(minutes=1),
    )
    assert quota_pool_order((newer, older), now=NOW + dt.timedelta(minutes=2)) == (
        QuotaPool.CLAUDE,
    )
    assert quota_pool_order((older, newer), now=NOW + dt.timedelta(minutes=2)) == (
        QuotaPool.CLAUDE,
    )
    tied = QuotaObservation(
        QuotaPool.CODEX,
        QuotaTrust.TRUSTED,
        0.7,
        digest({"quota": "tie"}),
        newer.observed_at,
    )
    assert quota_pool_order((newer, tied), now=NOW + dt.timedelta(minutes=2)) == quota_pool_order(
        (tied, newer), now=NOW + dt.timedelta(minutes=2)
    )
    assert quota_pool_order((newer,), now=NOW + dt.timedelta(hours=1)) == (QuotaPool.CODEX,)


def test_runtime_observation_is_authority_free() -> None:
    with pytest.raises(PolicyViolation, match="authority"):
        RuntimeObservation(
            model_id="m",
            workload="code",
            outcome=RuntimeOutcome.SUCCEEDED,
            latency_ms=10,
            input_tokens=20,
            output_tokens=5,
            cost=0.01,
            human_corrections=0,
            evidence_digest=DIGEST,
            observed_at=NOW,
            authority_granted=True,
        )


def test_decision_explains_first_hard_gate_rejection() -> None:
    rejected = _candidate("bad", QuotaPool.CODEX, enabled=False)
    selected = _candidate("good", QuotaPool.CODEX)
    decision = decide_model((rejected, selected), ())
    assert decision.selected_model_id == "good"
    assert decision.rejected == {"bad": ("enabled",)}
    assert not decision.authority_granted


def test_decision_digest_changes_with_gate_and_quota_evidence() -> None:
    candidate = _candidate("good", QuotaPool.CODEX)
    first_quota = QuotaObservation(
        QuotaPool.CODEX, QuotaTrust.TRUSTED, 0.9, digest({"quota": 1}), NOW
    )
    second_quota = QuotaObservation(
        QuotaPool.CODEX, QuotaTrust.TRUSTED, 0.8, digest({"quota": 2}), NOW
    )
    first = decide_model((candidate,), (first_quota,), now=NOW)
    second = decide_model((candidate,), (second_quota,), now=NOW)
    changed_gate = _candidate("good", QuotaPool.CODEX, **{"latency-money-token-budget": False})
    third = decide_model((changed_gate,), (first_quota,), now=NOW)
    assert len({first.evidence_digest, second.evidence_digest, third.evidence_digest}) == 3


def test_bounded_deliberation_keeps_contradiction_for_review() -> None:
    result = synthesize_deliberation(
        question_digest=DIGEST,
        evidence_packet_digest=DIGEST,
        budget=DeliberationBudget(2, 600, 2000, 1.0, 4),
        findings=(
            DeliberationFinding("kimi", 1, digest({"finding": 1}), False),
            DeliberationFinding("opus", 2, digest({"finding": 2}), True),
        ),
        elapsed_seconds=590,
        token_count=1900,
        cost=0.9,
        synthesizer_identity="synthesizer",
    )
    assert result.human_or_verifier_review_required
    assert not result.authority_granted


def test_deliberation_rejects_third_round() -> None:
    with pytest.raises(PolicyViolation, match="iki tur"):
        DeliberationBudget(3, 600, 100, 1, 2)


def test_deliberation_rejects_negative_usage_and_single_participant() -> None:
    finding = DeliberationFinding("kimi", 1, digest({"finding": 1}), False)
    budget = DeliberationBudget(2, 600, 100, 1, 2)
    with pytest.raises(PolicyViolation, match="distinct"):
        synthesize_deliberation(
            question_digest=DIGEST,
            evidence_packet_digest=DIGEST,
            budget=budget,
            findings=(finding,),
            elapsed_seconds=1,
            token_count=1,
            cost=0,
            synthesizer_identity="synth",
        )
    with pytest.raises(ValidationFailed, match="negatif"):
        synthesize_deliberation(
            question_digest=DIGEST,
            evidence_packet_digest=DIGEST,
            budget=budget,
            findings=(finding, DeliberationFinding("opus", 1, digest({"finding": 2}), False)),
            elapsed_seconds=-1,
            token_count=1,
            cost=0,
            synthesizer_identity="synth",
        )
