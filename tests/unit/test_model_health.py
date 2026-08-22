"""Health probe fixture'lari, sekil dogrulamasi, karantina ve staleness."""

from __future__ import annotations

import datetime as dt

import pytest
from typer.testing import CliRunner

from zekam.application.model_health_service import (
    AuthorizationRequiredProviderProbe,
    ProbeUnavailable,
)
from zekam.application.model_registry import load_inventory
from zekam.domain.errors import ValidationFailed
from zekam.domain.model_health import (
    CONTRACTS_BY_MODALITY,
    SECRET_CANARY,
    CapabilityCheck,
    ContractCapability,
    ProbeFailure,
    ProbeOutcome,
    ProbeStatus,
    QuarantinePolicy,
    StalenessReason,
    assess_staleness,
    consecutive_failures,
    evaluate_health,
    fixture_for,
    validate_chat_shape,
    validate_embedding_shape,
    validate_guardrail_shape,
    validate_rerank_shape,
    validate_shape,
    validate_transcript_shape,
    validate_vision_shape,
    verified_capabilities,
)
from zekam.domain.model_inventory import BenchmarkState, HealthState, Modality
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)


def test_production_probe_requires_exact_authorized_adapter() -> None:
    record = load_inventory().records[0]

    with pytest.raises(ProbeUnavailable, match="Exact authorized"):
        AuthorizationRequiredProviderProbe().run(record, fixture_for(record.modality))


def test_production_health_cli_fails_before_database_without_live_gate() -> None:
    result = CliRunner().invoke(app, ["model", "health", "--uygula"])

    assert result.exit_code == 6
    assert "authorization" in result.stderr
    assert "sentetik probe ile yazilamaz" in result.stderr


def _outcome(status: ProbeStatus, **overrides: object) -> ProbeOutcome:
    defaults: dict[str, object] = {
        "model_id": "m1",
        "modality": Modality.CHAT,
        "fixture_name": "minimal-mesaj",
        "status": status,
        "observed_at": NOW,
    }
    if status is ProbeStatus.FAILED:
        defaults["failure"] = ProbeFailure.SHAPE
    defaults.update(overrides)
    return ProbeOutcome(**defaults)  # type: ignore[arg-type]


# -- fixture'lar ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "modality",
    [
        Modality.CHAT,
        Modality.CODE,
        Modality.COMPLETION,
        Modality.EMBEDDING,
        Modality.RERANK,
        Modality.AUDIO_TRANSCRIPTION,
        Modality.GUARDRAIL,
        Modality.VISION_LANGUAGE,
    ],
)
def test_every_modality_has_a_fixture(modality: Modality) -> None:
    fixture = fixture_for(modality)
    assert fixture.modality is modality
    assert fixture.payload
    assert fixture.expectation


def test_fixture_digest_is_deterministic() -> None:
    assert fixture_for(Modality.CHAT).fixture_digest == fixture_for(Modality.CHAT).fixture_digest


def test_unknown_modality_produces_a_skip_fixture() -> None:
    fixture = fixture_for(Modality.UNKNOWN)
    assert fixture.modality is Modality.UNKNOWN
    assert fixture.payload == {}


def test_fixtures_carry_no_project_content() -> None:
    """Fixture icerigi sentetiktir; proje veya kullanici verisi tasimaz."""
    for modality in Modality:
        rendered = repr(fixture_for(modality).payload).lower()
        for forbidden in ("zekam", "gpu-fusion", "/users/", "c:\\"):
            assert forbidden not in rendered


# -- sekil dogrulamasi -----------------------------------------------------------------


def test_chat_shape_accepts_text() -> None:
    assert validate_chat_shape({"text": "merhaba"}).valid


@pytest.mark.parametrize(
    ("response", "failure"),
    [
        ({"text": ""}, ProbeFailure.EMPTY),
        ({"text": 42}, ProbeFailure.SHAPE),
        ({}, ProbeFailure.SHAPE),
    ],
)
def test_chat_shape_rejects_bad_payloads(
    response: dict[str, object], failure: ProbeFailure
) -> None:
    verdict = validate_chat_shape(response)
    assert not verdict.valid
    assert verdict.failure is failure


def test_chat_shape_detects_secret_echo() -> None:
    verdict = validate_chat_shape({"text": f"iste anahtar: {SECRET_CANARY}"})
    assert not verdict.valid
    assert verdict.failure is ProbeFailure.SECRET_ECHO


def test_embedding_shape_requires_finite_consistent_vectors() -> None:
    assert validate_embedding_shape({"vectors": [[0.1, 0.2]]}).valid
    assert validate_embedding_shape({"vectors": [[0.1], [0.1, 0.2]]}).failure is (
        ProbeFailure.DIMENSION
    )
    assert validate_embedding_shape({"vectors": [[float("nan")]]}).failure is (
        ProbeFailure.NON_FINITE
    )
    assert validate_embedding_shape({"vectors": []}).failure is ProbeFailure.SHAPE


def test_embedding_shape_checks_expected_dimension() -> None:
    verdict = validate_embedding_shape({"vectors": [[0.1, 0.2]]}, expected_dimension=1024)
    assert not verdict.valid
    assert verdict.failure is ProbeFailure.DIMENSION


def test_rerank_shape_requires_one_score_per_passage() -> None:
    assert validate_rerank_shape({"scores": [0.9, 0.1]}, passage_count=2).valid
    assert not validate_rerank_shape({"scores": [0.9]}, passage_count=2).valid


def test_transcript_shape_requires_text() -> None:
    assert validate_transcript_shape({"transcript": "merhaba"}).valid
    assert validate_transcript_shape({"transcript": "  "}).failure is ProbeFailure.EMPTY


def test_guardrail_shape_requires_both_labels() -> None:
    assert validate_guardrail_shape({"labels": {"safe": "safe", "unsafe": "unsafe"}}).valid
    assert not validate_guardrail_shape({"labels": {"safe": "safe"}}).valid
    assert (
        validate_guardrail_shape({"labels": {"safe": "safe", "unsafe": "safe"}}).failure
        is ProbeFailure.LABEL
    )


def test_vision_shape_requires_real_image_input() -> None:
    assert validate_vision_shape({"image_received": True, "text": "kirmizi"}).valid
    verdict = validate_vision_shape({"image_received": False, "text": "kirmizi"})
    assert verdict.failure is ProbeFailure.UNSUPPORTED


def test_validate_shape_dispatches_by_modality() -> None:
    fixture = fixture_for(Modality.RERANK)
    assert validate_shape(Modality.RERANK, {"scores": [0.9, 0.1]}, fixture=fixture).valid
    assert not validate_shape(Modality.UNKNOWN, {}, fixture=fixture).valid


# -- probe sonucu -----------------------------------------------------------------------


def test_failed_outcome_requires_a_category() -> None:
    with pytest.raises(ValidationFailed):
        ProbeOutcome(
            model_id="m1",
            modality=Modality.CHAT,
            fixture_name="f",
            status=ProbeStatus.FAILED,
        )


def test_passed_outcome_cannot_carry_a_category() -> None:
    with pytest.raises(ValidationFailed):
        ProbeOutcome(
            model_id="m1",
            modality=Modality.CHAT,
            fixture_name="f",
            status=ProbeStatus.PASSED,
            failure=ProbeFailure.SHAPE,
        )


def test_outcome_never_carries_prompt_or_response_text() -> None:
    document = _outcome(ProbeStatus.PASSED, response_digest="sha256:" + "a" * 64).as_dict()
    assert "prompt" not in document
    assert "response" not in document
    assert document["response_digest"].startswith("sha256:")


# -- karantina --------------------------------------------------------------------------


def test_consecutive_failures_counts_from_the_end() -> None:
    outcomes = [
        _outcome(ProbeStatus.FAILED),
        _outcome(ProbeStatus.PASSED),
        _outcome(ProbeStatus.FAILED),
        _outcome(ProbeStatus.FAILED),
    ]
    assert consecutive_failures(outcomes) == 2


def test_skipped_probes_do_not_break_the_streak() -> None:
    outcomes = [
        _outcome(ProbeStatus.FAILED),
        _outcome(ProbeStatus.SKIPPED),
        _outcome(ProbeStatus.FAILED),
    ]
    assert consecutive_failures(outcomes) == 2


def test_single_failure_does_not_quarantine() -> None:
    decision = evaluate_health([_outcome(ProbeStatus.FAILED)], now=NOW)
    assert decision.state is HealthState.UNTESTED
    assert decision.consecutive_failures == 1


def test_two_consecutive_failures_quarantine() -> None:
    decision = evaluate_health(
        [_outcome(ProbeStatus.FAILED), _outcome(ProbeStatus.FAILED)], now=NOW
    )
    assert decision.state is HealthState.QUARANTINED
    assert decision.quarantine_until == NOW + QuarantinePolicy().cooldown


def test_quarantine_threshold_is_configurable() -> None:
    policy = QuarantinePolicy(consecutive_failure_threshold=3)
    decision = evaluate_health(
        [_outcome(ProbeStatus.FAILED), _outcome(ProbeStatus.FAILED)], policy=policy, now=NOW
    )
    assert decision.state is not HealthState.QUARANTINED


def test_success_after_failures_clears_the_streak() -> None:
    decision = evaluate_health(
        [_outcome(ProbeStatus.FAILED), _outcome(ProbeStatus.PASSED)], now=NOW
    )
    assert decision.state is HealthState.HEALTH_PASSED
    assert decision.consecutive_failures == 0


def test_no_probe_means_untested() -> None:
    assert evaluate_health([], now=NOW).state is HealthState.UNTESTED


def test_policy_digest_changes_with_threshold() -> None:
    assert (
        QuarantinePolicy().policy_digest
        != QuarantinePolicy(consecutive_failure_threshold=5).policy_digest
    )


def test_invalid_threshold_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        QuarantinePolicy(consecutive_failure_threshold=0)


# -- staleness ----------------------------------------------------------------------------


def _staleness(**overrides: object):  # type: ignore[no-untyped-def]
    defaults: dict[str, object] = {
        "recorded_inventory_digest": "sha256:" + "a" * 64,
        "current_inventory_digest": "sha256:" + "a" * 64,
        "recorded_policy_digest": "sha256:" + "b" * 64,
        "current_policy_digest": "sha256:" + "b" * 64,
        "last_checked_at": NOW,
        "now": NOW,
    }
    defaults.update(overrides)
    return assess_staleness(**defaults)  # type: ignore[arg-type]


def test_fresh_result_is_not_stale() -> None:
    assert not _staleness().stale


def test_never_tested_is_stale() -> None:
    verdict = _staleness(last_checked_at=None)
    assert verdict.stale
    assert verdict.reasons == (StalenessReason.NEVER_TESTED,)


def test_inventory_change_makes_it_stale() -> None:
    verdict = _staleness(current_inventory_digest="sha256:" + "c" * 64)
    assert StalenessReason.INVENTORY_CHANGED in verdict.reasons


def test_policy_change_makes_it_stale() -> None:
    verdict = _staleness(current_policy_digest="sha256:" + "d" * 64)
    assert StalenessReason.POLICY_CHANGED in verdict.reasons


def test_old_result_is_stale() -> None:
    verdict = _staleness(now=NOW + dt.timedelta(days=8))
    assert StalenessReason.TOO_OLD in verdict.reasons


# -- sozlesme ------------------------------------------------------------------------------


def test_every_modality_has_a_contract_list() -> None:
    assert set(CONTRACTS_BY_MODALITY) == set(Modality)


def test_chat_contracts_include_tools_and_json() -> None:
    contracts = CONTRACTS_BY_MODALITY[Modality.CHAT]
    assert ContractCapability.TOOL_CALL in contracts
    assert ContractCapability.JSON_SCHEMA in contracts
    assert ContractCapability.TURKISH in contracts


def test_embedding_contracts_do_not_include_tools() -> None:
    assert ContractCapability.TOOL_CALL not in CONTRACTS_BY_MODALITY[Modality.EMBEDDING]


def test_only_verified_capabilities_are_reported() -> None:
    checks = [
        CapabilityCheck(
            model_id="m1",
            capability=ContractCapability.TOOL_CALL,
            verified=True,
            evidence="tool cagrisi dogru sema ile dondu",
        ),
        CapabilityCheck(
            model_id="m1",
            capability=ContractCapability.JSON_SCHEMA,
            verified=False,
            evidence="sema disi yanit",
            failure=ProbeFailure.SHAPE,
        ),
    ]
    assert verified_capabilities(checks) == ("tool-call",)


def test_capability_check_requires_evidence() -> None:
    with pytest.raises(ValidationFailed):
        CapabilityCheck(
            model_id="m1",
            capability=ContractCapability.TOOL_CALL,
            verified=True,
            evidence="   ",
        )


def test_capability_evidence_digest_is_deterministic() -> None:
    check = CapabilityCheck(
        model_id="m1",
        capability=ContractCapability.TURKISH,
        verified=True,
        evidence="Turkce karakterler korundu",
    )
    assert check.evidence_digest == check.evidence_digest


def test_quarantined_model_is_not_benchmark_eligible() -> None:
    from zekam.domain.model_health import benchmark_state_for

    decision = evaluate_health(
        [_outcome(ProbeStatus.FAILED), _outcome(ProbeStatus.FAILED)], now=NOW
    )
    assert benchmark_state_for(decision, _staleness()) is BenchmarkState.FAILED
