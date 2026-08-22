"""Provider-neutral model contract olcumleri.

Bu modul provider cagirmaz ve ham fixture/yanit saklamaz. WER/CER, guardrail
FP/FN, VL grounding, embedding determinism/batch equality ve rerank monotonicity
sonuclarini kanonik digest'e bagli verdict'lere cevirir.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.model_health import ContractCapability
from zekam.domain.model_inventory import Modality


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", value.casefold(), flags=re.UNICODE))


def _distance(left: tuple[Any, ...], right: tuple[Any, ...]) -> int:
    previous = list(range(len(right) + 1))
    for row, left_value in enumerate(left, start=1):
        current = [row]
        for column, right_value in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_value != right_value),
                )
            )
        previous = current
    return previous[-1]


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Casefold/token normalize edilmis Levenshtein WER."""

    expected = _tokens(reference)
    if not expected:
        raise ValidationFailed("WER reference bos olamaz")
    return _distance(expected, _tokens(hypothesis)) / len(expected)


def character_error_rate(reference: str, hypothesis: str) -> float:
    """Whitespace normalize edilmis Unicode CER."""

    expected = tuple(" ".join(reference.casefold().split()))
    observed = tuple(" ".join(hypothesis.casefold().split()))
    if not expected:
        raise ValidationFailed("CER reference bos olamaz")
    return _distance(expected, observed) / len(expected)


@dataclass(frozen=True, slots=True)
class ConfusionRates:
    false_positive_rate: float
    false_negative_rate: float
    samples: int

    @property
    def evidence_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "false_positive_rate": self.false_positive_rate,
            "false_negative_rate": self.false_negative_rate,
            "samples": self.samples,
        }


def confusion_rates(
    expected_unsafe: tuple[bool, ...], predicted_unsafe: tuple[bool, ...]
) -> ConfusionRates:
    """Guardrail icin negatiflerde FP, pozitiflerde FN oranini hesaplar."""

    if not expected_unsafe or len(expected_unsafe) != len(predicted_unsafe):
        raise ValidationFailed("guardrail expected/predicted uzunlugu eslesmeli")
    negatives = sum(not item for item in expected_unsafe)
    positives = sum(expected_unsafe)
    false_positives = sum(
        not expected and predicted
        for expected, predicted in zip(expected_unsafe, predicted_unsafe, strict=True)
    )
    false_negatives = sum(
        expected and not predicted
        for expected, predicted in zip(expected_unsafe, predicted_unsafe, strict=True)
    )
    return ConfusionRates(
        false_positive_rate=0.0 if negatives == 0 else false_positives / negatives,
        false_negative_rate=0.0 if positives == 0 else false_negatives / positives,
        samples=len(expected_unsafe),
    )


@dataclass(frozen=True, slots=True)
class GroundingRates:
    recall: float
    hallucination_rate: float

    @property
    def evidence_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, float]:
        return {"recall": self.recall, "hallucination_rate": self.hallucination_rate}


def grounding_rates(expected: tuple[str, ...], mentioned: tuple[str, ...]) -> GroundingRates:
    """Beklenen nesne kapsami ve gorselde olmayan nesne oranini hesaplar."""

    expected_set = {item.casefold().strip() for item in expected if item.strip()}
    mentioned_set = {item.casefold().strip() for item in mentioned if item.strip()}
    if not expected_set:
        raise ValidationFailed("VL grounding expected nesnesi bos olamaz")
    return GroundingRates(
        recall=len(expected_set & mentioned_set) / len(expected_set),
        hallucination_rate=(
            0.0 if not mentioned_set else len(mentioned_set - expected_set) / len(mentioned_set)
        ),
    )


def _assert_vectors(vectors: tuple[tuple[float, ...], ...]) -> int:
    if not vectors or not vectors[0]:
        raise ValidationFailed("embedding vektoru bos olamaz")
    dimension = len(vectors[0])
    if any(len(vector) != dimension for vector in vectors):
        raise ValidationFailed("embedding boyutlari eslesmiyor")
    if any(not math.isfinite(value) for vector in vectors for value in vector):
        raise ValidationFailed("embedding sonlu olmayan deger tasiyor")
    return dimension


@dataclass(frozen=True, slots=True)
class EmbeddingDeterminism:
    max_repeat_delta: float
    max_batch_delta: float
    dimension: int

    @property
    def evidence_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_repeat_delta": self.max_repeat_delta,
            "max_batch_delta": self.max_batch_delta,
            "dimension": self.dimension,
        }


def embedding_determinism(
    singles: tuple[tuple[float, ...], ...], batch: tuple[tuple[float, ...], ...]
) -> EmbeddingDeterminism:
    """Tekrarli tekli cagrilar ile ayni girdilerin batch sonucunu karsilastirir."""

    if len(singles) < 2 or len(batch) != len(singles):
        raise ValidationFailed("embedding determinism en az iki tekrar ve es batch ister")
    dimension = _assert_vectors(singles + batch)
    baseline = singles[0]
    repeat_delta = max(
        abs(value - baseline[index]) for vector in singles[1:] for index, value in enumerate(vector)
    )
    batch_delta = max(
        abs(batch[index][column] - singles[index][column])
        for index in range(len(singles))
        for column in range(dimension)
    )
    return EmbeddingDeterminism(repeat_delta, batch_delta, dimension)


@dataclass(frozen=True, slots=True)
class RerankMonotonicity:
    monotonic: bool
    minimum_margin: float

    @property
    def evidence_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {"monotonic": self.monotonic, "minimum_margin": self.minimum_margin}


def rerank_monotonicity(
    scores: tuple[float, ...], expected_order: tuple[int, ...]
) -> RerankMonotonicity:
    """Beklenen relevance sirasinda skorlarin azalmamasini dogrular."""

    if not scores or sorted(expected_order) != list(range(len(scores))):
        raise ValidationFailed("rerank expected_order tam permutasyon olmali")
    if any(not math.isfinite(score) for score in scores):
        raise ValidationFailed("rerank skoru sonlu olmali")
    ordered = tuple(scores[index] for index in expected_order)
    margins = tuple(ordered[index] - ordered[index + 1] for index in range(len(ordered) - 1))
    return RerankMonotonicity(
        monotonic=all(margin >= 0 for margin in margins),
        minimum_margin=min(margins) if margins else 0.0,
    )


@dataclass(frozen=True, slots=True)
class ContractThresholds:
    max_wer: float = 0.20
    max_cer: float = 0.10
    max_false_positive_rate: float = 0.05
    max_false_negative_rate: float = 0.05
    min_grounding_recall: float = 0.90
    max_hallucination_rate: float = 0.05
    max_embedding_delta: float = 1e-8
    min_rerank_margin: float = 0.0


@dataclass(frozen=True, slots=True)
class ContractObservation:
    modality: Modality
    transcript_pairs: tuple[tuple[str, str], ...] = ()
    guardrail_expected: tuple[bool, ...] = ()
    guardrail_predicted: tuple[bool, ...] = ()
    visual_expected: tuple[str, ...] = ()
    visual_mentioned: tuple[str, ...] = ()
    embedding_singles: tuple[tuple[float, ...], ...] = ()
    embedding_batch: tuple[tuple[float, ...], ...] = ()
    rerank_scores: tuple[float, ...] = ()
    rerank_expected_order: tuple[int, ...] = ()
    fixture_digest: str = ""
    response_digest: str = ""


@dataclass(frozen=True, slots=True)
class ContractEvaluation:
    capability: ContractCapability
    verified: bool
    metrics: dict[str, float | int | bool]
    fixture_digest: str
    response_digest: str

    @property
    def evidence_digest(self) -> str:
        return digest(
            {
                "capability": self.capability.value,
                "verified": self.verified,
                "metrics": self.metrics,
                "fixture_digest": self.fixture_digest,
                "response_digest": self.response_digest,
            }
        )


def evaluate_observation(
    observation: ContractObservation, thresholds: ContractThresholds | None = None
) -> ContractEvaluation:
    """Modalite observation'ini tek kanonik capability verdict'ine cevirir."""

    limits = thresholds or ContractThresholds()
    if observation.modality is Modality.AUDIO_TRANSCRIPTION:
        if not observation.transcript_pairs:
            raise ValidationFailed("audio contract transcript pair ister")
        wers = tuple(word_error_rate(left, right) for left, right in observation.transcript_pairs)
        cers = tuple(
            character_error_rate(left, right) for left, right in observation.transcript_pairs
        )
        audio_wer = sum(wers) / len(wers)
        audio_cer = sum(cers) / len(cers)
        metrics: dict[str, float | int | bool] = {
            "wer": audio_wer,
            "cer": audio_cer,
            "samples": len(wers),
        }
        verified = audio_wer <= limits.max_wer and audio_cer <= limits.max_cer
        capability = ContractCapability.AUDIO_INPUT
    elif observation.modality is Modality.GUARDRAIL:
        guardrail_rates = confusion_rates(
            observation.guardrail_expected, observation.guardrail_predicted
        )
        metrics = guardrail_rates.as_dict()
        verified = (
            guardrail_rates.false_positive_rate <= limits.max_false_positive_rate
            and guardrail_rates.false_negative_rate <= limits.max_false_negative_rate
        )
        capability = ContractCapability.GUARDRAIL_LABELS
    elif observation.modality is Modality.VISION_LANGUAGE:
        visual_rates = grounding_rates(observation.visual_expected, observation.visual_mentioned)
        metrics = visual_rates.as_dict()
        verified = (
            visual_rates.recall >= limits.min_grounding_recall
            and visual_rates.hallucination_rate <= limits.max_hallucination_rate
        )
        capability = ContractCapability.IMAGE_INPUT
    elif observation.modality is Modality.EMBEDDING:
        embedding_rates = embedding_determinism(
            observation.embedding_singles, observation.embedding_batch
        )
        metrics = embedding_rates.as_dict()
        verified = (
            max(embedding_rates.max_repeat_delta, embedding_rates.max_batch_delta)
            <= limits.max_embedding_delta
        )
        capability = ContractCapability.EMBEDDING_BATCH
    elif observation.modality is Modality.RERANK:
        rerank_rates = rerank_monotonicity(
            observation.rerank_scores, observation.rerank_expected_order
        )
        metrics = rerank_rates.as_dict()
        verified = (
            rerank_rates.monotonic and rerank_rates.minimum_margin >= limits.min_rerank_margin
        )
        capability = ContractCapability.RERANK_ENDPOINT
    else:
        raise ValidationFailed("modalite icin nicel contract evaluator tanimli degil")
    return ContractEvaluation(
        capability=capability,
        verified=bool(verified),
        metrics=metrics,
        fixture_digest=observation.fixture_digest,
        response_digest=observation.response_digest,
    )
