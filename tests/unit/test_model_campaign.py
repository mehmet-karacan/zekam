"""OpenCode benchmark campaign domain tests."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_campaign import (
    AUDIO_EXCLUSION_REASON,
    CampaignContinuation,
    CampaignMember,
    CampaignMemberDisposition,
    CampaignMemberResult,
    CampaignMemberResultStage,
    CampaignMemberResultStatus,
    CampaignOutcome,
    CampaignOutcomeStatus,
    OpenCodeBenchmarkCampaign,
    ResultAdoption,
    ResultRecoveryEvidence,
)


def _member(name: str = "aihub/code") -> CampaignMember:
    return CampaignMember(
        configured_model_id=name,
        canonical_model_id=f"canonical:{name}",
        modality="code",
        disposition=CampaignMemberDisposition.HEALTH_PENDING,
        fixture_digests=(digest({"fixture": 1}), digest({"fixture": 2})),
    )


def _audio() -> CampaignMember:
    return CampaignMember(
        configured_model_id="aihub/whisper",
        canonical_model_id=None,
        modality="audio_transcription",
        disposition=CampaignMemberDisposition.EXCLUDED_AUDIO,
        exclusion_reason=AUDIO_EXCLUSION_REASON,
    )


def _campaign(*members: CampaignMember, repetitions: int = 5) -> OpenCodeBenchmarkCampaign:
    return OpenCodeBenchmarkCampaign(
        campaign_key="opencode-aihub",
        revision=3,
        work_item_id=uuid4(),
        task_plan_id=uuid4(),
        source_revision="30da004",
        provider_ref="aihub",
        catalog_digest=digest("catalog"),
        endpoint_identity_digest=digest("endpoint-identity"),
        inventory_digest=digest("inventory"),
        policy_digest=digest("policy"),
        fixture_registry_digest=digest("fixtures"),
        verifier_identity="independent-verifier",
        verifier_provenance_digest=digest("verifier"),
        source_digest=digest("source"),
        repetitions=repetitions,
        verifier_provider_calls_per_trial=1,
        members=tuple(members) or (_member(), _audio()),
    )


def test_campaign_manifest_is_health_result_independent_and_budgeted() -> None:
    campaign = _campaign()
    assert campaign.configured_model_count == 2
    assert campaign.member_count == 2
    assert campaign.eligible_model_count == 1
    assert campaign.audio_excluded_count == 1
    assert campaign.health_call_budget == 1
    assert campaign.tested_call_budget == 10
    assert campaign.provider_call_budget == 21
    assert "health_evidence_digest" not in campaign.as_dict()["members"][0]


def test_campaign_digest_is_stable_across_member_and_fixture_order() -> None:
    first = _member("aihub/first")
    second = _member("aihub/second")
    reordered_first = CampaignMember(
        configured_model_id=first.configured_model_id,
        canonical_model_id=first.canonical_model_id,
        modality=first.modality,
        disposition=first.disposition,
        fixture_digests=tuple(reversed(first.fixture_digests)),
    )
    campaign = _campaign(first, second)
    assert (
        campaign.campaign_digest
        == replace(campaign, members=(second, reordered_first)).campaign_digest
    )


def test_audio_is_explicit_zero_call_exclusion() -> None:
    audio = _audio()
    assert audio.tested_call_budget(5) == 0
    assert audio.suite_digest is None
    with pytest.raises(PolicyViolation, match="excluded"):
        CampaignMember(
            configured_model_id="audio",
            canonical_model_id="canonical-audio",
            modality="audio_transcription",
            disposition=CampaignMemberDisposition.HEALTH_PENDING,
            fixture_digests=(digest("fixture"),),
        )


def test_ambiguous_or_silently_excluded_configured_model_fails_closed() -> None:
    with pytest.raises(PolicyViolation, match="belirsiz"):
        CampaignMember(
            configured_model_id="aihub/ambiguous",
            canonical_model_id=None,
            modality="code",
            disposition=CampaignMemberDisposition.HEALTH_PENDING,
            fixture_digests=(digest("fixture"),),
        )
    with pytest.raises(PolicyViolation, match="sessizce"):
        CampaignMember(
            configured_model_id="aihub/code",
            canonical_model_id="canonical-code",
            modality="code",
            disposition=CampaignMemberDisposition.EXCLUDED_AUDIO,
            exclusion_reason=AUDIO_EXCLUSION_REASON,
        )


def test_one_configured_route_can_expand_to_two_exact_canonical_targets() -> None:
    first = _member("aihub/reranker")
    second = replace(first, canonical_model_id="canonical:aihub/reranker-v2")
    campaign = _campaign(first, second, _audio())
    assert campaign.configured_model_count == 2
    assert campaign.member_count == 3
    assert campaign.eligible_model_count == 2
    with pytest.raises(ValidationFailed, match="target pair"):
        _campaign(first, first)


def test_campaign_repetition_and_metadata_are_fail_closed() -> None:
    with pytest.raises(ValidationFailed, match="en az 5"):
        _campaign(_member(), repetitions=4)
    with pytest.raises(PolicyViolation, match="endpoint"):
        CampaignMember(
            configured_model_id="https://provider.invalid/model",
            canonical_model_id="model",
            modality="code",
            disposition=CampaignMemberDisposition.HEALTH_PENDING,
            fixture_digests=(digest("fixture"),),
        )


def test_health_and_benchmark_results_have_distinct_invariants() -> None:
    health = CampaignMemberResult(
        stage=CampaignMemberResultStage.HEALTH,
        status=CampaignMemberResultStatus.PASSED,
        evidence_digest=digest("health"),
        actual_tested_call_count=0,
        actual_provider_call_count=1,
    )
    assert health.aggregate_id is None
    benchmark = CampaignMemberResult(
        stage=CampaignMemberResultStage.BENCHMARK,
        status=CampaignMemberResultStatus.PASSED,
        evidence_digest=digest("benchmark"),
        actual_tested_call_count=5,
        actual_provider_call_count=10,
        aggregate_id=uuid4(),
    )
    assert benchmark.result_digest != health.result_digest
    with pytest.raises(ValidationFailed, match="aggregate"):
        CampaignMemberResult(
            stage=CampaignMemberResultStage.BENCHMARK,
            status=CampaignMemberResultStatus.PASSED,
            evidence_digest=digest("missing"),
            actual_tested_call_count=5,
            actual_provider_call_count=5,
        )


def test_terminal_outcome_status_matches_counts() -> None:
    passed = CampaignOutcome(
        status=CampaignOutcomeStatus.PASSED,
        passed_count=1,
        failed_count=0,
        recovery_required_count=0,
        audio_excluded_count=1,
        actual_tested_call_count=5,
        actual_provider_call_count=11,
        evidence_digest=digest("outcome"),
    )
    assert passed.outcome_digest.startswith("sha256:")
    with pytest.raises(ValidationFailed, match="Passed campaign"):
        CampaignOutcome(
            status=CampaignOutcomeStatus.PASSED,
            passed_count=0,
            failed_count=1,
            recovery_required_count=0,
            audio_excluded_count=0,
            actual_tested_call_count=0,
            actual_provider_call_count=1,
            evidence_digest=digest("invalid"),
        )


def test_continuation_is_explicit_and_preserves_legacy_digest_profile() -> None:
    parent = _campaign()
    assert "benchmark_suite_version" not in parent.as_dict()
    continuation = replace(
        parent,
        revision=4,
        source_revision="source-after-bugfix",
        source_digest=digest("source-after-bugfix"),
        continuation=CampaignContinuation(
            parent_campaign_id=uuid4(),
            parent_source_revision=parent.source_revision,
            compatibility_evidence_digest=digest("compatibility"),
            continuation_provenance_digest=digest("continuation"),
            maximum_tested_call_count=5,
            maximum_provider_call_count=10,
        ),
    )
    assert continuation.campaign_digest != parent.campaign_digest
    assert continuation.as_dict()["benchmark_suite_version"] == 1
    with pytest.raises(ValidationFailed, match="en az 2"):
        replace(continuation, revision=1)
    larger = _campaign(_member("aihub/one"), _member("aihub/two"), _member("aihub/three"))
    bounded = replace(
        larger,
        revision=4,
        continuation=CampaignContinuation(
            parent_campaign_id=uuid4(),
            parent_source_revision=larger.source_revision,
            compatibility_evidence_digest=digest("compatibility-54"),
            continuation_provenance_digest=digest("continuation-54"),
            maximum_tested_call_count=27,
            maximum_provider_call_count=54,
        ),
    )
    assert bounded.current_provider_call_budget == 54


def test_adopted_result_is_zero_call_and_recovery_result_is_not_adoptable() -> None:
    aggregate_id = uuid4()
    adopted = CampaignMemberResult(
        stage=CampaignMemberResultStage.BENCHMARK,
        status=CampaignMemberResultStatus.PASSED,
        evidence_digest=digest("parent-result"),
        actual_tested_call_count=0,
        actual_provider_call_count=0,
        aggregate_id=aggregate_id,
        adoption=ResultAdoption(uuid4(), digest("adoption")),
    )
    assert adopted.result_digest.startswith("sha256:")
    with pytest.raises(PolicyViolation, match="Recovery-required"):
        CampaignMemberResult(
            stage=CampaignMemberResultStage.HEALTH,
            status=CampaignMemberResultStatus.RECOVERY_REQUIRED,
            evidence_digest=digest("recovery"),
            actual_tested_call_count=0,
            actual_provider_call_count=0,
            failure_category="provider-failure",
            adoption=ResultAdoption(uuid4(), digest("invalid-adoption")),
        )


def test_completed_claim_recovery_is_exact_failed_health_zero_call() -> None:
    recovered = CampaignMemberResult(
        stage=CampaignMemberResultStage.HEALTH,
        status=CampaignMemberResultStatus.FAILED,
        evidence_digest=digest("recovered-health"),
        actual_tested_call_count=0,
        actual_provider_call_count=0,
        failure_category="health-contract-failed",
        recovery_evidence=ResultRecoveryEvidence(uuid4(), uuid4(), digest("claim-recovery")),
    )
    assert recovered.aggregate_id is None
    with pytest.raises(PolicyViolation, match="exact failed health"):
        replace(recovered, failure_category="different")
