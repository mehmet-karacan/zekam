"""Explicit source capture and fresh snapshot admission for local continuity.

No full-tree scan, provider, bootstrap or old snapshot conversion. SQLite writer
serialization does not freeze Git/files: capture is rechecked, never called an
atomic filesystem snapshot. The root is an explicit trusted local binding and is
excluded from portable plans.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import secrets
import selectors
import signal
import sqlite3
import stat
import subprocess
import time
from contextlib import closing, suppress
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from zekam.application.fresh_bootstrap import MAX_CONFIG_BYTES
from zekam.application.ignore_rules import SYSTEM_DENY_LINES, IgnoreMatcher, system_deny_matcher
from zekam.application.local_continuity import ContinuityBinding, digest_text, uuid_text
from zekam.application.local_continuity_source_authority import (
    MAX_PORTABLE_PLAN_BYTES,
    FileIdentity,
    LocalBindingRevision,
    PortableSourcePlanRecord,
    authority_digest,
)
from zekam.application.local_continuity_source_plan import (
    MAX_IGNORE_BYTES,
    MAX_SOURCE_BYTES,
    CapturedSourceFile,
    ContinuitySourcePlan,
    ContinuitySourceRecipe,
)
from zekam.application.operational_store import SourceSnapshotRecord
from zekam.application.secret_detection import SECRET_RULES, scan_text
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import LayoutError, PolicyViolation, ValidationFailed
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

_PRIVATE_PARTS = frozenset({"veriler", "secrets", "credentials", ".git", ".claude", ".codex"})
_PRIVATE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3", ".zip", ".tar", ".gz", ".7z", ".pdf"})
_RENAME_EXCL = 0x00000004


def _source_authority_birthtime(info: os.stat_result) -> int:
    value = getattr(info, "st_birthtime", None)
    if not isinstance(value, (int, float)) or value < 0:
        raise PolicyViolation("Local source file birth time unavailable")
    return int(value * 1_000_000_000)


def _source_authority_identity(path: Path, *, regular: bool) -> FileIdentity:
    info = path.lstat()
    expected = stat.S_ISREG if regular else stat.S_ISDIR
    if (
        not expected(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or (regular and info.st_nlink != 1)
    ):
        raise PolicyViolation("Local source authority file identity rejected")
    return FileIdentity(
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_nlink,
        _source_authority_birthtime(info),
    )


def _source_authority_held_identity(descriptor: int) -> FileIdentity:
    info = os.fstat(descriptor)
    return FileIdentity(
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_nlink,
        _source_authority_birthtime(info),
    )


class _SourceAuthorityDeadline:
    def __init__(self) -> None:
        self.started = time.monotonic_ns()
        if type(self.started) is not int or not 0 <= self.started <= (2**63 - 1) - 20_000_000_000:
            raise PolicyViolation("Local source authority deadline unavailable")
        self.deadline, self.previous = self.started + 20_000_000_000, self.started

    def check(self) -> None:
        current = time.monotonic_ns()
        if type(current) is not int or not self.previous <= current <= self.deadline:
            raise PolicyViolation("Local source authority deadline exceeded")
        self.previous = current


def _source_authority_cleanup(
    operational_db: _GuardedSQLite | None,
    operational_fd: int | None,
    side_fd: int | None,
    root_fd: int | None,
) -> None:
    failure: BaseException | None = None
    for action in (
        None if operational_db is None else operational_db.rollback,
        None if operational_db is None else operational_db.close,
        None if operational_fd is None else lambda: os.close(operational_fd),
        None if side_fd is None else lambda: os.close(side_fd),
        None if root_fd is None else lambda: os.close(root_fd),
    ):
        if action is not None:
            try:
                action()
            except BaseException as exc:
                failure = failure or exc
    if failure is not None:
        raise failure


def _source_authority_operational_unchanged(
    path: Path, home: Path, descriptor: int, identity: FileIdentity, parent_digest: str
) -> bool:
    try:
        return _source_authority_held_identity(descriptor) == identity and (
            _source_authority_identity(path, regular=True),
            _source_authority_parent_chain(path, home),
        ) == (identity, parent_digest)
    except (OSError, PolicyViolation):
        return False


def _source_authority_connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    mode = "ro" if readonly else "rw"
    db = sqlite3.connect(f"{path.as_uri()}?mode={mode}&nofollow=1", uri=True, timeout=0.125)
    if readonly:
        db.execute("pragma query_only=on")
    db.row_factory = sqlite3.Row
    db.execute("pragma foreign_keys=on")
    db.execute("pragma busy_timeout=125")
    return db


def _source_authority_uuid4() -> str:
    raw = bytearray(secrets.token_bytes(16))
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(raw)))


def _source_authority_parent_chain(path: Path, anchor: Path) -> str:
    try:
        relative_parent = path.parent.relative_to(anchor)
    except ValueError:
        raise PolicyViolation("Local source authority path escaped its home") from None
    parts: list[dict[str, int]] = []
    current = anchor
    for part in ("", *relative_parent.parts):
        if part:
            current /= part
        info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise PolicyViolation("Local source authority ancestor rejected")
        parts.append(
            {"dev": info.st_dev, "ino": info.st_ino, "mode": info.st_mode, "uid": info.st_uid}
        )
    return authority_digest("zekam.local-source-ancestor-chain.v1", parts)


class _GuardedSQLite:
    """Deny raw SQLite use after setup; each reviewed statement proves the canary."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db, self._active, self._canary = db, "", 0
        db.set_authorizer(self._authorize)

    def _authorize(
        self,
        action: int,
        first: str | None,
        second: str | None,
        _database: str | None,
        _source: str | None,
    ) -> int:
        if (
            self._active == "CANARY"
            and action == sqlite3.SQLITE_PRAGMA
            and first == "application_id"
            and second is None
        ):
            self._canary += 1
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if self._active else sqlite3.SQLITE_DENY

    def execute(
        self,
        query_id: str,
        sql: str | tuple[object, ...] | None = None,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor:
        if sql is None or isinstance(sql, tuple):
            parameters, sql, query_id = (() if sql is None else sql), query_id, "VALIDATE"
        before, self._active = self._canary, "CANARY"
        try:
            try:
                self._db.execute("pragma application_id")
            except sqlite3.DatabaseError as exc:
                if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_AUTH:
                    raise
            else:
                raise PolicyViolation("Local source authority canary failed")
            if self._canary != before + 1:
                raise PolicyViolation("Local source authority canary drift")
            self._active = query_id
            return self._db.execute(sql, parameters)
        finally:
            self._active = ""

    def commit(self) -> None:
        self._active = "COMMIT"
        try:
            self._db.commit()
        finally:
            self._active = ""

    def rollback(self) -> None:
        self._active = "ROLLBACK"
        try:
            self._db.rollback()
        finally:
            self._active = ""

    def close(self) -> None:
        self._db.set_authorizer(None)
        self._db.close()


def _source_authority_baseline(
    db: sqlite3.Connection | _GuardedSQLite, prefix: str = "BASE"
) -> tuple[object, ...]:
    def rows(label: str, sql: str) -> tuple[tuple[object, ...], ...]:
        cursor = (
            db.execute(f"{prefix}_{label}", sql)
            if isinstance(db, _GuardedSQLite)
            else db.execute(sql)
        )
        return tuple(tuple(row) for row in cursor)

    return (
        rows(
            "SCHEMA",
            "select type,name,tbl_name,sql from sqlite_schema "
            "where name not like 'sqlite_stat%' order by type,name",
        ),
        rows("META", "select * from local_source_authority_meta"),
        rows("MIGRATION", "select * from local_source_authority_migration"),
        rows("REVISION", "select * from local_source_binding_revision order by revision_digest"),
        rows(
            "HEAD",
            "select * from local_source_binding_head "
            "order by device_id,source_binding_id,generation",
        ),
    )


def _source_authority_revision_values(
    candidate: LocalBindingRevision,
) -> tuple[object, ...]:
    body, op, root = candidate.body(), candidate.operational_identity, candidate.root_identity
    return (
        candidate.revision_digest,
        candidate.device_id,
        candidate.local_instance_id,
        body["operational_identity_digest"],
        op.dev,
        op.ino,
        op.uid,
        op.gid,
        op.mode,
        op.nlink,
        op.birthtime_ns,
        candidate.project_id,
        candidate.source_binding_id,
        candidate.root_path,
        candidate.root_path_digest(),
        root.dev,
        root.ino,
        root.uid,
        root.gid,
        root.mode,
        root.nlink,
        root.birthtime_ns,
        candidate.portable_plan_digest,
        candidate.previous_revision_digest,
        candidate.generation,
        candidate.body_json,
        candidate.created_at,
        0,
        0,
    )


def _open_owned_directory(parent: int | None, name: str | Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent)
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        os.close(descriptor)
        raise PolicyViolation("Portable source plan directory policy rejected")
    return descriptor


def _portable_plan_parent(home: Path, project_id: str) -> int:
    uuid_text(project_id, "Project")
    if not isinstance(home, Path) or not home.is_absolute():
        raise ValidationFailed("Portable source plan absolute home required")
    descriptors: list[int] = []
    try:
        current = _open_owned_directory(None, home)
        descriptors.append(current)
        for part in ("projeler", project_id, "baglantilar"):
            current = _open_owned_directory(current, part)
            descriptors.append(current)
        return os.dup(descriptors[-1])
    except OSError as exc:
        raise PolicyViolation("Portable source plan directory unavailable") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_plan_at(parent: int, name: str) -> bytes:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PolicyViolation("Portable source plan unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not 1 <= before.st_size <= MAX_PORTABLE_PLAN_BYTES
        ):
            raise PolicyViolation("Portable source plan file policy rejected")
        chunks: list[bytes] = []
        remaining = MAX_PORTABLE_PLAN_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        lexical = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            len(raw) != before.st_size
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(lexical)
        ):
            raise PolicyViolation("Portable source plan changed during read")
        return raw
    finally:
        os.close(descriptor)


def _rename_exclusive(parent: int, source: str, destination: str) -> None:
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        function = library.renameatx_np
    except (OSError, AttributeError) as exc:
        raise PolicyViolation("Exclusive portable plan publication unavailable") from exc
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if function(parent, source.encode(), parent, destination.encode(), _RENAME_EXCL) != 0:
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            raise FileExistsError(destination)
        raise OSError(number, "exclusive portable source plan publication failed")


def publish_portable_source_plan(home: Path, record: PortableSourcePlanRecord) -> str:
    if type(record) is not PortableSourcePlanRecord:
        raise ValidationFailed("Typed portable source plan record required")
    raw = record.bytes()
    name = record.plan.content_digest.removeprefix("sha256:") + ".json"
    parent = _portable_plan_parent(home, record.plan.recipe.project_id)
    temporary = f".{name}.{secrets.token_hex(16)}.tmp"
    descriptor: int | None = None
    try:
        try:
            existing = _read_plan_at(parent, name)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != raw:
                raise PolicyViolation("Portable source plan address conflict")
            PortableSourcePlanRecord.from_bytes(existing)
            return name
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        written = 0
        while written < len(raw):
            count = os.write(descriptor, raw[written:])
            if count <= 0:
                raise OSError("portable source plan short write")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            _rename_exclusive(parent, temporary, name)
        except FileExistsError:
            if _read_plan_at(parent, name) != raw:
                raise PolicyViolation("Portable source plan concurrent conflict") from None
        os.fsync(parent)
        PortableSourcePlanRecord.from_bytes(_read_plan_at(parent, name))
        return name
    except OSError as exc:
        raise PolicyViolation("Portable source plan publication failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(OSError):
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)


def read_portable_source_plan(
    home: Path, project_id: str, content_digest: str
) -> PortableSourcePlanRecord:
    digest_text(content_digest)
    name = content_digest.removeprefix("sha256:") + ".json"
    parent = _portable_plan_parent(home, project_id)
    try:
        record = PortableSourcePlanRecord.from_bytes(_read_plan_at(parent, name))
    finally:
        os.close(parent)
    if record.plan.recipe.project_id != project_id or record.plan.content_digest != content_digest:
        raise PolicyViolation("Portable source plan address or project mismatch")
    return record


def _bounded_git_process(
    command: list[str],
    environment: dict[str, str],
    input_bytes: bytes | None,
) -> tuple[int, bytes, bytes]:
    """Drain pipes within a bounded load-tolerant deadline and 16 KiB output cap."""
    process = subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.PIPE if input_bytes else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    # Full acceptance intentionally runs concurrent process/fault campaigns.  Short
    # total deadlines proved scheduler-sensitive for repeated local, read-only Git
    # observations.  Twenty seconds stays inside the 30-second typed-recovery
    # visibility SLO while remaining bounded and never introducing a blind retry.
    deadline = time.monotonic() + 20
    pending = memoryview(input_bytes or b"")
    try:
        with selectors.DefaultSelector() as selector:
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if process.stdin is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PolicyViolation("Source fixed Git observation timed out")
                for key, _ in selector.select(min(remaining, 0.1)):
                    try:
                        if key.data == "stdin":
                            pending = pending[os.write(key.fd, pending) :]
                            if not pending:
                                selector.unregister(key.fileobj)
                                assert process.stdin is not None
                                process.stdin.close()
                        else:
                            chunk = os.read(key.fd, 4096)
                            if not chunk:
                                selector.unregister(key.fileobj)
                            else:
                                output[key.data].extend(chunk)
                                if sum(len(value) for value in output.values()) > 16384:
                                    raise PolicyViolation(
                                        "Source Git output exceeded bounded capture"
                                    )
                    except BlockingIOError:
                        continue
            code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            return code, bytes(output["stdout"]), bytes(output["stderr"])
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # The short-lived group leader may already have exited and its process
            # group may no longer be signalable.  If our direct child is still alive,
            # fall back to killing that owned process instead of masking the original
            # timeout/output-bound finding with a cleanup race.
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                process.kill()
            process.wait(timeout=5)
        for final_stream in (process.stdin, process.stdout, process.stderr):
            if final_stream is not None:
                final_stream.close()


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _secret_policy_digest() -> str:
    return digest(
        {
            "system_deny": SYSTEM_DENY_LINES,
            "private_parts": sorted(_PRIVATE_PARTS),
            "private_suffixes": sorted(_PRIVATE_SUFFIXES),
            "private_prefixes": [".env"],
            "rules": [
                (rule.rule_id, rule.pattern.pattern, rule.pattern.flags) for rule in SECRET_RULES
            ],
        }
    )


class BoundedContinuitySource:
    def __init__(self, root: Path, recipe: ContinuitySourceRecipe) -> None:
        if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
            raise ValidationFailed("Source typed absolute root required")
        if not isinstance(recipe, ContinuitySourceRecipe):
            raise ValidationFailed("Source typed recipe required")
        recipe.__post_init__()
        self.root, self.recipe = root, recipe
        self._root_identity = self._root()
        self._files = KnowledgeFileStore(root)
        system = system_deny_matcher()
        for path in recipe.allowed_paths:
            parts = PurePosixPath(path).parts
            lowered = tuple(part.casefold() for part in parts)
            if (
                system.is_path_ignored(path)
                or any(part.startswith(".env") for part in lowered)
                or any(part in _PRIVATE_PARTS for part in lowered)
                or PurePosixPath(path).suffix.casefold() in _PRIVATE_SUFFIXES
            ):
                raise PolicyViolation(
                    "Source recipe contains forbidden user/private/generated material"
                )
        self._git_identity = self._git_layout()

    def _git_layout(self) -> tuple[int, ...]:
        try:
            with self._files._parent_handle(".git/HEAD", create=False) as (parent, name, _):
                info = os.fstat(parent)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    raise PolicyViolation("Source Git directory ownership unsupported")
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                )
                try:
                    leaf = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(leaf.st_mode)
                        or leaf.st_uid != os.geteuid()
                        or leaf.st_nlink != 1
                        or leaf.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    ):
                        raise PolicyViolation("Source Git HEAD policy unsupported")
                finally:
                    os.close(descriptor)
                return info.st_dev, info.st_ino, info.st_mode, info.st_uid
        except (OSError, LayoutError) as exc:
            raise PolicyViolation("Source Git layout unsupported by bounded recipe") from exc

    def _root(self) -> tuple[int, ...]:
        identity: list[int] = []
        for path in (*reversed(self.root.parents), self.root):
            info = path.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or (info.st_uid not in {0, os.geteuid()})
            ):
                raise PolicyViolation("Source root/ancestor must not be a symlink")
            # Directory link counts change whenever an unrelated sibling directory is
            # created or removed.  They are not an object-identity signal and made a
            # pinned source root spuriously drift under concurrent test/process temp
            # activity.  Device/inode/mode/owner still bind every ancestor exactly.
            identity.extend((info.st_dev, info.st_ino, info.st_mode, info.st_uid))
        return tuple(identity)

    def _directory_chain(self, relative: str) -> None:
        descriptors: list[int] = []
        try:
            current = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(current)
            for part in PurePosixPath(relative).parts[:-1]:
                current = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
                )
                descriptors.append(current)
                info = os.fstat(current)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                ):
                    raise PolicyViolation("Source selected directory policy rejected")
        except OSError as exc:
            raise PolicyViolation("Source selected directory unavailable") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _read(self, relative: str, maximum: int, *, optional: bool = False) -> bytes | None:
        self._directory_chain(relative)
        try:
            with self._files._parent_handle(relative, create=False) as (parent, name, _):
                try:
                    descriptor = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                    )
                except FileNotFoundError:
                    if optional:
                        return None
                    raise PolicyViolation("Source selected file missing") from None
                try:
                    before = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_uid != os.geteuid()
                        or before.st_nlink != 1
                        or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                        or not 0 <= before.st_size <= maximum
                    ):
                        raise PolicyViolation("Source regular file byte bound failed")
                    chunks: list[bytes] = []
                    total = 0
                    while chunk := os.read(descriptor, min(65536, maximum + 1 - total)):
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > maximum:
                            raise PolicyViolation("Source read byte bound exceeded")
                    after = os.fstat(descriptor)
                    leaf = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if (
                        _identity(before) != _identity(after)
                        or _identity(after) != _identity(leaf)
                        or any(item.st_uid != os.geteuid() for item in (after, leaf))
                        or any(item.st_nlink != 1 for item in (after, leaf))
                        or any(
                            item.st_mode & (stat.S_IWGRP | stat.S_IWOTH) for item in (after, leaf)
                        )
                        or total != before.st_size
                    ):
                        raise PolicyViolation("Source changed during bounded capture")
                    with self._files._parent_handle(relative, create=False) as (
                        current_parent,
                        current_name,
                        _,
                    ):
                        current_leaf = os.stat(
                            current_name, dir_fd=current_parent, follow_symlinks=False
                        )
                        if _identity(current_leaf) != _identity(after):
                            raise PolicyViolation("Source parent path changed during capture")
                    payload = b"".join(chunks)
                finally:
                    os.close(descriptor)
        except FileNotFoundError as exc:
            if optional:
                return None
            raise PolicyViolation("Source safe file capture unavailable") from exc
        except OSError as exc:
            raise PolicyViolation("Source safe file capture unavailable") from exc
        if self._root() != self._root_identity:
            raise PolicyViolation("Source root identity changed")
        try:
            text = payload.decode("utf-8")
        except UnicodeError as exc:
            raise PolicyViolation("Source UTF-8 text required") from exc
        if "\x00" in text or scan_text(text, relative_path=relative, rules=SECRET_RULES):
            raise PolicyViolation("Source binary or secret content rejected")
        return payload

    def _git(
        self,
        arguments: tuple[str, ...],
        *,
        input_bytes: bytes | None = None,
        missing_ok: bool = False,
    ) -> bytes:
        # Fixed call sites only; no user subcommands/options or inherited Git routing.
        if (
            not isinstance(arguments, tuple)
            or any(not isinstance(arg, str) for arg in arguments)
            or type(missing_ok) is not bool
            or (
                input_bytes is not None
                and (not isinstance(input_bytes, bytes) or len(input_bytes) > 8192)
            )
        ):
            raise ValidationFailed("Source fixed Git inputs malformed")
        allowed = {
            ("rev-parse", "--show-toplevel", "--verify", "HEAD^{commit}"),
            ("check-ignore", "--no-index", "--stdin", "-z"),
        }
        if arguments not in allowed and not (
            arguments[:3] == ("ls-files", "--error-unmatch", "--")
            and arguments[3:] == self.recipe.allowed_paths
        ):
            raise ValidationFailed("Source Git operation outside fixed read-only recipe")
        environment = {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
        }
        try:
            code, stdout, stderr = _bounded_git_process(
                [
                    "git",
                    "--no-optional-locks",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    f"core.excludesFile={os.devnull}",
                    "-c",
                    "protocol.ext.allow=never",
                    "-C",
                    str(self.root),
                    *arguments,
                ],
                environment,
                input_bytes,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PolicyViolation("Source fixed Git observation failed or timed out") from exc
        if code not in ({0, 1} if missing_ok else {0}) or len(stdout) + len(stderr) > 16384:
            raise PolicyViolation("Source fixed Git observation failed or exceeded output bound")
        return stdout

    def _head(self) -> str:
        if self._root() != self._root_identity or self._git_layout() != self._git_identity:
            raise PolicyViolation("Source root or Git identity changed")
        raw = self._git(("rev-parse", "--show-toplevel", "--verify", "HEAD^{commit}"))
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= 16384:
            raise PolicyViolation("Source Git identity bounded bytes required")
        try:
            lines = raw.decode("utf-8").split("\n")
        except UnicodeError:
            raise PolicyViolation("Source Git identity encoding malformed") from None
        if (
            len(lines) != 3
            or lines[0] != str(self.root)
            or lines[2] != ""
            or "\x00" in lines[0]
            or "\r" in lines[0]
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", lines[1]) is None
        ):
            raise PolicyViolation("Source Git exact root and canonical commit lines required")
        head = lines[1]
        if self._root() != self._root_identity or self._git_layout() != self._git_identity:
            raise PolicyViolation("Source root or Git identity changed during observation")
        return head

    def _ignore_capture(self) -> tuple[tuple[tuple[str, str | None], ...], IgnoreMatcher]:
        if self._git_layout() != self._git_identity:
            raise PolicyViolation("Source Git identity changed")
        # Sole private metadata exception; never added to the corpus allowlist.
        names = {".git/info/exclude"}
        for path in self.recipe.allowed_paths:
            for parent in PurePosixPath(path).parents:
                for filename in (".gitignore", ".zekamignore"):
                    names.add((parent / filename).as_posix())
        captured: list[tuple[str, str | None]] = []
        matcher = IgnoreMatcher()
        # Parent rules must precede child rules. Metadata is sorted separately.
        for ref in sorted(names, key=lambda name: (name.count("/"), name)):
            payload = self._read(ref, MAX_IGNORE_BYTES, optional=True)
            captured.append((ref, None if payload is None else digest_of_bytes(payload)))
            if payload is not None and ref.endswith(".zekamignore"):
                lines = payload.decode().splitlines()
                if any(
                    any(char in line for char in "\\[]")
                    for line in lines
                    if line and not line.startswith("#")
                ):
                    raise PolicyViolation("Source custom ignore syntax outside reviewed subset")
                parent_ref = str(PurePosixPath(ref).parent)
                matcher = matcher.extended(
                    IgnoreMatcher.from_lines(lines, base="" if parent_ref == "." else parent_ref)
                )
        return tuple(sorted(captured)), matcher

    def _capture_once(self) -> ContinuitySourcePlan:
        head = self._head()
        ignores, custom = self._ignore_capture()
        self._git(("ls-files", "--error-unmatch", "--", *self.recipe.allowed_paths))
        ignored = self._git(
            ("check-ignore", "--no-index", "--stdin", "-z"),
            input_bytes=b"\x00".join(path.encode() for path in self.recipe.allowed_paths) + b"\x00",
            missing_ok=True,
        )
        if ignored or any(custom.is_path_ignored(path) for path in self.recipe.allowed_paths):
            raise PolicyViolation("Source selected file is ignored")
        files = []
        for path in self.recipe.allowed_paths:
            payload = self._read(path, MAX_SOURCE_BYTES)
            if payload is None or not payload:
                raise PolicyViolation("Source selected file empty or missing")
            files.append(CapturedSourceFile(path, digest_of_bytes(payload), len(payload)))
        if self._head() != head or self._ignore_capture()[0] != ignores:
            raise PolicyViolation("Source HEAD or ignore policy changed during capture")
        return ContinuitySourcePlan(
            self.recipe, head, tuple(files), ignores, _secret_policy_digest()
        )

    def capture(self) -> ContinuitySourcePlan:
        first = self._capture_once()
        if self._capture_once() != first:
            raise PolicyViolation("Source tuple changed between complete bounded captures")
        return first

    def _admit(self, db: sqlite3.Connection) -> None:
        recipe = self.recipe
        row = db.execute(
            "select 1 from source_binding b join project p on p.id=b.project_id"
            " join project_knowledge_realm r on r.project_id=p.id"
            " where b.id=? and b.project_id=? and b.source_kind='git' and b.active=1"
            " and p.status='active' and r.realm_id=?",
            (recipe.source_binding_id, recipe.project_id, recipe.realm_id),
        ).fetchone()
        configs = db.execute(
            "select id,task_digest,config_digest,length(cast(sanitized_json as blob))"
            " from config_revision where active=1 limit 2"
        ).fetchall()
        if row is None or len(configs) != 1:
            raise PolicyViolation("Source active binding/realm/task/policy admission drift")
        config = configs[0]
        if config[1] != recipe.task_digest or config[2] != recipe.policy_digest:
            raise PolicyViolation("Source active binding/realm/task/policy admission drift")
        if type(config[3]) is not int or not 0 < config[3] <= MAX_CONFIG_BYTES:
            raise PolicyViolation("Source admitted configuration byte bound drift")
        try:
            payload = db.execute(
                "select cast(sanitized_json as blob) from config_revision where id=?", (config[0],)
            ).fetchone()[0]
            if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_CONFIG_BYTES:
                raise ValueError
            raw = payload.decode("utf-8")
            document = json.loads(raw)
            if (
                not isinstance(document, dict)
                or canonical_json(document) != raw
                or digest(document) != recipe.policy_digest
            ):
                raise ValueError
        except (ValueError, TypeError, RecursionError, ValidationFailed):
            raise PolicyViolation("Source admitted configuration payload drift") from None

    def apply(
        self,
        store: SQLiteOperationalStore,
        plan: ContinuitySourcePlan,
        *,
        expected_plan_digest: str,
    ) -> SourceSnapshotRecord:
        if not isinstance(store, SQLiteOperationalStore) or not isinstance(
            plan, ContinuitySourcePlan
        ):
            raise ValidationFailed("Source typed store and reviewed plan required")
        plan.__post_init__()
        digest_text(expected_plan_digest)
        if plan.recipe != self.recipe or plan.content_digest != expected_plan_digest:
            raise PolicyViolation("Source exact reviewed plan digest mismatch")
        with store.unit_of_work() as uow:
            self._admit(uow._db())
            if self.capture() != plan:
                raise PolicyViolation("Source changed before snapshot admission")
            snapshot = uow.capture_source_snapshot(
                source_binding_id=self.recipe.source_binding_id,
                revision_ref=plan.revision_ref,
                tree_digest=plan.tree_digest,
                content_digest=plan.content_digest,
                config_digest=plan.config_digest,
            )
            latest = (
                uow._db()
                .execute(
                    "select id from source_snapshot where source_binding_id=?"
                    " order by captured_at desc,id desc limit 1",
                    (self.recipe.source_binding_id,),
                )
                .fetchone()
            )
            if latest is None or latest[0] != snapshot.id:
                raise PolicyViolation(
                    "Source prior snapshot superseded; reviewed reconciliation required"
                )
            if self.capture() != plan:
                raise PolicyViolation("Source changed before snapshot commit")
            self._admit(uow._db())
            uow.commit()
            return snapshot

    def assert_snapshot(
        self, store: SQLiteOperationalStore, snapshot_id: str
    ) -> ContinuitySourcePlan:
        if not isinstance(store, SQLiteOperationalStore):
            raise ValidationFailed("Source typed operational store required")
        uuid_text(snapshot_id, "Source snapshot")
        plan = self.capture()
        with closing(sqlite3.connect(f"{store._path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("pragma query_only=on")
            db.execute("begin")
            self._admit(db)
            row = db.execute("select * from source_snapshot where id=?", (snapshot_id,)).fetchone()
            latest = db.execute(
                "select id from source_snapshot where source_binding_id=?"
                " order by captured_at desc,id desc limit 1",
                (self.recipe.source_binding_id,),
            ).fetchone()
            expected: dict[str, Any] = {
                "source_binding_id": self.recipe.source_binding_id,
                "revision_ref": plan.revision_ref,
                "tree_digest": plan.tree_digest,
                "content_digest": plan.content_digest,
                "config_digest": plan.config_digest,
            }
            if (
                row is None
                or latest is None
                or latest[0] != snapshot_id
                or any(row[key] != value for key, value in expected.items())
            ):
                raise PolicyViolation("Source snapshot stale, unknown recipe or different capture")
        if self.capture() != plan:
            raise PolicyViolation("Source changed across snapshot verification")
        return plan

    def probe(self, store: SQLiteOperationalStore, binding: ContinuityBinding) -> str:
        if not isinstance(binding, ContinuityBinding):
            raise ValidationFailed("Source typed continuity binding required")
        binding.__post_init__()
        if any(
            getattr(binding, name) != getattr(self.recipe, name)
            for name in (
                "project_id",
                "realm_id",
                "task_digest",
                "policy_digest",
            )
        ):
            raise PolicyViolation("Source continuity binding differs from exact recipe")
        return self.assert_snapshot(store, binding.source_snapshot_id).content_digest
