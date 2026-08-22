"""Provider-neutral nicel model contract evaluator testleri."""

from __future__ import annotations

import pytest

from zekam.domain.errors import ValidationFailed
from zekam.domain.model_contract import (
    ContractObservation,
    character_error_rate,
    confusion_rates,
    embedding_determinism,
    evaluate_observation,
    grounding_rates,
    rerank_monotonicity,
    word_error_rate,
)
from zekam.domain.model_health import ContractCapability
from zekam.domain.model_inventory import Modality

pytestmark = pytest.mark.unit


def test_wer_and_cer_use_real_edit_distance() -> None:
    assert word_error_rate("bir iki uc", "bir uc") == pytest.approx(1 / 3)
    assert character_error_rate("abc", "adc") == pytest.approx(1 / 3)


def test_guardrail_fp_fn_are_separate_rates() -> None:
    rates = confusion_rates((False, False, True, True), (False, True, True, False))
    assert rates.false_positive_rate == 0.5
    assert rates.false_negative_rate == 0.5


def test_vl_grounding_detects_hallucinated_object() -> None:
    rates = grounding_rates(("kirmizi-kare",), ("kirmizi-kare", "mavi-daire"))
    assert rates.recall == 1.0
    assert rates.hallucination_rate == 0.5


def test_embedding_repeat_and_batch_equality_are_measured() -> None:
    rates = embedding_determinism(
        ((0.1, 0.2), (0.1, 0.2)),
        ((0.1, 0.2), (0.1, 0.200001)),
    )
    assert rates.max_repeat_delta == 0
    assert rates.max_batch_delta == pytest.approx(0.000001)


def test_rerank_monotonicity_uses_expected_relevance_order() -> None:
    passed = rerank_monotonicity((0.8, 0.1, 0.5), (0, 2, 1))
    failed = rerank_monotonicity((0.1, 0.8), (0, 1))
    assert passed.monotonic and passed.minimum_margin == pytest.approx(0.3)
    assert not failed.monotonic


@pytest.mark.parametrize(
    ("observation", "capability", "verified"),
    (
        (
            ContractObservation(
                modality=Modality.AUDIO_TRANSCRIPTION,
                transcript_pairs=(("merhaba dunya", "merhaba dunya"),),
                fixture_digest="sha256:" + "1" * 64,
                response_digest="sha256:" + "2" * 64,
            ),
            ContractCapability.AUDIO_INPUT,
            True,
        ),
        (
            ContractObservation(
                modality=Modality.GUARDRAIL,
                guardrail_expected=(False, True),
                guardrail_predicted=(True, False),
                fixture_digest="sha256:" + "3" * 64,
                response_digest="sha256:" + "4" * 64,
            ),
            ContractCapability.GUARDRAIL_LABELS,
            False,
        ),
        (
            ContractObservation(
                modality=Modality.VISION_LANGUAGE,
                visual_expected=("kirmizi-kare",),
                visual_mentioned=("kirmizi-kare",),
                fixture_digest="sha256:" + "5" * 64,
                response_digest="sha256:" + "6" * 64,
            ),
            ContractCapability.IMAGE_INPUT,
            True,
        ),
        (
            ContractObservation(
                modality=Modality.RERANK,
                rerank_scores=(0.9, 0.2),
                rerank_expected_order=(0, 1),
                fixture_digest="sha256:" + "7" * 64,
                response_digest="sha256:" + "8" * 64,
            ),
            ContractCapability.RERANK_ENDPOINT,
            True,
        ),
    ),
)
def test_evaluation_never_accepts_caller_verified_flag(
    observation: ContractObservation,
    capability: ContractCapability,
    verified: bool,
) -> None:
    result = evaluate_observation(observation)
    assert result.capability is capability
    assert result.verified is verified
    assert result.evidence_digest.startswith("sha256:")


def test_unsupported_modality_has_no_fake_contract_pass() -> None:
    with pytest.raises(ValidationFailed, match="evaluator"):
        evaluate_observation(ContractObservation(modality=Modality.CHAT))
