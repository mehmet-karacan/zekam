"""Model-provider binding ve agsiz dry-run yapilandirma dogrulamasi.

Binding dosyasi yalniz logical ref, relative path hint ve ortam degiskeni
locator adlari tasir. Endpoint/credential degerleri rapora, digest girdisine veya
veritabanina yazilmaz.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from zekam.application.config import core_root, package_root
from zekam.application.model_registry import load_inventory
from zekam.application.provider_adapter import reviewed_endpoint_digest
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.domain.model_inventory import InventorySnapshot, Modality, ProviderProtocol
from zekam.domain.security import SecretRef

BINDING_SCHEMA = "zekam-model-provider-bindings/v1"
_ENV_LOCATOR = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_PROVIDER_REF = re.compile(r"^model:[A-Za-z0-9._-]{1,128}$")

REQUIRED_PROVIDER_MODALITIES: tuple[Modality, ...] = (
    Modality.CHAT,
    Modality.CODE,
    Modality.AUDIO_TRANSCRIPTION,
    Modality.EMBEDDING,
    Modality.VISION_LANGUAGE,
    Modality.RERANK,
    Modality.GUARDRAIL,
)


class RequestFormat(StrEnum):
    JSON = "json"
    MULTIPART = "multipart"


def default_binding_file() -> Path:
    repository_copy = core_root() / "config" / "model_provider_bindings.yaml"
    if repository_copy.is_file():
        return repository_copy
    return package_root() / "_config" / "model_provider_bindings.yaml"


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    modality: Modality
    model_id: str
    access_name: str
    provider_protocol: ProviderProtocol
    provider_ref: str
    endpoint_ref: str
    endpoint_env: str
    credential_ref: str
    secret_ref_name: str
    credential_env: str
    operation: str
    request_format: RequestFormat
    path_hint: str

    def __post_init__(self) -> None:
        if self.modality not in REQUIRED_PROVIDER_MODALITIES:
            raise ValidationFailed(f"Provider binding modalitesi desteklenmiyor: {self.modality}")
        if not _PROVIDER_REF.fullmatch(self.provider_ref):
            raise ValidationFailed("Provider binding provider_ref exact model ref olmali")
        if self.provider_ref != f"model:{self.model_id}":
            raise ValidationFailed("Provider binding provider_ref model_id ile eslesmeli")
        for locator in (self.endpoint_env, self.credential_env):
            if not _ENV_LOCATOR.fullmatch(locator):
                raise ValidationFailed("Provider binding ortam locator adi gecersiz")
        if not re.fullmatch(r"^[a-z0-9]+([._-][a-z0-9]+)*$", self.secret_ref_name):
            raise ValidationFailed("Provider binding SecretRef adi gecersiz")
        path = PurePosixPath(self.path_hint)
        if (
            not self.path_hint.startswith("/")
            or ".." in path.parts
            or "?" in self.path_hint
            or "#" in self.path_hint
        ):
            raise ValidationFailed("Provider binding path_hint guvenli absolute URL path olmali")
        if not self.operation.strip() or not self.access_name.strip():
            raise ValidationFailed("Provider binding operation/access_name bos olamaz")
        if self.modality is Modality.AUDIO_TRANSCRIPTION:
            if self.request_format is not RequestFormat.MULTIPART:
                raise ValidationFailed("Audio transcription multipart binding ister")
        elif self.request_format is not RequestFormat.JSON:
            raise ValidationFailed("Yalniz audio transcription multipart olabilir")

    @property
    def binding_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, str]:
        return {
            "modality": self.modality.value,
            "model_id": self.model_id,
            "access_name": self.access_name,
            "provider_protocol": self.provider_protocol.value,
            "provider_ref": self.provider_ref,
            "endpoint_ref": self.endpoint_ref,
            "endpoint_env": self.endpoint_env,
            "credential_ref": self.credential_ref,
            "secret_ref_name": self.secret_ref_name,
            "credential_env": self.credential_env,
            "operation": self.operation,
            "request_format": self.request_format.value,
            "path_hint": self.path_hint,
        }


@dataclass(frozen=True, slots=True)
class ProviderBindingSet:
    bindings: tuple[ProviderBinding, ...]
    schema: str = BINDING_SCHEMA

    def __post_init__(self) -> None:
        modalities = tuple(item.modality for item in self.bindings)
        if len(modalities) != len(set(modalities)):
            raise ValidationFailed("Her provider modalitesi tam bir binding tasimali")
        missing = set(REQUIRED_PROVIDER_MODALITIES) - set(modalities)
        extra = set(modalities) - set(REQUIRED_PROVIDER_MODALITIES)
        if missing or extra:
            raise ValidationFailed(
                "Provider binding modalite kapsami exact olmali: "
                f"missing={sorted(item.value for item in missing)}, "
                f"extra={sorted(item.value for item in extra)}"
            )
        model_ids = tuple(item.model_id for item in self.bindings)
        if len(model_ids) != len(set(model_ids)):
            raise ValidationFailed("Secili provider model_id degerleri tekil olmali")

    @property
    def binding_set_digest(self) -> str:
        return digest(
            {
                "schema": self.schema,
                "bindings": [
                    item.as_dict() for item in sorted(self.bindings, key=lambda row: row.modality)
                ],
            }
        )

    def for_modality(self, modality: Modality) -> ProviderBinding:
        return next(item for item in self.bindings if item.modality is modality)


def _binding_from_mapping(document: Mapping[str, Any]) -> ProviderBinding:
    try:
        return ProviderBinding(
            modality=Modality(str(document["modality"])),
            model_id=str(document["model_id"]),
            access_name=str(document["access_name"]),
            provider_protocol=ProviderProtocol(str(document["provider_protocol"])),
            provider_ref=str(document["provider_ref"]),
            endpoint_ref=str(document["endpoint_ref"]),
            endpoint_env=str(document["endpoint_env"]),
            credential_ref=str(document["credential_ref"]),
            secret_ref_name=str(document["secret_ref_name"]),
            credential_env=str(document["credential_env"]),
            operation=str(document["operation"]),
            request_format=RequestFormat(str(document["request_format"])),
            path_hint=str(document["path_hint"]),
        )
    except (KeyError, ValueError) as exc:
        raise ValidationFailed("Provider binding kaydi gecersiz") from exc


def load_provider_bindings(path: Path | None = None) -> ProviderBindingSet:
    target = path or default_binding_file()
    if not target.is_file():
        raise ConfigurationError("Model provider binding dosyasi bulunamadi")
    try:
        document = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError("Model provider binding dosyasi okunamadi") from exc
    if not isinstance(document, dict) or document.get("schema") != BINDING_SCHEMA:
        raise ValidationFailed("Model provider binding semasi gecersiz")
    rows = document.get("bindings")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise ValidationFailed("Model provider binding listesi gecersiz")
    return ProviderBindingSet(tuple(_binding_from_mapping(row) for row in rows))


@dataclass(frozen=True, slots=True)
class ProviderBindingCheck:
    binding: ProviderBinding
    inventory_match: bool
    endpoint_value_present: bool
    endpoint_value_valid: bool
    endpoint_binding_digest: str | None
    credential_value_present: bool
    secret_ref_present: bool
    secret_ref_match: bool
    reasons: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.reasons

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.binding.as_dict(),
            "binding_digest": self.binding.binding_digest,
            "inventory_match": self.inventory_match,
            "endpoint_value_present": self.endpoint_value_present,
            "endpoint_value_valid": self.endpoint_value_valid,
            "endpoint_binding_digest": self.endpoint_binding_digest,
            "credential_value_present": self.credential_value_present,
            "secret_ref_present": self.secret_ref_present,
            "secret_ref_match": self.secret_ref_match,
            "ready": self.ready,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ProviderConfigurationReport:
    binding_set_digest: str
    checks: tuple[ProviderBindingCheck, ...]

    @property
    def ready(self) -> bool:
        return all(item.ready for item in self.checks)

    @property
    def ready_count(self) -> int:
        return sum(item.ready for item in self.checks)

    @property
    def report_digest(self) -> str:
        return digest(self.as_dict(include_digest=False))

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema": "zekam-provider-configuration-report/v1",
            "dry_run": True,
            "ready": self.ready,
            "binding_count": len(self.checks),
            "ready_count": self.ready_count,
            "binding_set_digest": self.binding_set_digest,
            "provider_calls_made": 0,
            "network_calls_made": 0,
            "secret_values_reported": 0,
            "checks": [item.as_dict() for item in self.checks],
        }
        if include_digest:
            document["report_digest"] = self.report_digest
        return document


def evaluate_provider_configuration(
    *,
    bindings: ProviderBindingSet | None = None,
    inventory: InventorySnapshot | None = None,
    secret_refs: Mapping[str, SecretRef] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProviderConfigurationReport:
    """Yapilandirmayi salt okunur dogrular; ag veya provider cagrisi yapmaz."""
    selected = bindings or load_provider_bindings()
    snapshot = inventory or load_inventory()
    references = dict(secret_refs or {})
    environment = os.environ if environ is None else environ
    records = {item.model_id: item for item in snapshot.records}
    checks: list[ProviderBindingCheck] = []
    for binding in selected.bindings:
        reasons: list[str] = []
        record = records.get(binding.model_id)
        inventory_match = bool(
            record is not None
            and record.access_name == binding.access_name
            and record.modality is binding.modality
            and record.provider_protocol is binding.provider_protocol
            and record.endpoint_ref == binding.endpoint_ref
            and record.credential_ref == binding.credential_ref
            and record.enabled
        )
        if not inventory_match:
            reasons.append("inventory-mismatch")

        endpoint_present = bool(environment.get(binding.endpoint_env))
        endpoint_valid = False
        endpoint_digest: str | None = None
        if not endpoint_present:
            reasons.append("endpoint-value-missing")
        else:
            try:
                endpoint_digest = reviewed_endpoint_digest(
                    environment[binding.endpoint_env], path_hint=binding.path_hint
                )
                endpoint_valid = True
            except ValidationFailed:
                reasons.append("endpoint-value-invalid")

        credential_present = bool(environment.get(binding.credential_env))
        if not credential_present:
            reasons.append("credential-value-missing")

        reference = references.get(binding.secret_ref_name)
        secret_ref_present = reference is not None
        if reference is None:
            reasons.append("secret-ref-missing")
            secret_ref_match = False
        else:
            secret_ref_match = bool(
                reference.provider == binding.provider_ref
                and reference.store_locator == binding.credential_env
                and reference.permits(binding.operation)
                and reference.is_usable()
            )
            if not secret_ref_match:
                reasons.append("secret-ref-mismatch")

        checks.append(
            ProviderBindingCheck(
                binding=binding,
                inventory_match=inventory_match,
                endpoint_value_present=endpoint_present,
                endpoint_value_valid=endpoint_valid,
                endpoint_binding_digest=endpoint_digest,
                credential_value_present=credential_present,
                secret_ref_present=secret_ref_present,
                secret_ref_match=secret_ref_match,
                reasons=tuple(reasons),
            )
        )
    return ProviderConfigurationReport(selected.binding_set_digest, tuple(checks))
