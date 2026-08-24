"""ZK-P1-005 canonical scoring ve metrics sozlesme testleri."""

from __future__ import annotations

import pytest

from zekam.domain.canonical import digest
from zekam.domain.context_scoring import (
    CONTEXT_SCORING_POLICY_DIGEST,
    CONTEXT_SCORING_POLICY_VERSION,
    ContextCompilerMetricsV2,
    ContextRankFeatures,
    ContextScoreV2,
    ScopeProximity,
    SourceRevisionState,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed


def _features() -> ContextRankFeatures:
    return ContextRankFeatures(
        exact_identity=True,
        scope_proximity=ScopeProximity.WORK,
        source_revision_state=SourceRevisionState.CURRENT,
        evidence_strength=3,
        role_relevance=4,
        task_relevance=2,
        freshness_bucket=4,
        conflict_count=0,
        duplicate_group_digest=digest("group"),
        duplicate_group_size=2,
        tokenizer_profile_digest=digest("tokenizer"),
    )


def test_context_rank_features_canonical_ve_typed() -> None:
    features = _features()
    assert features.body()["scope_proximity"] == 3
    assert features.features_digest.startswith("sha256:")
    assert CONTEXT_SCORING_POLICY_VERSION == 2
    assert CONTEXT_SCORING_POLICY_DIGEST.startswith("sha256:")
    with pytest.raises(ValidationFailed, match="Duplicate group"):
        ContextRankFeatures(
            exact_identity=False,
            scope_proximity=ScopeProximity.EXTERNAL,
            source_revision_state=SourceRevisionState.MISMATCH,
            evidence_strength=0,
            role_relevance=0,
            task_relevance=0,
            freshness_bucket=0,
            conflict_count=0,
            duplicate_group_digest=None,
            duplicate_group_size=2,
            tokenizer_profile_digest=digest("tokenizer"),
        )


def test_context_score_v2_lexicographic_orderi_aciklar() -> None:
    score = ContextScoreV2(1, 3, 1, 3, 1, 3, 4, 2, 4, 0, -1, -20, "candidate")
    assert score.lexicographic == (
        1,
        3,
        1,
        3,
        1,
        3,
        4,
        2,
        4,
        0,
        -1,
        -20,
        "candidate",
    )


def test_context_metrics_exact_candidate_ve_token_partitioni_zorlar() -> None:
    metrics = ContextCompilerMetricsV2(
        input_count=3,
        input_tokens=30,
        eligible_count=3,
        eligible_tokens=30,
        selected_count=2,
        selected_tokens=20,
        omitted_count=1,
        omitted_tokens=10,
        required_total=1,
        required_selected=1,
        duplicate_suppressed_count=1,
        duplicate_suppressed_tokens=10,
        token_budget=20,
        token_utilization_ppm=1_000_000,
        token_efficiency_ppm=750_000,
        duplicate_token_ratio_ppm=333_333,
        omission_counts=(("duplicate", 1),),
    )
    assert metrics.metrics_digest.startswith("sha256:")
    assert metrics.body()["duplicate_suppressed_tokens"] == 10
    with pytest.raises(ValidationFailed, match="token partition"):
        ContextCompilerMetricsV2(
            input_count=3,
            input_tokens=31,
            eligible_count=3,
            eligible_tokens=30,
            selected_count=2,
            selected_tokens=20,
            omitted_count=1,
            omitted_tokens=10,
            required_total=1,
            required_selected=1,
            duplicate_suppressed_count=1,
            duplicate_suppressed_tokens=10,
            token_budget=20,
            token_utilization_ppm=1_000_000,
            token_efficiency_ppm=750_000,
            duplicate_token_ratio_ppm=333_333,
            omission_counts=(("duplicate", 1),),
        )
    with pytest.raises(PolicyViolation, match="required recall"):
        ContextCompilerMetricsV2(
            input_count=1,
            input_tokens=10,
            eligible_count=1,
            eligible_tokens=10,
            selected_count=0,
            selected_tokens=0,
            omitted_count=1,
            omitted_tokens=10,
            required_total=1,
            required_selected=0,
            duplicate_suppressed_count=0,
            duplicate_suppressed_tokens=0,
            token_budget=10,
            token_utilization_ppm=0,
            token_efficiency_ppm=0,
            duplicate_token_ratio_ppm=0,
            omission_counts=(("budget-exhausted", 1),),
        )
