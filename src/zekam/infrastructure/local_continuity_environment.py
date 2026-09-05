"""Provider-free, read-only SessionStart environment admission evidence.

Only explicit existing paths are inspected. Configuration uses the canonical
resolver with bounded captured documents, not a second merge implementation.
The SQLite authority is one consistent read transaction, including live WAL.
SQLite may coordinate through its SHM sidecar; no canonical rows, checkpoints,
configuration, layout or directories are written. Evidence is point-in-time,
not a lease: callers must revalidate their binding before hydration/effects.

The historical bootstrap receipt is deliberately not current authority: an
explicit schema upgrade or re-admitted configuration can legitimately outlive
the initial receipt. Current layout, schema and admitted config are required.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from zekam.application.active_task_contract import AUTHORITY_REF, MAX_TASK_BYTES, ActiveTaskContract
from zekam.application.config import (
    CONFIG_SCHEMA,
    USER_CONFIG_FILE,
    PersistenceBackend,
    default_config_file,
    load_settings,
)
from zekam.application.config import core_root as actual_core_root
from zekam.application.fresh_bootstrap import MAX_CONFIG_BYTES, OPERATIONAL_RELATIVE_PATH
from zekam.application.home import (
    HOME_ENTRIES,
    LAYOUT_FILE,
    LAYOUT_SCHEMA,
    PROJECT_ENTRIES,
    assert_separated_from_core,
)
from zekam.application.local_continuity import ContinuityBinding, uuid_text
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import ConfigurationError, ValidationFailed, ZekamError
from zekam.domain.identity import PRODUCT
from zekam.infrastructure.sqlite.operational_schema import (
    SCHEMA_DIGEST,
    SCHEMA_VERSION,
    _connect,
    _validate_connection,
)

ENVIRONMENT_SCHEMA = "zekam-continuity-environment/v1"
MAX_DOCUMENT_DEPTH = 16
MAX_DOCUMENT_NODES = 4096
MAX_CLIENTS = 16
_ENV_KEYS = (
    "ZEKAM_DATABASE_BACKEND",
    "ZEKAM_DATABASE_HOST",
    "ZEKAM_DATABASE_PORT",
    "ZEKAM_DATABASE_NAME",
    "ZEKAM_DATABASE_USER",
    "ZEKAM_DATABASE_SSLMODE",
    "ZEKAM_LOG_LEVEL",
)
_LEGACY_DATABASE_KEYS = frozenset({"host", "port", "name", "user", "sslmode"})


def _reject(reason: str) -> ConfigurationError:
    # Reasons are fixed developer strings, never source keys/values or paths.
    return ConfigurationError(f"Continuity environment: {reason}")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise _reject("duplicate or non-text mapping key")
        result[key] = value
    return result


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[str, Any]:
    return _unique_pairs(
        [
            (loader.construct_object(key, deep=True), loader.construct_object(value, deep=True))
            for key, value in node.value
        ]
    )


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _bounded_structure(text: str) -> None:
    """Check stream depth/nodes before YAML/JSON constructs recursive objects."""
    depth = documents = 0
    for nodes, event in enumerate(yaml.parse(text), start=1):
        if isinstance(event, yaml.events.DocumentStartEvent):
            documents += 1
        if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise _reject("YAML anchors and aliases forbidden")
        if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
            depth += 1
        elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
            depth -= 1
        if depth > MAX_DOCUMENT_DEPTH or nodes > MAX_DOCUMENT_NODES or documents > 1:
            raise _reject("document structural bound exceeded")


def _validate_values(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _reject("non-text mapping key")
            _validate_values(item)
    elif isinstance(value, list):
        for item in value:
            _validate_values(item)
    elif value is not None and type(value) not in (str, bool, int, float):
        raise _reject("non-JSON scalar forbidden")
    elif isinstance(value, float) and not math.isfinite(value):
        raise _reject("non-finite scalar forbidden")


def _document(payload: bytes, *, json_format: bool = False) -> dict[str, Any]:
    if not payload or len(payload) > MAX_CONFIG_BYTES:
        raise _reject("document byte bound exceeded")
    try:
        text = payload.decode("utf-8", errors="strict")
        _bounded_structure(text)
        value = (
            json.loads(text, object_pairs_hook=_unique_pairs)
            if json_format
            else yaml.load(text, Loader=_UniqueLoader)
        )
        if not isinstance(value, dict):
            raise _reject("document must be a mapping")
        _validate_values(value)
        return value
    except (ValueError, TypeError, RecursionError, yaml.YAMLError):
        raise _reject("malformed structured document") from None


def _path_identity(path: Path, *, directory: bool = False) -> tuple[int, ...]:
    if not path.is_absolute() or ".." in path.parts:
        raise _reject("canonical absolute path required")
    ancestors: list[int] = []
    for parent in reversed(path.parents):
        current = parent.lstat()
        if not stat.S_ISDIR(current.st_mode):
            raise _reject("symlink or non-directory ancestor")
        ancestors.extend((current.st_dev, current.st_ino, current.st_mode))
    current = path.lstat()
    valid = stat.S_ISDIR(current.st_mode) if directory else stat.S_ISREG(current.st_mode)
    if not valid:
        raise _reject("symlink or wrong path type")
    identity: tuple[int, ...] = (current.st_dev, current.st_ino, current.st_mode)
    if not directory:
        identity += (current.st_size, current.st_mtime_ns, current.st_ctime_ns)
    return (*ancestors, *identity)


def _capture(path: Path, maximum: int) -> tuple[tuple[int, ...], bytes]:
    before = _path_identity(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or (
                opened.st_dev,
                opened.st_ino,
            )
            != before[-6:-4]
        ):
            raise _reject("source changed during capture")
        payload = stream.read(maximum + 1)
    if not payload or len(payload) > maximum:
        raise _reject("file byte bound exceeded")
    if _path_identity(path) != before:
        raise _reject("source changed during capture")
    return before, payload


def _environment() -> dict[str, str]:
    captured = {key: value for key in _ENV_KEYS if (value := os.environ.get(key)) is not None}
    if any(value for key, value in captured.items() if key != "ZEKAM_LOG_LEVEL"):
        raise _reject("PostgreSQL or backend environment override forbidden")
    if any(len(value) > 128 for value in captured.values()):
        raise _reject("environment value bound exceeded")
    return captured


def _config_shape(document: dict[str, Any]) -> None:
    if document.get("schema") != CONFIG_SCHEMA:
        raise _reject("explicit config schema required")
    for section in ("database", "runtime", "storage", "knowledge", "diagnostic_trace"):
        if section in document and not isinstance(document[section], dict):
            raise _reject("config section must be a mapping")
    for section, fields in (
        ("database", ("backend", "host", "name", "user", "sslmode", "sqlite_relative_path")),
        ("runtime", ("log_level", "network_default", "permission_profile")),
        ("storage", ("object_store_relative",)),
        ("knowledge", ("embedding_model_ref", "embedding_distance")),
        ("diagnostic_trace", ("redaction_profile",)),
    ):
        for field in fields:
            if field in document.get(section, {}) and not isinstance(document[section][field], str):
                raise _reject("config text has wrong type")
    trace_key = document.get("diagnostic_trace", {}).get("encryption_key_ref")
    if trace_key is not None and not isinstance(trace_key, str):
        raise _reject("config secret reference has wrong type")
    extensions = document.get("database", {}).get("required_extensions", [])
    if (
        not isinstance(extensions, list)
        or len(extensions) > 32
        or any(not isinstance(item, str) for item in extensions)
    ):
        raise _reject("database extension metadata invalid")
    for section, integer_fields in (
        ("database", ("port", "connect_timeout_seconds", "minimum_server_version")),
        ("knowledge", ("embedding_dimension",)),
        (
            "diagnostic_trace",
            ("retention_days", "max_payload_bytes", "max_events", "max_total_bytes"),
        ),
    ):
        for field in integer_fields:
            if field in document.get(section, {}) and type(document[section][field]) is not int:
                raise _reject("config integer has wrong type")
    for field in ("enabled", "export_allowed"):
        trace = document.get("diagnostic_trace", {})
        if field in trace and type(trace[field]) is not bool:
            raise _reject("config boolean has wrong type")
    clients = document.get("clients", [])
    if not isinstance(clients, list) or len(clients) > MAX_CLIENTS:
        raise _reject("client metadata bound exceeded")
    for client in clients:
        if not isinstance(client, dict):
            raise _reject("client metadata must be a mapping")
        executable = client.get("executable")
        if not isinstance(executable, str) or len(executable) > 4096:
            raise _reject("client executable metadata invalid")


def _expected_layout() -> dict[str, Any]:
    return {
        "schema": LAYOUT_SCHEMA,
        "product": PRODUCT.name,
        "data_root_env": PRODUCT.data_root_env,
        **{
            name: [
                {
                    "path": entry.relative,
                    "ownership": entry.ownership.value,
                    "description": entry.description,
                }
                for entry in entries
            ]
            for name, entries in (("entries", HOME_ENTRIES), ("project_entries", PROJECT_ENTRIES))
        },
    }


@dataclass(frozen=True, slots=True)
class LocalContinuityEnvironment:
    home: Path
    core_root: Path
    task_path: Path
    operational_path: Path

    def validate(self, binding: ContinuityBinding) -> dict[str, Any]:
        if any(
            not isinstance(path, Path)
            for path in (self.home, self.core_root, self.task_path, self.operational_path)
        ):
            raise ValidationFailed("Typed continuity environment paths required")
        if not isinstance(binding, ContinuityBinding):
            raise ValidationFailed("Typed continuity binding required")
        binding.__post_init__()
        try:
            return self._validate(binding)
        except ConfigurationError:
            raise
        except (OSError, ValueError, TypeError, sqlite3.Error, ZekamError):
            raise _reject("input, configuration or authority validation failed") from None

    def _validate(self, binding: ContinuityBinding) -> dict[str, Any]:
        home_identity = _path_identity(self.home, directory=True)
        core_identity = _path_identity(self.core_root, directory=True)
        if self.core_root != actual_core_root():
            raise _reject("core does not match executing source")
        assert_separated_from_core(self.home, self.core_root)
        if self.task_path != self.core_root / AUTHORITY_REF:
            raise _reject("task must be the actual core authority")
        if self.operational_path != self.home / OPERATIONAL_RELATIVE_PATH:
            raise _reject("operational path must be exact home state path")
        default_path = default_config_file()
        if not default_path.is_relative_to(self.core_root):
            raise _reject("default config is outside core")
        user_path = self.home / USER_CONFIG_FILE
        layout_path = self.home / LAYOUT_FILE
        projection_path = self.task_path.with_suffix(".yaml")
        bounds = {
            self.task_path: MAX_TASK_BYTES,
            projection_path: MAX_CONFIG_BYTES,
            default_path: MAX_CONFIG_BYTES,
            user_path: MAX_CONFIG_BYTES,
            layout_path: MAX_CONFIG_BYTES,
        }
        captures = {path: _capture(path, bound) for path, bound in bounds.items()}
        task = ActiveTaskContract.from_bytes(captures[self.task_path][1])
        projection = _document(captures[projection_path][1])
        if canonical_json(projection) != canonical_json(task.projection()):
            raise _reject("task projection drift")
        if task.source_digest != binding.task_digest:
            raise _reject("actual task digest drift")
        layout = _document(captures[layout_path][1], json_format=True)
        if canonical_json(layout) != canonical_json(_expected_layout()):
            raise _reject("exact layout-v2 manifest required")
        directories = {
            self.home / entry.relative: _path_identity(self.home / entry.relative, directory=True)
            for entry in HOME_ENTRIES
        }
        documents = {path: _document(captures[path][1]) for path in (default_path, user_path)}
        for document in documents.values():
            _config_shape(document)
        database = documents[user_path].get("database", {})
        if str(database.get("backend", "sqlite")).lower() != "sqlite" or (
            _LEGACY_DATABASE_KEYS & database.keys()
        ):
            raise _reject("legacy PostgreSQL config forbidden")
        environment = _environment()
        try:
            settings = load_settings(
                home=self.home,
                default_file=default_path,
                environ=environment,
                document_loader=documents.__getitem__,
            )
        except (ZekamError, ValueError, TypeError, KeyError):
            raise _reject("canonical settings invalid") from None
        if settings.database.backend is not PersistenceBackend.SQLITE:
            raise _reject("SQLite backend required")
        if settings.database.sqlite_path(self.home) != self.operational_path:
            raise _reject("resolved operational path drift")
        sanitized = settings.sanitized()
        policy_digest = digest(sanitized)
        if policy_digest != binding.policy_digest:
            raise _reject("actual settings digest drift")
        config_id = self._admitted_config(binding, sanitized)
        for path, expected in captures.items():
            if _capture(path, bounds[path]) != expected:
                raise _reject("source document changed during validation")
        if _environment() != environment:
            raise _reject("environment changed during validation")
        for path, identity in directories.items():
            if _path_identity(path, directory=True) != identity:
                raise _reject("home directory changed during validation")
        if (
            _path_identity(self.home, directory=True) != home_identity
            or _path_identity(self.core_root, directory=True) != core_identity
        ):
            raise _reject("root changed during validation")
        evidence = {
            "schema": ENVIRONMENT_SCHEMA,
            "status": "validated",
            "binding_digest": binding.binding_digest,
            "task_digest": task.source_digest,
            "policy_digest": policy_digest,
            "layout_digest": digest_of_bytes(captures[layout_path][1]),
            "config_revision_id": config_id,
            "operational_schema_version": SCHEMA_VERSION,
            "operational_schema_digest": SCHEMA_DIGEST,
            "read_only": True,
            "authority_snapshot_only": True,
            "grants_authority": False,
            "provider_calls": 0,
            "network_calls": 0,
        }
        return {**evidence, "evidence_digest": digest(evidence)}

    def _admitted_config(self, binding: ContinuityBinding, sanitized: dict[str, Any]) -> str:
        before = _path_identity(self.operational_path)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.operational_path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _path_identity(sidecar)
        connection = _connect(self.operational_path, read_only=True)
        try:
            connection.execute("pragma query_only=on")
            connection.execute("begin")
            if _validate_connection(connection) != SCHEMA_VERSION:
                raise _reject("current operational schema required")
            rows = connection.execute(
                "select id,task_digest,config_digest,length(cast(sanitized_json as blob))"
                " from config_revision where active=1 limit 2"
            ).fetchall()
            if len(rows) != 1:
                raise _reject("exactly one admitted active config required")
            row = rows[0]
            if row[1] != binding.task_digest or row[2] != binding.policy_digest:
                raise _reject("active admitted task or policy drift")
            uuid_text(row[0], "Config revision")
            if type(row[3]) is not int or not 0 < row[3] <= MAX_CONFIG_BYTES:
                raise _reject("admitted config byte bound exceeded")
            raw = connection.execute(
                "select sanitized_json from config_revision where id=?", (row[0],)
            ).fetchone()[0]
            if not isinstance(raw, str):
                raise _reject("admitted config must be JSON text")
            admitted = _document(raw.encode("utf-8"), json_format=True)
            if canonical_json(admitted) != canonical_json(sanitized):
                raise _reject("admitted settings payload drift")
            if digest(admitted) != binding.policy_digest:
                raise _reject("admitted settings digest drift")
            return str(row[0])
        finally:
            connection.rollback()
            connection.close()
            # A live writer may legitimately change bytes while our read
            # transaction remains consistent. Replacement of the file may not.
            after = _path_identity(self.operational_path)
            if after[:-3] != before[:-3]:
                raise _reject("operational authority file replaced")
