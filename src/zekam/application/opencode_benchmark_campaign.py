"""OpenCode/AIHub benchmark campaign discovery and immutable planning.

This module is deliberately effect-free.  It reads the versioned Zekam scope,
the sanitized OpenCode catalog and the canonical inventory, then produces the
exact health and benchmark call budget.  Endpoint URLs, credentials and raw
fixture payloads are never part of the returned plan.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from zekam.application.model_benchmark_service import (
    default_fixture_file,
    load_fixture_registry,
    resolve_fixture_artifact,
)
from zekam.application.model_registry import load_inventory
from zekam.application.opencode_embedding import (
    OpenCodeModelCatalog,
    default_opencode_config_file,
    load_opencode_aihub_catalog,
    load_opencode_embedding_configuration,
)
from zekam.application.opencode_remote_benchmark import (
    RemoteFixtureArtifact,
    build_remote_suite,
    load_remote_fixture,
)
from zekam.application.provider_adapter import (
    ProviderCall,
    openai_chat_payload,
    openai_chat_text,
    openai_embedding_payload,
    openai_embeddings,
    openai_guardrail_labels,
    openai_guardrail_payload,
    openai_rerank_payload,
    openai_rerank_scores,
    openai_vision_objects,
    openai_vision_payload,
    reviewed_endpoint_digest,
)
from zekam.application.provider_contract_execution import (
    PreparedProviderContractCall,
    ProviderCallPlan,
    ProviderExecutionManifest,
    generated_vl_fixture_png,
)
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import FixtureRegistry
from zekam.domain.model_inventory import InventorySnapshot, Modality, ModelRecord
from zekam.domain.security import DataClassification

SCOPE_SCHEMA = "zekam-opencode-benchmark-scope/v1"
PROVIDER_ID = "litellm"
PROVIDER_FAMILY = "aihub"
AUDIO_EXCLUSION_REASON = "audio-user-scope-excluded"
BENCHMARK_SECRET_REF_NAME = "opencode-litellm-benchmark"


def default_scope_file() -> Path:
    from zekam.application.config import core_root, package_root

    repository_copy = core_root() / "config" / "opencode_benchmark_scope.yaml"
    if repository_copy.is_file():
        return repository_copy
    return package_root() / "_config" / "opencode_benchmark_scope.yaml"


@dataclass(frozen=True, slots=True)
class ScopeVerifier:
    model_id: str
    execution_identity: str

    def __post_init__(self) -> None:
        if not self.model_id.strip() or not self.execution_identity.strip():
            raise ValidationFailed("Campaign verifier identity bos olamaz")


@dataclass(frozen=True, slots=True)
class ScopeTarget:
    configured_model_id: str
    canonical_model_ids: tuple[str, ...]
    modality: Modality
    workload: str
    excluded_reason: str | None = None
    reviewed_duplicate_route: bool = False
    reviewed_modality_conflict: bool = False

    def __post_init__(self) -> None:
        if not self.configured_model_id.strip() or not self.workload.strip():
            raise ValidationFailed("Campaign target model/workload ister")
        if not self.canonical_model_ids or len(self.canonical_model_ids) != len(
            set(self.canonical_model_ids)
        ):
            raise ValidationFailed("Campaign target canonical Model ID seti gecersiz")
        if len(self.canonical_model_ids) > 1 and not self.reviewed_duplicate_route:
            raise PolicyViolation("Duplicate configured route explicit review ister")
        if self.modality is Modality.AUDIO_TRANSCRIPTION:
            if self.excluded_reason != AUDIO_EXCLUSION_REASON:
                raise PolicyViolation("Audio target exact kullanici exclusion reason ister")
        elif self.excluded_reason is not None:
            raise PolicyViolation("Audio disi target sessizce excluded edilemez")


@dataclass(frozen=True, slots=True)
class OpenCodeCampaignScope:
    version: int
    provider_id: str
    provider_family: str
    scope_policy: str
    repetitions: int
    verifier: ScopeVerifier
    targets: tuple[ScopeTarget, ...]

    def __post_init__(self) -> None:
        if self.version < 1 or self.repetitions < 5:
            raise ValidationFailed("Campaign version/repetition gecersiz")
        if self.provider_id != PROVIDER_ID or self.provider_family != PROVIDER_FAMILY:
            raise PolicyViolation("Campaign yalniz reviewed OpenCode AIHub provider'ini destekler")
        if self.scope_policy != "configured-canonical-all":
            raise PolicyViolation("Campaign scope policy gecersiz")
        configured = [item.configured_model_id for item in self.targets]
        if len(configured) != len(set(configured)):
            raise ValidationFailed("Campaign configured model kimligi tekil olmali")
        canonical = [model_id for item in self.targets for model_id in item.canonical_model_ids]
        if len(canonical) != len(set(canonical)):
            raise ValidationFailed("Campaign canonical Model ID baska targetta yinelenemez")

    @property
    def scope_digest(self) -> str:
        return digest(
            {
                "version": self.version,
                "provider_id": self.provider_id,
                "provider_family": self.provider_family,
                "scope_policy": self.scope_policy,
                "repetitions": self.repetitions,
                "verifier": {
                    "model_id": self.verifier.model_id,
                    "execution_identity": self.verifier.execution_identity,
                },
                "targets": [
                    {
                        "configured_model_id": item.configured_model_id,
                        "canonical_model_ids": list(item.canonical_model_ids),
                        "modality": item.modality.value,
                        "workload": item.workload,
                        "excluded_reason": item.excluded_reason,
                        "reviewed_duplicate_route": item.reviewed_duplicate_route,
                        "reviewed_modality_conflict": item.reviewed_modality_conflict,
                    }
                    for item in self.targets
                ],
            }
        )


@dataclass(frozen=True, slots=True)
class DiscoveredCampaignTarget:
    configured_model_id: str
    canonical_model_id: str
    modality: Modality
    workload: str
    inventory_digest: str
    fixture_digests: tuple[str, ...]
    excluded_reason: str | None

    @property
    def health_call_count(self) -> int:
        return 0 if self.excluded_reason is not None else 1

    def tested_call_count(self, repetitions: int) -> int:
        return 0 if self.excluded_reason is not None else len(self.fixture_digests) * repetitions

    def as_dict(self) -> dict[str, object]:
        return {
            "configured_model_id": self.configured_model_id,
            "canonical_model_id": self.canonical_model_id,
            "modality": self.modality.value,
            "workload": self.workload,
            "inventory_digest": self.inventory_digest,
            "fixture_digests": list(self.fixture_digests),
            "excluded_reason": self.excluded_reason,
            "health_call_count": self.health_call_count,
        }


@dataclass(frozen=True, slots=True)
class CampaignDiscovery:
    scope: OpenCodeCampaignScope
    catalog: OpenCodeModelCatalog
    inventory_digest: str
    fixture_registry_digest: str
    verifier_provenance_digest: str
    targets: tuple[DiscoveredCampaignTarget, ...]

    def __post_init__(self) -> None:
        parse_digest(self.inventory_digest)
        parse_digest(self.fixture_registry_digest)
        parse_digest(self.verifier_provenance_digest)
        if {item.configured_model_id for item in self.scope.targets} != set(
            self.catalog.configured_model_ids
        ):
            raise PolicyViolation("OpenCode configured model snapshot scope ile eslesmiyor")

    @property
    def configured_model_count(self) -> int:
        return len(self.scope.targets)

    @property
    def canonical_target_count(self) -> int:
        return len(self.targets)

    @property
    def audio_excluded_count(self) -> int:
        return sum(item.excluded_reason is not None for item in self.targets)

    @property
    def health_call_count(self) -> int:
        return sum(item.health_call_count for item in self.targets)

    @property
    def tested_call_count(self) -> int:
        return sum(item.tested_call_count(self.scope.repetitions) for item in self.targets)

    @property
    def provider_call_budget(self) -> int:
        return self.health_call_count + self.tested_call_count

    @property
    def discovery_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "zekam-opencode-benchmark-discovery/v1",
            "scope_digest": self.scope.scope_digest,
            "catalog_digest": digest(self.catalog.sanitized()),
            "endpoint_identity_digest": self.catalog.endpoint_identity_digest,
            "inventory_digest": self.inventory_digest,
            "fixture_registry_digest": self.fixture_registry_digest,
            "verifier_provenance_digest": self.verifier_provenance_digest,
            "configured_model_count": self.configured_model_count,
            "canonical_target_count": self.canonical_target_count,
            "audio_excluded_count": self.audio_excluded_count,
            "health_call_count": self.health_call_count,
            "tested_call_count": self.tested_call_count,
            "provider_call_budget": self.provider_call_budget,
            "repetitions": self.scope.repetitions,
            "targets": [item.as_dict() for item in self.targets],
            "grants_authority": False,
        }


class CampaignCallKind(StrEnum):
    HEALTH = "health"
    BENCHMARK = "benchmark"


@dataclass(frozen=True, slots=True)
class PreparedCampaignCall:
    kind: CampaignCallKind
    canonical_model_id: str
    configured_model_id: str
    modality: Modality
    fixture_digest: str
    repetition: int
    fixture_payload_digest: str
    prepared: PreparedProviderContractCall = field(repr=False)

    @property
    def call_id(self) -> str:
        return self.prepared.plan.call_id

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "call_id": self.call_id,
            "canonical_model_id": self.canonical_model_id,
            "configured_model_id": self.configured_model_id,
            "modality": self.modality.value,
            "fixture_digest": self.fixture_digest,
            "repetition": self.repetition,
            "fixture_payload_digest": self.fixture_payload_digest,
            "authorization_plan_digest": self.prepared.plan.authorization_plan_digest,
            "effect_digest": self.prepared.plan.effect_request.effect_digest,
            "target": self.prepared.plan.target,
            "call_resource": self.prepared.plan.call_resource,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class PreparedCampaignManifest:
    discovery: CampaignDiscovery
    calls: tuple[PreparedCampaignCall, ...]
    credential_locator: str = field(repr=False)
    endpoint_mapping: Mapping[tuple[str, str], str] = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.calls) != self.discovery.provider_call_budget:
            raise PolicyViolation("Campaign prepared call set exact budget ile eslesmiyor")
        call_ids = [item.call_id for item in self.calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValidationFailed("Campaign call ID tekil olmali")

    @property
    def execution_manifest(self) -> ProviderExecutionManifest:
        return ProviderExecutionManifest(
            binding_set_digest=self.discovery.discovery_digest,
            fixture_digest=self.discovery.fixture_registry_digest,
            calls=tuple(item.prepared.plan for item in self.calls),
        )

    @property
    def manifest_digest(self) -> str:
        return digest(
            {
                "discovery_digest": self.discovery.discovery_digest,
                "calls": [item.as_dict() for item in self.calls],
            }
        )

    def sanitized(self) -> dict[str, object]:
        return {
            "schema": "zekam-opencode-benchmark-manifest/v1",
            "manifest_digest": self.manifest_digest,
            "discovery": self.discovery.as_dict(),
            "call_count": len(self.calls),
            "health_call_count": sum(item.kind is CampaignCallKind.HEALTH for item in self.calls),
            "tested_call_count": sum(
                item.kind is CampaignCallKind.BENCHMARK for item in self.calls
            ),
            "calls": [item.as_dict() for item in self.calls],
            "endpoint_values_reported": 0,
            "secret_values_reported": 0,
            "raw_fixture_payloads_reported": 0,
            "grants_authority": False,
        }


def _operation(modality: Modality) -> tuple[str, str]:
    if modality is Modality.EMBEDDING:
        return "embeddings", "/embeddings"
    if modality is Modality.RERANK:
        return "rerank", "/rerank"
    if modality in {
        Modality.CHAT,
        Modality.COMPLETION,
        Modality.CODE,
        Modality.VISION_LANGUAGE,
        Modality.GUARDRAIL,
    }:
        return "chat-completions", "/chat/completions"
    raise PolicyViolation("Audio/unknown campaign provider endpoint'i yok")


def _operation_endpoint(embedding_endpoint: str, suffix: str) -> tuple[str, str]:
    parsed = urlsplit(embedding_endpoint)
    if not parsed.path.endswith("/embeddings"):
        raise ConfigurationError("OpenCode embedding endpoint base path'i bulunamadi")
    base_path = parsed.path[: -len("/embeddings")]
    path_hint = f"{base_path}{suffix}" or suffix
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, path_hint, "", ""))
    return endpoint, path_hint


def _provider_payload(
    modality: Modality,
    *,
    backend_model: str,
    artifact: RemoteFixtureArtifact,
) -> Mapping[str, Any]:
    payload = artifact.payload
    if modality is Modality.CHAT:
        return {"model": backend_model, **dict(payload), "temperature": 0}
    if modality is Modality.CODE:
        return openai_chat_payload(
            backend_model,
            str(payload["instruction"]),
            system="Yalniz calisabilir Python kodu ve istenen assert'i ver.",
        )
    if modality is Modality.COMPLETION:
        return openai_chat_payload(
            backend_model,
            str(payload["prompt"]),
            system="Yalniz istenen completion metnini ver.",
        )
    if modality is Modality.EMBEDDING:
        return openai_embedding_payload(
            backend_model, tuple(str(item) for item in payload["input"])
        )
    if modality is Modality.RERANK:
        return openai_rerank_payload(
            backend_model,
            str(payload["query"]),
            tuple(str(item) for item in payload["documents"]),
        )
    if modality is Modality.VISION_LANGUAGE:
        return openai_vision_payload(
            backend_model,
            str(payload["question"]),
            generated_vl_fixture_png(),
            media_type="image/png",
        )
    if modality is Modality.GUARDRAIL:
        return openai_guardrail_payload(
            backend_model, tuple(str(item) for item in payload["samples"])
        )
    raise PolicyViolation("Audio/unknown campaign payload'i yok")


def normalize_provider_response(
    modality: Modality,
    raw_response: Mapping[str, Any],
    *,
    artifact: RemoteFixtureArtifact,
) -> Mapping[str, Any]:
    """OpenAI-compatible raw response'u provider-neutral evaluator sekline cevirir."""

    if modality is Modality.CHAT:
        import json

        text = openai_chat_text(raw_response)
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = None
        return {"json": body}
    if modality is Modality.CODE:
        return {"code": openai_chat_text(raw_response)}
    if modality is Modality.COMPLETION:
        return {"text": openai_chat_text(raw_response)}
    if modality is Modality.EMBEDDING:
        return {"vectors": [list(row) for row in openai_embeddings(raw_response)]}
    if modality is Modality.RERANK:
        return {"scores": list(openai_rerank_scores(raw_response))}
    if modality is Modality.VISION_LANGUAGE:
        return {
            "answer": openai_chat_text(raw_response),
            "objects": list(openai_vision_objects(raw_response)),
        }
    if modality is Modality.GUARDRAIL:
        count = len(artifact.payload["samples"])
        return {"labels": list(openai_guardrail_labels(raw_response, expected_count=count))}
    raise PolicyViolation("Audio/unknown campaign response'u normalize edilemez")


def prepare_campaign_manifest(
    discovery: CampaignDiscovery,
    *,
    config_file: Path | None = None,
    registry: FixtureRegistry | None = None,
    inventory: InventorySnapshot | None = None,
) -> PreparedCampaignManifest:
    """Exact 1 health + fixture x repetition provider call manifestini kurar."""

    snapshot = inventory or load_inventory()
    fixtures = registry or load_fixture_registry()
    config_path = config_file or default_opencode_config_file()
    embedding_configuration = load_opencode_embedding_configuration(
        config_path,
        provider_id=discovery.scope.provider_id,
        selected_model_id="openai/BAAI/bge-m3",
        inventory=snapshot,
    )
    if (
        embedding_configuration.endpoint_identity.identity_digest
        != discovery.catalog.endpoint_identity_digest
    ):
        raise PolicyViolation("Campaign endpoint identity discovery sonrasi drift")
    fixtures_by_digest = {item.fixture_digest: item for item in fixtures.fixtures}
    fixture_root = default_fixture_file().parent.resolve(strict=True)
    calls: list[PreparedCampaignCall] = []
    endpoints: dict[tuple[str, str], str] = {}
    for target in discovery.targets:
        if target.excluded_reason is not None:
            continue
        record = snapshot.by_id(target.canonical_model_id)
        if record is None or record.inventory_digest != target.inventory_digest:
            raise PolicyViolation("Campaign inventory target drift")
        fixture = fixtures_by_digest[target.fixture_digests[0]]
        resolve_fixture_artifact(fixture, allow_root=fixture_root)
        artifact = load_remote_fixture(fixture, allow_root=fixture_root)
        operation, suffix = _operation(target.modality)
        endpoint, path_hint = _operation_endpoint(
            embedding_configuration.embedding_endpoint, suffix
        )
        endpoint_ref = f"opencode:{discovery.scope.provider_id}:{operation}"
        endpoints[(endpoint_ref, operation)] = endpoint
        endpoint_binding_digest = reviewed_endpoint_digest(endpoint, path_hint=path_hint)
        provider_payload = _provider_payload(
            target.modality,
            backend_model=record.backend_model,
            artifact=artifact,
        )
        fixture_payload_digest = digest(dict(artifact.payload))
        specifications = [(CampaignCallKind.HEALTH, 0)] + [
            (CampaignCallKind.BENCHMARK, repetition)
            for repetition in range(1, discovery.scope.repetitions + 1)
        ]
        for kind, repetition in specifications:
            call_id = (
                f"{kind.value}-{target.canonical_model_id}-"
                f"{fixture.fixture_digest[7:19]}-{repetition}"
            )
            provisional = ProviderCallPlan(
                call_id=call_id,
                modality=target.modality,
                model_id=target.canonical_model_id,
                provider_ref=discovery.scope.provider_id,
                endpoint_ref=endpoint_ref,
                operation=operation,
                secret_ref_name=BENCHMARK_SECRET_REF_NAME,
                request_format="json",
                fixture_digest=fixture.fixture_digest,
                payload_digest=digest(dict(provider_payload)),
                endpoint_binding_digest=endpoint_binding_digest,
                endpoint_path_hint=path_hint,
            )
            provider_call = ProviderCall(
                provider_ref=provisional.provider_ref,
                endpoint_ref=provisional.endpoint_ref,
                operation=provisional.operation,
                request_identity=provisional.call_id,
                payload=provider_payload,
                data_categories=(DataClassification.PUBLIC,),
                retention_assumption="public-benchmark-no-retention",
                endpoint_path_hint=path_hint,
                endpoint_binding_digest=endpoint_binding_digest,
                authorization_plan_digest=provisional.authorization_plan_digest,
                authorization_resource=provisional.call_resource,
            )
            if provider_call.payload_digest != provisional.payload_digest:
                raise PolicyViolation("Campaign provider payload plan drift")
            calls.append(
                PreparedCampaignCall(
                    kind=kind,
                    canonical_model_id=target.canonical_model_id,
                    configured_model_id=target.configured_model_id,
                    modality=target.modality,
                    fixture_digest=fixture.fixture_digest,
                    repetition=repetition,
                    fixture_payload_digest=fixture_payload_digest,
                    prepared=PreparedProviderContractCall(provisional, provider_call),
                )
            )
    return PreparedCampaignManifest(
        discovery=discovery,
        calls=tuple(calls),
        credential_locator=embedding_configuration.credential_locator,
        endpoint_mapping=endpoints,
    )


def _exact_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"Campaign {label} object olmali")
    return value


def load_campaign_scope(path: Path | None = None) -> OpenCodeCampaignScope:
    target = path or default_scope_file()
    if not target.is_file():
        raise ConfigurationError("OpenCode benchmark scope bulunamadi")
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    root = _exact_mapping(document, label="scope")
    expected = {
        "schema",
        "version",
        "provider_id",
        "provider_family",
        "scope_policy",
        "repetitions",
        "verifier",
        "targets",
    }
    if set(root) != expected or root.get("schema") != SCOPE_SCHEMA:
        raise ConfigurationError("OpenCode benchmark scope exact schema gecersiz")
    raw_verifier = _exact_mapping(root["verifier"], label="verifier")
    if set(raw_verifier) != {"model_id", "execution_identity"}:
        raise ConfigurationError("Campaign verifier alanlari gecersiz")
    raw_targets = root["targets"]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigurationError("Campaign target listesi bos olamaz")
    targets: list[ScopeTarget] = []
    allowed = {
        "configured_model_id",
        "canonical_model_ids",
        "modality",
        "workload",
        "excluded_reason",
        "reviewed_duplicate_route",
        "reviewed_modality_conflict",
    }
    for raw in raw_targets:
        row = _exact_mapping(raw, label="target")
        if set(row) - allowed or not {
            "configured_model_id",
            "canonical_model_ids",
            "modality",
            "workload",
        } <= set(row):
            raise ConfigurationError("Campaign target alanlari gecersiz")
        canonical = row["canonical_model_ids"]
        if not isinstance(canonical, list) or any(not isinstance(item, str) for item in canonical):
            raise ConfigurationError("Campaign canonical model listesi gecersiz")
        targets.append(
            ScopeTarget(
                configured_model_id=str(row["configured_model_id"]),
                canonical_model_ids=tuple(canonical),
                modality=Modality(str(row["modality"])),
                workload=str(row["workload"]),
                excluded_reason=(
                    None if row.get("excluded_reason") is None else str(row["excluded_reason"])
                ),
                reviewed_duplicate_route=bool(row.get("reviewed_duplicate_route", False)),
                reviewed_modality_conflict=bool(row.get("reviewed_modality_conflict", False)),
            )
        )
    return OpenCodeCampaignScope(
        version=int(root["version"]),
        provider_id=str(root["provider_id"]),
        provider_family=str(root["provider_family"]),
        scope_policy=str(root["scope_policy"]),
        repetitions=int(root["repetitions"]),
        verifier=ScopeVerifier(
            model_id=str(raw_verifier["model_id"]),
            execution_identity=str(raw_verifier["execution_identity"]),
        ),
        targets=tuple(targets),
    )


def _validate_target_record(target: ScopeTarget, record: ModelRecord) -> None:
    if (
        record.access_name != target.configured_model_id
        and record.backend_model != target.configured_model_id
    ):
        raise PolicyViolation("Campaign configured/canonical model route mismatch")
    if record.modality is not target.modality:
        raise PolicyViolation("Campaign canonical modality mismatch")
    if record.modality_conflict is not None and not target.reviewed_modality_conflict:
        raise PolicyViolation("Campaign modality conflict explicit review ister")
    if not record.enabled:
        raise PolicyViolation("Campaign disabled canonical model tasiyamaz")


def discover_campaign(
    *,
    config_file: Path | None = None,
    scope_file: Path | None = None,
    inventory: InventorySnapshot | None = None,
    registry: FixtureRegistry | None = None,
    verifier_provenance_digest: str,
) -> CampaignDiscovery:
    snapshot = inventory or load_inventory()
    fixtures = registry or load_fixture_registry()
    scope = load_campaign_scope(scope_file)
    catalog = load_opencode_aihub_catalog(
        config_file or default_opencode_config_file(), provider_id=scope.provider_id
    )
    if set(catalog.configured_model_ids) != {item.configured_model_id for item in scope.targets}:
        raise PolicyViolation("OpenCode catalog ve reviewed campaign scope drift")
    discovered: list[DiscoveredCampaignTarget] = []
    for target in scope.targets:
        for canonical_model_id in target.canonical_model_ids:
            record = snapshot.by_id(canonical_model_id)
            if record is None:
                raise PolicyViolation("Campaign canonical Model ID inventory'de yok")
            _validate_target_record(target, record)
            fixture_digests: tuple[str, ...] = ()
            if target.excluded_reason is None:
                fixture_digests = build_remote_suite(fixtures, target.modality).fixture_digests
            discovered.append(
                DiscoveredCampaignTarget(
                    configured_model_id=target.configured_model_id,
                    canonical_model_id=canonical_model_id,
                    modality=target.modality,
                    workload=target.workload,
                    inventory_digest=record.inventory_digest,
                    fixture_digests=fixture_digests,
                    excluded_reason=target.excluded_reason,
                )
            )
    return CampaignDiscovery(
        scope=scope,
        catalog=catalog,
        inventory_digest=snapshot.snapshot_digest,
        fixture_registry_digest=fixtures.registry_digest,
        verifier_provenance_digest=verifier_provenance_digest,
        targets=tuple(discovered),
    )
