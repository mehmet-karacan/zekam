"""Canli provider kapisindan onceki exact contract manifesti testleri."""

from __future__ import annotations

import json
import wave
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application.provider_adapter import MultipartProviderCall, ProviderCall
from zekam.application.provider_configuration import load_provider_bindings
from zekam.application.provider_contract_execution import (
    assemble_contract_observations,
    build_provider_execution_manifest,
    build_provider_policy_candidate,
    evaluate_text_contracts,
    generated_vl_fixture_png,
    load_provider_contract_fixtures,
    prepare_provider_contract_calls,
)
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.domain.model_contract import evaluate_observation
from zekam.domain.model_inventory import Modality
from zekam.domain.policy import PolicyDocument, default_policy_rules
from zekam.domain.work import EffectKind

pytestmark = pytest.mark.unit


def _base_policy() -> PolicyDocument:
    return PolicyDocument.create(
        realm_id=uuid4(),
        name="varsayilan",
        revision=1,
        rules=default_policy_rules(),
    )


def _chat_response(content: str) -> dict[str, object]:
    return {"choices": [{"message": {"content": content}}]}


def _passing_responses() -> dict[str, dict[str, object]]:
    vector = [0.1, 0.2, 0.3]
    return {
        "chat-contract": _chat_response('{"answer":"Ankara","evidence":"fixture"}'),
        "code-contract": _chat_response("def add(a, b):\n    return a + b\nassert add(1, 2) == 3"),
        "audio_transcription-contract": {"text": "Zekam yerel model doğrulama kaydı."},
        "embedding-single-1": {"data": [{"index": 0, "embedding": vector}]},
        "embedding-single-2": {"data": [{"index": 0, "embedding": vector}]},
        "embedding-single-3": {"data": [{"index": 0, "embedding": vector}]},
        "embedding-batch": {"data": [{"index": index, "embedding": vector} for index in range(3)]},
        "vision_language-contract": _chat_response('{"objects":["red square","blue circle"]}'),
        "rerank-contract": {
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.5},
                {"index": 2, "relevance_score": 0.1},
            ]
        },
        "guardrail-contract": _chat_response(json.dumps({"labels": ["safe"] * 5 + ["unsafe"] * 5})),
    }


def _write_valid_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * 160)


def _runtime_environment(audio: Path) -> dict[str, str]:
    environment = {"ZEKAM_FIXTURE_WHISPER_WAV": str(audio)}
    for binding in load_provider_bindings().bindings:
        environment[binding.endpoint_env] = f"https://models.example.test{binding.path_hint}"
    return environment


def test_manifest_has_exact_ten_one_shot_call_plans_for_seven_targets() -> None:
    manifest = build_provider_execution_manifest()
    assert len(manifest.calls) == 10
    assert len(manifest.targets) == 7
    assert len({item.call_id for item in manifest.calls}) == 10
    assert sum(item.modality is Modality.EMBEDDING for item in manifest.calls) == 4
    assert all(item.authorization_scope()["max_uses"] == 1 for item in manifest.calls)
    assert all(
        item.authorization_scope()["resources"] == [item.target, item.call_resource]
        for item in manifest.calls
    )
    assert all(item.authorization_plan_digest.startswith("sha256:") for item in manifest.calls)
    document = manifest.as_dict()
    assert document["provider_calls_made"] == 0
    assert document["network_calls_made"] == 0
    assert document["grants_authority"] is False


def test_runtime_manifest_binds_all_ten_auths_and_embedding_variants_exactly(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "fixture.wav"
    _write_valid_wav(audio)
    manifest = build_provider_execution_manifest(
        environ=_runtime_environment(audio), audio_allowed_root=tmp_path
    )
    assert all(item.runtime_bound for item in manifest.calls)
    assert len({item.authorization_plan_digest for item in manifest.calls}) == 10
    assert len({item.effect_request.effect_digest for item in manifest.calls}) == 10
    embeddings = [item for item in manifest.calls if item.modality is Modality.EMBEDDING]
    assert len({item.authorization_plan_digest for item in embeddings}) == 4
    assert len({item.effect_request.effect_digest for item in embeddings}) == 4


def test_explicit_public_absolute_whisper_locator_needs_no_repo_root(tmp_path: Path) -> None:
    audio = tmp_path / "user-public-turkish.wav"
    _write_valid_wav(audio)

    manifest = build_provider_execution_manifest(environ=_runtime_environment(audio))
    whisper = next(item for item in manifest.calls if item.modality is Modality.AUDIO_TRANSCRIPTION)

    assert whisper.runtime_bound is True
    assert whisper.fixture_digest.startswith("sha256:")
    assert str(audio) not in json.dumps(whisper.as_dict())


def test_whisper_bytes_drift_and_allow_root_escape_fail_closed(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    audio = allowed / "fixture.wav"
    _write_valid_wav(audio)
    environment = _runtime_environment(audio)
    manifest = build_provider_execution_manifest(environ=environment, audio_allowed_root=allowed)
    with pytest.raises(ConfigurationError, match="exact path"):
        build_provider_execution_manifest(
            environ=_runtime_environment(outside / "missing.wav"),
            audio_allowed_root=allowed,
        )
    with audio.open("ab") as changed:
        changed.write(b"drift")
    with pytest.raises(ValidationFailed, match="manifest runtime binding"):
        prepare_provider_contract_calls(
            manifest=manifest,
            environ=environment,
            audio_allowed_root=allowed,
        )


def test_provider_policy_candidate_opens_only_manifest_targets() -> None:
    base = _base_policy()
    manifest = build_provider_execution_manifest()
    candidate = build_provider_policy_candidate(base, manifest)
    provider_rule = candidate.rule_for(EffectKind.PROVIDER_CALL)
    network_rule = candidate.rule_for(EffectKind.NETWORK_CALL)
    push_rule = candidate.rule_for(EffectKind.GIT_PUSH)
    assert candidate.revision == base.revision + 1
    assert candidate.network_default_deny
    assert candidate.push_default_deny
    assert provider_rule is not None and provider_rule.allow
    assert set(provider_rule.allowed_resources) == set(manifest.policy_resources)
    assert network_rule is not None and not network_rule.allow
    assert push_rule is not None and not push_rule.allow
    assert "model:out-of-scope" not in provider_rule.allowed_resources


def test_generated_public_vl_png_is_deterministic_and_valid() -> None:
    first = generated_vl_fixture_png()
    second = generated_vl_fixture_png()
    assert first == second
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in first and b"IDAT" in first and first.endswith(b"IEND\xaeB`\x82")


def test_audio_fixture_missing_fails_before_any_call_is_prepared() -> None:
    with pytest.raises(ConfigurationError, match="audio locator"):
        prepare_provider_contract_calls(environ={})


def test_all_ten_payloads_are_prepared_locally_without_transport(tmp_path: Path) -> None:
    audio = tmp_path / "fixture.wav"
    _write_valid_wav(audio)
    prepared = prepare_provider_contract_calls(
        environ=_runtime_environment(audio), audio_allowed_root=tmp_path
    )
    assert len(prepared) == 10
    assert sum(isinstance(item.call, ProviderCall) for item in prepared) == 9
    assert sum(isinstance(item.call, MultipartProviderCall) for item in prepared) == 1
    assert all(item.call.request_identity == item.plan.call_id for item in prepared)
    assert all(item.call.data_categories[0].value == "public" for item in prepared)


def test_synthetic_responses_pass_every_quantitative_and_text_contract(tmp_path: Path) -> None:
    audio = tmp_path / "fixture.wav"
    _write_valid_wav(audio)
    prepared = prepare_provider_contract_calls(
        environ=_runtime_environment(audio), audio_allowed_root=tmp_path
    )
    responses = _passing_responses()
    observations = assemble_contract_observations(prepared, responses)
    assert set(observations) == {
        Modality.AUDIO_TRANSCRIPTION,
        Modality.GUARDRAIL,
        Modality.VISION_LANGUAGE,
        Modality.EMBEDDING,
        Modality.RERANK,
    }
    assert all(evaluate_observation(item).verified for item in observations.values())
    assert evaluate_text_contracts(prepared, responses) == {
        "chat_json_shape": True,
        "code_required_markers": True,
    }


def test_response_set_and_text_contracts_fail_closed(tmp_path: Path) -> None:
    audio = tmp_path / "fixture.wav"
    _write_valid_wav(audio)
    prepared = prepare_provider_contract_calls(
        environ=_runtime_environment(audio), audio_allowed_root=tmp_path
    )
    responses = _passing_responses()
    responses.pop("rerank-contract")
    with pytest.raises(ValidationFailed, match="exact call manifest"):
        assemble_contract_observations(prepared, responses)

    responses = _passing_responses()
    responses["chat-contract"] = _chat_response("not-json")
    responses["code-contract"] = _chat_response("print('missing contract markers')")
    assert evaluate_text_contracts(prepared, responses) == {
        "chat_json_shape": False,
        "code_required_markers": False,
    }

    responses = _passing_responses()
    responses["chat-contract"] = _chat_response(
        '{"answer":"Ankara","evidence":"fixture","extra":true}'
    )
    assert evaluate_text_contracts(prepared, responses)["chat_json_shape"] is False
    responses["chat-contract"] = _chat_response('{"answer":"Ankara","evidence":123}')
    assert evaluate_text_contracts(prepared, responses)["chat_json_shape"] is False


def test_fixture_registry_is_public_and_digest_bound() -> None:
    fixtures = load_provider_contract_fixtures()
    assert fixtures.document["data_classification"] == "public"
    assert fixtures.fixture_digest.startswith("sha256:")
