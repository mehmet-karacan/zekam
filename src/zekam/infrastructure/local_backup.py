"""Atomic, content-verified backup bundles for the composed local stores."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from zekam.application.composition import build_context
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_core_services import (
    LocalCoreServices,
    validate_local_sqlite_store,
)
from zekam.infrastructure.local_file_security import (
    owned_regular,
    path_or_parent_is_link_or_reparse,
    private_directory,
    private_regular,
    restrict_private_tree,
)

LEGACY_BUNDLE_SCHEMA = "zekam-local-backup-bundle/v1"
BUNDLE_SCHEMA = "zekam-local-backup-bundle/v2"
MAX_FILES = 100_000
MAX_DIRECTORIES = 100_000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024

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
_FILES = (
    "config.yaml",
    "layout.json",
    "bootstrap.json",
    "analytics/CURRENT",
    "config/git-publication-trust.json",
)
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
    if (
        type(value) is not str
        or not value
        or value == "."
        or "\x00" in value
        or "\\" in value
        or ":" in value
        or len(value.encode()) > 4096
    ):
        raise ValidationFailed("Backup relative path invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or PureWindowsPath(value).drive
        or ".." in path.parts
        or str(path) != value
    ):
        raise ValidationFailed("Backup relative path escapes bundle")
    return value


def _regular(path: Path) -> os.stat_result:
    info = path.lstat()
    if not owned_regular(path):
        raise PolicyViolation("Backup source must be private owned regular file")
    return info


def _portable_file_mode(info: os.stat_result) -> int:
    if os.name != "nt":
        return stat.S_IMODE(info.st_mode)
    readonly = bool(
        int(getattr(info, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_READONLY", 0))
    )
    return 0o400 if readonly else 0o600


def _destination(path: Path) -> Path:
    if (
        not path.is_absolute()
        or not path.parent.is_dir()
        or path.exists()
        or path.is_symlink()
        or path_or_parent_is_link_or_reparse(path)
        or not private_directory(path.parent)
    ):
        raise ValidationFailed("Backup destination must be new absolute path")
    return path


def _sync_file(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_regular(source: Path, target: Path, mode: int) -> None:
    before = _regular(source)
    if before.st_size > MAX_FILE_BYTES:
        raise PolicyViolation("Backup file exceeds bound")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied = 0
    with source.open("rb") as source_stream, target.open("wb") as target_stream:
        while chunk := source_stream.read(1024 * 1024):
            copied += len(chunk)
            if copied > MAX_FILE_BYTES:
                raise PolicyViolation("Backup file exceeds bound")
            target_stream.write(chunk)
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


def _snapshot_database(
    source: Path, target: Path, mode: int, *, relative: str | None = None
) -> None:
    info = _regular(source)
    if info.st_size > MAX_DATABASE_BYTES:
        raise PolicyViolation("Backup database exceeds bound")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_db = sqlite3.connect(f"{source.resolve(strict=True).as_uri()}?mode=ro", uri=True)
    target_db = sqlite3.connect(target)
    try:
        source_db.backup(target_db)
        target_db.commit()
        if target_db.execute("pragma journal_mode=delete").fetchone() != ("delete",):
            raise PolicyViolation("Backup snapshot self-contained DELETE journal ister")
        if relative is not None:
            _load_backup_sqlite_extension(relative, target_db)
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


def _walk_tree_files(root: Path) -> Iterator[Path]:
    """Yield private tree files without following links or buffering a full tree."""

    pending = [root]
    directory_count = 1
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    target = Path(entry.path)
                    try:
                        info = target.lstat()
                        is_reparse = bool(
                            int(getattr(info, "st_file_attributes", 0))
                            & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
                        )
                        if stat.S_ISLNK(info.st_mode) or is_reparse:
                            raise PolicyViolation("Backup source tree cannot contain links")
                        if entry.is_dir(follow_symlinks=False):
                            directory_count += 1
                            if directory_count > MAX_DIRECTORIES:
                                raise PolicyViolation("Backup directory count exceeds bound")
                            pending.append(target)
                        elif entry.is_file(follow_symlinks=False):
                            yield target
                        else:
                            raise PolicyViolation("Backup source tree entry must be regular")
                    except OSError as exc:
                        raise PolicyViolation("Backup source tree entry unreadable") from exc
        except OSError as exc:
            raise PolicyViolation("Backup source tree unreadable") from exc


def _walk_tree_entries(root: Path) -> Iterator[tuple[Path, bool]]:
    """Yield bounded bundle entries without following links or buffering a tree."""

    pending = [root]
    directory_count = 1
    file_count = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    target = Path(entry.path)
                    try:
                        info = target.lstat()
                        is_reparse = bool(
                            int(getattr(info, "st_file_attributes", 0))
                            & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
                        )
                        if stat.S_ISLNK(info.st_mode) or is_reparse:
                            raise PolicyViolation("Backup tree cannot contain links")
                        if entry.is_dir(follow_symlinks=False):
                            directory_count += 1
                            if directory_count > MAX_DIRECTORIES:
                                raise ValidationFailed("Backup directory count exceeds bound")
                            pending.append(target)
                            yield target, True
                        elif entry.is_file(follow_symlinks=False):
                            file_count += 1
                            if file_count > MAX_FILES:
                                raise ValidationFailed("Backup file count exceeds bound")
                            yield target, False
                        else:
                            raise PolicyViolation("Backup tree entry must be regular")
                    except OSError as exc:
                        raise PolicyViolation("Backup tree entry unreadable") from exc
        except OSError as exc:
            raise PolicyViolation("Backup tree unreadable") from exc


def _project_sqlite_path(relative: str) -> bool:
    """Only owned store locations, never CAS objects or arbitrary attachments."""
    parts = PurePosixPath(relative).parts
    return (
        (
            len(parts) == 4
            and parts[0] == "projeler"
            and parts[2:] == ("runtime", "provider-ledger.sqlite3")
        )
        or (
            len(parts) == 5
            and parts[:3] == ("knowledge-index", "vector", "opencode-bge-m3")
            and parts[4] in {"knowledge.sqlite3", "vector-cache.sqlite3"}
        )
        or (
            len(parts) == 4
            and parts[:2] == ("knowledge-index", "graph")
            and parts[3] == "code-graph.sqlite3"
        )
    )


def _load_backup_sqlite_extension(relative: str, db: sqlite3.Connection) -> None:
    if _project_sqlite_path(relative) and PurePosixPath(relative).name == "knowledge.sqlite3":
        import sqlite_vec

        db.enable_load_extension(True)
        try:
            sqlite_vec.load(db)
        finally:
            db.enable_load_extension(False)


def _validate_bundle_sqlite(relative: str, path: Path) -> None:
    if relative in _DATABASE_NAMES:
        validate_local_sqlite_store(_DATABASE_NAMES[relative], path)
        return
    if not _project_sqlite_path(relative):
        raise PolicyViolation("Backup SQLite path contract invalid")
    if _regular(path).st_size > MAX_DATABASE_BYTES:
        raise PolicyViolation("Backup project SQLite exceeds bound")
    with sqlite3.connect(
        f"{path.resolve(strict=True).as_uri()}?mode=ro&immutable=1", uri=True, timeout=5
    ) as db:
        db.execute("pragma query_only=on")
        _load_backup_sqlite_extension(relative, db)
        if (
            db.execute("pragma integrity_check").fetchone() != ("ok",)
            or db.execute("pragma foreign_key_check").fetchone() is not None
        ):
            raise PolicyViolation("Backup project SQLite integrity invalid")


def _sources(
    home: Path, *, bundle_schema: str = BUNDLE_SCHEMA
) -> tuple[tuple[str, Path, str], ...]:
    database_set = set(_DATABASES)
    found: dict[str, tuple[Path, str]] = {}

    def add(relative: str, path: Path, kind: str) -> None:
        found[relative] = (path, kind)
        if len(found) > MAX_FILES:
            raise PolicyViolation("Backup file count exceeds bound")

    for relative in _DATABASES:
        path = home / relative
        if path.exists() or path.is_symlink():
            add(relative, path, "sqlite")
    for relative in _FILES:
        path = home / relative
        if path.exists() or path.is_symlink():
            add(relative, path, "file")
    for root_name in _TREES:
        root = home / root_name
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise PolicyViolation("Backup source tree invalid")
        for path in _walk_tree_files(root):
            relative = path.relative_to(home).as_posix()
            if relative in database_set or relative.endswith(_SIDECAR_SUFFIXES):
                continue
            add(
                relative,
                path,
                "sqlite"
                if bundle_schema == BUNDLE_SCHEMA and _project_sqlite_path(relative)
                else "file",
            )
    return tuple((name, *found[name]) for name in sorted(found))


def _source_census(
    sources: tuple[tuple[str, Path, str], ...],
) -> dict[str, tuple[str, int, int]]:
    """Capture bounded source identity without treating mtime as drift."""

    census: dict[str, tuple[str, int, int]] = {}
    for relative, path, kind in sources:
        info = _regular(path)
        census[relative] = (kind, int(info.st_dev), int(info.st_ino))
    return census


def _assert_source_census_unchanged(
    before: dict[str, tuple[str, int, int]],
    after: dict[str, tuple[str, int, int]],
) -> None:
    if before.keys() != after.keys():
        raise PolicyViolation("Backup source census drift")
    if any(before[relative] != after[relative] for relative in before):
        raise PolicyViolation("Backup source identity drift")


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
        if not private_directory(path):
            raise PolicyViolation("Backup source directory must be private and owned")
        mode = 0o700 if os.name == "nt" else stat.S_IMODE(path.lstat().st_mode)
        result.append({"path": relative, "mode": mode})
    return tuple(result)


def create_bundle(services: LocalCoreServices, home: Path, destination: Path) -> dict[str, Any]:
    """Capture one consistent, independently verifiable local backup bundle."""

    if not services.status()["all_ready"]:
        raise PolicyViolation("Local core stores are not ready for backup")
    destination = _destination(destination)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    restrict_private_tree(staging)
    entries: list[dict[str, object]] = []
    total = 0
    try:
        sources = _sources(home)
        source_census = _source_census(sources)
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
            mode = _portable_file_mode(info)
            target = staging / relative
            if kind == "sqlite":
                _snapshot_database(source, target, mode, relative=relative)
                _validate_bundle_sqlite(relative, target)
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
        _assert_source_census_unchanged(source_census, _source_census(_sources(home)))
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
            (item for item, is_directory in _walk_tree_entries(staging) if is_directory),
            key=lambda item: item.as_posix(),
            reverse=True,
        ):
            _sync_directory(directory)
        _sync_directory(staging)
        # Manifest serialization and fsync can take a while for a large bundle.
        # Recheck immediately before publication so a file added/removed or
        # replaced during that window cannot be silently omitted from the bundle.
        _assert_source_census_unchanged(source_census, _source_census(_sources(home)))
        os.replace(staging, destination)
        _sync_directory(destination.parent)
        return document
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _strict_document(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
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
    if not private_directory(bundle):
        raise PolicyViolation("Backup bundle root identity invalid")
    manifest_path = bundle / "MANIFEST.json"
    _regular(manifest_path)
    if not private_regular(manifest_path, 0o400):
        raise PolicyViolation("Backup manifest mode invalid")
    with manifest_path.open("rb") as stream:
        manifest_raw = stream.read(MAX_MANIFEST_BYTES + 1)
    document = _strict_document(manifest_raw)
    keys = {
        "schema",
        "directories",
        "entries",
        "file_count",
        "total_bytes",
        "grants_authority",
        "manifest_digest",
    }
    if set(document) != keys or document.get("schema") not in {BUNDLE_SCHEMA, LEGACY_BUNDLE_SCHEMA}:
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
        directory = bundle / relative
        if not private_directory(directory) or (
            os.name != "nt" and stat.S_IMODE(directory.lstat().st_mode) != entry["mode"]
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
        expected_kind = (
            "sqlite"
            if relative in _DATABASE_NAMES
            or (document["schema"] == BUNDLE_SCHEMA and _project_sqlite_path(relative))
            else "file"
        )
        if entry["kind"] != expected_kind or (
            expected_kind == "file" and not _allowed_regular_path(relative)
        ):
            raise PolicyViolation("Backup path/kind contract drift")
        info = _regular(path)
        if (
            _portable_file_mode(info) != entry["mode"]
            or info.st_size != entry["size_bytes"]
            or _sha256(path) != entry["sha256"]
        ):
            raise PolicyViolation("Backup entry drift")
        if entry["kind"] == "sqlite":
            _validate_bundle_sqlite(relative, path)
        names.append(relative)
        total += info.st_size
    if not set(names) >= (_REQUIRED_DATABASES | _REQUIRED_FILES):
        raise PolicyViolation("Backup mandatory file set incomplete")
    actual_files: list[str] = []
    actual_directory_names: list[str] = []
    for path, is_directory in _walk_tree_entries(bundle):
        relative = path.relative_to(bundle).as_posix()
        if is_directory:
            actual_directory_names.append(relative)
        else:
            actual_files.append(relative)
    actual = sorted(actual_files)
    actual_directories = sorted(actual_directory_names)
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
    restrict_private_tree(staging)
    try:
        for entry in document["directories"]:
            directory = staging / _safe_relative(entry["path"])
            directory.mkdir(parents=True, exist_ok=True, mode=int(entry["mode"]))
            os.chmod(directory, int(entry["mode"]))
        for entry in document["entries"]:
            relative = _safe_relative(entry["path"])
            _copy_regular(bundle / relative, staging / relative, int(entry["mode"]))
            if entry["kind"] == "sqlite":
                _validate_bundle_sqlite(relative, staging / relative)
        for directory in sorted(
            (item for item, is_directory in _walk_tree_entries(staging) if is_directory),
            key=lambda item: item.as_posix(),
            reverse=True,
        ):
            _sync_directory(directory)
        _sync_directory(staging)
        restored = create_manifest_for_restored(staging, bundle_schema=document["schema"])
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
            "bundle_schema": document["schema"],
            "project_sqlite_snapshot_contract": document["schema"] == BUNDLE_SCHEMA,
            "grants_authority": False,
        }
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def create_manifest_for_restored(
    home: Path, *, bundle_schema: str = BUNDLE_SCHEMA
) -> dict[str, Any]:
    """Read-only exact census used only to verify the newly restored target."""

    entries: list[dict[str, object]] = []
    total = 0
    sources = _sources(home, bundle_schema=bundle_schema)
    directories = _directories(home, sources)
    for relative, source, kind in sources:
        info = _regular(source)
        total += info.st_size
        entries.append(
            {
                "path": relative,
                "kind": kind,
                "mode": _portable_file_mode(info),
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
