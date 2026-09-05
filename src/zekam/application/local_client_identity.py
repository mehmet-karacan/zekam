"""Additive macOS native inventory pins, never lifecycle or model admission.

These are identities observed on the MacBook on 2026-09-02. Historical Windows
contracts remain separate. Neither a pinned binary nor its version output
proves command-hook semantics, delivery, checkpoint ACK, or model capability.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed

IDENTITY_SCHEMA = "zekam-local-client-inventory/v1"
MAC_PLATFORM = "darwin-arm64"
_CLIENT_IDS = frozenset({"codex", "claude-code", "opencode"})
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class MacNativeArtifactPin:
    client_id: str
    version: str
    native_sha256: str

    def __post_init__(self) -> None:
        if type(self.client_id) is not str or self.client_id not in _CLIENT_IDS:
            raise ValidationFailed("Inventory client must be exact and known")
        if type(self.version) is not str or not _VERSION.fullmatch(self.version):
            raise ValidationFailed("Inventory version must be exact semantic version")
        if type(self.native_sha256) is not str or not _SHA256.fullmatch(self.native_sha256):
            raise ValidationFailed("Inventory native SHA-256 must be canonical")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-macos-native-artifact-pin/v1",
            "client_id": self.client_id,
            "version": self.version,
            "platform": MAC_PLATFORM,
            "native_format": "mach-o-64-executable",
            "native_sha256": self.native_sha256,
            "purpose": "inventory-only",
            "wire_contract_reviewed": False,
            "lifecycle_proven": False,
            "grants_authority": False,
        }


MAC_NATIVE_ARTIFACT_PINS = (
    MacNativeArtifactPin(
        "codex", "0.151.0", "98491713ffb196061003ee148636e743997cc31d76144ba7c53462269896891d"
    ),
    MacNativeArtifactPin(
        "claude-code", "2.1.252", "b661c6a094fcc32656bf7c0071c5b45bf900b34d4f0a1ab3d78fd59aeba2c2c7"
    ),
    MacNativeArtifactPin(
        "opencode", "1.18.16", "a41776bf64c75786d6baf531b840ffb873c090d7c44793ae2dd4b1896de56a1f"
    ),
)


def mac_native_pin(client_id: str) -> MacNativeArtifactPin:
    if type(client_id) is not str:
        raise ValidationFailed("Inventory client must be exact text")
    for pin in MAC_NATIVE_ARTIFACT_PINS:
        if pin.client_id == client_id:
            return pin
    raise ValidationFailed("No observed Mac artifact pin for this client")


@dataclass(frozen=True, slots=True)
class NativeClientObservation:
    """Immutable observation; no constructor option can promote its authority."""

    pin: MacNativeArtifactPin
    observed_at: dt.datetime

    def __post_init__(self) -> None:
        if not isinstance(self.pin, MacNativeArtifactPin):
            raise ValidationFailed("Typed inventory pin required")
        self.pin.__post_init__()
        if self.pin != mac_native_pin(self.pin.client_id):
            raise ValidationFailed("Inventory pin is not an exact observed Mac artifact")
        if (
            not isinstance(self.observed_at, dt.datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValidationFailed("Inventory observation requires timezone-aware datetime")

    def body(self) -> dict[str, Any]:
        self.__post_init__()
        body = {
            "schema": IDENTITY_SCHEMA,
            "client_id": self.pin.client_id,
            "version": self.pin.version,
            "platform": MAC_PLATFORM,
            "native_sha256": self.pin.native_sha256,
            "artifact_pin_digest": digest(self.pin.body()),
            "observed_at": self.observed_at.astimezone(dt.UTC).isoformat(),
            "inventory_observed": True,
            "version_source": "exact-native-artifact-pin",
            "runtime_version_probe": "not-run",
            "runtime_version_observed": False,
            "wire_contract_reviewed": False,
            "lifecycle_proven": False,
            "model_capability_proven": False,
            "grants_authority": False,
            "hooks_activated": False,
            "external_provider_calls": 0,
            "subprocess_calls": 0,
            "live_user_config_written": False,
            "native_path_recorded": False,
        }
        return {**body, "evidence_digest": digest(body)}
