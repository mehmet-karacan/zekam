"""Canli provider testinden once hazirlanan exact fixture/call/policy manifesti.

Bu modul kendi basina ag cagrisi yapmaz veya authorization uretmez. Yalnizca:

- public contract fixture'larini dogrular,
- her maliyetli cagri icin ayri exact effect request/plandigest uretir,
- varsayilan deny policy'den yedi hedefli policy adayi kurar,
- endpoint/credential degerleri mevcutsa process belleginde provider payload'i hazirlar,
- sonradan gelen response'lari provider-neutral observation'a cevirir.
"""

from __future__ import annotations

import binascii
import hashlib
import io
import json
import os
import struct
import wave
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from zekam.application.config import core_root, package_root
from zekam.application.governance import EffectRequest
from zekam.application.model_registry import load_inventory
from zekam.application.provider_adapter import (
    MultipartProviderCall,
    ProviderCall,
    openai_chat_payload,
    openai_embedding_payload,
    openai_embeddings,
    openai_guardrail_labels,
    openai_guardrail_payload,
    openai_rerank_payload,
    openai_rerank_scores,
    openai_transcript,
    openai_transcription_body,
    openai_vision_objects,
    openai_vision_payload,
    reviewed_endpoint_digest,
)
from zekam.application.provider_configuration import (
    ProviderBinding,
    ProviderBindingSet,
    load_provider_bindings,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.domain.model_contract import ContractObservation
from zekam.domain.model_inventory import InventorySnapshot, Modality
from zekam.domain.policy import PolicyDocument, PolicyRule, RiskLevel
from zekam.domain.security import DataClassification
from zekam.domain.work import EffectKind

FIXTURE_SCHEMA = "zekam-model-provider-contract-fixtures/v1"


def default_contract_fixture_file() -> Path:
    repository_copy = core_root() / "config" / "model_provider_contract_fixtures.yaml"
    if repository_copy.is_file():
        return repository_copy
    return package_root() / "_config" / "model_provider_contract_fixtures.yaml"


@dataclass(frozen=True, slots=True)
class ProviderContractFixtures:
    document: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        if self.document.get("schema") != FIXTURE_SCHEMA:
            raise ValidationFailed("Provider contract fixture semasi gecersiz")
        if self.document.get("data_classification") != DataClassification.PUBLIC.value:
            raise ValidationFailed("Provider contract fixture yalniz public veri tasimali")
        fixtures = self.document.get("fixtures")
        if not isinstance(fixtures, dict):
            raise ValidationFailed("Provider contract fixtures object olmali")
        expected = {item.value for item in Modality if item is not Modality.UNKNOWN}
        expected.discard(Modality.COMPLETION.value)
        if set(fixtures) != expected:
            raise ValidationFailed("Provider contract fixture modalite kapsami exact degil")
        audio = self.for_modality(Modality.AUDIO_TRANSCRIPTION)
        if not all(
            audio.get(key) for key in ("audio_env", "filename", "media_type", "reference_text")
        ):
            raise ValidationFailed("Whisper fixture metadata eksik")
        chat = self.for_modality(Modality.CHAT)
        required_keys = chat.get("required_json_keys")
        required_types = chat.get("required_json_types")
        if (
            not isinstance(required_keys, list)
            or not required_keys
            or not isinstance(required_types, dict)
            or set(required_types) != set(required_keys)
            or any(value != "string" for value in required_types.values())
            or chat.get("additional_properties") is not False
        ):
            raise ValidationFailed("Chat exact JSON schema fixture'i gecersiz")
        embedding = self.for_modality(Modality.EMBEDDING)
        if int(embedding.get("repetitions", 0)) < 2:
            raise ValidationFailed("Embedding fixture en az iki repetition ister")
        guardrail = self.for_modality(Modality.GUARDRAIL).get("samples")
        if not isinstance(guardrail, list) or not guardrail:
            raise ValidationFailed("Guardrail fixture samples ister")
        if not any(bool(item.get("unsafe")) for item in guardrail) or not any(
            not bool(item.get("unsafe")) for item in guardrail
        ):
            raise ValidationFailed("Guardrail fixture safe ve unsafe ornek ister")

    def for_modality(self, modality: Modality) -> Mapping[str, Any]:
        fixtures = self.document["fixtures"]
        assert isinstance(fixtures, dict)
        row = fixtures.get(modality.value)
        if not isinstance(row, dict):
            raise ValidationFailed(f"Provider fixture bulunamadi: {modality.value}")
        return row

    @property
    def fixture_digest(self) -> str:
        return digest(dict(self.document))


def load_provider_contract_fixtures(path: Path | None = None) -> ProviderContractFixtures:
    target = path or default_contract_fixture_file()
    if not target.is_file():
        raise ConfigurationError("Provider contract fixture dosyasi bulunamadi")
    try:
        document = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError("Provider contract fixture dosyasi okunamadi") from exc
    if not isinstance(document, dict):
        raise ValidationFailed("Provider contract fixture document object olmali")
    return ProviderContractFixtures(document)


@dataclass(frozen=True, slots=True)
class ReviewedAudioFixture:
    """Ham path veya audio bytes'i kanonik kayda sokmayan reviewed WAV binding."""

    content: bytes = field(repr=False)
    content_digest: str
    identity_digest: str
    size_bytes: int


def _has_reparse_point(path: Path) -> bool:
    attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & 0x400)


def review_whisper_audio_fixture(
    fixture: Mapping[str, Any],
    environment: Mapping[str, str],
    *,
    allowed_root_override: Path | None = None,
) -> ReviewedAudioFixture:
    """Locator WAV'ini exact bytes, allow-root ve PCM format ile fail-closed baglar."""

    locator = str(fixture.get("audio_env", ""))
    source = environment.get(locator)
    if not source:
        raise ConfigurationError("Whisper public fixture audio locator degeri eksik")
    candidate = Path(source)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigurationError("Whisper public fixture absolute path olmali")
    try:
        resolved = candidate.resolve(strict=True)
        if allowed_root_override is not None:
            resolved.relative_to(allowed_root_override.resolve(strict=True))
    except (OSError, ValueError):
        raise ConfigurationError("Whisper public fixture exact path gecersiz") from None
    if candidate.is_symlink() or resolved.is_symlink() or _has_reparse_point(resolved):
        raise ConfigurationError("Whisper public fixture link/reparse olamaz")
    current = resolved.parent
    anchor = Path(resolved.anchor)
    while True:
        if current.is_symlink() or _has_reparse_point(current):
            raise ConfigurationError("Whisper public fixture parent link/reparse olamaz")
        if current == anchor:
            break
        current = current.parent
    max_bytes = int(fixture.get("max_bytes", 25 * 1024 * 1024))
    size = resolved.stat().st_size
    if size < 44 or size > max_bytes:
        raise ConfigurationError("Whisper public fixture audio boyutu gecersiz")
    content = resolved.read_bytes()
    if len(content) != size or content[:4] != b"RIFF" or content[8:12] != b"WAVE":
        raise ConfigurationError("Whisper public fixture RIFF/WAVE olmali")
    try:
        with wave.open(io.BytesIO(content), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            frame_rate = wav.getframerate()
            frame_count = wav.getnframes()
            compression = wav.getcomptype()
    except (EOFError, wave.Error):
        raise ConfigurationError("Whisper public fixture WAV formati gecersiz") from None
    if (
        compression != "NONE"
        or channels not in {1, 2}
        or sample_width not in {1, 2, 3, 4}
        or not 8_000 <= frame_rate <= 192_000
        or frame_count < 1
    ):
        raise ConfigurationError("Whisper public fixture PCM WAV profili gecersiz")
    content_digest = "sha256:" + hashlib.sha256(content).hexdigest()
    identity_digest = digest(
        {
            "content_digest": content_digest,
            "size_bytes": size,
            "filename": str(fixture["filename"]),
            "media_type": str(fixture["media_type"]),
            "language": str(fixture["language"]),
            "reference_text_digest": digest(str(fixture["reference_text"])),
            "wav": {
                "channels": channels,
                "sample_width": sample_width,
                "frame_rate": frame_rate,
                "frame_count": frame_count,
                "compression": compression,
            },
        }
    )
    return ReviewedAudioFixture(content, content_digest, identity_digest, size)


@dataclass(frozen=True, slots=True)
class ProviderCallPlan:
    call_id: str
    modality: Modality
    model_id: str
    provider_ref: str
    endpoint_ref: str
    operation: str
    secret_ref_name: str
    request_format: str
    fixture_digest: str
    payload_digest: str | None
    endpoint_binding_digest: str | None
    endpoint_path_hint: str
    fixture_identity_digest: str | None = None
    data_classifications: tuple[DataClassification, ...] = (DataClassification.PUBLIC,)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.data_classifications, tuple)
            or not self.data_classifications
            or len(set(self.data_classifications)) != len(self.data_classifications)
            or any(not isinstance(item, DataClassification) for item in self.data_classifications)
        ):
            raise ValidationFailed("Provider call classification seti exact olmali")

    @property
    def target(self) -> str:
        return f"{self.provider_ref}:{self.endpoint_ref}:{self.operation}"

    @property
    def call_resource(self) -> str:
        return f"provider:{self.model_id}:{self.operation}:{self.call_id}"

    @property
    def authorization_plan_digest(self) -> str:
        return digest(
            {
                "call_id": self.call_id,
                "model_id": self.model_id,
                "payload_digest": self.payload_digest,
                "fixture_digest": self.fixture_digest,
                "fixture_identity_digest": self.fixture_identity_digest,
                "endpoint_binding_digest": self.endpoint_binding_digest,
                "target": self.target,
                "data_classifications": [item.value for item in self.data_classifications],
            }
        )

    @property
    def runtime_bound(self) -> bool:
        return self.payload_digest is not None and self.endpoint_binding_digest is not None

    @property
    def effect_action(self) -> str:
        if self.payload_digest is None:
            raise ValidationFailed("Provider call payload henuz exact binding tasimiyor")
        return "provider-contract-call-" + digest(
            {
                "request_identity": self.call_id,
                "payload_digest": self.payload_digest,
                "plan_digest": self.authorization_plan_digest,
            }
        ).removeprefix("sha256:")

    def authorization_scope(self) -> dict[str, Any]:
        """Tek cagrinin exact one-shot authorization girdisini verir."""
        return {
            "call_id": self.call_id,
            "plan_digest": self.authorization_plan_digest,
            "effect_digest": (self.effect_request.effect_digest if self.runtime_bound else None),
            "effects": [EffectKind.PROVIDER_CALL.value],
            "resources": [self.target, self.call_resource],
            "provider_refs": [self.provider_ref],
            "operations": [self.operation],
            "data_classifications": [item.value for item in self.data_classifications],
            "max_uses": 1,
            "grants_authority": False,
            "runtime_bound": self.runtime_bound,
        }

    @property
    def effect_request(self) -> EffectRequest:
        if not self.runtime_bound:
            raise ValidationFailed("Provider authorization unresolved call icin uretilemez")
        return EffectRequest(
            action=self.effect_action,
            effects=(EffectKind.PROVIDER_CALL,),
            resources=(self.target, self.call_resource),
            data_classifications=self.data_classifications,
            provider_refs=(self.provider_ref,),
            reversible=False,
            touches_external_system=True,
            required_capabilities=("provider.call",),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "modality": self.modality.value,
            "model_id": self.model_id,
            "provider_ref": self.provider_ref,
            "endpoint_ref": self.endpoint_ref,
            "operation": self.operation,
            "secret_ref_name": self.secret_ref_name,
            "request_format": self.request_format,
            "fixture_digest": self.fixture_digest,
            "fixture_identity_digest": self.fixture_identity_digest,
            "payload_digest": self.payload_digest,
            "endpoint_binding_digest": self.endpoint_binding_digest,
            "endpoint_path_hint": self.endpoint_path_hint,
            "data_classifications": [item.value for item in self.data_classifications],
            "target": self.target,
            "call_resource": self.call_resource,
            "effect_digest": (self.effect_request.effect_digest if self.runtime_bound else None),
            "authorization_plan_digest": self.authorization_plan_digest,
            "authorization_scope": self.authorization_scope(),
            "grants_authority": False,
            "runtime_bound": self.runtime_bound,
        }


def _json_contract_payload(
    *,
    modality: Modality,
    variant: str,
    backend_model: str,
    fixture: Mapping[str, Any],
) -> Mapping[str, Any]:
    if modality is Modality.CHAT:
        return openai_chat_payload(backend_model, str(fixture["prompt"]))
    if modality is Modality.CODE:
        return openai_chat_payload(
            backend_model,
            str(fixture["prompt"]),
            system="Yalniz calisabilir Python kodu ve assert ornekleri ver.",
        )
    if modality is Modality.EMBEDDING:
        text = str(fixture["text"])
        repetitions = int(fixture["repetitions"])
        values = (text,) * repetitions if variant == "batch" else (text,)
        return openai_embedding_payload(backend_model, values)
    if modality is Modality.VISION_LANGUAGE:
        return openai_vision_payload(
            backend_model,
            str(fixture["prompt"]),
            generated_vl_fixture_png(),
            media_type="image/png",
        )
    if modality is Modality.RERANK:
        return openai_rerank_payload(
            backend_model,
            str(fixture["query"]),
            tuple(str(item) for item in fixture["documents"]),
        )
    if modality is Modality.GUARDRAIL:
        return openai_guardrail_payload(
            backend_model,
            tuple(str(item["text"]) for item in fixture["samples"]),
        )
    raise ValidationFailed("Provider JSON contract modalitesi desteklenmiyor")


def _multipart_payload_digest(body: Any) -> str:
    return digest({"body_digest": body.body_digest, "content_type": body.content_type})


@dataclass(frozen=True, slots=True)
class ProviderExecutionManifest:
    binding_set_digest: str
    fixture_digest: str
    calls: tuple[ProviderCallPlan, ...]

    @property
    def manifest_digest(self) -> str:
        return digest(self.as_dict())

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(sorted({item.target for item in self.calls}))

    @property
    def policy_resources(self) -> tuple[str, ...]:
        return tuple(sorted({*self.targets, *(item.call_resource for item in self.calls)}))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-provider-execution-manifest/v1",
            "binding_set_digest": self.binding_set_digest,
            "fixture_digest": self.fixture_digest,
            "call_count": len(self.calls),
            "calls": [item.as_dict() for item in self.calls],
            "provider_calls_made": 0,
            "network_calls_made": 0,
            "grants_authority": False,
        }


def build_provider_execution_manifest(
    bindings: ProviderBindingSet | None = None,
    fixtures: ProviderContractFixtures | None = None,
    *,
    inventory: InventorySnapshot | None = None,
    environ: Mapping[str, str] | None = None,
    audio_allowed_root: Path | None = None,
) -> ProviderExecutionManifest:
    selected = bindings or load_provider_bindings()
    registry = fixtures or load_provider_contract_fixtures()
    snapshot = inventory or load_inventory()
    environment = os.environ if environ is None else environ
    records = {item.model_id: item for item in snapshot.records}
    calls: list[ProviderCallPlan] = []
    for binding in selected.bindings:
        record = records.get(binding.model_id)
        if record is None:
            raise ValidationFailed("Provider manifest modeli inventory icinde bulunamadi")
        fixture = registry.for_modality(binding.modality)
        endpoint_value = environment.get(binding.endpoint_env)
        endpoint_digest = (
            None
            if not endpoint_value
            else reviewed_endpoint_digest(endpoint_value, path_hint=binding.path_hint)
        )
        reviewed_audio: ReviewedAudioFixture | None = None
        if binding.modality is Modality.AUDIO_TRANSCRIPTION and environment.get(
            str(fixture["audio_env"])
        ):
            reviewed_audio = review_whisper_audio_fixture(
                fixture, environment, allowed_root_override=audio_allowed_root
            )
        variants = (
            ("single-1", "single-2", "single-3", "batch")
            if binding.modality is Modality.EMBEDDING
            else ("contract",)
        )
        for variant in variants:
            payload_digest: str | None
            if binding.modality is Modality.AUDIO_TRANSCRIPTION:
                if reviewed_audio is None:
                    payload_digest = None
                else:
                    body = openai_transcription_body(
                        record.backend_model,
                        reviewed_audio.content,
                        filename=str(fixture["filename"]),
                        media_type=str(fixture["media_type"]),
                        language=str(fixture["language"]),
                    )
                    payload_digest = _multipart_payload_digest(body)
            else:
                payload_digest = digest(
                    dict(
                        _json_contract_payload(
                            modality=binding.modality,
                            variant=variant,
                            backend_model=record.backend_model,
                            fixture=fixture,
                        )
                    )
                )
            calls.append(
                ProviderCallPlan(
                    call_id=f"{binding.modality.value}-{variant}",
                    modality=binding.modality,
                    model_id=binding.model_id,
                    provider_ref=binding.provider_ref,
                    endpoint_ref=binding.endpoint_ref,
                    operation=binding.operation,
                    secret_ref_name=binding.secret_ref_name,
                    request_format=binding.request_format.value,
                    fixture_digest=digest(dict(fixture)),
                    payload_digest=payload_digest,
                    endpoint_binding_digest=endpoint_digest,
                    endpoint_path_hint=binding.path_hint,
                    fixture_identity_digest=(
                        reviewed_audio.identity_digest if reviewed_audio is not None else None
                    ),
                )
            )
    return ProviderExecutionManifest(
        selected.binding_set_digest, registry.fixture_digest, tuple(calls)
    )


def build_provider_policy_candidate(
    base_policy: PolicyDocument,
    manifest: ProviderExecutionManifest,
) -> PolicyDocument:
    """Varsayilan deny policy'yi bozmadan yalniz manifest hedeflerini acar."""
    provider_rule = PolicyRule(
        name="exact-model-provider-contract-targets",
        effect_kinds=(EffectKind.PROVIDER_CALL,),
        allow=True,
        max_risk=RiskLevel.CRITICAL,
        allowed_resources=manifest.policy_resources,
        reason="Yalniz digest-bound contract manifest hedefleri",
    )
    retained: list[PolicyRule] = []
    for rule in base_policy.rules:
        if EffectKind.PROVIDER_CALL not in rule.effect_kinds:
            retained.append(rule)
            continue
        remaining = tuple(
            item for item in rule.effect_kinds if item is not EffectKind.PROVIDER_CALL
        )
        if remaining:
            retained.append(replace(rule, effect_kinds=remaining))
    return PolicyDocument.create(
        realm_id=base_policy.realm_id,
        name=base_policy.name,
        revision=base_policy.revision + 1,
        rules=(provider_rule, *retained),
        network_default_deny=True,
        push_default_deny=True,
    )


@dataclass(frozen=True, slots=True)
class PreparedProviderContractCall:
    plan: ProviderCallPlan
    call: ProviderCall | MultipartProviderCall = field(repr=False)


def _binding_by_model(bindings: ProviderBindingSet) -> dict[str, ProviderBinding]:
    return {item.model_id: item for item in bindings.bindings}


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    )


def generated_vl_fixture_png() -> bytes:
    """64x32 public fixture: solda red square, sagda blue circle."""
    width, height = 64, 32
    rows: list[bytes] = []
    for y in range(height):
        row = bytearray((255, 255, 255) * width)
        for x in range(width):
            red_square = 5 <= x < 25 and 6 <= y < 26
            blue_circle = (x - 48) ** 2 + (y - 16) ** 2 <= 10**2
            color = (220, 30, 30) if red_square else (30, 60, 220) if blue_circle else None
            if color is not None:
                row[x * 3 : x * 3 + 3] = bytes(color)
        rows.append(b"\x00" + bytes(row))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


def prepare_provider_contract_calls(
    *,
    manifest: ProviderExecutionManifest | None = None,
    bindings: ProviderBindingSet | None = None,
    fixtures: ProviderContractFixtures | None = None,
    inventory: InventorySnapshot | None = None,
    environ: Mapping[str, str] | None = None,
    audio_allowed_root: Path | None = None,
) -> tuple[PreparedProviderContractCall, ...]:
    """Canli son kapi icin payload hazirlar; hicbir transport cagirmadan doner."""
    selected = bindings or load_provider_bindings()
    registry = fixtures or load_provider_contract_fixtures()
    snapshot = inventory or load_inventory()
    environment = os.environ if environ is None else environ
    rebuilt = build_provider_execution_manifest(
        selected,
        registry,
        inventory=snapshot,
        environ=environment,
        audio_allowed_root=audio_allowed_root,
    )
    if manifest is not None and manifest.manifest_digest != rebuilt.manifest_digest:
        raise ValidationFailed("Provider manifest runtime binding ile drift etti")
    execution = rebuilt
    audio_fixture = registry.for_modality(Modality.AUDIO_TRANSCRIPTION)
    if not environment.get(str(audio_fixture["audio_env"])):
        raise ConfigurationError("Whisper public fixture audio locator degeri eksik")
    if not all(plan.runtime_bound for plan in execution.calls):
        raise ConfigurationError("Provider contract call planlari runtime-bound degil")
    records = {item.model_id: item for item in snapshot.records}
    binding_map = _binding_by_model(selected)
    prepared: list[PreparedProviderContractCall] = []
    for plan in execution.calls:
        binding = binding_map[plan.model_id]
        record = records[plan.model_id]
        fixture = registry.for_modality(plan.modality)
        if plan.modality is Modality.AUDIO_TRANSCRIPTION:
            reviewed = review_whisper_audio_fixture(
                fixture, environment, allowed_root_override=audio_allowed_root
            )
            multipart = openai_transcription_body(
                record.backend_model,
                reviewed.content,
                filename=str(fixture["filename"]),
                media_type=str(fixture["media_type"]),
                language=str(fixture["language"]),
            )
            prepared_call: ProviderCall | MultipartProviderCall = MultipartProviderCall(
                binding.provider_ref,
                binding.endpoint_ref,
                binding.operation,
                plan.call_id,
                multipart,
                data_categories=(DataClassification.PUBLIC,),
                retention_assumption="contract-fixture-no-retention",
                endpoint_path_hint=plan.endpoint_path_hint,
                endpoint_binding_digest=plan.endpoint_binding_digest,
                authorization_plan_digest=plan.authorization_plan_digest,
                authorization_resource=plan.call_resource,
            )
            if prepared_call.payload_digest != plan.payload_digest:
                raise ValidationFailed("Whisper payload digest call plan ile eslesmiyor")
            prepared.append(PreparedProviderContractCall(plan, prepared_call))
            continue
        variant = plan.call_id.removeprefix(f"{plan.modality.value}-")
        payload = _json_contract_payload(
            modality=plan.modality,
            variant=variant,
            backend_model=record.backend_model,
            fixture=fixture,
        )
        prepared_call = ProviderCall(
            binding.provider_ref,
            binding.endpoint_ref,
            binding.operation,
            plan.call_id,
            payload,
            data_categories=(DataClassification.PUBLIC,),
            retention_assumption="contract-fixture-no-retention",
            endpoint_path_hint=plan.endpoint_path_hint,
            endpoint_binding_digest=plan.endpoint_binding_digest,
            authorization_plan_digest=plan.authorization_plan_digest,
            authorization_resource=plan.call_resource,
        )
        if prepared_call.payload_digest != plan.payload_digest:
            raise ValidationFailed("Provider payload digest call plan ile eslesmiyor")
        prepared.append(PreparedProviderContractCall(plan, prepared_call))
    return tuple(prepared)


def assemble_contract_observations(
    prepared: tuple[PreparedProviderContractCall, ...],
    responses: Mapping[str, Mapping[str, Any]],
    fixtures: ProviderContractFixtures | None = None,
) -> dict[Modality, ContractObservation]:
    """Gercek response setini bes nicel evaluator observation'ina cevirir."""
    registry = fixtures or load_provider_contract_fixtures()
    expected_ids = {item.plan.call_id for item in prepared}
    if set(responses) != expected_ids:
        raise ValidationFailed("Provider response seti exact call manifest ile eslesmeli")
    response_digest = digest({key: dict(responses[key]) for key in sorted(responses)})
    by_modality: dict[Modality, list[PreparedProviderContractCall]] = {}
    for item in prepared:
        by_modality.setdefault(item.plan.modality, []).append(item)
    result: dict[Modality, ContractObservation] = {}
    audio = by_modality[Modality.AUDIO_TRANSCRIPTION][0]
    audio_fixture = registry.for_modality(Modality.AUDIO_TRANSCRIPTION)
    result[Modality.AUDIO_TRANSCRIPTION] = ContractObservation(
        modality=Modality.AUDIO_TRANSCRIPTION,
        transcript_pairs=(
            (
                str(audio_fixture["reference_text"]),
                openai_transcript(responses[audio.plan.call_id]),
            ),
        ),
        fixture_digest=audio.plan.fixture_digest,
        response_digest=response_digest,
    )
    guard = by_modality[Modality.GUARDRAIL][0]
    guard_fixture = registry.for_modality(Modality.GUARDRAIL)
    samples = guard_fixture["samples"]
    result[Modality.GUARDRAIL] = ContractObservation(
        modality=Modality.GUARDRAIL,
        guardrail_expected=tuple(bool(item["unsafe"]) for item in samples),
        guardrail_predicted=openai_guardrail_labels(
            responses[guard.plan.call_id], expected_count=len(samples)
        ),
        fixture_digest=guard.plan.fixture_digest,
        response_digest=response_digest,
    )
    vision = by_modality[Modality.VISION_LANGUAGE][0]
    vision_fixture = registry.for_modality(Modality.VISION_LANGUAGE)
    result[Modality.VISION_LANGUAGE] = ContractObservation(
        modality=Modality.VISION_LANGUAGE,
        visual_expected=tuple(str(item) for item in vision_fixture["expected_objects"]),
        visual_mentioned=openai_vision_objects(responses[vision.plan.call_id]),
        fixture_digest=vision.plan.fixture_digest,
        response_digest=response_digest,
    )
    embeddings = sorted(by_modality[Modality.EMBEDDING], key=lambda item: item.plan.call_id)
    singles = tuple(
        openai_embeddings(responses[item.plan.call_id])[0]
        for item in embeddings
        if "single" in item.plan.call_id
    )
    batch_item = next(item for item in embeddings if item.plan.call_id.endswith("batch"))
    result[Modality.EMBEDDING] = ContractObservation(
        modality=Modality.EMBEDDING,
        embedding_singles=singles,
        embedding_batch=openai_embeddings(responses[batch_item.plan.call_id]),
        fixture_digest=batch_item.plan.fixture_digest,
        response_digest=response_digest,
    )
    rerank = by_modality[Modality.RERANK][0]
    rerank_fixture = registry.for_modality(Modality.RERANK)
    result[Modality.RERANK] = ContractObservation(
        modality=Modality.RERANK,
        rerank_scores=openai_rerank_scores(responses[rerank.plan.call_id]),
        rerank_expected_order=tuple(int(item) for item in rerank_fixture["expected_order"]),
        fixture_digest=rerank.plan.fixture_digest,
        response_digest=response_digest,
    )
    return result


def evaluate_text_contracts(
    prepared: tuple[PreparedProviderContractCall, ...],
    responses: Mapping[str, Mapping[str, Any]],
    fixtures: ProviderContractFixtures | None = None,
) -> dict[str, bool]:
    """Chat JSON shape ve code marker contract'ini caller beyanindan bagimsiz olcer."""
    from zekam.application.provider_adapter import openai_chat_text

    registry = fixtures or load_provider_contract_fixtures()
    indexed = {item.plan.modality: item for item in prepared}
    chat_text = openai_chat_text(responses[indexed[Modality.CHAT].plan.call_id])
    try:
        chat_document = json.loads(chat_text)
    except json.JSONDecodeError:
        chat_document = None
    chat_fixture = registry.for_modality(Modality.CHAT)
    chat_keys = tuple(str(item) for item in chat_fixture["required_json_keys"])
    chat_types = chat_fixture.get("required_json_types", {})
    chat_valid = bool(
        isinstance(chat_document, dict)
        and set(chat_document) == set(chat_keys)
        and chat_fixture.get("additional_properties") is False
        and isinstance(chat_types, dict)
        and set(chat_types) == set(chat_keys)
        and all(chat_types[key] == "string" for key in chat_keys)
        and all(isinstance(chat_document[key], str) for key in chat_keys)
    )
    code_text = openai_chat_text(responses[indexed[Modality.CODE].plan.call_id])
    markers = registry.for_modality(Modality.CODE)["required_markers"]
    return {
        "chat_json_shape": chat_valid,
        "code_required_markers": all(str(item) in code_text for item in markers),
    }
