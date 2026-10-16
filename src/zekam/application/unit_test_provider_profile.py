"""Bounded, credential-free provider projections for explicit test batches."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from zekam.application.bounded_file import read_bounded_bytes
from zekam.application.provider_adapter import validated_provider_endpoint
from zekam.application.unit_test_models import validate_model_id
from zekam.application.unit_test_secret_scan import find_secret_value
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

PROFILE_SCHEMA = "zekam-test-provider-profile/v1"
MAX_PROFILE_BYTES = 16_384


@dataclass(frozen=True, slots=True)
class BatchProvider:
    provider_id: str
    endpoint: str
    network_scope: str
    models: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.provider_id):
            raise ValidationFailed("Batch custom provider id bounded literal olmali")
        if (
            not isinstance(self.endpoint, str)
            or len(self.endpoint) > 2048
            or any(char in self.endpoint for char in "{}\\")
            or any(
                ord(char) < 33 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                for char in self.endpoint
            )
        ):
            raise ValidationFailed("Batch provider endpoint bounded literal olmali")
        try:
            validated_provider_endpoint(self.endpoint)
            parsed = urlsplit(self.endpoint)
        except ValueError as exc:
            raise ValidationFailed("Batch provider endpoint URL gecersiz") from exc
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValidationFailed("Batch provider port gecersiz") from exc
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError:
            address = None
        if port == 0 or any(part in {".", ".."} for part in parsed.path.split("/")):
            raise ValidationFailed("Batch provider endpoint normalize degil")
        local = address is not None and address.is_loopback
        if self.network_scope not in {"loopback", "remote"}:
            raise ValidationFailed("Batch provider network scope gecersiz")
        if self.network_scope == "loopback" and not local:
            raise PolicyViolation(
                "Loopback profili literal loopback IP ister; hostname kabul edilmez"
            )
        if self.network_scope == "remote" and parsed.scheme != "https":
            raise PolicyViolation("Remote batch provider HTTPS ister")
        if not 1 <= len(self.models) <= 32 or len(dict(self.models)) != len(self.models):
            raise ValidationFailed("Batch provider model listesi bounded ve unique olmali")
        for alias, actual in self.models:
            for model in (alias, actual):
                validate_model_id(f"{self.provider_id}/{model}")
                if any(char in model for char in "{}#"):
                    raise ValidationFailed("Batch model alias/upstream literal olmali")
        if find_secret_value(self.body()) is not None:
            raise PolicyViolation("Batch provider profile secret degeri tasiyamaz")

    def body(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "endpoint": self.endpoint,
            "scope": self.network_scope,
            "models": dict(self.models),
        }


@dataclass(frozen=True, slots=True)
class BatchProviderProfile:
    providers: tuple[BatchProvider, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.providers) <= 8:
            raise ValidationFailed("Batch provider sayisi bounded olmali")
        if len({item.provider_id for item in self.providers}) != len(self.providers):
            raise ValidationFailed("Batch provider id tekrar edemez")

    @property
    def profile_digest(self) -> str:
        return digest(
            {"schema": PROFILE_SCHEMA, "providers": [item.body() for item in self.providers]}
        )

    def resolve(self, model: str) -> tuple[BatchProvider, str]:
        validate_model_id(model)
        provider_id, alias = model.split("/", 1)
        alias = alias.split("#", 1)[0]
        for item in self.providers:
            if item.provider_id == provider_id and alias in dict(item.models):
                return item, dict(item.models)[alias]
        raise PolicyViolation("Batch model reviewed provider profilinde yok")

    def validate_selection(self, builder: str, verifier: str) -> bool:
        builder_provider, builder_actual = self.resolve(builder)
        verifier_provider, verifier_actual = self.resolve(verifier)
        if builder_actual.casefold() == verifier_actual.casefold():
            raise PolicyViolation("Batch verifier alias'i ayni upstream modeli gizleyemez")
        return "remote" in {builder_provider.network_scope, verifier_provider.network_scope}

    def summary(self) -> dict[str, Any]:
        return {
            "profile_digest": self.profile_digest,
            "providers": [
                {
                    "id": item.provider_id,
                    "endpoint_digest": digest(item.endpoint),
                    "scope": item.network_scope,
                    "models": sorted(dict(item.models)),
                }
                for item in self.providers
            ],
        }


def load_batch_provider_profile(path: str | Path | None) -> BatchProviderProfile | None:
    if path is None:
        return None

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValidationFailed("Batch provider profile duplicate key")
            result[key] = value
        return result

    def reject_constant(_):
        raise ValidationFailed("Batch provider profile nonfinite JSON")

    try:
        document = json.loads(
            read_bounded_bytes(Path(path), max_bytes=MAX_PROFILE_BYTES).decode(
                "utf-8", errors="strict"
            ),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
        if not isinstance(document, dict) or set(document) != {"schema", "providers"}:
            raise ValidationFailed("Batch provider profile exact schema ister")
        if document["schema"] != PROFILE_SCHEMA or not isinstance(document["providers"], list):
            raise ValidationFailed("Batch provider profile schema gecersiz")
        providers = []
        for item in document["providers"]:
            if not isinstance(item, dict) or set(item) != {"id", "endpoint", "scope", "models"}:
                raise ValidationFailed("Batch provider profile exact alanlar ister")
            if not isinstance(item["models"], dict):
                raise ValidationFailed("Batch provider models object olmali")
            if not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in item["models"].items()
            ):
                raise ValidationFailed("Batch provider model alias/upstream metin olmali")
            if not all(isinstance(item[key], str) for key in ("id", "endpoint", "scope")):
                raise ValidationFailed("Batch provider identity metin olmali")
            providers.append(
                BatchProvider(
                    item["id"],
                    item["endpoint"],
                    item["scope"],
                    tuple(sorted(item["models"].items())),
                )
            )
        return BatchProviderProfile(tuple(sorted(providers, key=lambda item: item.provider_id)))
    except OSError as exc:
        raise ValidationFailed("Batch provider profile bounded regular dosya olmali") from exc
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ValidationFailed("Batch provider profile strict UTF-8 JSON olmali") from exc
