"""Atomic, content-verified backup bundles for the composed local stores."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from zekam.application.composition import build_context
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_core_services import (
    LocalCoreServices,
    validate_local_sqlite_store,
)

BUNDLE_SCHEMA = "zekam-local-backup-bundle/v1"
MAX_FILES = 100_000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024

_DATABASES = (
    "state/operational.db",
    "state/learning.db",
    "state/improvement.db",
    "yerel/source-authority.sqlite3",
    "modeller/registry/models.db",
    "benchmarklar/benchmark.db",
    "modeller/routing/routing.db",
)
_TREES = (
    "artifacts",
    "knowledge-index",
    "benchmarklar/artifacts",
    "analytics/raw",
    "analytics/manifests",
    "analytics/generations",
    "analytics/reports",
    "analytics/receipts",
    "analytics/quarantine",
    "projeler",
    "runtime/spool",
    "runtime/outbox",
    "runtime/recovery",
)
_FILES = ("config.yaml", "layout.json", "bootstrap.json", "analytics/CURRENT")
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_DATABASE_NAMES = {
    "state/operational.db": "operational",
    "state/learning.db": "learning",
    "state/improvement.db": "improvement",
    "yerel/source-authority.sqlite3": "source_authority",
    "modeller/registry/models.db": "registry",
    "benchmarklar/benchmark.db": "benchmark",
    "modeller/routing/routing.db": "routing",
}
_REQUIRED_DATABASES = frozenset(
    {
        "state/operational.db",
        "state/learning.db",
        "state/improvement.db",
        "modeller/registry/models.db",
        "benchmarklar/benchmark.db",
        "modeller/routing/routing.db",
    }
)
_REQUIRED_DIRECTORIES = frozenset(
    {
        "state",
        "yerel",
        "artifacts",
        "artifacts/sha256",
        "knowledge-index",
        "knowledge-index/exact",
        "knowledge-index/lexical",
        "knowledge-index/vector",
        "knowledge-index/manifests",
        "knowledge-index/snapshots",
        "knowledge-index/quarantine",
        "modeller",
        "modeller/registry",
        "modeller/routing",
        "benchmarklar",
        "benchmarklar/artifacts",
        "analytics",
        "analytics/raw",
        "analytics/manifests",
        "analytics/generations",
        "analytics/reports",
        "analytics/receipts",
        "analytics/quarantine",
        "runtime",
        "runtime/spool",
        "runtime/outbox",
        "runtime/recovery",
        "projeler",
    }
)
_REQUIRED_FILES = frozenset({"config.yaml", "layout.json"})


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _safe_relative(value: object) -> str:
    if type(value) is not str or not value or len(value.encode()) > 4096:
        raise ValidationFailed("Backup relative path invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValidationFailed("Backup relative path escapes bundle")
    return value


def _regular(path: Path) -> os.stat_result:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise PolicyViolation("Backup source must be private owned regular file")
    return info


def _destination(path: Path) -> Path:
    if not path.is_absolute() or not path.parent.is_dir() or path.exists() or path.is_symlink():
        raise ValidationFailed("Backup destination must be new absolute path")
    return path


def _sync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_regular(source: Path, target: Path, mode: int) -> None:
    before = _regular(source)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_bytes(source.read_bytes())
    os.chmod(target, mode)
    _sync_file(target)
    after = _regular(source)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise PolicyViolation("Backup source changed during capture")


def _snapshot_database(source: Path, target: Path, mode: int) -> None:
    info = _regular(source)
    if info.st_size > MAX_DATABASE_BYTES:
        raise PolicyViolation("Backup database exceeds bound")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_db = sqlite3.connect(f"{source.resolve(strict=True).as_uri()}?mode=ro", uri=True)
    target_db = sqlite3.connect(target)
    try:
        source_db.backup(target_db)
        target_db.commit()
        result = target_db.execute("pragma integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise PolicyViolation("Backup database snapshot is not integral")
    finally:
        target_db.close()
        source_db.close()
    os.chmod(target, mode)
    if _regular(target).st_size > MAX_DATABASE_BYTES:
        raise PolicyViolation("Backup database snapshot exceeds bound")
    _sync_file(target)


def _sources(home: Path) -> tuple[tuple[str, Path, str], ...]:
    database_set = set(_DATABASES)
    found: dict[str, tuple[Path, str]] = {}
    for relative in _DATABASES:
        path = home / relative
        if path.exists() or path.is_symlink():
            found[relative] = (path, "sqlite")
    for relative in _FILES:
        path = home / relative
        if path.exists() or path.is_symlink():
            found[relative] = (path, "file")
    for root_name in _TREES:
        root = home / root_name
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise PolicyViolation("Backup source tree invalid")
        for path in root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = path.relative_to(home).as_posix()
            if relative in database_set or relative.endswith(_SIDECAR_SUFFIXES):
                continue
            found[relative] = (path, "file")
    if len(found) > MAX_FILES:
        raise PolicyViolation("Backup file count exceeds bound")
    return tuple((name, *found[name]) for name in sorted(found))


def _allowed_regular_path(relative: str) -> bool:
    return relative in _FILES or any(relative.startswith(f"{root}/") for root in _TREES)


def _directories(
    home: Path, files: tuple[tuple[str, Path, str], ...]
) -> tuple[dict[str, object], ...]:
    names = {
        parent.as_posix()
        for relative, _, _ in files
        for parent in PurePosixPath(relative).parents
        if parent.as_posix() != "."
    }
    names.update(relative for relative in _TREES if (home / relative).is_dir())
    names.update(relative for relative in _REQUIRED_DIRECTORIES if (home / relative).is_dir())
    names.update(
        parent.as_posix()
        for relative in tuple(names)
        for parent in PurePosixPath(relative).parents
        if parent.as_posix() != "."
    )
    result: list[dict[str, object]] = []
    for relative in sorted(names):
        path = home / relative
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise PolicyViolation("Backup source directory must be private and owned")
        result.append({"path": relative, "mode": stat.S_IMODE(info.st_mode)})
    return tuple(result)


def create_bundle(services: LocalCoreServices, home: Path, destination: Path) -> dict[str, Any]:
    """Capture one consistent, independently verifiable local backup bundle."""

    if not services.status()["all_ready"]:
        raise PolicyViolation("Local core stores are not ready for backup")
    destination = _destination(destination)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    os.chmod(staging, 0o700)
    entries: list[dict[str, object]] = []
    total = 0
    try:
        sources = _sources(home)
        directories = _directories(home, sources)
        source_names = {relative for relative, _, _ in sources}
        directory_names = {str(entry["path"]) for entry in directories}
        if not source_names >= (_REQUIRED_DATABASES | _REQUIRED_FILES):
            raise PolicyViolation("Backup mandatory file set incomplete")
        if not directory_names >= _REQUIRED_DIRECTORIES:
            raise PolicyViolation("Backup mandatory directory set incomplete")
        for entry in directories:
            path = staging / str(entry["path"])
            mode = entry["mode"]
            assert type(mode) is int
            path.mkdir(parents=True, exist_ok=True, mode=mode)
            os.chmod(path, mode)
        for relative, source, kind in sources:
            info = _regular(source)
            mode = stat.S_IMODE(info.st_mode)
            target = staging / relative
            if kind == "sqlite":
                _snapshot_database(source, target, mode)
                validate_local_sqlite_store(_DATABASE_NAMES[relative], target)
            else:
                _copy_regular(source, target, mode)
            copied = _regular(target)
            total += copied.st_size
            if total > MAX_TOTAL_BYTES:
                raise PolicyViolation("Backup aggregate size exceeds bound")
            entries.append(
                {
                    "path": relative,
                    "kind": kind,
                    "mode": mode,
                    "size_bytes": copied.st_size,
                    "sha256": _sha256(target),
                }
            )
        body: dict[str, Any] = {
            "schema": BUNDLE_SCHEMA,
            "directories": list(directories),
            "entries": entries,
            "file_count": len(entries),
            "total_bytes": total,
            "grants_authority": False,
        }
        document = {**body, "manifest_digest": digest(body)}
        manifest = staging / "MANIFEST.json"
        manifest.write_bytes(canonical_json(document).encode())
        os.chmod(manifest, 0o400)
        _sync_file(manifest)
        for directory in sorted(
            (item for item in staging.rglob("*") if item.is_dir()), reverse=True
        ):
            _sync_directory(directory)
        _sync_directory(staging)
        os.replace(staging, destination)
        _sync_directory(destination.parent)
        return document
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _strict_document(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > 16 * 1024 * 1024:
        raise ValidationFailed("Backup manifest size invalid")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValidationFailed("Backup manifest duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Backup manifest JSON invalid") from exc
    if type(value) is not dict or canonical_json(value).encode() != raw:
        raise ValidationFailed("Backup manifest is not canonical")
    return value


def verify_bundle(bundle: Path) -> dict[str, Any]:
    """Verify exact manifest, census, content, modes, and SQLite integrity."""

    if not bundle.is_absolute() or bundle.is_symlink() or not bundle.is_dir():
        raise ValidationFailed("Backup bundle path invalid")
    bundle_info = bundle.lstat()
    if bundle_info.st_uid != os.geteuid() or stat.S_IMODE(bundle_info.st_mode) != 0o700:
        raise PolicyViolation("Backup bundle root identity invalid")
    manifest_path = bundle / "MANIFEST.json"
    manifest_info = _regular(manifest_path)
    if stat.S_IMODE(manifest_info.st_mode) != 0o400:
        raise PolicyViolation("Backup manifest mode invalid")
    document = _strict_document(manifest_path.read_bytes())
    keys = {
        "schema",
        "directories",
        "entries",
        "file_count",
        "total_bytes",
        "grants_authority",
        "manifest_digest",
    }
    if set(document) != keys or document.get("schema") != BUNDLE_SCHEMA:
        raise ValidationFailed("Backup manifest schema invalid")
    body = dict(document)
    manifest_digest = body.pop("manifest_digest", None)
    if type(manifest_digest) is not str or digest(body) != manifest_digest:
        raise ValidationFailed("Backup manifest digest invalid")
    entries = document.get("entries")
    directories = document.get("directories")
    if type(directories) is not list or len(directories) > MAX_FILES:
        raise ValidationFailed("Backup directory census invalid")
    directory_names: list[str] = []
    for entry in directories:
        if type(entry) is not dict or set(entry) != {"path", "mode"}:
            raise ValidationFailed("Backup directory schema invalid")
        relative = _safe_relative(entry["path"])
        if type(entry["mode"]) is not int or entry["mode"] & 0o022:
            raise ValidationFailed("Backup directory mode invalid")
        info = (bundle / relative).lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != entry["mode"]
        ):
            raise PolicyViolation("Backup directory drift")
        directory_names.append(relative)
    if directory_names != sorted(directory_names) or len(directory_names) != len(
        set(directory_names)
    ):
        raise PolicyViolation("Backup directory census drift")
    if not set(directory_names) >= _REQUIRED_DIRECTORIES:
        raise PolicyViolation("Backup mandatory directory set incomplete")
    if type(entries) is not list or len(entries) > MAX_FILES:
        raise ValidationFailed("Backup entry census invalid")
    names: list[str] = []
    total = 0
    for entry in entries:
        if type(entry) is not dict or set(entry) != {
            "path",
            "kind",
            "mode",
            "size_bytes",
            "sha256",
        }:
            raise ValidationFailed("Backup entry schema invalid")
        relative = _safe_relative(entry["path"])
        if entry["kind"] not in {"file", "sqlite"}:
            raise ValidationFailed("Backup entry kind invalid")
        if type(entry["mode"]) is not int or entry["mode"] & 0o022:
            raise ValidationFailed("Backup entry mode invalid")
        if type(entry["size_bytes"]) is not int or entry["size_bytes"] < 0:
            raise ValidationFailed("Backup entry size invalid")
        if type(entry["sha256"]) is not str or len(entry["sha256"]) != 64:
            raise ValidationFailed("Backup entry digest invalid")
        path = bundle / relative
        expected_kind = "sqlite" if relative in _DATABASE_NAMES else "file"
        if entry["kind"] != expected_kind or (
            expected_kind == "file" and not _allowed_regular_path(relative)
        ):
            raise PolicyViolation("Backup path/kind contract drift")
        info = _regular(path)
        if (
            stat.S_IMODE(info.st_mode) != entry["mode"]
            or info.st_size != entry["size_bytes"]
            or _sha256(path) != entry["sha256"]
        ):
            raise PolicyViolation("Backup entry drift")
        if entry["kind"] == "sqlite":
            try:
                name = _DATABASE_NAMES[relative]
            except KeyError as exc:
                raise ValidationFailed("Backup SQLite path contract invalid") from exc
            validate_local_sqlite_store(name, path)
        names.append(relative)
        total += info.st_size
    if not set(names) >= (_REQUIRED_DATABASES | _REQUIRED_FILES):
        raise PolicyViolation("Backup mandatory file set incomplete")
    actual = sorted(
        path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if not path.is_dir()
    )
    actual_directories = sorted(
        path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if path.is_dir()
    )
    if (
        names != sorted(names)
        or len(names) != len(set(names))
        or actual != ["MANIFEST.json", *names]
    ):
        raise PolicyViolation("Backup file census drift")
    if actual_directories != directory_names:
        raise PolicyViolation("Backup directory census drift")
    if (
        type(document.get("file_count")) is not int
        or document["file_count"] != len(names)
        or type(document.get("total_bytes")) is not int
        or document["total_bytes"] != total
    ):
        raise PolicyViolation("Backup manifest totals drift")
    if document.get("grants_authority") is not False:
        raise PolicyViolation("Backup manifest cannot grant authority")
    return document


def restore_bundle(bundle: Path, target: Path) -> dict[str, Any]:
    """Restore to a new disposable target and atomically publish it."""

    document = verify_bundle(bundle)
    target = _destination(target)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    os.chmod(staging, 0o700)
    try:
        for entry in document["directories"]:
            directory = staging / _safe_relative(entry["path"])
            directory.mkdir(parents=True, exist_ok=True, mode=int(entry["mode"]))
            os.chmod(directory, int(entry["mode"]))
        for entry in document["entries"]:
            relative = _safe_relative(entry["path"])
            _copy_regular(bundle / relative, staging / relative, int(entry["mode"]))
            if entry["kind"] == "sqlite":
                validate_local_sqlite_store(_DATABASE_NAMES[relative], staging / relative)
        for directory in sorted(
            (item for item in staging.rglob("*") if item.is_dir()), reverse=True
        ):
            _sync_directory(directory)
        _sync_directory(staging)
        restored = create_manifest_for_restored(staging)
        if (
            restored["entries"] != document["entries"]
            or restored["directories"] != document["directories"]
        ):
            raise PolicyViolation("Restored backup differs from manifest")
        restored_services = LocalCoreServices.from_context(build_context(home=str(staging)))
        if not restored_services.status()["all_ready"]:
            raise PolicyViolation("Restored local service graph is not ready")
        os.replace(staging, target)
        _sync_directory(target.parent)
        return {
            "schema": "zekam-local-backup-restore-receipt/v1",
            "manifest_digest": document["manifest_digest"],
            "file_count": document["file_count"],
            "total_bytes": document["total_bytes"],
            "status": "restored",
            "grants_authority": False,
        }
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def create_manifest_for_restored(home: Path) -> dict[str, Any]:
    """Read-only exact census used only to verify the newly restored target."""

    entries: list[dict[str, object]] = []
    total = 0
    sources = _sources(home)
    directories = _directories(home, sources)
    for relative, source, kind in sources:
        info = _regular(source)
        total += info.st_size
        entries.append(
            {
                "path": relative,
                "kind": kind,
                "mode": stat.S_IMODE(info.st_mode),
                "size_bytes": info.st_size,
                "sha256": _sha256(source),
            }
        )
    return {
        "directories": list(directories),
        "entries": entries,
        "file_count": len(entries),
        "total_bytes": total,
    }
