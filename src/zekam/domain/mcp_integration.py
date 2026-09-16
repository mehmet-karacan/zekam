"""Secret-free MCP registry contracts shared by native client adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from zekam.domain.canonical import digest
from zekam.domain.client_integration import ClientIntegrationId
from zekam.domain.errors import ConfigurationError, ValidationFailed

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class McpTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


@dataclass(frozen=True, slots=True)
class McpServerRegistration:
    """One portable, secret-free MCP server registration."""

    name: str
    transport: McpTransport
    command: tuple[str, ...] = ()
    url: str | None = None
    env_vars: tuple[str, ...] = ()
    bearer_token_env_var: str | None = None
    clients: tuple[ClientIntegrationId, ...] = tuple(ClientIntegrationId)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ConfigurationError("MCP server adi gecersiz")
        if type(self.enabled) is not bool:
            raise ConfigurationError("MCP enabled gercek boolean olmali")
        if len(set(self.env_vars)) != len(self.env_vars) or any(
            not _ENV.fullmatch(item) for item in self.env_vars
        ):
            raise ConfigurationError("MCP environment variable referansi gecersiz")
        if len(set(self.clients)) != len(self.clients) or not self.clients:
            raise ConfigurationError("MCP en az bir tekil istemci ister")
        if self.bearer_token_env_var is not None and not _ENV.fullmatch(self.bearer_token_env_var):
            raise ConfigurationError("MCP bearer token environment referansi gecersiz")
        if self.transport is McpTransport.STDIO:
            if not self.command or any(
                not isinstance(item, str) or not item or "\x00" in item for item in self.command
            ):
                raise ConfigurationError("STDIO MCP command ister")
            if self.url is not None or self.bearer_token_env_var is not None:
                raise ConfigurationError("STDIO MCP URL veya bearer token kabul etmez")
        else:
            if self.command:
                raise ConfigurationError("HTTP MCP command kabul etmez")
            if not isinstance(self.url, str):
                raise ConfigurationError("HTTP MCP URL ister")
            parsed = urlsplit(self.url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ConfigurationError("HTTP MCP URL gecersiz veya credential iceriyor")
            if self.bearer_token_env_var is not None and parsed.scheme != "https":
                raise ConfigurationError("Bearer token kullanan HTTP MCP HTTPS ister")

    @classmethod
    def from_mapping(cls, value: object) -> McpServerRegistration:
        if not isinstance(value, dict):
            raise ConfigurationError("MCP registry entry mapping olmali")
        allowed = {
            "name",
            "transport",
            "command",
            "url",
            "env_vars",
            "bearer_token_env_var",
            "clients",
            "enabled",
        }
        if set(value) - allowed:
            raise ConfigurationError("MCP registry entry desteklenmeyen alan iceriyor")
        try:
            clients = tuple(ClientIntegrationId(item) for item in value.get("clients", ()))
            return cls(
                name=str(value["name"]),
                transport=McpTransport(str(value["transport"])),
                command=tuple(value.get("command") or ()),
                url=value.get("url"),
                env_vars=tuple(value.get("env_vars") or ()),
                bearer_token_env_var=value.get("bearer_token_env_var"),
                clients=clients,
                enabled=value.get("enabled", True),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError("MCP registry entry gecersiz") from exc

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport.value,
            "command": list(self.command),
            "url": self.url,
            "env_vars": list(self.env_vars),
            "bearer_token_env_var": self.bearer_token_env_var,
            "clients": [item.value for item in self.clients],
            "enabled": self.enabled,
        }

    @property
    def registration_digest(self) -> str:
        return digest({"schema": "zekam-mcp-registration/v1", **self.as_dict()})


def validate_registry_entries(
    entries: tuple[McpServerRegistration, ...],
) -> tuple[McpServerRegistration, ...]:
    if len(entries) > 128:
        raise ValidationFailed("MCP registry en fazla 128 server kabul eder")
    names = [item.name.casefold() for item in entries]
    if len(names) != len(set(names)):
        raise ValidationFailed("MCP registry server adlari tekil olmali")
    return tuple(sorted(entries, key=lambda item: item.name.casefold()))
