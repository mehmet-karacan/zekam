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
    GEMINI = "gemini"


SUPPORTED_CLIENT_INTEGRATIONS: Final = tuple(ClientIntegrationId)

# Receipt'li ilk uc istemci; digest ve eski receipt yorumu bu kume uzerinden korunur.
LEGACY_CLIENT_INTEGRATIONS: Final = (
    ClientIntegrationId.OPENCODE,
    ClientIntegrationId.CODEX,
    ClientIntegrationId.CLAUDE_CODE,
)

POLICY_VERSION: Final = 2
_VERSION_KEY: Final = "version"

# Proje-yerel giris her desteklenen istemci icin kokte hazirdir ve kullanici-geneli
# kurulum degildir. Fiziksel skill projection yalniz kendi dizinini tarayan istemcilere
# yapilir: OpenCode `.agents` ve `.claude` dizinlerini, Gemini `.agents` dizinini kendisi
# kesfeder; bu nedenle ayri `.opencode/.gemini` kopyasi uretilmez.
PROJECT_LOCAL_PROJECTION_CLIENTS: Final = frozenset(
    {ClientIntegrationId.CODEX, ClientIntegrationId.CLAUDE_CODE}
)


@dataclass(frozen=True, slots=True)
class ClientIntegrationPolicy:
    """One canonical global-integration decision; executable presence is separate.

    ``True`` yalniz kullanicinin acik v2 secimiyle kullanici-geneli yonetilen
    Zekam girisini (instruction/hook/agent bootstrap) secer. Surumsuz eski
    ``cli.integrations`` degerleri yeni oturumlarda global kurulum uretmez; istenen
    degerler ``legacy_selection`` altinda yalniz uzlastirma/temizlik plani icin kalir.
    """

    opencode: bool = False
    codex: bool = False
    claude_code: bool = False
    gemini: bool = False
    legacy_selection: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in self.clients().values()):
            raise ConfigurationError("CLI integration degerleri gercek boolean olmali")

    @classmethod
    def from_mapping(cls, value: object) -> ClientIntegrationPolicy:
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ConfigurationError("cli.integrations mapping olmali")
        expected = {item.value for item in SUPPORTED_CLIENT_INTEGRATIONS}
        unknown = set(value) - expected - {_VERSION_KEY}
        if unknown:
            raise ConfigurationError(
                "Desteklenmeyen CLI integration alani: " + ", ".join(sorted(map(str, unknown)))
            )
        for key, enabled in value.items():
            if key == _VERSION_KEY:
                continue
            if type(enabled) is not bool:
                raise ConfigurationError(f"cli.integrations.{key} gercek boolean olmali")
        version = value.get(_VERSION_KEY)
        if version is not None and (type(version) is not int or version != POLICY_VERSION):
            raise ConfigurationError(f"cli.integrations.version {POLICY_VERSION} olmali")
        if version is None:
            requested = tuple(
                item.value for item in SUPPORTED_CLIENT_INTEGRATIONS if value.get(item.value)
            )
            return cls(legacy_selection=requested)
        return cls(
            opencode=value.get(ClientIntegrationId.OPENCODE.value, False),
            codex=value.get(ClientIntegrationId.CODEX.value, False),
            claude_code=value.get(ClientIntegrationId.CLAUDE_CODE.value, False),
            gemini=value.get(ClientIntegrationId.GEMINI.value, False),
        )

    def clients(self) -> dict[str, bool]:
        return {
            ClientIntegrationId.OPENCODE.value: self.opencode,
            ClientIntegrationId.CODEX.value: self.codex,
            ClientIntegrationId.CLAUDE_CODE.value: self.claude_code,
            ClientIntegrationId.GEMINI.value: self.gemini,
        }

    @property
    def legacy(self) -> bool:
        return bool(self.legacy_selection)

    def enabled(self, client: ClientIntegrationId | str) -> bool:
        try:
            identity = ClientIntegrationId(client)
        except ValueError as exc:
            raise ValidationFailed("Desteklenmeyen CLI integration kimligi") from exc
        return self.clients()[identity.value]

    def project_local_projection(self, client: ClientIntegrationId | str) -> bool:
        """Zekam kokundeki proje-yerel skill projection hedefi; global secimden bagimsizdir."""

        try:
            identity = ClientIntegrationId(client)
        except ValueError as exc:
            raise ValidationFailed("Desteklenmeyen CLI integration kimligi") from exc
        return identity in PROJECT_LOCAL_PROJECTION_CLIENTS

    def projection_enabled(self, client: ClientIntegrationId | str) -> bool:
        """Proje-yerel skill projection hedefi: varsayilan ortak dizinler veya acik v2 opt-in."""

        return self.project_local_projection(client) or self.enabled(client)

    def with_change(
        self, *, enable: tuple[str, ...] = (), disable: tuple[str, ...] = ()
    ) -> ClientIntegrationPolicy:
        overlap = set(enable) & set(disable)
        if overlap:
            raise PolicyViolation("Ayni istemci birlikte enable ve disable edilemez")
        values = self.clients()
        changes = tuple((item, True) for item in enable) + tuple((item, False) for item in disable)
        for requested, state in changes:
            try:
                identity = ClientIntegrationId(requested)
            except ValueError as exc:
                raise ValidationFailed("Desteklenmeyen CLI integration kimligi") from exc
            values[identity.value] = state
        return ClientIntegrationPolicy.from_mapping({_VERSION_KEY: POLICY_VERSION, **values})

    def body(self) -> dict[str, Any]:
        """Kalici yapilandirma govdesi; her zaman acik v2 surumu tasir."""

        return {_VERSION_KEY: POLICY_VERSION, **self.clients()}

    @property
    def policy_digest(self) -> str:
        if self.legacy:
            # Eski receipt'lerin bagli oldugu v1 digest bicimi degismez.
            requested = set(self.legacy_selection)
            return digest(
                {
                    "schema": "zekam-cli-integration-policy/v1",
                    "integrations": {
                        item.value: item.value in requested for item in LEGACY_CLIENT_INTEGRATIONS
                    },
                }
            )
        return digest({"schema": "zekam-cli-integration-policy/v2", "integrations": self.body()})


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
