"""OpenCode embedding provider yapilandirmasini guvenli bicimde kesfeder.

Bu modul ag cagrisi yapmaz. OpenCode JSON dosyasindan yalniz secilen provider'in
endpoint/model metadata'sini ve environment credential locator'ini okur. Credential
degeri dosyadan kabul edilmez; ``SecretStore`` uyumlu adapter ile process belleginde
cozulur. Gercek HTTP ve redirect deny davranisi mevcut Zekam provider transportuna aittir.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from zekam.application.environment import environment_value
from zekam.application.provider_adapter import (
    ProviderCall,
    openai_embedding_payload,
    openai_embeddings,
    reviewed_endpoint_digest,
    validated_provider_endpoint,
)
from zekam.application.provider_contract_execution import (
    PreparedProviderContractCall,
    ProviderCallPlan,
    ProviderExecutionManifest,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, NotFound, PolicyViolation, ValidationFailed
from zekam.domain.model_inventory import HealthState, InventorySnapshot, Modality, ModelRecord
from zekam.domain.security import DataClassification, SecretBackend, SecretRef, SecretValue

MAX_OPENCODE_CONFIG_BYTES = 256 * 1024
_ENV_PLACEHOLDER = re.compile(r"^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")
_PROVIDER_FIELDS = frozenset({"npm", "name", "options", "models"})
_OPTION_FIELDS = frozenset({"baseURL", "apiKey", "timeout", "chunkTimeout"})
_MODEL_FIELDS = frozenset({"name"})

OPENCODE_EMBEDDING_SECRET_REF_NAME = "opencode-litellm-embedding"
AIHUB_PROVIDER_HOST = "aihub-api.turktelekom.com.tr"
_HEALTH_PASSED_STATES = frozenset(
    {
        HealthState.HEALTH_PASSED,
        HealthState.CONTRACT_PASSED,
        HealthState.BENCHMARK_ELIGIBLE,
        HealthState.PROJECT_QUALIFIED,
        HealthState.ACTIVE_CANDIDATE,
    }
)
SYNTHETIC_EMBEDDING_FIXTURE = (
    "A product version service validates active services.",
    "A product version service validates active services.",
    "The service checks whether a product release is active.",
    "A recipe explains how to bake chocolate cake.",
    "GPU Fusion urun surumu servisi aktif servisleri dogrular.",
    "Cikolatali kek pisirme tarifi.",
)


def default_opencode_config_file() -> Path:
    return Path.home() / ".config" / "opencode" / "opencode.json"


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & 0x400)


def _secure_json_document(path: Path, *, max_bytes: int) -> Mapping[str, Any]:
    if max_bytes < 1:
        raise ValidationFailed("OpenCode config boyut limiti pozitif olmali")
    if not path.is_absolute():
        raise ConfigurationError("OpenCode config yolu absolute olmali")
    try:
        candidate = path.resolve(strict=True)
    except OSError:
        raise ConfigurationError("OpenCode config dosyasi bulunamadi") from None
    if _is_link_or_reparse(path) or _is_link_or_reparse(candidate):
        raise ConfigurationError("OpenCode config link/reparse olamaz")
    current = candidate.parent
    anchor = Path(candidate.anchor)
    while True:
        if _is_link_or_reparse(current):
            raise ConfigurationError("OpenCode config parent link/reparse olamaz")
        if current == anchor:
            break
        current = current.parent
    stat = candidate.stat()
    if stat.st_size < 2 or stat.st_size > max_bytes:
        raise ConfigurationError("OpenCode config dosya boyutu gecersiz")
    raw = candidate.read_bytes()
    if len(raw) != stat.st_size:
        raise ConfigurationError("OpenCode config okuma sirasinda degisti")

    def exact_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ConfigurationError("OpenCode config duplicate JSON key tasiyor")
            result[key] = value
        return result

    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=exact_object)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigurationError("OpenCode config gecerli UTF-8 JSON degil") from None
    if not isinstance(document, dict):
        raise ConfigurationError("OpenCode config JSON object olmali")
    return document


@dataclass(frozen=True, slots=True)
class EndpointIdentity:
    """Credential icermeyen normalize provider endpoint kimligi."""

    scheme: str
    host: str
    port: int
    base_path: str
    identity_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "base_path": self.base_path,
            "identity_digest": self.identity_digest,
        }


def _endpoint(value: str) -> tuple[EndpointIdentity, str]:
    if not value or value != value.strip():
        raise ConfigurationError("OpenCode provider baseURL gecersiz")
    validated_provider_endpoint(value)
    parsed = urlsplit(value)
    try:
        explicit_port = parsed.port
    except ValueError:
        raise ConfigurationError("OpenCode provider endpoint portu gecersiz") from None
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold()
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    if any(segment in {".", ".."} for segment in parsed.path.split("/")):
        raise ConfigurationError("OpenCode provider endpoint path'i normalize degil")
    base_path = parsed.path.rstrip("/") or "/"
    identity = EndpointIdentity(
        scheme=scheme,
        host=host,
        port=port,
        base_path=base_path,
        identity_digest=digest(
            {"scheme": scheme, "host": host, "port": port, "base_path": base_path}
        ),
    )
    display_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    netloc = display_host if port == default_port else f"{display_host}:{port}"
    embedding_path = "/embeddings" if base_path == "/" else f"{base_path}/embeddings"
    return identity, urlunsplit((scheme, netloc, embedding_path, "", ""))


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"OpenCode {label} JSON object olmali")
    return value


def _strict_fields(document: Mapping[str, Any], allowed: frozenset[str], *, label: str) -> None:
    if set(document) - allowed:
        raise ConfigurationError(f"OpenCode {label} bilinmeyen alan tasiyor")


def _optional_positive_integer(document: Mapping[str, Any], field_name: str) -> None:
    value = document.get(field_name)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
        raise ConfigurationError(f"OpenCode provider {field_name} pozitif integer olmali")


def _canonical_model(inventory: InventorySnapshot, configured_id: str) -> ModelRecord | None:
    exact_access = tuple(
        record for record in inventory.records if record.access_name == configured_id
    )
    matches = exact_access or tuple(
        record for record in inventory.records if record.backend_model == configured_id
    )
    if len(matches) > 1:
        raise ValidationFailed("OpenCode model kimligi kanonik envanterde belirsiz")
    return matches[0] if matches else None


@dataclass(frozen=True, slots=True)
class OpenCodeModelCatalog:
    """OpenCode provider/model metadata'sinin secret-safe kanonik gorunumu."""

    provider_id: str
    configured_model_ids: tuple[str, ...]
    endpoint_identity_digest: str
    provider_family: str
    credential_source: str = "environment"

    def sanitized(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "provider_family": self.provider_family,
            "provider_family_verified": self.provider_family == "aihub",
            "configured_model_count": len(self.configured_model_ids),
            "configured_model_ids": list(self.configured_model_ids),
            "endpoint_identity_digest": self.endpoint_identity_digest,
            "credential_source": self.credential_source,
        }


def load_opencode_aihub_catalog(
    path: Path,
    *,
    provider_id: str,
    max_bytes: int = MAX_OPENCODE_CONFIG_BYTES,
) -> OpenCodeModelCatalog:
    """OpenCode provider'ini exact AIHub ailesine baglayarak fail-closed yukler.

    Endpoint ve credential locator dondurulmez. Yalniz endpoint identity digest'i,
    provider family sonucu ve model kimlikleri raporlanabilir.
    """

    if not provider_id or provider_id != provider_id.strip():
        raise ConfigurationError("OpenCode provider id gecersiz")
    document = _secure_json_document(path, max_bytes=max_bytes)
    providers = _mapping(document.get("provider"), label="provider")
    provider = _mapping(providers.get(provider_id), label="selected provider")
    _strict_fields(provider, _PROVIDER_FIELDS, label="selected provider")
    enabled = document.get("enabled_providers")
    if enabled is not None and (
        not isinstance(enabled, list)
        or any(not isinstance(item, str) or not item for item in enabled)
        or provider_id not in enabled
    ):
        raise ConfigurationError("OpenCode selected provider enabled degil")
    for field_name in ("npm", "name"):
        value = provider.get(field_name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigurationError(f"OpenCode provider {field_name} metin olmali")
    options = _mapping(provider.get("options"), label="provider options")
    _strict_fields(options, _OPTION_FIELDS, label="provider options")
    _optional_positive_integer(options, "timeout")
    _optional_positive_integer(options, "chunkTimeout")
    base_url = options.get("baseURL")
    api_key = options.get("apiKey")
    if not isinstance(base_url, str) or not isinstance(api_key, str):
        raise ConfigurationError("OpenCode provider baseURL/apiKey metin olmali")
    if _ENV_PLACEHOLDER.fullmatch(api_key) is None:
        raise ConfigurationError("OpenCode apiKey exact environment locator olmali")
    endpoint_identity, _ = _endpoint(base_url)
    if endpoint_identity.host != AIHUB_PROVIDER_HOST:
        raise PolicyViolation("OpenCode provider exact AIHub ailesine bagli degil")
    models = _mapping(provider.get("models"), label="provider models")
    if not models:
        raise ConfigurationError("OpenCode selected provider model tasimali")
    model_ids: list[str] = []
    for model_id, raw_model in models.items():
        if not isinstance(model_id, str) or not model_id.strip():
            raise ConfigurationError("OpenCode provider model id gecersiz")
        model = _mapping(raw_model, label="provider model")
        _strict_fields(model, _MODEL_FIELDS, label="provider model")
        name = model.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ConfigurationError("OpenCode provider model name gecersiz")
        model_ids.append(model_id)
    return OpenCodeModelCatalog(
        provider_id=provider_id,
        configured_model_ids=tuple(sorted(model_ids)),
        endpoint_identity_digest=endpoint_identity.identity_digest,
        provider_family="aihub",
    )


@dataclass(frozen=True, slots=True)
class OpenCodeModelEligibility:
    """Configured/canonical/health kesitinden turetilen fail-closed durum."""

    configured_model_id: str
    canonical_model_id: str | None
    canonical_present: bool
    configured: bool
    declared_enabled: bool
    health_passed: bool
    active: bool
    enabled: bool
    benchmark_eligible: bool
    reasons: tuple[str, ...]

    def sanitized(self) -> dict[str, object]:
        return {
            "configured_model_id": self.configured_model_id,
            "canonical_model_id": self.canonical_model_id,
            "canonical_present": self.canonical_present,
            "configured": self.configured,
            "declared_enabled": self.declared_enabled,
            "health_passed": self.health_passed,
            "active": self.active,
            "enabled": self.enabled,
            "benchmark_eligible": self.benchmark_eligible,
            "reasons": list(self.reasons),
        }


def evaluate_opencode_aihub_models(
    catalog: OpenCodeModelCatalog,
    inventory: InventorySnapshot,
    *,
    fresh_benchmark_eligible_ids: Sequence[str] | None = None,
) -> tuple[OpenCodeModelEligibility, ...]:
    """OpenCode ve inventory kesitini endpoint/credential olmadan degerlendirir.

    ``fresh_benchmark_eligible_ids`` DB-backed ``ModelHealthService`` sonucundan
    verildiginde staleness de kapinin parcasi olur. Verilmezse kanonik kaydin health
    state'i kullanilir; untested/failed/quarantined kayitlar yine fail-closed kalir.
    """

    if catalog.provider_family != "aihub":
        raise PolicyViolation("OpenCode provider family dogrulanmadi")
    fresh = (
        None if fresh_benchmark_eligible_ids is None else frozenset(fresh_benchmark_eligible_ids)
    )
    configured_to_record: dict[str, ModelRecord | None] = {}
    ambiguous: set[str] = set()
    for configured_id in catalog.configured_model_ids:
        try:
            configured_to_record[configured_id] = _canonical_model(inventory, configured_id)
        except ValidationFailed:
            configured_to_record[configured_id] = None
            ambiguous.add(configured_id)
    configured_by_canonical = {
        record.model_id: configured_id
        for configured_id, record in configured_to_record.items()
        if record is not None
    }
    results: list[OpenCodeModelEligibility] = []
    for record in inventory.records:
        bound_configured_id = configured_by_canonical.get(record.model_id)
        configured = bound_configured_id is not None
        health_passed = (
            record.model_id in fresh
            if fresh is not None
            else record.health_state in _HEALTH_PASSED_STATES and not record.is_quarantined()
        )
        reasons: list[str] = []
        if not configured:
            reasons.append("not-configured-in-opencode")
        if not record.enabled:
            reasons.append("disabled-in-canonical-inventory")
        if record.modality_conflict is not None:
            reasons.append("canonical-modality-conflict")
        if not health_passed:
            reasons.append("health-not-passed-or-stale")
        eligible = not reasons
        results.append(
            OpenCodeModelEligibility(
                configured_model_id=bound_configured_id or record.access_name,
                canonical_model_id=record.model_id,
                canonical_present=True,
                configured=configured,
                declared_enabled=record.enabled,
                health_passed=health_passed,
                active=eligible,
                enabled=eligible,
                benchmark_eligible=eligible,
                reasons=tuple(reasons),
            )
        )
    for configured_id, matched_record in configured_to_record.items():
        if matched_record is not None:
            continue
        reason = (
            "canonical-model-ambiguous" if configured_id in ambiguous else "canonical-model-missing"
        )
        results.append(
            OpenCodeModelEligibility(
                configured_model_id=configured_id,
                canonical_model_id=None,
                canonical_present=False,
                configured=True,
                declared_enabled=False,
                health_passed=False,
                active=False,
                enabled=False,
                benchmark_eligible=False,
                reasons=(reason,),
            )
        )
    return tuple(
        sorted(
            results,
            key=lambda item: (item.configured_model_id, item.canonical_model_id or ""),
        )
    )


@dataclass(frozen=True, slots=True)
class OpenCodeEmbeddingConfiguration:
    provider_id: str
    endpoint_identity: EndpointIdentity
    model_ids: tuple[str, ...]
    selected_model_id: str
    canonical_model_id: str
    credential_locator: str
    _embedding_endpoint: str = field(repr=False)

    @property
    def embedding_endpoint(self) -> str:
        """Mevcut Zekam transportuna verilecek, normalize exact operation endpoint'i."""
        return self._embedding_endpoint

    def sanitized(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "endpoint_identity": self.endpoint_identity.as_dict(),
            "model_ids": list(self.model_ids),
            "selected_model_id": self.selected_model_id,
            "canonical_model_id": self.canonical_model_id,
            "credential_source": "environment",
        }


def load_opencode_embedding_configuration(
    path: Path,
    *,
    provider_id: str,
    selected_model_id: str,
    inventory: InventorySnapshot,
    max_bytes: int = MAX_OPENCODE_CONFIG_BYTES,
) -> OpenCodeEmbeddingConfiguration:
    """Secilen OpenCode provider/model binding'ini fail-closed yukler."""

    if not provider_id or provider_id != provider_id.strip():
        raise ConfigurationError("OpenCode provider id gecersiz")
    if not selected_model_id or selected_model_id != selected_model_id.strip():
        raise ConfigurationError("OpenCode selected model id gecersiz")
    document = _secure_json_document(path, max_bytes=max_bytes)
    providers = _mapping(document.get("provider"), label="provider")
    provider = _mapping(providers.get(provider_id), label="selected provider")
    _strict_fields(provider, _PROVIDER_FIELDS, label="selected provider")
    enabled = document.get("enabled_providers")
    if enabled is not None and (
        not isinstance(enabled, list)
        or any(not isinstance(item, str) or not item for item in enabled)
        or provider_id not in enabled
    ):
        raise ConfigurationError("OpenCode selected provider enabled degil")
    for field_name in ("npm", "name"):
        value = provider.get(field_name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigurationError(f"OpenCode provider {field_name} metin olmali")
    options = _mapping(provider.get("options"), label="provider options")
    _strict_fields(options, _OPTION_FIELDS, label="provider options")
    _optional_positive_integer(options, "timeout")
    _optional_positive_integer(options, "chunkTimeout")
    base_url = options.get("baseURL")
    api_key = options.get("apiKey")
    if not isinstance(base_url, str) or not isinstance(api_key, str):
        raise ConfigurationError("OpenCode provider baseURL/apiKey metin olmali")
    locator_match = _ENV_PLACEHOLDER.fullmatch(api_key)
    if locator_match is None:
        raise ConfigurationError("OpenCode apiKey exact environment locator olmali")
    credential_locator = locator_match.group(1)
    endpoint_identity, embedding_endpoint = _endpoint(base_url)

    models = _mapping(provider.get("models"), label="provider models")
    if not models:
        raise ConfigurationError("OpenCode selected provider model tasimali")
    model_ids: list[str] = []
    for model_id, raw_model in models.items():
        if not isinstance(model_id, str) or not model_id.strip():
            raise ConfigurationError("OpenCode provider model id gecersiz")
        model = _mapping(raw_model, label="provider model")
        _strict_fields(model, _MODEL_FIELDS, label="provider model")
        name = model.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ConfigurationError("OpenCode provider model name gecersiz")
        model_ids.append(model_id)
    if selected_model_id not in model_ids:
        raise ConfigurationError("OpenCode selected model provider altinda bulunamadi")
    canonical = _canonical_model(inventory, selected_model_id)
    if canonical is None or canonical.modality is not Modality.EMBEDDING:
        raise ValidationFailed("OpenCode selected model embedding modalitesinde degil")
    return OpenCodeEmbeddingConfiguration(
        provider_id=provider_id,
        endpoint_identity=endpoint_identity,
        model_ids=tuple(sorted(model_ids)),
        selected_model_id=selected_model_id,
        canonical_model_id=canonical.model_id,
        credential_locator=credential_locator,
        _embedding_endpoint=embedding_endpoint,
    )


@dataclass(frozen=True, slots=True)
class OpenCodeCredentialStore:
    """OpenCode environment locator'ini exact SecretRef ile process belleginde cozer."""

    provider_id: str
    credential_locator: str
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ, repr=False)
    backend: SecretBackend = SecretBackend.ENVIRONMENT

    def resolve(self, reference: SecretRef) -> SecretValue:
        if reference.store_backend is not self.backend:
            raise PolicyViolation("OpenCode credential store backend eslesmiyor")
        if (
            reference.provider != self.provider_id
            or reference.store_locator != self.credential_locator
        ):
            raise PolicyViolation("OpenCode credential exact provider/locator eslesmiyor")
        raw = environment_value(self.environ, self.credential_locator)
        if raw is None or not raw.strip():
            raise NotFound("OpenCode credential degeri bulunamadi")
        return SecretValue(raw)


@dataclass(frozen=True, slots=True)
class OpenCodeEndpointResolver:
    """OpenCode'dan reviewed edilen tek embedding endpoint'ini cozer."""

    provider_id: str
    endpoint_ref: str
    endpoint: str = field(repr=False)

    def resolve(self, endpoint_ref: str, operation: str) -> str:
        if endpoint_ref != self.endpoint_ref or operation != "embeddings":
            raise PolicyViolation("OpenCode embedding endpoint exact binding disinda")
        return self.endpoint


def build_opencode_embedding_probe_manifest(
    configurations: Sequence[OpenCodeEmbeddingConfiguration],
) -> tuple[ProviderExecutionManifest, tuple[PreparedProviderContractCall, ...]]:
    """Uc adayi public sentetik batch ile exact runtime-bound plana baglar."""

    if not configurations:
        raise ValidationFailed("OpenCode embedding probe en az bir aday ister")
    provider_ids = {item.provider_id for item in configurations}
    endpoint_digests = {item.endpoint_identity.identity_digest for item in configurations}
    locators = {item.credential_locator for item in configurations}
    if len(provider_ids) != 1 or len(endpoint_digests) != 1 or len(locators) != 1:
        raise ValidationFailed("OpenCode embedding adaylari exact provider binding paylasmali")

    fixture_digest = digest(
        {
            "schema": "zekam-opencode-embedding-synthetic-fixture/v1",
            "texts": list(SYNTHETIC_EMBEDDING_FIXTURE),
            "data_classification": "public",
        }
    )
    calls: list[ProviderCallPlan] = []
    prepared: list[PreparedProviderContractCall] = []
    for index, configuration in enumerate(configurations, start=1):
        payload = openai_embedding_payload(
            configuration.selected_model_id, SYNTHETIC_EMBEDDING_FIXTURE
        )
        endpoint_path = urlsplit(configuration.embedding_endpoint).path
        endpoint_digest = reviewed_endpoint_digest(
            configuration.embedding_endpoint, path_hint=endpoint_path
        )
        call_id = f"opencode-embedding-probe-{index}"
        plan = ProviderCallPlan(
            call_id=call_id,
            modality=Modality.EMBEDDING,
            model_id=configuration.canonical_model_id,
            provider_ref=configuration.provider_id,
            endpoint_ref=f"opencode:{configuration.provider_id}:embeddings",
            operation="embeddings",
            secret_ref_name=OPENCODE_EMBEDDING_SECRET_REF_NAME,
            request_format="json",
            fixture_digest=fixture_digest,
            payload_digest=digest(dict(payload)),
            endpoint_binding_digest=endpoint_digest,
            endpoint_path_hint=endpoint_path,
        )
        call = ProviderCall(
            provider_ref=plan.provider_ref,
            endpoint_ref=plan.endpoint_ref,
            operation=plan.operation,
            request_identity=plan.call_id,
            payload=payload,
            data_categories=(DataClassification.PUBLIC,),
            retention_assumption="provider-contract-only",
            region="configured-provider",
            endpoint_path_hint=plan.endpoint_path_hint,
            endpoint_binding_digest=plan.endpoint_binding_digest,
            authorization_plan_digest=plan.authorization_plan_digest,
            authorization_resource=plan.call_resource,
        )
        calls.append(plan)
        prepared.append(PreparedProviderContractCall(plan=plan, call=call))
    manifest = ProviderExecutionManifest(
        binding_set_digest=digest(
            {
                "provider_id": configurations[0].provider_id,
                "endpoint_identity_digest": configurations[0].endpoint_identity.identity_digest,
                "models": [item.selected_model_id for item in configurations],
            }
        ),
        fixture_digest=fixture_digest,
        calls=tuple(calls),
    )
    return manifest, tuple(prepared)


@dataclass(frozen=True, slots=True)
class EmbeddingProbeMetrics:
    model_id: str
    canonical_model_id: str
    dimension: int
    duplicate_max_delta: float
    related_similarity: float
    unrelated_similarity: float
    semantic_margin: float
    latency_ms: int
    verified: bool
    evidence_digest: str

    def sanitized(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "canonical_model_id": self.canonical_model_id,
            "dimension": self.dimension,
            "duplicate_max_delta": self.duplicate_max_delta,
            "related_similarity": self.related_similarity,
            "unrelated_similarity": self.unrelated_similarity,
            "semantic_margin": self.semantic_margin,
            "latency_ms": self.latency_ms,
            "verified": self.verified,
            "evidence_digest": self.evidence_digest,
        }


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(item * item for item in left))
    right_norm = math.sqrt(sum(item * item for item in right))
    if left_norm == 0.0 or right_norm == 0.0:
        raise ValidationFailed("OpenCode embedding sifir norm uretmemeli")
    return dot / (left_norm * right_norm)


def evaluate_opencode_embedding_response(
    configuration: OpenCodeEmbeddingConfiguration,
    response: Mapping[str, Any],
    *,
    latency_ms: int,
) -> EmbeddingProbeMetrics:
    """Ham yaniti process belleginde dogrular ve yalniz metric/digest dondurur."""

    if latency_ms < 0:
        raise ValidationFailed("OpenCode embedding latency negatif olamaz")
    vectors = openai_embeddings(response)
    if len(vectors) != len(SYNTHETIC_EMBEDDING_FIXTURE):
        raise ValidationFailed("OpenCode embedding response exact fixture sayisini tasimali")
    dimension = len(vectors[0])
    if dimension < 1 or any(len(vector) != dimension for vector in vectors):
        raise ValidationFailed("OpenCode embedding boyutu tutarsiz")
    if any(not math.isfinite(value) for vector in vectors for value in vector):
        raise ValidationFailed("OpenCode embedding sonlu olmayan deger tasiyor")
    duplicate_delta = max(
        abs(left - right) for left, right in zip(vectors[0], vectors[1], strict=True)
    )
    related_similarity = _cosine(vectors[0], vectors[2])
    unrelated_similarity = _cosine(vectors[0], vectors[3])
    semantic_margin = related_similarity - unrelated_similarity
    verified = duplicate_delta <= 1e-6 and semantic_margin > 0.0
    evidence = {
        "schema": "zekam-opencode-embedding-probe-metrics/v1",
        "model_id": configuration.selected_model_id,
        "canonical_model_id": configuration.canonical_model_id,
        "dimension": dimension,
        "duplicate_max_delta": duplicate_delta,
        "related_similarity": related_similarity,
        "unrelated_similarity": unrelated_similarity,
        "semantic_margin": semantic_margin,
        "latency_ms": latency_ms,
        "verified": verified,
    }
    return EmbeddingProbeMetrics(
        model_id=configuration.selected_model_id,
        canonical_model_id=configuration.canonical_model_id,
        dimension=dimension,
        duplicate_max_delta=float(f"{duplicate_delta:.12g}"),
        related_similarity=float(f"{related_similarity:.12g}"),
        unrelated_similarity=float(f"{unrelated_similarity:.12g}"),
        semantic_margin=float(f"{semantic_margin:.12g}"),
        latency_ms=latency_ms,
        verified=verified,
        evidence_digest=digest(evidence),
    )


@dataclass(frozen=True, slots=True)
class EmbeddingCandidateEvaluation:
    canonical_model_id: str
    access_name: str
    configured: bool
    enabled: bool
    eligible: bool
    reasons: tuple[str, ...]

    def sanitized(self) -> dict[str, object]:
        return {
            "canonical_model_id": self.canonical_model_id,
            "access_name": self.access_name,
            "configured": self.configured,
            "enabled": self.enabled,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
        }


def evaluate_embedding_candidates(
    configured_model_ids: Sequence[str], inventory: InventorySnapshot
) -> tuple[EmbeddingCandidateEvaluation, ...]:
    """Kanonik embedding adaylarini endpoint/credential olmadan degerlendirir."""

    configured = frozenset(configured_model_ids)
    results: list[EmbeddingCandidateEvaluation] = []
    for record in inventory.records:
        if record.modality is not Modality.EMBEDDING:
            continue
        is_configured = record.access_name in configured or record.backend_model in configured
        reasons: list[str] = []
        if not is_configured:
            reasons.append("not-configured-in-opencode")
        if not record.enabled:
            reasons.append("disabled-in-canonical-inventory")
        if record.modality_conflict is not None:
            reasons.append("canonical-modality-conflict")
        if not record.is_benchmark_eligible():
            reasons.append("canonical-health-not-eligible")
        results.append(
            EmbeddingCandidateEvaluation(
                canonical_model_id=record.model_id,
                access_name=record.access_name,
                configured=is_configured,
                enabled=record.enabled,
                eligible=not reasons,
                reasons=tuple(reasons),
            )
        )
    return tuple(sorted(results, key=lambda item: item.access_name))
