"""OpenCode configured modeller icin provider-neutral remote benchmark adapteri.

Modul authorization, HTTP veya secret cozmez. Cagriyi mevcut authorized provider
zincirine baglanabilecek typed ``RemoteProviderInvoker`` portuna verir. Public fixture
payload'i ve tested response yalniz process belleginde kalir; kanonik cikti yalniz
digest, metrik ve evaluator provenance tasiyan ``TrialResult`` nesnesidir.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from zekam.application.model_benchmark_service import resolve_fixture_artifact
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import (
    BenchmarkFixture,
    BenchmarkPlan,
    BenchmarkSuite,
    ExecutionEligibility,
    FixtureRegistry,
    SuiteKind,
    TrialResult,
    TrialStatus,
    VerifierIdentity,
    VerifierVerdict,
)
from zekam.domain.model_inventory import Modality

REMOTE_FIXTURE_SCHEMA = "zekam-opencode-remote-benchmark-fixture/v1"
PUBLIC_DATA_CLASSIFICATION = "public"
SUPPORTED_MODALITIES: tuple[Modality, ...] = (
    Modality.CHAT,
    Modality.COMPLETION,
    Modality.CODE,
    Modality.EMBEDDING,
    Modality.RERANK,
    Modality.VISION_LANGUAGE,
    Modality.GUARDRAIL,
)
EVALUATOR_PROVENANCE_DIGEST = digest({"evaluator": "opencode-provider-neutral", "version": 1})


def _adapter_provenance_digest(model_id: str, evaluator_digest: str) -> str:
    return digest(
        {
            "adapter": "opencode-remote-benchmark",
            "model_id": model_id,
            "evaluator": evaluator_digest,
            "version": 1,
        }
    )


@dataclass(frozen=True, slots=True)
class RemoteFixtureArtifact:
    """Dogrulanmis public fixture; payload canonical evidence'e yazilmaz."""

    case_id: str
    version: int
    modality: Modality
    payload: Mapping[str, Any] = field(repr=False)
    expectation: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class RemoteProviderInvocation:
    """AuthorizedProviderClient zincirine verilecek typed, secret-free cagri."""

    model_id: str
    modality: Modality
    fixture_digest: str
    repetition: int
    payload: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.model_id.strip() or self.repetition < 1:
            raise ValidationFailed("Remote benchmark invocation kimligi gecersiz")
        if self.modality not in SUPPORTED_MODALITIES:
            raise PolicyViolation("Remote benchmark audio/unknown modaliteyi cagirmaz")


@dataclass(frozen=True, slots=True)
class RemoteProviderResponse:
    """Provider response process-memory envelope; serialization API'si yoktur."""

    payload: Mapping[str, Any] = field(repr=False)
    latency_ms: int
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    actual_cost: float | None = None

    def __post_init__(self) -> None:
        if min(self.latency_ms, self.input_tokens, self.output_tokens) < 0:
            raise ValidationFailed("Remote benchmark response metrikleri negatif olamaz")
        costs = (
            (self.estimated_cost,)
            if self.actual_cost is None
            else (self.estimated_cost, self.actual_cost)
        )
        if any(not math.isfinite(value) or value < 0 for value in costs):
            raise ValidationFailed("Remote benchmark cost sonlu ve negatif olmayan olmali")

    @property
    def response_digest(self) -> str:
        return digest(dict(self.payload))


class RemoteProviderInvoker(Protocol):
    """Mevcut authorized provider client uzerinden uygulanacak dar port."""

    def invoke(self, request: RemoteProviderInvocation) -> RemoteProviderResponse: ...


@dataclass(slots=True)
class ProcessMemoryResponseStore:
    """Raw response'u verifier'a tek kullanimlik aktaran process-memory store."""

    _responses: dict[tuple[str, str, int, str], RemoteProviderResponse] = field(
        default_factory=dict, init=False, repr=False
    )

    @staticmethod
    def key(
        plan: BenchmarkPlan,
        fixture: BenchmarkFixture,
        repetition: int,
        response_digest: str,
    ) -> tuple[str, str, int, str]:
        return (plan.plan_digest, fixture.fixture_digest, repetition, response_digest)

    def put(
        self,
        *,
        plan: BenchmarkPlan,
        fixture: BenchmarkFixture,
        repetition: int,
        response: RemoteProviderResponse,
    ) -> None:
        key = self.key(plan, fixture, repetition, response.response_digest)
        if key in self._responses:
            raise PolicyViolation("Remote benchmark raw response replay reddedildi")
        self._responses[key] = response

    def consume(
        self,
        *,
        plan: BenchmarkPlan,
        fixture: BenchmarkFixture,
        result: TrialResult,
    ) -> RemoteProviderResponse:
        key = self.key(plan, fixture, result.repetition, result.response_digest)
        response = self._responses.pop(key, None)
        if response is None:
            raise PolicyViolation(
                "Remote benchmark verifier response binding bulunamadi veya tuketildi"
            )
        return response


@dataclass(frozen=True, slots=True)
class NeutralEvaluation:
    parse_ok: bool
    format_ok: bool
    evidence_ok: bool
    approved: bool
    quality: float
    reliability: float
    metrics: Mapping[str, float | int | bool]

    def __post_init__(self) -> None:
        if not 0 <= self.quality <= 1 or not 0 <= self.reliability <= 1:
            raise ValidationFailed("Evaluator quality/reliability 0..1 araliginda olmali")

    def evidence_body(self) -> dict[str, object]:
        return {
            "parse_ok": self.parse_ok,
            "format_ok": self.format_ok,
            "evidence_ok": self.evidence_ok,
            "approved": self.approved,
            "quality": self.quality,
            "reliability": self.reliability,
            "metrics": dict(self.metrics),
        }


def load_remote_fixture(fixture: BenchmarkFixture, *, allow_root: Path) -> RemoteFixtureArtifact:
    """Digest-bound artifact'i strict schema ile process bellegine alir."""

    source = resolve_fixture_artifact(fixture, allow_root=allow_root)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Remote benchmark fixture JSON okunamadi") from exc
    expected_fields = {
        "schema",
        "case_id",
        "version",
        "data_classification",
        "modality",
        "payload",
        "expectation",
    }
    if not isinstance(document, dict) or set(document) != expected_fields:
        raise ValidationFailed("Remote benchmark fixture exact shape gecersiz")
    if (
        document["schema"] != REMOTE_FIXTURE_SCHEMA
        or document["case_id"] != fixture.case_id
        or document["version"] != fixture.version
        or document["data_classification"] != PUBLIC_DATA_CLASSIFICATION
        or document["modality"] != fixture.modality
    ):
        raise ValidationFailed("Remote benchmark fixture registry binding drift")
    payload = document["payload"]
    expectation = document["expectation"]
    if not isinstance(payload, dict) or not isinstance(expectation, dict):
        raise ValidationFailed("Remote benchmark payload/expectation object olmali")
    if digest(expectation) != fixture.expected_schema_digest:
        raise PolicyViolation("Remote benchmark expected schema digest drift")
    modality = Modality(str(document["modality"]))
    if modality not in SUPPORTED_MODALITIES:
        raise PolicyViolation("Remote benchmark audio/unknown fixture kabul etmez")
    return RemoteFixtureArtifact(
        case_id=fixture.case_id,
        version=fixture.version,
        modality=modality,
        payload=cast(dict[str, Any], payload),
        expectation=cast(dict[str, Any], expectation),
    )


def build_remote_suite(registry: FixtureRegistry, modality: Modality) -> BenchmarkSuite:
    """Tek modalite icin yalniz remote-safe public fixture suite'i kurar."""

    if modality not in SUPPORTED_MODALITIES:
        raise PolicyViolation("Audio ve unknown OpenCode remote benchmark disidir")
    selected = tuple(
        fixture.fixture_digest
        for fixture in registry.fixtures
        if fixture.modality == modality.value
        and fixture.execution_eligibility is ExecutionEligibility.REMOTE_ALLOWED
        and "opencode-remote" in fixture.tags
    )
    if not selected:
        raise ValidationFailed(f"OpenCode remote fixture bulunamadi: {modality.value}")
    return BenchmarkSuite(
        suite_id=f"opencode-remote:{modality.value}",
        version=registry.schema_version,
        kind=SuiteKind.GENERAL,
        fixture_digests=selected,
    )


def _exact_keys(value: Mapping[str, Any], expected: set[str]) -> bool:
    return set(value) == expected


def _sequence(value: object) -> Sequence[object] | None:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return cast(Sequence[object], value)
    return None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _failed(*, parse_ok: bool = True, format_ok: bool = False) -> NeutralEvaluation:
    return NeutralEvaluation(parse_ok, format_ok, False, False, 0.0, 0.0, {"verified": False})


def _chat_or_completion(
    artifact: RemoteFixtureArtifact, response: Mapping[str, Any]
) -> NeutralEvaluation:
    if artifact.modality is Modality.COMPLETION:
        if not _exact_keys(response, {"text"}) or not isinstance(response.get("text"), str):
            return _failed()
        marker = artifact.expectation.get("required_marker")
        approved = isinstance(marker, str) and marker in cast(str, response["text"])
        return NeutralEvaluation(
            True, True, approved, approved, float(approved), 1.0, {"marker_present": approved}
        )
    if not _exact_keys(response, {"json"}) or not isinstance(response.get("json"), Mapping):
        return _failed()
    body = cast(Mapping[str, Any], response["json"])
    keys = artifact.expectation.get("required_keys")
    types = artifact.expectation.get("required_types")
    if not isinstance(keys, list) or not isinstance(types, dict):
        raise ValidationFailed("Chat expectation gecersiz")
    exact = set(body) == set(keys)
    typed = exact and all(
        types.get(key) == "string" and isinstance(body.get(key), str) for key in keys
    )
    approved = bool(exact and typed and artifact.expectation.get("additional_properties") is False)
    return NeutralEvaluation(
        True,
        exact,
        approved,
        approved,
        float(approved),
        1.0,
        {
            "exact_keys": exact,
            "typed": typed,
        },
    )


def _code(artifact: RemoteFixtureArtifact, response: Mapping[str, Any]) -> NeutralEvaluation:
    if not _exact_keys(response, {"code"}) or not isinstance(response.get("code"), str):
        return _failed()
    markers = artifact.expectation.get("required_markers")
    if not isinstance(markers, list) or any(not isinstance(item, str) for item in markers):
        raise ValidationFailed("Code expectation marker listesi gecersiz")
    code = cast(str, response["code"])
    present = sum(marker in code for marker in markers)
    approved = present == len(markers) and bool(markers)
    quality = present / len(markers) if markers else 0.0
    return NeutralEvaluation(
        True,
        True,
        approved,
        approved,
        quality,
        1.0,
        {
            "required_marker_count": len(markers),
            "present_marker_count": present,
        },
    )


def _vectors(value: object) -> tuple[tuple[float, ...], ...] | None:
    rows = _sequence(value)
    if rows is None or not rows:
        return None
    vectors: list[tuple[float, ...]] = []
    dimension: int | None = None
    for row in rows:
        values = _sequence(row)
        if values is None or not values:
            return None
        vector = tuple(_finite_number(item) for item in values)
        if any(item is None for item in vector):
            return None
        exact = cast(tuple[float, ...], vector)
        if dimension is None:
            dimension = len(exact)
        if len(exact) != dimension or math.sqrt(sum(item * item for item in exact)) == 0:
            return None
        vectors.append(exact)
    return tuple(vectors)


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _embedding(artifact: RemoteFixtureArtifact, response: Mapping[str, Any]) -> NeutralEvaluation:
    if not _exact_keys(response, {"vectors"}):
        return _failed()
    vectors = _vectors(response.get("vectors"))
    expected_count = int(artifact.expectation.get("vector_count", 0))
    if vectors is None or len(vectors) != expected_count:
        return _failed(format_ok=True)
    duplicate = artifact.expectation.get("duplicate_pair")
    similar = artifact.expectation.get("similar_pair")
    dissimilar = artifact.expectation.get("dissimilar_pair")
    if not all(
        isinstance(pair, list) and len(pair) == 2 for pair in (duplicate, similar, dissimilar)
    ):
        raise ValidationFailed("Embedding expectation pair'leri gecersiz")
    dup = cast(list[int], duplicate)
    sim = cast(list[int], similar)
    dis = cast(list[int], dissimilar)
    duplicate_delta = max(abs(a - b) for a, b in zip(vectors[dup[0]], vectors[dup[1]], strict=True))
    semantic_margin = _cosine(vectors[sim[0]], vectors[sim[1]]) - _cosine(
        vectors[dis[0]], vectors[dis[1]]
    )
    approved = duplicate_delta <= float(
        artifact.expectation["max_duplicate_delta"]
    ) and semantic_margin >= float(artifact.expectation["min_semantic_margin"])
    return NeutralEvaluation(
        True,
        True,
        approved,
        approved,
        float(approved),
        1.0,
        {
            "dimension": len(vectors[0]),
            "duplicate_max_delta": duplicate_delta,
            "semantic_margin": semantic_margin,
            "finite": True,
        },
    )


def _rerank(artifact: RemoteFixtureArtifact, response: Mapping[str, Any]) -> NeutralEvaluation:
    if not _exact_keys(response, {"scores"}):
        return _failed()
    raw_scores = _sequence(response.get("scores"))
    expected_order = artifact.expectation.get("expected_order")
    if (
        raw_scores is None
        or not isinstance(expected_order, list)
        or len(raw_scores) != len(expected_order)
    ):
        return _failed(format_ok=True)
    scores = tuple(_finite_number(item) for item in raw_scores)
    if any(item is None for item in scores):
        return _failed(format_ok=True)
    exact = cast(tuple[float, ...], scores)
    observed_order = sorted(range(len(exact)), key=lambda index: (-exact[index], index))
    margins = [
        exact[expected_order[index]] - exact[expected_order[index + 1]]
        for index in range(len(expected_order) - 1)
    ]
    minimum_margin = min(margins) if margins else 0.0
    approved = observed_order == expected_order and minimum_margin >= float(
        artifact.expectation["min_margin"]
    )
    return NeutralEvaluation(
        True,
        True,
        approved,
        approved,
        float(approved),
        1.0,
        {
            "monotonic": observed_order == expected_order,
            "minimum_margin": minimum_margin,
        },
    )


def _vision(artifact: RemoteFixtureArtifact, response: Mapping[str, Any]) -> NeutralEvaluation:
    if not _exact_keys(response, {"answer", "objects"}) or not isinstance(
        response.get("answer"), str
    ):
        return _failed()
    raw_objects = _sequence(response.get("objects"))
    required = artifact.expectation.get("required_objects")
    forbidden = artifact.expectation.get("forbidden_objects")
    if (
        raw_objects is None
        or any(not isinstance(item, str) for item in raw_objects)
        or not isinstance(required, list)
        or not isinstance(forbidden, list)
    ):
        return _failed(format_ok=True)
    observed = {cast(str, item).casefold() for item in raw_objects}
    required_set = {str(item).casefold() for item in required}
    forbidden_set = {str(item).casefold() for item in forbidden}
    recall = len(observed & required_set) / len(required_set) if required_set else 1.0
    hallucinations = len(observed & forbidden_set)
    approved = recall >= float(artifact.expectation["min_grounding_recall"]) and hallucinations == 0
    return NeutralEvaluation(
        True,
        True,
        approved,
        approved,
        recall,
        1.0,
        {
            "grounding_recall": recall,
            "hallucination_count": hallucinations,
        },
    )


def _guardrail(artifact: RemoteFixtureArtifact, response: Mapping[str, Any]) -> NeutralEvaluation:
    if not _exact_keys(response, {"labels"}):
        return _failed()
    labels = _sequence(response.get("labels"))
    expected = artifact.expectation.get("unsafe_labels")
    if (
        labels is None
        or not isinstance(expected, list)
        or len(labels) != len(expected)
        or any(type(item) is not bool for item in labels)
        or any(type(item) is not bool for item in expected)
    ):
        return _failed(format_ok=True)
    predicted = cast(Sequence[bool], labels)
    expected_labels = cast(list[bool], expected)
    safe_count = sum(not item for item in expected_labels)
    unsafe_count = sum(expected_labels)
    false_positive = sum(
        not actual and predicted[index] for index, actual in enumerate(expected_labels)
    )
    false_negative = sum(
        actual and not predicted[index] for index, actual in enumerate(expected_labels)
    )
    fp_rate = false_positive / safe_count if safe_count else 0.0
    fn_rate = false_negative / unsafe_count if unsafe_count else 0.0
    approved = fp_rate <= float(
        artifact.expectation["max_false_positive_rate"]
    ) and fn_rate <= float(artifact.expectation["max_false_negative_rate"])
    quality = 1.0 - ((false_positive + false_negative) / len(expected_labels))
    return NeutralEvaluation(
        True,
        True,
        approved,
        approved,
        quality,
        1.0,
        {
            "false_positive_rate": fp_rate,
            "false_negative_rate": fn_rate,
        },
    )


@dataclass(frozen=True, slots=True)
class DeterministicProviderNeutralVerifier:
    """Provider kimliginden bagimsiz, saf ve deterministik response verifier'i."""

    provenance_digest: str = EVALUATOR_PROVENANCE_DIGEST

    def verify(
        self, artifact: RemoteFixtureArtifact, response: Mapping[str, Any]
    ) -> NeutralEvaluation:
        if artifact.modality in {Modality.CHAT, Modality.COMPLETION}:
            return _chat_or_completion(artifact, response)
        if artifact.modality is Modality.CODE:
            return _code(artifact, response)
        if artifact.modality is Modality.EMBEDDING:
            return _embedding(artifact, response)
        if artifact.modality is Modality.RERANK:
            return _rerank(artifact, response)
        if artifact.modality is Modality.VISION_LANGUAGE:
            return _vision(artifact, response)
        if artifact.modality is Modality.GUARDRAIL:
            return _guardrail(artifact, response)
        raise PolicyViolation("Audio/unknown response evaluator'e ulasamaz")


@dataclass(frozen=True, slots=True)
class OpenCodeRemoteBenchmarkAdapter:
    """Existing benchmark host'a takilan remote adapter; HTTP uygulamaz."""

    routed_model_id: str
    fixture_root: Path
    invoker: RemoteProviderInvoker = field(repr=False)
    response_store: ProcessMemoryResponseStore = field(repr=False)
    evaluator: DeterministicProviderNeutralVerifier = field(
        default_factory=DeterministicProviderNeutralVerifier, repr=False
    )

    @property
    def execution_mode(self) -> str:
        return "remote"

    @property
    def model_id(self) -> str:
        return self.routed_model_id

    @property
    def adapter_digest(self) -> str:
        return _adapter_provenance_digest(self.model_id, self.evaluator.provenance_digest)

    def invoke(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, repetition: int
    ) -> TrialResult:
        if not plan.remote_execution or plan.model_id != self.model_id:
            raise PolicyViolation("OpenCode remote benchmark plan/model binding drift")
        if fixture.execution_eligibility is not ExecutionEligibility.REMOTE_ALLOWED:
            raise PolicyViolation("Local-only fixture remote provider'a verilemez")
        artifact = load_remote_fixture(fixture, allow_root=self.fixture_root)
        request = RemoteProviderInvocation(
            model_id=self.model_id,
            modality=artifact.modality,
            fixture_digest=fixture.fixture_digest,
            repetition=repetition,
            payload=artifact.payload,
        )
        response = self.invoker.invoke(request)
        evaluation = self.evaluator.verify(artifact, response.payload)
        self.response_store.put(
            plan=plan,
            fixture=fixture,
            repetition=repetition,
            response=response,
        )
        evidence_digest = digest(
            {
                "plan_digest": plan.plan_digest,
                "fixture_digest": fixture.fixture_digest,
                "repetition": repetition,
                "response_digest": response.response_digest,
                "adapter_provenance_digest": self.adapter_digest,
                "evaluator_provenance_digest": self.evaluator.provenance_digest,
                "evaluation": evaluation.evidence_body(),
            }
        )
        status = TrialStatus.PASSED if evaluation.approved else TrialStatus.FAILED
        return TrialResult(
            fixture_digest=fixture.fixture_digest,
            repetition=repetition,
            status=status,
            parse_ok=evaluation.parse_ok,
            format_ok=evaluation.format_ok,
            evidence_ok=evaluation.evidence_ok,
            verifier_approved=False,
            quality=evaluation.quality,
            reliability=evaluation.reliability,
            latency_ms=response.latency_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            retry_count=0,
            human_corrections=0,
            estimated_cost=response.estimated_cost,
            actual_cost=response.actual_cost,
            response_digest=response.response_digest,
            evidence_digest=evidence_digest,
            failure_category=None if status is TrialStatus.PASSED else "evaluation",
        )


@dataclass(frozen=True, slots=True)
class OpenCodeDeterministicBenchmarkVerifier:
    """Raw response'u one-shot tuketip provider-neutral olarak yeniden degerlendirir."""

    identity: VerifierIdentity
    fixture_root: Path
    response_store: ProcessMemoryResponseStore = field(repr=False)
    evaluator: DeterministicProviderNeutralVerifier = field(
        default_factory=DeterministicProviderNeutralVerifier, repr=False
    )

    @property
    def execution_mode(self) -> str:
        return "remote"

    @property
    def verifier(self) -> VerifierIdentity:
        return self.identity

    def verify(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, result: TrialResult
    ) -> VerifierVerdict:
        if self.identity.model_id == plan.model_id:
            raise PolicyViolation("Tested model kendi deterministic verifier'i olamaz")
        response = self.response_store.consume(plan=plan, fixture=fixture, result=result)
        artifact = load_remote_fixture(fixture, allow_root=self.fixture_root)
        evaluation = self.evaluator.verify(artifact, response.payload)
        expected_status = TrialStatus.PASSED if evaluation.approved else TrialStatus.FAILED
        expected_evidence_digest = digest(
            {
                "plan_digest": plan.plan_digest,
                "fixture_digest": fixture.fixture_digest,
                "repetition": result.repetition,
                "response_digest": response.response_digest,
                "adapter_provenance_digest": _adapter_provenance_digest(
                    plan.model_id, self.evaluator.provenance_digest
                ),
                "evaluator_provenance_digest": self.evaluator.provenance_digest,
                "evaluation": evaluation.evidence_body(),
            }
        )
        metrics_match = (
            result.fixture_digest == fixture.fixture_digest
            and result.response_digest == response.response_digest
            and result.evidence_digest == expected_evidence_digest
            and result.status is expected_status
            and not result.verifier_approved
            and result.retry_count == 0
            and result.human_corrections == 0
            and result.parse_ok == evaluation.parse_ok
            and result.format_ok == evaluation.format_ok
            and result.evidence_ok == evaluation.evidence_ok
            and result.quality == evaluation.quality
            and result.reliability == evaluation.reliability
            and result.latency_ms == response.latency_ms
            and result.input_tokens == response.input_tokens
            and result.output_tokens == response.output_tokens
            and result.estimated_cost == response.estimated_cost
            and result.actual_cost == response.actual_cost
            and result.failure_category
            == (None if expected_status is TrialStatus.PASSED else "evaluation")
        )
        approved = evaluation.approved and metrics_match
        evidence_digest = digest(
            {
                "tested_model_id": plan.model_id,
                "verifier_model_id": self.identity.model_id,
                "verifier_execution_identity": self.identity.execution_identity,
                "verifier_provenance_digest": self.identity.provenance_digest,
                "evaluator_provenance_digest": self.evaluator.provenance_digest,
                "fixture_digest": fixture.fixture_digest,
                "response_digest": response.response_digest,
                "evaluation": evaluation.evidence_body(),
                "metrics_match": metrics_match,
            }
        )
        return VerifierVerdict(
            tested_model_id=plan.model_id,
            verifier_model_id=self.identity.model_id,
            execution_identity=self.identity.execution_identity,
            tested_response_digest=response.response_digest,
            approved=approved,
            evidence_digest=evidence_digest,
        )


def embedding_repetitions_are_deterministic(results: tuple[TrialResult, ...]) -> bool:
    """Bes veya daha fazla embedding tekrarinin exact response digest'ini karsilastirir."""

    return len(results) >= 5 and len({item.response_digest for item in results}) == 1
