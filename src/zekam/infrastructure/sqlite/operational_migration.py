"""Explicit externally admitted, verified operational schema-v3 to v4 migration.

V4 remains dormant: this boundary installs schema only and never creates an
attachment, process generation, receipt, hook, decoder, or runtime writer.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.operational_store import OperationalSchemaStatus
from zekam.domain.errors import ConcurrencyConflict, ConfigurationError
from zekam.infrastructure.local_file_security import (
    private_directory,
    private_regular,
    restrict_private_file,
)
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.operational_backup import (
    SQLiteOperationalBackup,
    logical_database_digest,
)

_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_OPEN_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_OPEN_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_LOCK_BYTES = 1
_PATH_TYPE = type(Path())


def _effective_user_id() -> int:
    """Return the POSIX effective uid without exposing it to Windows typing."""

    getter = getattr(os, "getuid", None)
    if not callable(getter):
        raise ConfigurationError("Operational migration POSIX owner identity unavailable")
    return int(getter())


class OperationalMigrationAdmission(Protocol):
    """Trusted outer coordinator; implementations own actual writer admission."""

    def stop_new_admission(self) -> None: ...

    def drain_and_reap(self) -> None: ...

    def assert_no_admitted_authority(self) -> None: ...

    def release_admission(self) -> None: ...

    def mark_recovery_required(self) -> None: ...

    def trusted_home(self) -> Path: ...


@dataclass(frozen=True, slots=True)
class MigrationSpoolTarget:
    home: Path
    client_id: str
    session_id: str
    external_session_id: str

    def __post_init__(self) -> None:
        if type(self.home) is not _PATH_TYPE or not self.home.is_absolute():
            raise ConfigurationError("Operational migration spool home must be absolute")
        for value in (self.client_id, self.session_id, self.external_session_id):
            try:
                size = len(value.encode("utf-8")) if type(value) is str else 0
            except UnicodeEncodeError as exc:
                raise ConfigurationError("Operational migration spool target invalid") from exc
            if type(value) is not str or not value or size > 512:
                raise ConfigurationError("Operational migration spool target invalid")


@dataclass(frozen=True, slots=True)
class OperationalMigrationReceipt:
    status: OperationalSchemaStatus
    backup_path: Path
    source_v3_logical_digest: str
    source_v3_original_digest: str
    source_v3_size_bytes: int


@contextmanager
def _directory_anchor(path: Path) -> Iterator[int]:
    if not path.is_absolute():
        raise ConfigurationError("Operational migration parent path must be absolute")
    descriptor = os.open("/", os.O_RDONLY | _OPEN_DIRECTORY | _OPEN_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | _OPEN_DIRECTORY | _OPEN_NOFOLLOW | _OPEN_NONBLOCK,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise ConfigurationError("Operational migration parent path unsafe") from exc
            opened = os.fstat(next_descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_descriptor)
                raise ConfigurationError("Operational migration parent path unsafe")
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    finally:
        os.close(descriptor)


def _assert_anchor_path(path: Path, parent_descriptor: int) -> None:
    try:
        anchored = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        lexical = os.lstat(path)
    except OSError as exc:
        raise ConfigurationError("Operational migration anchored path unavailable") from exc
    if (anchored.st_dev, anchored.st_ino, stat.S_IFMT(anchored.st_mode)) != (
        lexical.st_dev,
        lexical.st_ino,
        stat.S_IFMT(lexical.st_mode),
    ):
        raise ConfigurationError("Operational migration ancestor/path identity drift")


def _assert_safe_parent_chain(path: Path) -> None:
    with _directory_anchor(path.parent) as parent_descriptor:
        try:
            info = os.fstat(parent_descriptor)
        except OSError as exc:
            raise ConfigurationError("Operational migration parent path unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ConfigurationError("Operational migration parent path unsafe")


def _identity(
    path: Path, *, parent_descriptor: int | None = None
) -> tuple[int, int, int, int, int]:
    if not path.is_absolute() or path.is_symlink():
        raise ConfigurationError("Operational migration path must be absolute regular file")
    if parent_descriptor is None:
        with _directory_anchor(path.parent) as anchored:
            return _identity(path, parent_descriptor=anchored)
    _assert_anchor_path(path, parent_descriptor)
    flags = os.O_RDONLY | _OPEN_NOFOLLOW | _OPEN_NONBLOCK
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise ConfigurationError("Operational migration path identity unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
            stat.S_IFMT(opened.st_mode),
            opened.st_uid,
            opened.st_nlink,
        ) != (
            current.st_dev,
            current.st_ino,
            stat.S_IFMT(current.st_mode),
            current.st_uid,
            current.st_nlink,
        ):
            raise ConfigurationError("Operational migration path identity drift")
        if opened.st_nlink != 1 or opened.st_uid != _effective_user_id():
            raise ConfigurationError("Operational migration path ownership/link unsafe")
        return (
            opened.st_dev,
            opened.st_ino,
            stat.S_IFMT(opened.st_mode),
            opened.st_uid,
            opened.st_nlink,
        )
    finally:
        os.close(descriptor)


@contextmanager
def _migration_lock(path: Path, *, parent_descriptor: int | None = None) -> Iterator[None]:
    if not path.is_absolute() or path.is_symlink():
        raise ConfigurationError("Operational migration lock path invalid")
    if parent_descriptor is None:
        with (
            _directory_anchor(path.parent) as anchored,
            _migration_lock(path, parent_descriptor=anchored),
        ):
            yield
        return
    try:
        existing = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise ConfigurationError("Operational migration lock unavailable") from exc
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode)
        or existing.st_nlink != 1
        or existing.st_uid != _effective_user_id()
        or existing.st_size > _LOCK_BYTES
        or existing.st_mode & 0o022
    ):
        raise ConfigurationError("Operational migration lock identity invalid")
    flags = os.O_RDWR | _OPEN_NOFOLLOW | _OPEN_NONBLOCK
    flags |= os.O_CREAT | (os.O_EXCL if existing is None else 0)
    try:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_descriptor)
    except OSError as exc:
        raise ConfigurationError("Operational migration lock unavailable") from exc
    acquired = False
    try:
        opened = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > _LOCK_BYTES
            or opened.st_nlink != 1
            or opened.st_uid != _effective_user_id()
            or opened.st_mode & 0o022
            or (
                opened.st_dev,
                opened.st_ino,
            )
            != (current.st_dev, current.st_ino)
        ):
            raise ConfigurationError("Operational migration lock identity invalid")
        if opened.st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        _assert_anchor_path(path, parent_descriptor)
        if os.name == "nt":
            import msvcrt

            try:
                members = vars(msvcrt)
                members["locking"](descriptor, members["LK_NBLCK"], 1)
            except OSError as exc:
                raise ConcurrencyConflict("Operational migration already active") from exc
        else:
            import fcntl

            try:
                members = vars(fcntl)
                members["flock"](descriptor, members["LOCK_EX"] | members["LOCK_NB"])
            except OSError as exc:
                raise ConcurrencyConflict("Operational migration already active") from exc
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                members = vars(msvcrt)
                members["locking"](descriptor, members["LK_UNLCK"], 1)
            else:
                import fcntl

                members = vars(fcntl)
                members["flock"](descriptor, members["LOCK_UN"])
        os.close(descriptor)


def _windows_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ConfigurationError("Operational migration Windows path unavailable") from exc
    return bool(
        int(getattr(info, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


def _windows_assert_safe_parent_chain(path: Path) -> None:
    if not path.is_absolute():
        raise ConfigurationError("Operational migration parent path must be absolute")
    current = Path(path.anchor)
    for component in path.parent.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except OSError as exc:
            raise ConfigurationError("Operational migration parent path unavailable") from exc
        if not stat.S_ISDIR(info.st_mode) or current.is_symlink() or _windows_reparse(current):
            raise ConfigurationError("Operational migration parent path unsafe")


def _windows_scoped_parent_snapshot(
    path: Path, *, trusted_home: Path
) -> tuple[tuple[str, int, int], ...]:
    try:
        resolved_home = trusted_home.resolve(strict=True)
        resolved_parent = path.parent.resolve(strict=True)
        resolved_path = path.resolve(strict=path.exists())
        resolved_parent.relative_to(resolved_home)
        resolved_path.relative_to(resolved_home)
    except (OSError, ValueError) as exc:
        raise ConfigurationError("Operational migration target outside trusted home") from exc
    if resolved_path != path or resolved_parent != path.parent or resolved_home != trusted_home:
        raise ConfigurationError("Operational migration target lexical/resolved drift")
    current = resolved_home
    directories = [current]
    for component in resolved_parent.relative_to(resolved_home).parts:
        current /= component
        directories.append(current)
    snapshot: list[tuple[str, int, int]] = []
    for directory in directories:
        if not private_directory(directory) or _windows_reparse(directory):
            raise ConfigurationError("Operational migration scoped parent ACL unsafe")
        info = directory.lstat()
        snapshot.append((str(directory), info.st_dev, info.st_ino))
    return tuple(snapshot)


def _windows_assert_parent_snapshot(snapshot: tuple[tuple[str, int, int], ...]) -> None:
    for raw_path, expected_device, expected_inode in snapshot:
        path = Path(raw_path)
        if not private_directory(path) or _windows_reparse(path):
            raise ConfigurationError("Operational migration parent identity drift")
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (expected_device, expected_inode):
            raise ConfigurationError("Operational migration parent identity drift")


def _windows_identity(path: Path) -> tuple[int, int, int, int]:
    if not path.is_absolute() or not private_regular(path):
        raise ConfigurationError("Operational migration path must be private regular file")
    info = path.lstat()
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_nlink)


@contextmanager
def _windows_migration_lock(path: Path) -> Iterator[None]:
    if not path.is_absolute() or path.is_symlink() or _windows_reparse(path.parent):
        raise ConfigurationError("Operational migration lock path invalid")
    if not private_directory(path.parent):
        raise ConfigurationError("Operational migration lock parent ACL unsafe")
    if path.exists() and not private_regular(path):
        raise ConfigurationError("Operational migration lock identity invalid")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if not path.exists():
        flags |= os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ConfigurationError("Operational migration lock unavailable") from exc
    acquired = False
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        restrict_private_file(path)
        if _windows_identity(path)[0:3] != (
            os.fstat(descriptor).st_dev,
            os.fstat(descriptor).st_ino,
            stat.S_IFMT(os.fstat(descriptor).st_mode),
        ):
            raise ConfigurationError("Operational migration lock identity drift")
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        members = vars(msvcrt)
        try:
            members["locking"](descriptor, members["LK_NBLCK"], _LOCK_BYTES)
        except OSError as exc:
            raise ConcurrencyConflict("Operational migration already active") from exc
        acquired = True
        yield
    finally:
        if acquired:
            os.lseek(descriptor, 0, os.SEEK_SET)
            members = vars(msvcrt)
            members["locking"](descriptor, members["LK_UNLCK"], _LOCK_BYTES)
        os.close(descriptor)


@contextmanager
def _spool_barrier(
    targets: tuple[MigrationSpoolTarget, ...], *, trusted_home: Path
) -> Iterator[None]:
    ordered = tuple(sorted(targets, key=lambda item: item.session_id.encode("utf-8")))
    if len({item.session_id for item in ordered}) != len(ordered):
        raise ConfigurationError("Operational migration duplicate spool session")
    groups: dict[tuple[Path, str], list[MigrationSpoolTarget]] = {}
    for target in ordered:
        if target.home != trusted_home:
            raise ConfigurationError("Operational migration spool home mismatch")
        groups.setdefault((target.home, target.client_id), []).append(target)
    group_order = sorted(groups.values(), key=lambda group: group[0].session_id.encode("utf-8"))
    with ExitStack() as stack:
        for group in group_order:
            first = group[0]
            spool = ClientLifecycleSpool(first.home, client_id=first.client_id)
            if not spool.lock_path.exists() or spool.lock_path.is_symlink():
                raise ConfigurationError("Operational migration existing spool lock required")
            pinned_lock = _identity(spool.lock_path)
            stack.enter_context(
                spool.frozen_session_entries(
                    client_id=first.client_id,
                    session_id=first.external_session_id,
                )
            )
            if _identity(spool.lock_path) != pinned_lock:
                raise ConfigurationError("Operational migration spool lock identity drift")
            for target in group[1:]:
                spool.read_session_entries(
                    client_id=target.client_id,
                    session_id=target.external_session_id,
                )
        yield


@contextmanager
def _windows_spool_barrier(
    targets: tuple[MigrationSpoolTarget, ...], *, trusted_home: Path
) -> Iterator[None]:
    ordered = tuple(sorted(targets, key=lambda item: item.session_id.encode("utf-8")))
    if len({item.session_id for item in ordered}) != len(ordered):
        raise ConfigurationError("Operational migration duplicate spool session")
    groups: dict[tuple[Path, str], list[MigrationSpoolTarget]] = {}
    for target in ordered:
        if target.home != trusted_home:
            raise ConfigurationError("Operational migration spool home mismatch")
        groups.setdefault((target.home, target.client_id), []).append(target)
    group_order = sorted(groups.values(), key=lambda group: group[0].session_id.encode("utf-8"))
    with ExitStack() as stack:
        for group in group_order:
            first = group[0]
            spool = ClientLifecycleSpool(first.home, client_id=first.client_id)
            pinned_lock = _windows_identity(spool.lock_path)
            stack.enter_context(
                spool.frozen_session_entries(
                    client_id=first.client_id,
                    session_id=first.external_session_id,
                )
            )
            if _windows_identity(spool.lock_path) != pinned_lock:
                raise ConfigurationError("Operational migration spool lock identity drift")
            for target in group[1:]:
                spool.read_session_entries(
                    client_id=target.client_id,
                    session_id=target.external_session_id,
                )
        yield


def _binding_rows(connection: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (
                str(row[0]),
                str(row[1]),
                str(row[2]),
            )
            for row in connection.execute(
                "select session_id,client_id,external_session_id from continuity_session_binding"
            )
        )
    )


def _connect_existing_writer(path: Path) -> sqlite3.Connection:
    """Open an existing DB without SQLite's implicit create side effect."""
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys=on")
        if connection.execute("pragma foreign_keys").fetchone()[0] != 1:
            raise ConfigurationError("Operational migration foreign keys unavailable")
        connection.execute("pragma busy_timeout=5000")
        return connection
    except sqlite3.DatabaseError as exc:
        raise ConfigurationError("Operational migration existing writer open failed") from exc


def _expected_bindings(
    targets: tuple[MigrationSpoolTarget, ...],
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (target.session_id, target.client_id, target.external_session_id) for target in targets
        )
    )


def _migrate_connection(
    connection: sqlite3.Connection,
    *,
    source_logical_digest: str,
    source_original_digest: str,
    target_version: int = 4,
) -> None:
    if target_version not in {4, 5}:
        raise ConfigurationError("Operational migration target must be v4 or v5")
    if schema._validate_connection(connection) != 3:
        raise ConfigurationError("Operational migration exact schema-v3 required")
    schema._assert_quiescent(connection, require_terminal_close=True)
    from zekam.infrastructure.sqlite.operational_backup import _logical_rows_digest

    if _logical_rows_digest(connection) != source_logical_digest:
        raise ConfigurationError("Operational migration live logical digest drift")
    if schema._original_rows_digest(connection, 3) != source_original_digest:
        raise ConfigurationError("Operational migration original-v3 digest drift")
    schema._apply_migration(connection, 4)
    if target_version == 5:
        schema._apply_migration(connection, 5)
    if schema._validate_connection(connection) != target_version:
        raise ConfigurationError("Operational migration target validation failed")
    if schema._original_rows_digest(connection, 3) != source_original_digest:
        raise ConfigurationError("Operational migration mutated original-v3 rows")


def _migrate_v3_forward(
    database: Path,
    backup: Path,
    *,
    migration_lock: Path,
    admission: OperationalMigrationAdmission,
    spool_targets: tuple[MigrationSpoolTarget, ...],
    target_version: int,
) -> OperationalMigrationReceipt:
    """Apply a reviewed v3 forward path while all writers are fenced."""
    if os.name == "nt":
        return _migrate_v3_forward_windows(
            database,
            backup,
            migration_lock=migration_lock,
            admission=admission,
            spool_targets=spool_targets,
            target_version=target_version,
        )
    if target_version not in {4, 5}:
        raise ConfigurationError("Operational migration target must be v4 or v5")
    if any(
        type(path) is not _PATH_TYPE or not path.is_absolute()
        for path in (database, backup, migration_lock)
    ):
        raise ConfigurationError("Operational migration paths must be absolute")
    if type(spool_targets) is not tuple or any(
        type(target) is not MigrationSpoolTarget for target in spool_targets
    ):
        raise ConfigurationError("Operational migration spool targets invalid")
    _assert_safe_parent_chain(database)
    _assert_safe_parent_chain(backup)
    _assert_safe_parent_chain(migration_lock)
    for method in (
        "stop_new_admission",
        "drain_and_reap",
        "assert_no_admitted_authority",
        "release_admission",
        "mark_recovery_required",
        "trusted_home",
    ):
        if not callable(getattr(admission, method, None)):
            raise ConfigurationError("Operational migration admission boundary invalid")
    trusted_home = admission.trusted_home()
    if type(trusted_home) is not _PATH_TYPE or not trusted_home.is_absolute():
        raise ConfigurationError("Operational migration trusted home invalid")
    with _directory_anchor(trusted_home) as home_descriptor:
        home_identity = os.fstat(home_descriptor)
        if home_identity.st_uid != _effective_user_id() or home_identity.st_mode & 0o022:
            raise ConfigurationError("Operational migration trusted home ownership invalid")
    admission.stop_new_admission()
    committed = False
    try:
        with ExitStack() as anchors:
            home_descriptor = anchors.enter_context(_directory_anchor(trusted_home))
            database_parent = anchors.enter_context(_directory_anchor(database.parent))
            backup_parent = anchors.enter_context(_directory_anchor(backup.parent))
            lock_parent = anchors.enter_context(_directory_anchor(migration_lock.parent))
            if os.fstat(home_descriptor).st_ino != home_identity.st_ino:
                raise ConfigurationError("Operational migration trusted home identity drift")
            _assert_anchor_path(database, database_parent)
            with _migration_lock(migration_lock, parent_descriptor=lock_parent):
                admission.drain_and_reap()
                admission.assert_no_admitted_authority()
                with _spool_barrier(spool_targets, trusted_home=trusted_home):
                    pinned_identity = _identity(database, parent_descriptor=database_parent)
                    connection = schema._connect(database, read_only=True)
                    try:
                        _assert_anchor_path(database, database_parent)
                        connection.execute("begin")
                        if schema._validate_connection(connection) != 3:
                            raise ConfigurationError(
                                "Operational migration exact schema-v3 required"
                            )
                        schema._assert_quiescent(connection, require_terminal_close=True)
                        if _binding_rows(connection) != _expected_bindings(spool_targets):
                            raise ConfigurationError(
                                "Operational migration spool coverage mismatch"
                            )
                        from zekam.infrastructure.sqlite.operational_backup import (
                            _logical_rows_digest,
                        )

                        source_logical_digest = _logical_rows_digest(connection)
                        source_original_digest = schema._original_rows_digest(connection, 3)
                    finally:
                        connection.close()
                    try:
                        os.stat(
                            backup.name,
                            dir_fd=backup_parent,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        raise ConfigurationError(
                            "Operational migration backup destination overwrite forbidden"
                        )
                    created = SQLiteOperationalBackup(database).create_backup_anchored(
                        str(backup), parent_descriptor=backup_parent
                    )
                    _assert_anchor_path(backup, backup_parent)
                    backup_identity = _identity(backup, parent_descriptor=backup_parent)
                    if logical_database_digest(backup) != created.logical_digest:
                        raise ConfigurationError(
                            "Operational migration backup logical digest drift"
                        )
                    _assert_anchor_path(database, database_parent)
                    if _identity(
                        database, parent_descriptor=database_parent
                    ) != pinned_identity or (
                        logical_database_digest(database) != source_logical_digest
                    ):
                        raise ConfigurationError(
                            "Operational migration source changed after backup"
                        )
                    _assert_anchor_path(database, database_parent)
                    if (
                        _identity(backup, parent_descriptor=backup_parent) != backup_identity
                        or logical_database_digest(backup) != created.logical_digest
                    ):
                        raise ConfigurationError(
                            "Operational migration backup changed before writer"
                        )
                    writer = _connect_existing_writer(database)
                    try:
                        _assert_anchor_path(database, database_parent)
                        writer.execute("begin immediate")
                        if (
                            _identity(database, parent_descriptor=database_parent)
                            != pinned_identity
                        ):
                            raise ConfigurationError(
                                "Operational migration writer path identity drift"
                            )
                        if _binding_rows(writer) != _expected_bindings(spool_targets):
                            raise ConfigurationError("Operational migration spool coverage drift")
                        _migrate_connection(
                            writer,
                            source_logical_digest=source_logical_digest,
                            source_original_digest=source_original_digest,
                            target_version=target_version,
                        )
                        _assert_anchor_path(database, database_parent)
                        writer.commit()
                        committed = True
                    except Exception:
                        writer.rollback()
                        raise
                    finally:
                        writer.close()
                    _assert_anchor_path(database, database_parent)
                    result = schema.status(database)
                    _assert_anchor_path(database, database_parent)
                    verification = schema._connect(database, read_only=True)
                    try:
                        _assert_anchor_path(database, database_parent)
                        verification.execute("begin")
                        postcommit_original = schema._original_rows_digest(verification, 3)
                    finally:
                        verification.close()
                    if (
                        not result.schema_ok
                        or not result.integrity_ok
                        or result.schema_version != target_version
                        or _identity(database, parent_descriptor=database_parent) != pinned_identity
                        or postcommit_original != source_original_digest
                        or _identity(backup, parent_descriptor=backup_parent) != backup_identity
                        or logical_database_digest(backup) != created.logical_digest
                    ):
                        raise ConfigurationError(
                            "Operational migration postcommit verification failed"
                        )
                    receipt = OperationalMigrationReceipt(
                        status=result,
                        backup_path=backup,
                        source_v3_logical_digest=source_logical_digest,
                        source_v3_original_digest=source_original_digest,
                        source_v3_size_bytes=created.size_bytes,
                    )
                    admission.release_admission()
                    return receipt
    except Exception:
        if committed:
            admission.mark_recovery_required()
        else:
            admission.release_admission()
        raise


def _migrate_v3_forward_windows(
    database: Path,
    backup: Path,
    *,
    migration_lock: Path,
    admission: OperationalMigrationAdmission,
    spool_targets: tuple[MigrationSpoolTarget, ...],
    target_version: int,
) -> OperationalMigrationReceipt:
    """Windows-native public migration with ACL, identity, and lock fencing."""

    if target_version not in {4, 5}:
        raise ConfigurationError("Operational migration target must be v4 or v5")
    paths = (database, backup, migration_lock)
    if any(type(path) is not _PATH_TYPE or not path.is_absolute() for path in paths):
        raise ConfigurationError("Operational migration paths must be absolute")
    if type(spool_targets) is not tuple or any(
        type(target) is not MigrationSpoolTarget for target in spool_targets
    ):
        raise ConfigurationError("Operational migration spool targets invalid")
    for path in paths:
        _windows_assert_safe_parent_chain(path)
    for method in (
        "stop_new_admission",
        "drain_and_reap",
        "assert_no_admitted_authority",
        "release_admission",
        "mark_recovery_required",
        "trusted_home",
    ):
        if not callable(getattr(admission, method, None)):
            raise ConfigurationError("Operational migration admission boundary invalid")
    trusted_home = admission.trusted_home()
    if (
        type(trusted_home) is not _PATH_TYPE
        or not trusted_home.is_absolute()
        or not private_directory(trusted_home)
    ):
        raise ConfigurationError("Operational migration trusted home ACL invalid")
    parent_snapshots = tuple(
        _windows_scoped_parent_snapshot(path, trusted_home=trusted_home) for path in paths
    )
    trusted_home_identity = trusted_home.lstat()
    admission.stop_new_admission()
    committed = False
    try:
        if (
            not private_directory(trusted_home)
            or trusted_home.lstat().st_ino != trusted_home_identity.st_ino
        ):
            raise ConfigurationError("Operational migration trusted home identity drift")
        for snapshot in parent_snapshots:
            _windows_assert_parent_snapshot(snapshot)
        with _windows_migration_lock(migration_lock):
            for snapshot in parent_snapshots:
                _windows_assert_parent_snapshot(snapshot)
            admission.drain_and_reap()
            admission.assert_no_admitted_authority()
            with _windows_spool_barrier(spool_targets, trusted_home=trusted_home):
                for snapshot in parent_snapshots:
                    _windows_assert_parent_snapshot(snapshot)
                pinned_identity = _windows_identity(database)
                connection = schema._connect(database, read_only=True)
                try:
                    connection.execute("begin")
                    if schema._validate_connection(connection) != 3:
                        raise ConfigurationError("Operational migration exact schema-v3 required")
                    schema._assert_quiescent(connection, require_terminal_close=True)
                    if _binding_rows(connection) != _expected_bindings(spool_targets):
                        raise ConfigurationError("Operational migration spool coverage mismatch")
                    from zekam.infrastructure.sqlite.operational_backup import (
                        _logical_rows_digest,
                    )

                    source_logical_digest = _logical_rows_digest(connection)
                    source_original_digest = schema._original_rows_digest(connection, 3)
                finally:
                    connection.close()
                if backup.exists() or backup.is_symlink():
                    if backup.is_symlink() or not private_regular(backup):
                        raise ConfigurationError("Operational migration existing backup unsafe")
                    backup_identity = _windows_identity(backup)
                    backup_connection = schema._connect(backup, read_only=True)
                    try:
                        backup_connection.execute("begin")
                        backup_version = schema._validate_connection(backup_connection)
                        backup_original = schema._original_rows_digest(backup_connection, 3)
                    finally:
                        backup_connection.close()
                    backup_logical = logical_database_digest(backup)
                    backup_size = backup.stat().st_size
                    if (
                        backup_version != 3
                        or backup_logical != source_logical_digest
                        or backup_original != source_original_digest
                    ):
                        raise ConfigurationError("Operational migration existing backup drift")
                else:
                    created = SQLiteOperationalBackup(database).create_backup(str(backup))
                    backup_identity = _windows_identity(backup)
                    backup_logical = created.logical_digest
                    backup_size = created.size_bytes
                if logical_database_digest(backup) != backup_logical:
                    raise ConfigurationError("Operational migration backup logical digest drift")
                if (
                    _windows_identity(database) != pinned_identity
                    or logical_database_digest(database) != source_logical_digest
                ):
                    raise ConfigurationError("Operational migration source changed after backup")
                if (
                    _windows_identity(backup) != backup_identity
                    or logical_database_digest(backup) != backup_logical
                ):
                    raise ConfigurationError("Operational migration backup changed before writer")
                writer = _connect_existing_writer(database)
                try:
                    for snapshot in parent_snapshots:
                        _windows_assert_parent_snapshot(snapshot)
                    writer.execute("begin immediate")
                    if _windows_identity(database) != pinned_identity:
                        raise ConfigurationError("Operational migration writer path identity drift")
                    if _binding_rows(writer) != _expected_bindings(spool_targets):
                        raise ConfigurationError("Operational migration spool coverage drift")
                    _migrate_connection(
                        writer,
                        source_logical_digest=source_logical_digest,
                        source_original_digest=source_original_digest,
                        target_version=target_version,
                    )
                    writer.commit()
                    committed = True
                except Exception:
                    writer.rollback()
                    raise
                finally:
                    writer.close()
                for snapshot in parent_snapshots:
                    _windows_assert_parent_snapshot(snapshot)
                result = schema.status(database)
                verification = schema._connect(database, read_only=True)
                try:
                    verification.execute("begin")
                    postcommit_original = schema._original_rows_digest(verification, 3)
                finally:
                    verification.close()
                for snapshot in parent_snapshots:
                    _windows_assert_parent_snapshot(snapshot)
                if (
                    not result.schema_ok
                    or not result.integrity_ok
                    or result.schema_version != target_version
                    or _windows_identity(database) != pinned_identity
                    or postcommit_original != source_original_digest
                    or _windows_identity(backup) != backup_identity
                    or logical_database_digest(backup) != backup_logical
                ):
                    raise ConfigurationError("Operational migration postcommit verification failed")
                receipt = OperationalMigrationReceipt(
                    status=result,
                    backup_path=backup,
                    source_v3_logical_digest=source_logical_digest,
                    source_v3_original_digest=source_original_digest,
                    source_v3_size_bytes=backup_size,
                )
                admission.release_admission()
                return receipt
    except Exception:
        if committed:
            admission.mark_recovery_required()
        else:
            admission.release_admission()
        raise


def migrate_v3_to_v4(
    database: Path,
    backup: Path,
    *,
    migration_lock: Path,
    admission: OperationalMigrationAdmission,
    spool_targets: tuple[MigrationSpoolTarget, ...],
) -> OperationalMigrationReceipt:
    """Preserve the reviewed historical v3-to-v4 migration surface."""

    return _migrate_v3_forward(
        database,
        backup,
        migration_lock=migration_lock,
        admission=admission,
        spool_targets=spool_targets,
        target_version=4,
    )


def migrate_v3_to_v5(
    database: Path,
    backup: Path,
    *,
    migration_lock: Path,
    admission: OperationalMigrationAdmission,
    spool_targets: tuple[MigrationSpoolTarget, ...],
) -> OperationalMigrationReceipt:
    """Atomically migrate v3 through v4 to evolution-authority v5."""

    return _migrate_v3_forward(
        database,
        backup,
        migration_lock=migration_lock,
        admission=admission,
        spool_targets=spool_targets,
        target_version=5,
    )
