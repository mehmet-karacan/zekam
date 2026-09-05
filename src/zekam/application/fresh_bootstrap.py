"""Atomic, zero-data ZEKAM_HOME v2 bootstrap.

The bootstrap never reads or connects to PostgreSQL.  A legacy PostgreSQL
configuration is detected as metadata and blocks mutation.  Fresh state is
built in a sibling staging directory and published with one rename.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from zekam.application.config import CONFIG_SCHEMA, PersistenceBackend, load_settings
from zekam.application.home import HomeLayout, assert_separated_from_core
from zekam.application.operational_store import OperationalSchemaPort
from zekam.domain.canonical import parse_digest
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.infrastructure.local_file_security import restrict_private_tree

BOOTSTRAP_SCHEMA = "zekam-fresh-bootstrap/v1"
BOOTSTRAP_RECEIPT_SCHEMA = "zekam-bootstrap-receipt/v1"
BACKUP_CONTRACT_SCHEMA = "zekam-backup-contract/v1"
STAGE_MARKER_SCHEMA = "zekam-bootstrap-stage/v1"
LOCK_SCHEMA = "zekam-bootstrap-lock/v1"
OPERATIONAL_RELATIVE_PATH = "state/operational.db"
RECEIPT_RELATIVE_PATH = "state/manifests/bootstrap-receipt.json"
BACKUP_CONTRACT_RELATIVE_PATH = "state/manifests/backup-contract.json"
MAX_CONFIG_BYTES = 1_048_576
FAULT_POINTS = frozenset({"after-layout", "after-config", "after-database", "before-publish"})
_PROCESS_LOCK_GUARD = threading.Lock()
_PROCESS_OWNED_LOCKS: set[Path] = set()

type BootstrapAction = Literal["create", "already-initialized"]


class _UniqueConfigLoader(yaml.SafeLoader):
    """Legacy metadata inspection must reject ambiguous duplicate keys."""


def _unique_config_mapping(loader: yaml.SafeLoader, node: yaml.Node) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if not isinstance(key, str) or key in result:
            raise ConfigurationError("Legacy config duplicate/gecersiz key tasiyor")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_UniqueConfigLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_config_mapping
)


def _exact_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"JSON duplicate alan iceriyor: {key}")
        result[key] = value
    return result


def _load_exact_json(path: Path, *, label: str) -> object:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_exact_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ConfigurationError) as exc:
        raise ConfigurationError(f"{label} okunamadi") from exc


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if os.name != "nt":
        path.chmod(mode)
    _fsync_file(path)


def _fsync_file(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    """Flush staged files and directories before the atomic publish rename."""
    if os.name == "nt":
        return
    directories: list[Path] = []
    for current, _, files in os.walk(root):
        directory = Path(current)
        directories.append(directory)
        for name in files:
            _fsync_file(directory / name)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _write_config(home: Path) -> Path:
    path = home / "config.yaml"
    path.write_text(
        f"schema: {CONFIG_SCHEMA}\n"
        "database:\n"
        "  backend: sqlite\n"
        f"  sqlite_relative_path: {OPERATIONAL_RELATIVE_PATH}\n"
        "storage:\n"
        "  object_store_relative: artifacts/sha256\n"
        "runtime:\n"
        "  network_default: deny\n",
        encoding="utf-8",
        newline="\n",
    )
    if os.name != "nt":
        path.chmod(0o600)
    _fsync_file(path)
    return path


@dataclass(frozen=True, slots=True)
class LegacyConfigDetection:
    detected: bool
    reasons: tuple[str, ...]


def detect_legacy_postgresql_config(home: Path) -> LegacyConfigDetection:
    """Inspect bounded config metadata without importing a driver or connecting."""
    path = home / "config.yaml"
    if not path.is_file():
        return LegacyConfigDetection(False, ())
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ConfigurationError("Legacy config boyut sinirini asiyor")
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueConfigLoader) or {}
    except (OSError, UnicodeError, yaml.YAMLError, ConfigurationError) as exc:
        raise ConfigurationError("Legacy config guvenle okunamadi") from exc
    if not isinstance(document, dict):
        raise ConfigurationError("Legacy config kok nesnesi mapping olmali")
    database = document.get("database")
    mapping = database if isinstance(database, dict) else {}
    reasons: list[str] = []
    if str(mapping.get("backend", "")).casefold() == "postgresql":
        reasons.append("database.backend=postgresql")
    if any(key in mapping for key in ("host", "port", "name", "user", "sslmode")):
        reasons.append("legacy-connection-metadata-present")
    return LegacyConfigDetection(bool(reasons), tuple(reasons))


@dataclass(frozen=True, slots=True)
class FreshBootstrapPlan:
    home: Path
    core_root: Path
    action: BootstrapAction
    authority_digest: str
    plan_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": BOOTSTRAP_SCHEMA,
            "home": str(self.home),
            "core_root": str(self.core_root),
            "action": self.action,
            "authority_digest": self.authority_digest,
            "layout_schema": "zekam-home-layout/v2",
            "operational_engine": "cpython-sqlite",
            "operational_relative_path": OPERATIONAL_RELATIVE_PATH,
            "network_required": False,
            "docker_required": False,
            "legacy_postgresql_data_used": False,
        }


def _plan_digest(value: dict[str, object]) -> str:
    return _canonical_digest(value)


def _validate_existing(
    home: Path,
    *,
    core_root: Path,
    authority_digest: str,
    schema: OperationalSchemaPort,
) -> None:
    detection = detect_legacy_postgresql_config(home)
    if detection.detected:
        raise ConfigurationError(
            "Legacy PostgreSQL config algilandi; baglanti kurulmadan fresh home gerekli"
        )
    issues = HomeLayout(home).verify()
    if issues:
        raise ConfigurationError(
            f"Mevcut ZEKAM_HOME v2 degil veya eksik: {issues[0].kind}:{issues[0].relative}"
        )
    settings = load_settings(home=home, environ={})
    if settings.database.backend is not PersistenceBackend.SQLITE:
        raise ConfigurationError("Fresh bootstrap yalniz SQLite operational store kabul eder")
    database = settings.database.sqlite_path(home)
    current = schema.status(database)
    if (
        not current.integrity_ok
        or not current.schema_ok
        or current.schema_version != schema.schema_version
    ):
        raise ConfigurationError("Operational store integrity/schema gate gecmedi")
    receipt = home / RECEIPT_RELATIVE_PATH
    if not receipt.is_file():
        raise ConfigurationError("Bootstrap receipt eksik")
    receipt_document = validate_bootstrap_receipt(receipt, schema=schema)
    expected_create = FreshBootstrapPlan(
        home=home,
        core_root=core_root,
        action="create",
        authority_digest=authority_digest,
        plan_digest="",
    )
    expected_plan_digest = _plan_digest(expected_create.as_dict())
    if receipt_document["authority_digest"] != authority_digest:
        raise ConfigurationError("Bootstrap receipt authority drift")
    if receipt_document["plan_digest"] != expected_plan_digest:
        raise ConfigurationError("Bootstrap receipt create-plan binding drift")
    if receipt_document["layout_digest"] != _file_digest(home / "layout.json"):
        raise ConfigurationError("Bootstrap receipt layout digest drift")
    if receipt_document["config_digest"] != _file_digest(home / "config.yaml"):
        raise ConfigurationError("Bootstrap receipt config digest drift")


def validate_bootstrap_receipt(path: Path, *, schema: OperationalSchemaPort) -> dict[str, object]:
    """Validate receipt shape and its self-digest without trusting extra fields."""
    document = _load_exact_json(path, label="Bootstrap receipt")
    if not isinstance(document, dict):
        raise ConfigurationError("Bootstrap receipt nesne olmali")
    expected = {
        "schema",
        "status",
        "plan_digest",
        "authority_digest",
        "layout_digest",
        "config_digest",
        "operational_schema_version",
        "operational_schema_digest",
        "operational_engine",
        "initial_operational_rows",
        "legacy_postgresql_data_used",
        "network_calls",
        "docker_calls",
        "receipt_digest",
    }
    if set(document) != expected:
        raise ConfigurationError("Bootstrap receipt alanlari exact degil")
    claimed = document.pop("receipt_digest")
    if not isinstance(claimed, str):
        raise ConfigurationError("Bootstrap receipt digest tipi gecersiz")
    try:
        parse_digest(claimed)
    except ValidationFailed as exc:
        raise ConfigurationError("Bootstrap receipt digest canonical degil") from exc
    if claimed != _canonical_digest(document):
        raise ConfigurationError("Bootstrap receipt digest drift")
    document["receipt_digest"] = claimed
    if (
        document["schema"] != BOOTSTRAP_RECEIPT_SCHEMA
        or document["status"] != "completed"
        or document["operational_engine"] != "cpython-sqlite"
        or type(document["operational_schema_version"]) is not int
        or document["operational_schema_version"] != schema.schema_version
        or document["operational_schema_digest"] != schema.schema_digest
        or type(document["initial_operational_rows"]) is not int
        or document["initial_operational_rows"] != 0
        or document["legacy_postgresql_data_used"] is not False
        or type(document["network_calls"]) is not int
        or document["network_calls"] != 0
        or type(document["docker_calls"]) is not int
        or document["docker_calls"] != 0
    ):
        raise ConfigurationError("Bootstrap receipt invariant ihlali")
    for field in (
        "plan_digest",
        "authority_digest",
        "layout_digest",
        "config_digest",
        "operational_schema_digest",
    ):
        value = document[field]
        if not isinstance(value, str):
            raise ConfigurationError(f"Bootstrap receipt {field} tipi gecersiz")
        try:
            parse_digest(value)
        except ValidationFailed as exc:
            raise ConfigurationError(f"Bootstrap receipt {field} canonical degil") from exc
    return document


def plan_fresh_bootstrap(
    *,
    home: Path,
    core_root: Path,
    authority_digest: str,
    schema: OperationalSchemaPort,
) -> FreshBootstrapPlan:
    """Build a mutation-free exact plan for a new or already valid home."""
    resolved_home = home.expanduser().resolve(strict=False)
    resolved_core = core_root.expanduser().resolve(strict=False)
    parse_digest(authority_digest)
    assert_separated_from_core(resolved_home, resolved_core)
    action: BootstrapAction = "create"
    if resolved_home.exists():
        if not resolved_home.is_dir():
            raise ConfigurationError("ZEKAM_HOME regular directory olmali")
        _validate_existing(
            resolved_home,
            core_root=resolved_core,
            authority_digest=authority_digest,
            schema=schema,
        )
        action = "already-initialized"
    provisional = FreshBootstrapPlan(
        home=resolved_home,
        core_root=resolved_core,
        action=action,
        authority_digest=authority_digest,
        plan_digest="",
    )
    return FreshBootstrapPlan(
        home=provisional.home,
        core_root=provisional.core_root,
        action=provisional.action,
        authority_digest=provisional.authority_digest,
        plan_digest=_plan_digest(provisional.as_dict()),
    )


def _receipt(
    home: Path, plan: FreshBootstrapPlan, *, schema: OperationalSchemaPort
) -> dict[str, object]:
    config_path = home / "config.yaml"
    layout_path = home / "layout.json"
    document: dict[str, object] = {
        "schema": BOOTSTRAP_RECEIPT_SCHEMA,
        "status": "completed",
        "plan_digest": plan.plan_digest,
        "authority_digest": plan.authority_digest,
        "layout_digest": _file_digest(layout_path),
        "config_digest": _file_digest(config_path),
        "operational_schema_version": schema.schema_version,
        "operational_schema_digest": schema.schema_digest,
        "operational_engine": "cpython-sqlite",
        "initial_operational_rows": 0,
        "legacy_postgresql_data_used": False,
        "network_calls": 0,
        "docker_calls": 0,
    }
    document["receipt_digest"] = _canonical_digest(document)
    return document


def _recover_stale_stages(plan: FreshBootstrapPlan) -> tuple[str, ...]:
    parent = plan.home.parent
    recovered: list[str] = []
    prefix = f".{plan.home.name}.bootstrap-"
    for candidate in sorted(parent.glob(prefix + "*")):
        marker = candidate / ".bootstrap-stage.json"
        if not candidate.is_dir() or not marker.is_file():
            continue
        try:
            document = _load_exact_json(marker, label="Bootstrap stage marker")
        except ConfigurationError:
            continue
        if not isinstance(document, dict) or set(document) != {
            "schema",
            "home_name",
            "plan_digest",
        }:
            continue
        if document["schema"] != STAGE_MARKER_SCHEMA or document["home_name"] != plan.home.name:
            continue
        try:
            parse_digest(str(document["plan_digest"]))
        except (TypeError, ValueError, ValidationFailed):
            continue
        getuid = getattr(os, "getuid", None)
        if os.name != "nt" and (getuid is None or candidate.stat().st_uid != getuid()):
            continue
        quarantine = parent / f".{plan.home.name}.bootstrap-recovery"
        quarantine.mkdir(mode=0o700, exist_ok=True)
        destination = quarantine / candidate.name
        os.replace(candidate, destination)
        recovered.append(destination.name)
    return tuple(recovered)


def _lock_is_stale(lock_path: Path, plan: FreshBootstrapPlan) -> bool:
    document = _load_exact_json(lock_path, label="Bootstrap lock metadata")
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "home_name",
        "plan_digest",
        "pid",
    }:
        raise ConfigurationError("Bootstrap lock metadata exact degil")
    if document["schema"] != LOCK_SCHEMA or document["home_name"] != plan.home.name:
        raise ConfigurationError("Bootstrap lock ownership dogrulanamadi")
    if type(document["pid"]) is not int or document["pid"] < 1:
        raise ConfigurationError("Bootstrap lock PID gecersiz")
    plan_digest = document["plan_digest"]
    if not isinstance(plan_digest, str):
        raise ConfigurationError("Bootstrap lock plan digest tipi gecersiz")
    parse_digest(plan_digest)
    getuid = getattr(os, "getuid", None)
    if os.name != "nt" and (getuid is None or lock_path.stat().st_uid != getuid()):
        raise ConfigurationError("Bootstrap lock owner uyusmuyor")
    pid = document["pid"]
    if pid == os.getpid():
        # PID yeniden kullanilabilir. Bu process lock'u bu incarnation'da
        # edinmediyse ayni PID'li kayit onceki process'ten kalmistir.
        with _PROCESS_LOCK_GUARD:
            return lock_path not in _PROCESS_OWNED_LOCKS
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _acquire_bootstrap_lock(lock_path: Path, plan: FreshBootstrapPlan) -> None:
    document = {
        "schema": LOCK_SCHEMA,
        "home_name": plan.home.name,
        "plan_digest": plan.plan_digest,
        "pid": os.getpid(),
    }
    for attempt in range(2):
        descriptor, candidate_name = tempfile.mkstemp(
            prefix=f".{plan.home.name}.lock-candidate-", dir=lock_path.parent
        )
        candidate = Path(candidate_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(document, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                candidate.chmod(0o600)
            linked = False
            with _PROCESS_LOCK_GUARD:
                try:
                    os.link(candidate, lock_path)
                except FileExistsError:
                    pass
                else:
                    _PROCESS_OWNED_LOCKS.add(lock_path)
                    linked = True
            if not linked:
                if attempt == 0 and _lock_is_stale(lock_path, plan):
                    recovery = lock_path.parent / f".{plan.home.name}.bootstrap-recovery"
                    recovery.mkdir(mode=0o700, exist_ok=True)
                    stem = f"{lock_path.name}.dead-{document['pid']}-{plan.plan_digest[7:19]}"
                    destination = recovery / stem
                    sequence = 1
                    while destination.exists():
                        destination = recovery / f"{stem}-{sequence}"
                        sequence += 1
                    os.replace(lock_path, destination)
                    _fsync_directory(recovery)
                    _fsync_directory(lock_path.parent)
                    continue
                raise ConfigurationError(
                    "Fresh bootstrap baska bir canli islem tarafindan kilitli"
                ) from None
            try:
                _fsync_directory(lock_path.parent)
            except OSError:
                _release_bootstrap_lock(lock_path)
                raise
            return
        finally:
            candidate.unlink(missing_ok=True)
    raise ConfigurationError("Fresh bootstrap stale lock recovery basarisiz")


def _release_bootstrap_lock(lock_path: Path) -> None:
    """Release physical and in-process ownership as one process-local operation."""
    with _PROCESS_LOCK_GUARD:
        lock_path.unlink(missing_ok=True)
        _PROCESS_OWNED_LOCKS.discard(lock_path)


def apply_fresh_bootstrap(
    plan: FreshBootstrapPlan,
    *,
    schema: OperationalSchemaPort,
    fault_at: str | None = None,
) -> dict[str, object]:
    """Apply an exact plan with sibling staging and atomic publish."""
    if fault_at is not None and fault_at not in FAULT_POINTS:
        raise ConfigurationError("Bilinmeyen bootstrap fault point")
    current = plan_fresh_bootstrap(
        home=plan.home,
        core_root=plan.core_root,
        authority_digest=plan.authority_digest,
        schema=schema,
    )
    if current.plan_digest != plan.plan_digest or current.action != plan.action:
        raise ConfigurationError("Bootstrap plan drift tespit edildi")
    if plan.action == "already-initialized":
        return validate_bootstrap_receipt(plan.home / RECEIPT_RELATIVE_PATH, schema=schema)

    parent = plan.home.parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / f".{plan.home.name}.bootstrap.lock"
    _acquire_bootstrap_lock(lock_path, plan)
    stage: Path | None = None
    try:
        recovered = _recover_stale_stages(plan)
        stage = Path(tempfile.mkdtemp(prefix=f".{plan.home.name}.bootstrap-", dir=parent))
        HomeLayout(stage).ensure()
        _write_json(
            stage / ".bootstrap-stage.json",
            {
                "schema": STAGE_MARKER_SCHEMA,
                "home_name": plan.home.name,
                "plan_digest": plan.plan_digest,
            },
        )
        if fault_at == "after-layout":
            raise OSError("injected-after-layout")
        _write_config(stage)
        if fault_at == "after-config":
            raise OSError("injected-after-config")
        database_path = stage / OPERATIONAL_RELATIVE_PATH
        schema.bootstrap(database_path)
        if os.name != "nt":
            database_path.chmod(0o600)
        _fsync_file(database_path)
        if fault_at == "after-database":
            raise OSError("injected-after-database")
        _write_json(
            stage / BACKUP_CONTRACT_RELATIVE_PATH,
            {
                "schema": BACKUP_CONTRACT_SCHEMA,
                "authority": OPERATIONAL_RELATIVE_PATH,
                "method": "sqlite-backup-api",
                "destination": "state/backups",
                "restore_requires": ["integrity_check", "schema_digest", "logical_parity"],
            },
        )
        # Receipt is computed against final relative content; staging root is not serialized.
        staged_plan = FreshBootstrapPlan(
            home=stage,
            core_root=plan.core_root,
            action=plan.action,
            authority_digest=plan.authority_digest,
            plan_digest=plan.plan_digest,
        )
        _write_json(
            stage / RECEIPT_RELATIVE_PATH,
            _receipt(stage, staged_plan, schema=schema),
        )
        (stage / ".bootstrap-stage.json").unlink()
        try:
            restrict_private_tree(stage)
        except OSError as exc:
            raise ConfigurationError("Fresh bootstrap private path policy failed") from exc
        if fault_at == "before-publish":
            raise OSError("injected-before-publish")
        _fsync_tree(stage)
        try:
            os.rename(stage, plan.home)
        except FileExistsError:
            _validate_existing(
                plan.home,
                core_root=plan.core_root,
                authority_digest=plan.authority_digest,
                schema=schema,
            )
            raise ConfigurationError(
                "Bootstrap concurrent publish ile tamamlandi; yeniden dene"
            ) from None
        stage = None
        _fsync_directory(parent)
        result = validate_bootstrap_receipt(plan.home / RECEIPT_RELATIVE_PATH, schema=schema)
        result["recovered_stages"] = list(recovered)
        return result
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
        _release_bootstrap_lock(lock_path)
