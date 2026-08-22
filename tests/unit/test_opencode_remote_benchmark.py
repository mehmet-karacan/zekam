"""OpenCode remote public benchmark suite ve provider-neutral evaluator testleri."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from zekam.application.model_benchmark_service import default_fixture_file, load_fixture_registry
from zekam.application.opencode_remote_benchmark import (
    SUPPORTED_MODALITIES,
    DeterministicProviderNeutralVerifier,
    NeutralEvaluation,
    OpenCodeDeterministicBenchmarkVerifier,
    OpenCodeRemoteBenchmarkAdapter,
    ProcessMemoryResponseStore,
    RemoteProviderInvocation,
    RemoteProviderResponse,
    build_remote_suite,
    embedding_repetitions_are_deterministic,
    load_remote_fixture,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import BenchmarkFixture, BenchmarkPlan, VerifierIdentity
from zekam.domain.model_inventory import Modality

pytestmark = pytest.mark.unit

MODEL_ID = "configured-model"
VERIFIER = VerifierIdentity(
    "independent-verifier",
    "deterministic:opencode-remote",
    digest("independent-verifier-provenance"),
)
RAW_CANARY = "raw-response-must-remain-in-process-memory"


def _response(modality: Modality) -> dict[str, Any]:
    responses: dict[Modality, dict[str, Any]] = {
        Modality.CHAT: {"json": {"durum": "hazir", "dil": "tr"}},
        Modality.COMPLETION: {"text": "Yerel dogrulama ZEKAM-TAMAM"},
        Modality.CODE: {"code": "def topla(a, b):\n    return a + b\n\nassert topla(2, 3) == 5"},
        Modality.EMBEDDING: {"vectors": [[1.0, 0.0], [1.0, 0.0], [0.9, 0.1], [0.0, 1.0]]},
        Modality.RERANK: {"scores": [0.9, 0.1, 0.6]},
        Modality.VISION_LANGUAGE: {
            "answer": "Beyaz zeminde kirmizi kare var.",
            "objects": ["kirmizi kare", "beyaz zemin"],
        },
        Modality.GUARDRAIL: {"labels": [False, True, False, True]},
    }
    return responses[modality]


@dataclass(slots=True)
class FakeInvoker:
    overrides: dict[Modality, dict[str, Any]] = field(default_factory=dict)
    calls: list[RemoteProviderInvocation] = field(default_factory=list)

    def invoke(self, request: RemoteProviderInvocation) -> RemoteProviderResponse:
        self.calls.append(request)
        return RemoteProviderResponse(
            payload=self.overrides.get(request.modality, _response(request.modality)),
            latency_ms=12,
            input_tokens=20,
            output_tokens=10,
            estimated_cost=0.02,
            actual_cost=0.019,
        )


def _fixture(modality: Modality) -> BenchmarkFixture:
    matches = [
        fixture
        for fixture in load_fixture_registry().fixtures
        if fixture.modality == modality.value and "opencode-remote" in fixture.tags
    ]
    assert len(matches) == 1
    return matches[0]


def _plan(fixture: BenchmarkFixture) -> BenchmarkPlan:
    registry = load_fixture_registry()
    suite = build_remote_suite(registry, Modality(fixture.modality))
    return BenchmarkPlan(
        model_id=MODEL_ID,
        suite_digest=suite.suite_digest,
        inventory_digest=digest("inventory"),
        policy_digest=digest("policy"),
        fixture_registry_digest=registry.registry_digest,
        remote_execution=True,
    )


def _adapter_pair(
    invoker: FakeInvoker,
) -> tuple[OpenCodeRemoteBenchmarkAdapter, OpenCodeDeterministicBenchmarkVerifier]:
    store = ProcessMemoryResponseStore()
    root = default_fixture_file().parent.resolve(strict=True)
    return (
        OpenCodeRemoteBenchmarkAdapter(MODEL_ID, root, invoker, store),
        OpenCodeDeterministicBenchmarkVerifier(VERIFIER, root, store),
    )


def _evaluate(modality: Modality, response: dict[str, Any]) -> NeutralEvaluation:
    fixture = _fixture(modality)
    artifact = load_remote_fixture(
        fixture, allow_root=default_fixture_file().parent.resolve(strict=True)
    )
    return DeterministicProviderNeutralVerifier().verify(artifact, response)


def test_remote_registry_has_exact_non_audio_modality_coverage() -> None:
    registry = load_fixture_registry()
    covered = {
        Modality(fixture.modality)
        for fixture in registry.fixtures
        if "opencode-remote" in fixture.tags
    }

    assert covered == set(SUPPORTED_MODALITIES)
    for modality in SUPPORTED_MODALITIES:
        suite = build_remote_suite(registry, modality)
        assert len(suite.fixture_digests) == 1
    with pytest.raises(PolicyViolation, match="Audio"):
        build_remote_suite(registry, Modality.AUDIO_TRANSCRIPTION)


@pytest.mark.parametrize("modality", SUPPORTED_MODALITIES)
def test_tested_adapter_and_independent_verifier_pass_each_modality(
    modality: Modality,
) -> None:
    fixture = _fixture(modality)
    plan = _plan(fixture)
    invoker = FakeInvoker()
    adapter, verifier = _adapter_pair(invoker)

    tested = adapter.invoke(plan=plan, fixture=fixture, repetition=1)

    assert tested.status.value == "passed"
    assert not tested.verifier_approved
    assert tested.retry_count == 0
    assert len(invoker.calls) == 1
    assert RAW_CANARY not in repr(tested)
    verdict = verifier.verify(plan=plan, fixture=fixture, result=tested)
    assert verdict.approved
    assert verdict.verifier_model_id != verdict.tested_model_id
    with pytest.raises(PolicyViolation, match="tuketildi"):
        verifier.verify(plan=plan, fixture=fixture, result=tested)


@pytest.mark.parametrize(
    ("modality", "malformed"),
    [
        (Modality.CHAT, {"json": {"durum": "hazir", "dil": "tr", "extra": "x"}}),
        (Modality.COMPLETION, {"text": "marker yok", "extra": True}),
        (Modality.CODE, {"text": "def topla(a, b): return a + b"}),
        (Modality.EMBEDDING, {"data": [[1.0, 0.0]]}),
        (Modality.RERANK, {"scores": [0.9, 0.1, 0.6], "extra": 1}),
        (Modality.VISION_LANGUAGE, {"answer": "kare"}),
        (Modality.GUARDRAIL, {"labels": [False, True, False, True], "extra": 1}),
    ],
)
def test_every_modality_rejects_non_exact_response_shape(
    modality: Modality, malformed: dict[str, Any]
) -> None:
    assert not _evaluate(modality, malformed).approved


def test_embedding_rejects_non_finite_and_non_deterministic_vectors() -> None:
    non_finite = _response(Modality.EMBEDDING)
    non_finite["vectors"][0][0] = float("nan")
    assert not _evaluate(Modality.EMBEDDING, non_finite).approved

    non_deterministic = _response(Modality.EMBEDDING)
    non_deterministic["vectors"][1] = [0.0, 1.0]
    verdict = _evaluate(Modality.EMBEDDING, non_deterministic)
    assert not verdict.approved
    assert verdict.metrics["duplicate_max_delta"] == 1.0


def test_rerank_requires_monotonic_order_and_margin() -> None:
    wrong_order = _evaluate(Modality.RERANK, {"scores": [0.2, 0.9, 0.6]})
    narrow_margin = _evaluate(Modality.RERANK, {"scores": [0.9, 0.8, 0.85]})
    assert not wrong_order.approved and not wrong_order.metrics["monotonic"]
    assert not narrow_margin.approved
    assert float(narrow_margin.metrics["minimum_margin"]) < 0.05


def test_vision_requires_grounding_and_rejects_hallucination() -> None:
    missing = _evaluate(
        Modality.VISION_LANGUAGE,
        {"answer": "Kare var.", "objects": ["kirmizi kare"]},
    )
    hallucinated = _evaluate(
        Modality.VISION_LANGUAGE,
        {
            "answer": "Kare ve araba var.",
            "objects": ["kirmizi kare", "beyaz zemin", "araba"],
        },
    )
    assert not missing.approved and missing.metrics["grounding_recall"] == 0.5
    assert not hallucinated.approved and hallucinated.metrics["hallucination_count"] == 1


def test_guardrail_measures_false_positive_and_false_negative() -> None:
    verdict = _evaluate(
        Modality.GUARDRAIL,
        {"labels": [True, False, False, True]},
    )
    assert not verdict.approved
    assert verdict.metrics["false_positive_rate"] == 0.5
    assert verdict.metrics["false_negative_rate"] == 0.5


def test_raw_response_is_not_serialized_and_cost_is_strict() -> None:
    response = RemoteProviderResponse(
        payload={"text": RAW_CANARY},
        latency_ms=1,
        input_tokens=1,
        output_tokens=1,
        estimated_cost=0.0,
    )
    assert RAW_CANARY not in repr(response)
    assert "text" not in repr(response)
    assert response.response_digest.startswith("sha256:")
    with pytest.raises(ValidationFailed, match="cost"):
        replace(response, estimated_cost=float("inf"))
    with pytest.raises(ValidationFailed, match="cost"):
        replace(response, actual_cost=-1.0)


def test_self_verifier_is_rejected_without_consuming_raw_response() -> None:
    fixture = _fixture(Modality.CHAT)
    plan = _plan(fixture)
    adapter, _ = _adapter_pair(FakeInvoker())
    tested = adapter.invoke(plan=plan, fixture=fixture, repetition=1)
    verifier = OpenCodeDeterministicBenchmarkVerifier(
        VerifierIdentity(MODEL_ID, "self", digest("self")),
        default_fixture_file().parent.resolve(strict=True),
        adapter.response_store,
    )
    with pytest.raises(PolicyViolation, match="kendi deterministic verifier"):
        verifier.verify(plan=plan, fixture=fixture, result=tested)


def test_tested_adapter_and_verifier_have_distinct_identity_and_provenance() -> None:
    adapter, verifier = _adapter_pair(FakeInvoker())

    assert adapter.model_id == MODEL_ID
    assert verifier.verifier.model_id == "independent-verifier"
    assert verifier.verifier.execution_identity == "deterministic:opencode-remote"
    assert adapter.adapter_digest != verifier.verifier.provenance_digest
    assert adapter.execution_mode == verifier.execution_mode == "remote"


def test_verifier_rejects_tampered_canonical_metric_or_provenance() -> None:
    fixture = _fixture(Modality.CHAT)
    plan = _plan(fixture)
    adapter, verifier = _adapter_pair(FakeInvoker())
    tested = adapter.invoke(plan=plan, fixture=fixture, repetition=1)
    metric_verdict = verifier.verify(
        plan=plan,
        fixture=fixture,
        result=replace(tested, latency_ms=999),
    )

    second_adapter, second_verifier = _adapter_pair(FakeInvoker())
    second_tested = second_adapter.invoke(plan=plan, fixture=fixture, repetition=1)
    provenance_verdict = second_verifier.verify(
        plan=plan,
        fixture=fixture,
        result=replace(second_tested, evidence_digest=digest("tampered-evidence")),
    )

    assert not metric_verdict.approved
    assert not provenance_verdict.approved


def test_audio_is_excluded_before_invoker_and_makes_zero_calls() -> None:
    invoker = FakeInvoker()
    store = ProcessMemoryResponseStore()
    request = RemoteProviderInvocation
    with pytest.raises(PolicyViolation, match="audio/unknown"):
        request(
            model_id=MODEL_ID,
            modality=Modality.AUDIO_TRANSCRIPTION,
            fixture_digest=digest("audio"),
            repetition=1,
            payload={},
        )
    assert invoker.calls == []
    assert repr(store) == "ProcessMemoryResponseStore()"


def test_embedding_determinism_is_checked_across_five_repetitions() -> None:
    fixture = _fixture(Modality.EMBEDDING)
    plan = _plan(fixture)
    adapter, _ = _adapter_pair(FakeInvoker())
    results = tuple(
        adapter.invoke(plan=plan, fixture=fixture, repetition=repetition)
        for repetition in range(1, 6)
    )

    assert embedding_repetitions_are_deterministic(results)
    assert not embedding_repetitions_are_deterministic(results[:4])
    assert not embedding_repetitions_are_deterministic(
        (*results[:-1], replace(results[-1], response_digest=digest("repetition-drift")))
    )
