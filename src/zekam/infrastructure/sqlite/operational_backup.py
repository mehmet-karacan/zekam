"""SQLite online backup/restore with integrity, schema and logical parity gates."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from zekam.application.operational_store import OperationalBackupReceipt
from zekam.domain.errors import ConfigurationError
from zekam.infrastructure.sqlite.operational_schema import (
    SCHEMA_DIGESTS,
    SCHEMA_VERSION,
    _apply_forward_path,
    _assert_quiescent,
    _connect,
    _original_rows_digest,
    _validate_connection,
    _validate_target_version,
)

_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_OPEN_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_OPEN_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if value is None or isinstance(value, str | int | float):
        return value
    raise ConfigurationError(f"SQLite logical digest desteklenmeyen tip: {type(value).__name__}")


def _logical_rows_digest(connection: sqlite3.Connection) -> str:
    """Read rows from the caller's pinned, verified snapshot transaction."""
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
                " and name not like 'sqlite_%' order by name"
            ).fetchall()
        ]
        payload: list[dict[str, Any]] = []
        for table in tables:
            columns = [row[1] for row in connection.execute(f'pragma table_info("{table}")')]
            ordering = ", ".join(f'"{column}"' for column in columns)
            rows = connection.execute(f'select * from "{table}" order by {ordering}').fetchall()
            payload.append(
                {
                    "table": table,
                    "columns": columns,
                    "rows": [[_json_value(item) for item in row] for row in rows],
                }
            )
    except sqlite3.DatabaseError as exc:
        raise ConfigurationError("SQLite logical digest okunamadi") from exc
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def logical_database_digest(path: Path) -> str:
    """Digest an exact supported snapshot, independent of SQLite page layout."""
    connection = _connect(path, read_only=True)
    try:
        connection.execute("begin")
        _validate_connection(connection)
        return _logical_rows_digest(connection)
    except sqlite3.DatabaseError as exc:
        raise ConfigurationError("SQLite logical digest integrity/schema gate gecmedi") from exc
    finally:
        connection.close()


def original_v1_data_digest(path: Path) -> str:
    """Compare original v1 rows across versions without whole-schema comparison."""
    return original_data_digest(path, source_version=1)


def original_data_digest(path: Path, *, source_version: int) -> str:
    """Compare every source-version table and historical ledger row across upgrades."""
    if type(source_version) is not int or source_version not in SCHEMA_DIGESTS:
        raise ConfigurationError("SQLite original row source version unsupported")
    connection = _connect(path, read_only=True)
    try:
        connection.execute("begin")
        current_version = _validate_connection(connection)
        if source_version > current_version:
            raise ConfigurationError("SQLite original row source version exceeds database version")
        return _original_rows_digest(connection, source_version)
    finally:
        connection.close()


def _assert_secure_leaf(identity: os.stat_result) -> None:
    if (
        not stat.S_ISREG(identity.st_mode)
        or identity.st_uid != os.getuid()
        or identity.st_nlink != 1
        or identity.st_mode & 0o077
    ):
        raise ConfigurationError("SQLite backup temporary identity/link/mode unsafe")


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise ConfigurationError("SQLite backup temporary write failed")
        view = view[written:]


def _fsync(_path: Path) -> None:
    """Compatibility seam; durability is performed on held descriptors."""


def _read_all(descriptor: int, size: int) -> bytes:
    if size <= 0 or size > _MAX_SNAPSHOT_BYTES:
        raise ConfigurationError("SQLite backup snapshot size out of bounds")
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        if not chunk:
            raise ConfigurationError("SQLite backup persisted readback truncated")
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _serialized_digest(content: bytes, *, expected_version: int) -> str:
    if not content.startswith(b"SQLite format 3\x00"):
        raise ConfigurationError("SQLite backup persisted header invalid")
    verification = sqlite3.connect(":memory:")
    try:
        verification.deserialize(content)
        verification.execute("pragma journal_mode=delete")
        if _validate_connection(verification) != expected_version:
            raise ConfigurationError("SQLite backup persisted schema drift")
        return _logical_rows_digest(verification)
    except sqlite3.DatabaseError as exc:
        raise ConfigurationError("SQLite backup persisted integrity drift") from exc
    finally:
        verification.close()


@contextmanager
def _parent_anchor(path: Path, supplied: int | None = None) -> Iterator[int]:
    if supplied is not None:
        descriptor = os.dup(supplied)
    else:
        if not path.is_absolute():
            raise ConfigurationError("SQLite backup destination parent must be absolute")
        descriptor = os.open("/", os.O_RDONLY | _OPEN_DIRECTORY | _OPEN_NOFOLLOW)
        try:
            for component in path.parts[1:]:
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | _OPEN_DIRECTORY | _OPEN_NOFOLLOW | _OPEN_NONBLOCK,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
        except OSError as exc:
            os.close(descriptor)
            raise ConfigurationError("SQLite backup destination parent unsafe") from exc
    try:
        opened = os.fstat(descriptor)
        lexical = os.lstat(path)
        if not stat.S_ISDIR(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (lexical.st_dev, lexical.st_ino):
            raise ConfigurationError("SQLite backup destination parent identity drift")
        yield descriptor
    finally:
        os.close(descriptor)


def _anchored_leaf_identity(parent: Path, descriptor: int, name: str) -> os.stat_result:
    try:
        anchored = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        lexical = os.lstat(parent / name)
    except OSError as exc:
        raise ConfigurationError("SQLite backup anchored artifact unavailable") from exc
    if (anchored.st_dev, anchored.st_ino, stat.S_IFMT(anchored.st_mode)) != (
        lexical.st_dev,
        lexical.st_ino,
        stat.S_IFMT(lexical.st_mode),
    ):
        raise ConfigurationError("SQLite backup ancestor/path identity drift")
    return anchored


def _anchored_unlink(descriptor: int, name: str) -> None:
    with suppress(FileNotFoundError):
        os.unlink(name, dir_fd=descriptor)


def _assert_v4_restore_dormant(connection: sqlite3.Connection) -> None:
    """A restored v4 file may contain no native authority or live incarnation."""
    tables = (
        "continuity_hook_attachment",
        "continuity_hook_process_generation",
        "continuity_managed_process_receipt",
        "continuity_hook_attachment_revision",
        "continuity_hook_recovery_case",
        "continuity_hook_recovery_resolution",
        "continuity_native_event_receipt",
        "continuity_turn_commit_receipt",
        "continuity_internal_event_receipt",
    )
    for table in tables:
        if connection.execute(f'select 1 from "{table}" limit 1').fetchone() is not None:
            raise ConfigurationError(
                "SQLite restore recovery-required: v4 native authority is not dormant"
            )


class SQLiteOperationalBackup:
    def __init__(self, source: Path) -> None:
        self._source = source

    def create_backup(self, destination: str) -> OperationalBackupReceipt:
        return self._copy_verified(self._source, Path(destination), restoring=False)

    def create_backup_anchored(
        self, destination: str, *, parent_descriptor: int
    ) -> OperationalBackupReceipt:
        """Create under a caller-held no-follow destination-parent anchor."""
        if type(parent_descriptor) is not int or parent_descriptor < 0:
            raise ConfigurationError("SQLite backup destination anchor invalid")
        return self._copy_verified(
            self._source,
            Path(destination),
            restoring=False,
            destination_parent_descriptor=parent_descriptor,
        )

    def restore_backup(
        self, backup_path: str, destination: str, *, target_version: int = SCHEMA_VERSION
    ) -> OperationalBackupReceipt:
        return self._copy_verified(
            Path(backup_path), Path(destination), restoring=True, target_version=target_version
        )

    def restore_v4_backup(self, backup_path: str, destination: str) -> OperationalBackupReceipt:
        """Restore only an exact dormant v4 snapshot to a new destination."""
        return self._copy_verified(
            Path(backup_path),
            Path(destination),
            restoring=True,
            target_version=4,
            allow_v4_restore=True,
        )

    def _copy_verified(
        self,
        source: Path,
        destination: Path,
        *,
        restoring: bool,
        target_version: int = SCHEMA_VERSION,
        allow_v4_restore: bool = False,
        destination_parent_descriptor: int | None = None,
    ) -> OperationalBackupReceipt:
        if restoring and not (allow_v4_restore and target_version == 4):
            _validate_target_version(target_version)
        if not source.is_file() or source.is_symlink():
            raise ConfigurationError("SQLite backup source integrity/schema gate gecmedi")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with _parent_anchor(destination.parent, destination_parent_descriptor) as parent_descriptor:
            try:
                os.stat(
                    destination.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise ConfigurationError("SQLite backup destination overwrite edilemez")
            temporary_name = f".{destination.name}.partial-{uuid4().hex}"
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | _OPEN_NOFOLLOW | _OPEN_NONBLOCK
            try:
                descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
            except OSError as exc:
                raise ConfigurationError("SQLite backup temporary creation failed") from exc
            try:
                initial = _anchored_leaf_identity(
                    destination.parent, parent_descriptor, temporary_name
                )
                _assert_secure_leaf(initial)
                return self._copy_into_anchored_temporary(
                    source,
                    destination,
                    temporary_name,
                    parent_descriptor,
                    descriptor,
                    restoring=restoring,
                    target_version=target_version,
                    allow_v4_restore=allow_v4_restore,
                )
            finally:
                os.close(descriptor)
                _anchored_unlink(parent_descriptor, temporary_name)
                for suffix in ("-journal", "-wal", "-shm"):
                    _anchored_unlink(parent_descriptor, temporary_name + suffix)

    def _copy_into_anchored_temporary(
        self,
        source: Path,
        destination: Path,
        temporary_name: str,
        parent_descriptor: int,
        temporary_descriptor: int,
        *,
        restoring: bool,
        target_version: int,
        allow_v4_restore: bool,
    ) -> OperationalBackupReceipt:
        # Read-write/no-create is only an ancestor-swap tripwire. The database
        # image itself is built in memory and is never written through this path.
        tripwire_uri = (destination.parent / temporary_name).as_uri() + "?mode=rw"
        try:
            tripwire = sqlite3.connect(tripwire_uri, uri=True)
            tripwire.close()
        except (OSError, sqlite3.DatabaseError) as exc:
            raise ConfigurationError("SQLite backup destination ancestor identity drift") from exc
        _anchored_leaf_identity(destination.parent, parent_descriptor, temporary_name)
        source_connection = _connect(source, read_only=True)
        target_connection = sqlite3.connect(":memory:")
        source_verified = False
        try:
            source_connection.execute("begin")
            source_version = _validate_connection(source_connection)
            if allow_v4_restore:
                if source_version != 4:
                    raise ConfigurationError("SQLite restore exact dormant v4 required")
                _assert_v4_restore_dormant(source_connection)
            source_digest = _logical_rows_digest(source_connection)
            original_digest = _original_rows_digest(source_connection, source_version)
            page_count = int(source_connection.execute("pragma page_count").fetchone()[0])
            page_size = int(source_connection.execute("pragma page_size").fetchone()[0])
            if page_count <= 0 or page_size <= 0 or page_count * page_size > _MAX_SNAPSHOT_BYTES:
                raise ConfigurationError("SQLite backup source snapshot too large")
            source_connection.backup(target_connection)
            target_connection.execute("pragma journal_mode=delete")
            target_version_found = _validate_connection(target_connection)
            if target_version_found != source_version:
                raise ConfigurationError("SQLite backup schema parity drift")
            if _logical_rows_digest(target_connection) != source_digest:
                raise ConfigurationError("SQLite backup logical parity drift")
            source_verified = True
            if restoring:
                if source_version > target_version:
                    raise ConfigurationError("SQLite restore downgrade forbidden")
                target_connection.execute("begin immediate")
                _assert_quiescent(target_connection, restoring=True)
                if source_version < target_version:
                    if target_version == 4:
                        raise ConfigurationError(
                            "SQLite restore cannot replace explicit v3 to v4 migration"
                        )
                    _apply_forward_path(target_connection, source_version, target_version)
                target_connection.commit()
                if _original_rows_digest(target_connection, source_version) != original_digest:
                    raise ConfigurationError(
                        f"SQLite restore original v{source_version} row parity drift"
                    )
            target_digest_before_serialize = _logical_rows_digest(target_connection)
            serialized_buffer = bytearray(target_connection.serialize())
            # A folded online snapshot has no external WAL. Normalize the two
            # SQLite header journal-version bytes before persisting/deserializing.
            if len(serialized_buffer) >= 20:
                serialized_buffer[18] = 1
                serialized_buffer[19] = 1
            serialized = bytes(serialized_buffer)
            if not serialized or len(serialized) > _MAX_SNAPSHOT_BYTES:
                raise ConfigurationError("SQLite backup serialized snapshot too large")
        except (ConfigurationError, sqlite3.DatabaseError) as exc:
            if isinstance(exc, ConfigurationError) and (allow_v4_restore or source_verified):
                raise
            raise ConfigurationError("SQLite backup source integrity/schema gate gecmedi") from exc
        finally:
            target_connection.close()
            source_connection.close()

        before_write = _anchored_leaf_identity(
            destination.parent, parent_descriptor, temporary_name
        )
        _assert_secure_leaf(before_write)
        if (before_write.st_dev, before_write.st_ino) != (
            os.fstat(temporary_descriptor).st_dev,
            os.fstat(temporary_descriptor).st_ino,
        ):
            raise ConfigurationError("SQLite backup temporary descriptor identity drift")
        _write_all(temporary_descriptor, serialized)
        os.fsync(temporary_descriptor)
        _fsync(destination.parent / temporary_name)
        after_write = _anchored_leaf_identity(destination.parent, parent_descriptor, temporary_name)
        _assert_secure_leaf(after_write)
        if after_write.st_size != len(serialized):
            raise ConfigurationError("SQLite backup temporary size drift")

        expected_version = target_version if restoring else source_version
        persisted = _read_all(temporary_descriptor, after_write.st_size)
        target_digest = _serialized_digest(persisted, expected_version=expected_version)
        if target_digest != target_digest_before_serialize:
            raise ConfigurationError("SQLite backup serialized logical parity drift")
        # A competing creator is a conflict, never an overwrite authorization.
        linked = False
        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            linked = True
        except FileExistsError as exc:
            raise ConfigurationError("SQLite backup destination overwrite edilemez") from exc
        try:
            published = _anchored_leaf_identity(
                destination.parent, parent_descriptor, destination.name
            )
            if (
                not stat.S_ISREG(published.st_mode)
                or published.st_uid != os.getuid()
                or published.st_nlink != 2
                or published.st_mode & 0o077
            ):
                raise ConfigurationError("SQLite backup publication identity/link unsafe")
            if (published.st_dev, published.st_ino) != (
                after_write.st_dev,
                after_write.st_ino,
            ):
                raise ConfigurationError("SQLite backup published identity drift")
            published_descriptor = os.open(
                destination.name,
                os.O_RDONLY | _OPEN_NOFOLLOW | _OPEN_NONBLOCK,
                dir_fd=parent_descriptor,
            )
            try:
                published_stat = os.fstat(published_descriptor)
                if published_stat.st_nlink != 2:
                    raise ConfigurationError("SQLite backup publication link count drift")
                os.fsync(published_descriptor)
                published_content = _read_all(published_descriptor, published_stat.st_size)
                if (
                    _serialized_digest(published_content, expected_version=expected_version)
                    != target_digest_before_serialize
                ):
                    raise ConfigurationError("SQLite backup published logical drift")
            finally:
                os.close(published_descriptor)
            _anchored_unlink(parent_descriptor, temporary_name)
            final = _anchored_leaf_identity(destination.parent, parent_descriptor, destination.name)
            _assert_secure_leaf(final)
            if os.name != "nt":
                os.fsync(parent_descriptor)
            return OperationalBackupReceipt(
                source_schema_version=source_version,
                source_schema_digest=SCHEMA_DIGESTS[source_version],
                logical_digest=source_digest,
                size_bytes=final.st_size,
            )
        except Exception:
            if linked:
                _anchored_unlink(parent_descriptor, destination.name)
            raise
