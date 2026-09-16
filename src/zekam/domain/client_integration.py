"""Typed, authority-free policy for native CLI integrations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed


class ClientIntegrationId(StrEnum):
    """Stable adapter identities exposed by the current package."""

    OPENCODE = "opencode"
    CODEX = "codex"
    CLAUDE_CODE = "claude-code"


SUPPORTED_CLIENT_INTEGRATIONS: Final = tuple(ClientIntegrationId)


@dataclass(frozen=True, slots=True)
class ClientIntegrationPolicy:
    """One canonical integration decision; executable presence is separate."""

    opencode: bool = True
    codex: bool = False
    claude_code: bool = False

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in self.body().values()):
            raise ConfigurationError("CLI integration degerleri gercek boolean olmali")

    @classmethod
    def from_mapping(cls, value: object) -> ClientIntegrationPolicy:
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ConfigurationError("cli.integrations mapping olmali")
        expected = {item.value for item in SUPPORTED_CLIENT_INTEGRATIONS}
        unknown = set(value) - expected
        if unknown:
            raise ConfigurationError(
                "Desteklenmeyen CLI integration alani: " + ", ".join(sorted(map(str, unknown)))
            )
        for key, enabled in value.items():
            if type(enabled) is not bool:
                raise ConfigurationError(f"cli.integrations.{key} gercek boolean olmali")
        return cls(
            opencode=value.get(ClientIntegrationId.OPENCODE.value, True),
            codex=value.get(ClientIntegrationId.CODEX.value, False),
            claude_code=value.get(ClientIntegrationId.CLAUDE_CODE.value, False),
        )

    def enabled(self, client: ClientIntegrationId | str) -> bool:
        try:
            identity = ClientIntegrationId(client)
        except ValueError as exc:
            raise ValidationFailed("Desteklenmeyen CLI integration kimligi") from exc
        return {
            ClientIntegrationId.OPENCODE: self.opencode,
            ClientIntegrationId.CODEX: self.codex,
            ClientIntegrationId.CLAUDE_CODE: self.claude_code,
        }[identity]

    def with_change(
        self, *, enable: tuple[str, ...] = (), disable: tuple[str, ...] = ()
    ) -> ClientIntegrationPolicy:
        overlap = set(enable) & set(disable)
        if overlap:
            raise PolicyViolation("Ayni istemci birlikte enable ve disable edilemez")
        values = self.body()
        changes = tuple((item, True) for item in enable) + tuple(
            (item, False) for item in disable
        )
        for requested, state in changes:
            try:
                identity = ClientIntegrationId(requested)
            except ValueError as exc:
                raise ValidationFailed("Desteklenmeyen CLI integration kimligi") from exc
            values[identity.value] = state
        return ClientIntegrationPolicy.from_mapping(values)

    def body(self) -> dict[str, bool]:
        return {
            ClientIntegrationId.OPENCODE.value: self.opencode,
            ClientIntegrationId.CODEX.value: self.codex,
            ClientIntegrationId.CLAUDE_CODE.value: self.claude_code,
        }

    @property
    def policy_digest(self) -> str:
        return digest({"schema": "zekam-cli-integration-policy/v1", "integrations": self.body()})


@dataclass(frozen=True, slots=True)
class ClientIntegrationState:
    """Read-only distinction between policy, package support and native state."""

    client: ClientIntegrationId
    enabled: bool
    supported_capabilities: tuple[str, ...]
    executable_present: bool
    managed_artifacts_present: bool
    cleanup_required: bool
    conflicts: tuple[str, ...] = ()

    @property
    def effective_state(self) -> str:
        if self.conflicts:
            return "blocked-conflict"
        if self.enabled:
            return "enabled-ready" if self.executable_present else "enabled-not-installed"
        return "disabled-cleanup-required" if self.cleanup_required else "disabled-clean"

    def as_dict(self) -> dict[str, Any]:
        return {
            "client": self.client.value,
            "enabled": self.enabled,
            "supported_capabilities": list(self.supported_capabilities),
            "executable_present": self.executable_present,
            "managed_artifacts_present": self.managed_artifacts_present,
            "cleanup_required": self.cleanup_required,
            "conflicts": list(self.conflicts),
            "effective_state": self.effective_state,
        }
