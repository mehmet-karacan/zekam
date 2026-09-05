"""Yapilandirma yukleme.

Oncelik sirasi:

1. `config/zekam.default.yaml` (core varsayilanlari, secret icermez)
2. `$ZEKAM_HOME/config.yaml` (kullanici override'i, secret icermez)
3. Ortam degiskenleri (`ZEKAM_*`)

Parola ve token degerleri yapilandirma dosyasindan okunmaz; yalnizca ortam degiskeni
veya Secret Broker uzerinden gelir ve hicbir zaman loglanmaz.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from zekam.application.environment import environment_value
from zekam.domain.canonical import digest
from zekam.domain.config_provenance import (
    ConfigLayer,
    ConfigProvenanceGraph,
    ManagedFieldRequirement,
    ManagedRequirementMode,
    PermissionProfileRevision,
    builtin_permission_profiles,
    compile_config_provenance,
)
from zekam.domain.diagnostic_trace import DiagnosticTracePolicy
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed

CONFIG_SCHEMA = "zekam-config/v1"
USER_CONFIG_FILE = "config.yaml"


class PersistenceBackend(StrEnum):
    """Supported local persistence engine."""

    SQLITE = "sqlite"


#: Yapilandirma dosyasinda gorunmesi yasak anahtarlar.
FORBIDDEN_CONFIG_KEYS: frozenset[str] = frozenset(
    {"password", "passwd", "secret", "token", "api_key", "apikey", "private_key"}
)


def package_root() -> Path:
    """Kurulu `zekam` paketinin kokunu dondurur."""
    return Path(__file__).resolve().parents[1]


def core_root() -> Path:
    """Core source/dagitim kokunu dondurur.

    Gelistirme kurulumunda repository koku, wheel kurulumunda paket kokudur.
    """
    candidate = package_root().parents[1]
    if (candidate / "pyproject.toml").is_file():
        return candidate
    return package_root()


def default_config_file() -> Path:
    """Core varsayilan yapilandirma dosyasi.

    Gelistirme agacinda `config/`, wheel kurulumunda paket icindeki `_config/`
    dizini kullanilir.
    """
    repository_copy = package_root().parents[1] / "config" / "zekam.default.yaml"
    if repository_copy.is_file():
        return repository_copy
    return package_root() / "_config" / "zekam.default.yaml"


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Secili persistence ve baglanti ayarlari. Secret bu nesnede tutulmaz."""

    host: str
    port: int
    name: str
    user: str
    backend: PersistenceBackend = PersistenceBackend.SQLITE
    sqlite_relative_path: str = "state/operational.db"
    sslmode: str = "prefer"
    connect_timeout_seconds: int = 5
    minimum_server_version: int = 18
    required_extensions: tuple[str, ...] = ("vector",)

    def sqlite_path(self, home: Path) -> Path:
        """SQLite dosyasini ZEKAM_HOME icinde ve portable olarak cozer."""
        return resolve_sqlite_path(home, self.sqlite_relative_path)

    def sanitized(self) -> dict[str, Any]:
        """Log ve rapor icin guvenli gorunum."""
        return {
            "backend": self.backend.value,
            "sqlite_relative_path": self.sqlite_relative_path,
        }


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Calisma zamani davranisi."""

    log_level: str = "INFO"
    network_default: str = "deny"
    permission_profile: str = "workspace-write-no-network"


@dataclass(frozen=True, slots=True)
class KnowledgeSettings:
    """Bilgi duzlemi varsayilanlari."""

    embedding_model_ref: str = "openai/BAAI/bge-m3"
    embedding_dimension: int = 1024
    embedding_distance: str = "cosine"


@dataclass(frozen=True, slots=True)
class DiagnosticTraceSettings:
    """Raw trace varsayilan kapalidir; yalniz secret-free policy metadata tasir."""

    enabled: bool = False
    retention_days: int = 7
    max_payload_bytes: int = 1_048_576
    max_events: int = 10_000
    max_total_bytes: int = 64 * 1_048_576
    encryption_key_ref: str | None = None
    export_allowed: bool = False
    redaction_profile: str = "strict-v1"


@dataclass(frozen=True, slots=True)
class ClientSettings:
    """Yerel istemcinin secret-free executable kaydi."""

    name: str
    executable: Path

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise ConfigurationError("Istemci adi bos veya kenar bosluklu olamaz")
        if not self.executable.is_absolute():
            raise ConfigurationError("Istemci executable yolu absolute olmali")
        try:
            resolved = self.executable.resolve(strict=True)
        except OSError:
            raise ConfigurationError("Istemci executable dosyasi bulunamadi") from None
        if not resolved.is_file():
            raise ConfigurationError("Istemci executable yolu regular file olmali")
        object.__setattr__(self, "executable", resolved)

    def sanitized(self) -> dict[str, str]:
        """Yalniz istemci adi ve exact executable metadata'sini verir."""
        return {"name": self.name, "executable": str(self.executable)}


@dataclass(frozen=True, slots=True)
class Settings:
    """Cozulmus uygulama yapilandirmasi."""

    home: Path
    database: DatabaseSettings
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    knowledge: KnowledgeSettings = field(default_factory=KnowledgeSettings)
    diagnostic_trace: DiagnosticTraceSettings = field(default_factory=DiagnosticTraceSettings)
    clients: tuple[ClientSettings, ...] = ()
    object_store_relative: str = "artifacts/sha256"
    sources: tuple[str, ...] = ()
    config_provenance: ConfigProvenanceGraph | None = None
    permission_profile: PermissionProfileRevision | None = None

    def sanitized(self) -> dict[str, Any]:
        """Secret icermeyen rapor gorunumu."""
        return {
            "home": str(self.home),
            "database": self.database.sanitized(),
            "runtime": {
                "log_level": self.runtime.log_level,
                "network_default": self.runtime.network_default,
                "permission_profile": self.runtime.permission_profile,
            },
            "knowledge": {
                "embedding_model_ref": self.knowledge.embedding_model_ref,
                "embedding_dimension": self.knowledge.embedding_dimension,
                "embedding_distance": self.knowledge.embedding_distance,
            },
            "diagnostic_trace": {
                "enabled": self.diagnostic_trace.enabled,
                "retention_days": self.diagnostic_trace.retention_days,
                "max_payload_bytes": self.diagnostic_trace.max_payload_bytes,
                "max_events": self.diagnostic_trace.max_events,
                "max_total_bytes": self.diagnostic_trace.max_total_bytes,
                "encryption_key_ref": self.diagnostic_trace.encryption_key_ref,
                "export_allowed": self.diagnostic_trace.export_allowed,
                "redaction_profile": self.diagnostic_trace.redaction_profile,
            },
            "clients": [client.sanitized() for client in self.clients],
            "object_store_relative": self.object_store_relative,
            "sources": list(self.sources),
            "config_provenance": (
                None
                if self.config_provenance is None
                else {
                    "layer_stack": list(self.config_provenance.layer_stack),
                    "effective_digest": self.config_provenance.effective_digest,
                    "graph_digest": self.config_provenance.graph_digest,
                }
            ),
            "permission_profile": (
                None
                if self.permission_profile is None
                else {
                    "name": self.permission_profile.name,
                    "revision": self.permission_profile.revision,
                    "profile_digest": self.permission_profile.profile_digest,
                    "managed": self.permission_profile.managed,
                    "grants_authority": False,
                }
            ),
        }


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _assert_no_secret_keys(document: Mapping[str, Any], path: str = "") -> None:
    for key, value in document.items():
        location = f"{path}.{key}" if path else str(key)
        if str(key).lower() in FORBIDDEN_CONFIG_KEYS:
            raise ConfigurationError(f"Yapilandirma dosyasi secret alani tasiyamaz: {location}")
        if isinstance(value, Mapping):
            _assert_no_secret_keys(value, location)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    _assert_no_secret_keys(item, f"{location}[{index}]")


def _parse_clients(document: Mapping[str, Any]) -> tuple[ClientSettings, ...]:
    if "clients" not in document:
        return ()
    rows = document["clients"]
    if not isinstance(rows, list):
        raise ConfigurationError("Clients yapilandirmasi liste olmali")
    clients: list[ClientSettings] = []
    names: set[str] = set()
    executables: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"name", "executable"}:
            raise ConfigurationError("Her client exact name ve executable alanlarini tasimali")
        name = row["name"]
        executable = row["executable"]
        if not isinstance(name, str) or not isinstance(executable, str) or not executable:
            raise ConfigurationError("Client name ve executable metin olmali")
        client = ClientSettings(name=name, executable=Path(executable))
        name_key = client.name.casefold()
        executable_key = os.path.normcase(str(client.executable))
        if name_key in names or executable_key in executables:
            raise ConfigurationError("Duplicate client adi veya executable yolu yasak")
        names.add(name_key)
        executables.add(executable_key)
        clients.append(client)
    return tuple(clients)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - hata metni sanitize edilir
        raise ConfigurationError(f"Yapilandirma dosyasi okunamadi: {path.name}") from exc
    return _validate_document(loaded, path)


def _validate_document(loaded: object, path: Path) -> dict[str, Any]:
    """Keep file and captured-document configuration validation identical."""
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"Yapilandirma dosyasi sozluk olmali: {path.name}")
    schema = loaded.get("schema")
    if schema is not None and schema != CONFIG_SCHEMA:
        raise ConfigurationError(
            f"Desteklenmeyen yapilandirma semasi: {schema!r}, beklenen {CONFIG_SCHEMA!r}"
        )
    _assert_no_secret_keys(loaded)
    return loaded


def _env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    database: dict[str, Any] = {}
    runtime: dict[str, Any] = {}

    log_level = environment_value(environ, "ZEKAM_LOG_LEVEL")
    if log_level:
        runtime["log_level"] = log_level.upper()

    if database:
        overrides["database"] = database
    if runtime:
        overrides["runtime"] = runtime
    return overrides


def resolve_sqlite_path(home: Path, relative_path: str) -> Path:
    """Portable SQLite locator'ini ZEKAM_HOME disina cikmadan cozer."""
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ConfigurationError("SQLite yolu ZEKAM_HOME'a gore portable olmali")
    root = home.resolve()
    target = (root / relative).resolve()
    root_comparison = _windows_path_for_comparison(root)
    target_comparison = _windows_path_for_comparison(target)
    try:
        common = os.path.commonpath((root_comparison, target_comparison))
    except (OSError, ValueError):
        raise ConfigurationError("SQLite yolu ZEKAM_HOME disina cikamaz") from None
    if os.path.normcase(common) != os.path.normcase(root_comparison):
        raise ConfigurationError("SQLite yolu ZEKAM_HOME disina cikamaz")
    return target


def _windows_path_for_comparison(path: Path) -> str:
    value = os.fspath(path)
    if os.name != "nt":
        return value
    folded = os.path.normcase(value)
    if folded.startswith("\\\\?\\unc\\"):
        return f"\\\\{value[8:]}"
    if folded.startswith("\\\\?\\"):
        return value[4:]
    return value


def load_settings(
    *,
    home: Path,
    environ: Mapping[str, str] | None = None,
    default_file: Path | None = None,
    session_overrides: Mapping[str, Any] | None = None,
    session_permission_capabilities: tuple[str, ...] = (),
    document_loader: Callable[[Path], dict[str, Any]] | None = None,
) -> Settings:
    """Resolve the canonical layers; an optional trusted loader supplies snapshots.

    Injected documents still pass the same schema/secret validation. The caller
    owns bounded capture and source stability; precedence and provenance do not
    change when this hook is used.
    """
    environ = os.environ if environ is None else environ
    if environment_value(environ, "ZEKAM_DATABASE_BACKEND"):
        raise ConfigurationError(
            "ZEKAM_DATABASE_BACKEND runtime override yasak; ilk secim icin zekam init kullanin"
        )
    default_path = default_file or default_config_file()
    user_path = home / USER_CONFIG_FILE

    sources: list[str] = []
    layers: list[ConfigLayer] = []

    def read_document(path: Path) -> dict[str, Any]:
        if document_loader is None:
            return _load_yaml(path)
        return _validate_document(document_loader(path), path)

    default_document = read_document(default_path)
    if default_document:
        sources.append("core-default")
        layers.append(ConfigLayer("core-default", 10, default_document))

    user_document = read_document(user_path)
    if user_document:
        sources.append("user-config")
        layers.append(ConfigLayer("user-config", 20, user_document))

    managed_profile_name = "workspace-write-no-network"
    managed_document = {
        "runtime": {
            "network_default": "deny",
            "permission_profile": managed_profile_name,
        }
    }
    managed_requirements = tuple(
        ManagedFieldRequirement(path, ManagedRequirementMode.EXACT, digest(value))
        for path, value in (
            ("runtime.network_default", "deny"),
            ("runtime.permission_profile", managed_profile_name),
        )
    )
    sources.append("managed-policy")
    layers.append(
        ConfigLayer(
            "managed-policy",
            30,
            managed_document,
            managed=True,
            requirements=managed_requirements,
        )
    )

    env_document = _env_overrides(environ)
    if env_document:
        sources.append("environment")
        layers.append(ConfigLayer("environment", 40, env_document))

    if session_overrides:
        _assert_no_secret_keys(session_overrides)
        sources.append("session")
        layers.append(ConfigLayer("session", 50, dict(session_overrides)))

    if not layers:
        layers.append(ConfigLayer("implicit-default", 0, {}))
    provenance = compile_config_provenance(tuple(layers))
    document = provenance.effective_document

    database_document = dict(document.get("database") or {})
    unsupported_database_keys = set(database_document) - {"backend", "sqlite_relative_path"}
    if unsupported_database_keys:
        raise ConfigurationError("Yerel veritabani yapilandirmasi desteklenmeyen alan iceriyor")
    try:
        backend = PersistenceBackend(
            str(database_document.get("backend", PersistenceBackend.SQLITE.value)).lower()
        )
        database = DatabaseSettings(
            host=str(database_document.get("host", "127.0.0.1")),
            port=int(database_document.get("port", 5433)),
            name=str(database_document.get("name", "zekam")),
            user=str(database_document.get("user", "zekam")),
            backend=backend,
            sqlite_relative_path=str(
                database_document.get("sqlite_relative_path", "state/operational.db")
            ),
            sslmode=str(database_document.get("sslmode", "prefer")),
            connect_timeout_seconds=int(database_document.get("connect_timeout_seconds", 5)),
            minimum_server_version=int(database_document.get("minimum_server_version", 18)),
            required_extensions=tuple(database_document.get("required_extensions") or ("vector",)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("Veritabani ayarlari gecersiz") from exc

    runtime_document = dict(document.get("runtime") or {})
    runtime = RuntimeSettings(
        log_level=str(runtime_document.get("log_level", "INFO")).upper(),
        network_default=str(runtime_document.get("network_default", "deny")),
        permission_profile=str(runtime_document.get("permission_profile", managed_profile_name)),
    )
    profile_matches = tuple(
        profile
        for profile in builtin_permission_profiles()
        if profile.name == runtime.permission_profile
    )
    if len(profile_matches) != 1:
        raise ConfigurationError("Named permission profile bulunamadi")
    permission_profile = profile_matches[0]
    permission_profile.resolve_session(session_permission_capabilities)

    knowledge_document = dict(document.get("knowledge") or {})
    knowledge = KnowledgeSettings(
        embedding_model_ref=str(
            knowledge_document.get("embedding_model_ref", "openai/BAAI/bge-m3")
        ),
        embedding_dimension=int(knowledge_document.get("embedding_dimension", 1024)),
        embedding_distance=str(knowledge_document.get("embedding_distance", "cosine")),
    )

    trace_document = dict(document.get("diagnostic_trace") or {})
    diagnostic_trace = DiagnosticTraceSettings(
        enabled=bool(trace_document.get("enabled", False)),
        retention_days=int(trace_document.get("retention_days", 7)),
        max_payload_bytes=int(trace_document.get("max_payload_bytes", 1_048_576)),
        max_events=int(trace_document.get("max_events", 10_000)),
        max_total_bytes=int(trace_document.get("max_total_bytes", 64 * 1_048_576)),
        encryption_key_ref=(
            None
            if trace_document.get("encryption_key_ref") is None
            else str(trace_document["encryption_key_ref"])
        ),
        export_allowed=bool(trace_document.get("export_allowed", False)),
        redaction_profile=str(trace_document.get("redaction_profile", "strict-v1")),
    )
    try:
        DiagnosticTracePolicy(
            enabled=diagnostic_trace.enabled,
            retention_days=diagnostic_trace.retention_days,
            max_payload_bytes=diagnostic_trace.max_payload_bytes,
            max_events=diagnostic_trace.max_events,
            max_total_bytes=diagnostic_trace.max_total_bytes,
            encryption_key_ref=diagnostic_trace.encryption_key_ref,
            export_allowed=diagnostic_trace.export_allowed,
            redaction_profile=diagnostic_trace.redaction_profile,
        )
    except (PolicyViolation, ValidationFailed) as exc:
        raise ConfigurationError("Diagnostic trace ayarlari gecersiz") from exc

    storage_document = dict(document.get("storage") or {})
    object_store_relative = str(storage_document.get("object_store_relative", "artifacts/sha256"))
    clients = _parse_clients(document)

    return Settings(
        home=home,
        database=database,
        runtime=runtime,
        knowledge=knowledge,
        diagnostic_trace=diagnostic_trace,
        clients=clients,
        object_store_relative=object_store_relative,
        sources=tuple(sources),
        config_provenance=provenance,
        permission_profile=permission_profile,
    )


def database_password(environ: Mapping[str, str] | None = None) -> str | None:
    """Parolayi yalnizca ortam degiskeninden okur; hicbir yere yazmaz."""
    environ = os.environ if environ is None else environ
    return environment_value(environ, "ZEKAM_DATABASE_PASSWORD") or None
