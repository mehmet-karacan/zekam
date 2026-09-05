"""Bounded, receipt-gated local-file effects for POSIX and Windows runtimes."""

from __future__ import annotations

import importlib
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from zekam.application.local_runtime import RUNTIME_OUTBOX_KINDS, LocalOutboxClaim, LocalOutboxEvent
from zekam.application.local_runtime_service import (
    LocalDeliveryResult,
    LocalEffectRequest,
    LocalEffectResult,
)
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_file_security import private_directory, private_regular


def _relative_parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValidationFailed("Local effect relative_path ister")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise PolicyViolation("Local effect path runtime root disina cikamaz")
    return candidate.parts


class LocalJournalEffectExecutor:
    def __init__(self, root: Path, *, pause_after_write_ms: int = 0) -> None:
        if type(pause_after_write_ms) is not int or not 0 <= pause_after_write_ms <= 60_000:
            raise ValidationFailed("Effect chaos pause 0..60000 ms olmali")
        self._directory = _PinnedJournalDirectory(root)
        self._pause = pause_after_write_ms / 1000

    def __call__(self, request: LocalEffectRequest) -> LocalEffectResult:
        if (
            not isinstance(request, LocalEffectRequest)
            or request.operation != "local.append-journal/v1"
            or not isinstance(request.payload, dict)
        ):
            raise ValidationFailed("Local effect operation desteklenmiyor")
        parts = _relative_parts(request.payload.get("relative_path"))
        key = request.idempotency_key
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 512
            or key != key.strip()
            or any(ord(character) < 32 for character in key)
        ):
            raise ValidationFailed("Local effect bounded single-line key required")
        value = request.payload.get("line")
        if not isinstance(value, str) or not value or "\n" in value:
            raise ValidationFailed("Local journal line tek satir metin olmali")
        try:
            payload = f"{key}\t{value}\n".encode()
        except UnicodeError:
            raise ValidationFailed("Local effect journal requires UTF-8") from None
        if len(payload) > MAX_EFFECT_JOURNAL_RECORD_BYTES:
            raise ValidationFailed("Local effect journal record byte bound exceeded")
        self._directory.append(parts, payload, pause=self._pause)
        return LocalEffectResult(
            "completed",
            digest({"idempotency_key": request.idempotency_key, "line": value}),
        )


MAX_JOURNAL_BYTES = 256 * 1024 * 1024
MAX_JOURNAL_RECORD_BYTES = 16 * 1024
MAX_EFFECT_JOURNAL_RECORD_BYTES = 1024 * 1024 + 4096


def _effective_user_id() -> int:
    getter = getattr(os, "geteuid")  # noqa: B009 -- POSIX-only branch
    return int(getter())


def _directory_identity(info: os.stat_result) -> tuple[int, ...]:
    if not stat.S_ISDIR(info.st_mode):
        raise PolicyViolation("Local outbox parent must be a physical directory")
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


class _PinnedJournalDirectory:
    """Lexical path admission with no-follow, held directory descriptors.

    Existing ancestors are pinned at construction. Missing descendants may be
    created only beneath the same observed ancestors; no existing bytes are removed.
    Re-observation detects path replacement but is not a same-UID filesystem lease.
    """

    def __init__(self, root: Path) -> None:
        if os.name not in {"posix", "nt"} or (os.name == "posix" and not hasattr(os, "O_NOFOLLOW")):
            raise PolicyViolation("Local outbox anchored journal unsupported on this platform")
        if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
            raise ValidationFailed("Local outbox absolute lexical root required")
        if root == Path(root.anchor):
            raise PolicyViolation("Local outbox root cannot be a filesystem root")
        self.root = root
        self._identities: dict[Path, tuple[int, ...]] = {}
        if os.name == "nt":
            if root.exists() and not private_directory(root):
                raise PolicyViolation("Local outbox root must be private and physical")
            return
        # Admission must remain read-only; a missing runtime directory is legal.
        with self.open(create=False, allow_missing=True):
            pass

    @contextmanager
    def open(
        self,
        *,
        create: bool,
        allow_missing: bool = False,
        relative_parent: tuple[str, ...] = (),
    ) -> Iterator[int | None]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(self.root.anchor, flags)
        path = Path(self.root.anchor)
        try:
            info = os.fstat(descriptor)
            identity = _directory_identity(info)
            if self._identities.setdefault(path, identity) != identity:
                raise PolicyViolation("Local outbox ancestor identity drift")
            for part in (*self.root.parts[1:], *relative_parent):
                path = path / part
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if path in self._identities:
                        raise PolicyViolation(
                            "Local outbox existing ancestor disappeared"
                        ) from None
                    if not create:
                        if allow_missing:
                            yield None
                            return
                        raise PolicyViolation("Local outbox admitted directory missing") from None
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(part, flags, dir_fd=descriptor)
                except OSError:
                    raise PolicyViolation("Local outbox parent symlink or invalid type") from None
                try:
                    info = os.fstat(child)
                    identity = _directory_identity(info)
                    if path.is_relative_to(self.root) and (
                        info.st_uid != _effective_user_id() or stat.S_IMODE(info.st_mode) & 0o022
                    ):
                        raise PolicyViolation(
                            "Local outbox root must be owned and not shared-writable"
                        )
                    if self._identities.setdefault(path, identity) != identity:
                        raise PolicyViolation("Local outbox ancestor identity drift")
                except BaseException:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def append(self, parts: tuple[str, ...], payload: bytes, *, pause: float) -> None:
        # Callers supply bounded bytes and validated relative components only.
        # The constructor fails explicitly on unsupported platforms.
        if os.name == "nt":
            self._append_windows(parts, payload, pause=pause)
            return
        fcntl: Any = importlib.import_module("fcntl")

        relative_parent, name = parts[:-1], parts[-1]
        with self.open(create=True, relative_parent=relative_parent) as parent:
            assert parent is not None
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_APPEND
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            descriptor = os.open(name, flags, 0o600, dir_fd=parent)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                before = os.fstat(descriptor)
                self._verify_leaf(before)
                if before.st_size + len(payload) > MAX_JOURNAL_BYTES:
                    raise PolicyViolation("Local outbox journal byte bound exceeded")
                self._verify_path(parent, parts, before)
                offset = before.st_size
                written = 0
                while written < len(payload):
                    count = os.write(descriptor, payload[written:])
                    if count <= 0:
                        raise OSError("Local outbox short write made no progress")
                    written += count
                os.fsync(descriptor)
                if pause:
                    time.sleep(pause)
                pread = getattr(os, "pread")  # noqa: B009 -- POSIX-only branch
                if pread(descriptor, len(payload), offset) != payload:
                    raise PolicyViolation("Local outbox appended record readback drift")
                after = os.fstat(descriptor)
                self._verify_leaf(after)
                if after.st_size != offset + len(payload):
                    raise PolicyViolation("Local outbox journal changed during append")
                os.fsync(parent)
                self._verify_path(parent, parts, after)
            finally:
                os.close(descriptor)

    def _append_windows(self, parts: tuple[str, ...], payload: bytes, *, pause: float) -> None:
        import msvcrt

        parent = self.root
        if not parent.exists():
            if not private_directory(parent.parent):
                raise PolicyViolation("Local outbox root parent must be private and physical")
            parent.mkdir()
        if not private_directory(parent):
            raise PolicyViolation("Local outbox root must be private and physical")
        for part in parts[:-1]:
            parent = parent / part
            with suppress(FileExistsError):
                parent.mkdir()
            if not private_directory(parent):
                raise PolicyViolation("Local outbox parent reparse or ACL drift")
        target = parent / parts[-1]
        existed = target.exists() or target.is_symlink()
        descriptor = os.open(
            target,
            os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
            0o600,
        )
        locked = False
        try:
            if not existed and not private_regular(target):
                raise PolicyViolation("Local outbox journal ACL invalid")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise PolicyViolation("Local outbox journal writer already active") from exc
            locked = True
            before = os.fstat(descriptor)
            self._verify_windows_leaf(target, before)
            if before.st_size + len(payload) > MAX_JOURNAL_BYTES:
                raise PolicyViolation("Local outbox journal byte bound exceeded")
            offset = os.lseek(descriptor, 0, os.SEEK_END)
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OSError("Local outbox short write made no progress")
                written += count
            os.fsync(descriptor)
            if pause:
                time.sleep(pause)
            os.lseek(descriptor, offset, os.SEEK_SET)
            if os.read(descriptor, len(payload)) != payload:
                raise PolicyViolation("Local outbox appended record readback drift")
            after = os.fstat(descriptor)
            self._verify_windows_leaf(target, after)
            if after.st_size != offset + len(payload):
                raise PolicyViolation("Local outbox journal changed during append")
        finally:
            if locked:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            os.close(descriptor)

    @staticmethod
    def _verify_windows_leaf(path: Path, opened: os.stat_result) -> None:
        if not private_regular(path):
            raise PolicyViolation("Local outbox private regular journal required")
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise PolicyViolation("Local outbox journal path identity drift")

    @staticmethod
    def _verify_leaf(info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != _effective_user_id()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise PolicyViolation("Local outbox owned single-link regular journal required")

    def _verify_path(self, parent: int, parts: tuple[str, ...], expected: os.stat_result) -> None:
        with self.open(create=False, relative_parent=parts[:-1]) as current:
            assert current is not None
            if _directory_identity(os.fstat(current)) != _directory_identity(os.fstat(parent)):
                raise PolicyViolation("Local outbox parent changed during append")
            info = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
            self._verify_leaf(info)
            if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
                raise PolicyViolation("Local outbox journal path identity drift")


class LocalJournalOutboxPublisher:
    def __init__(self, root: Path, *, pause_after_write_ms: int = 0) -> None:
        if type(pause_after_write_ms) is not int or not 0 <= pause_after_write_ms <= 60_000:
            raise ValidationFailed("Outbox chaos pause 0..60000 ms olmali")
        self._directory = _PinnedJournalDirectory(root)
        self._pause = pause_after_write_ms / 1000

    def __call__(self, claim: LocalOutboxClaim) -> LocalDeliveryResult:
        if not isinstance(claim, LocalOutboxClaim) or not isinstance(claim.event, LocalOutboxEvent):
            raise ValidationFailed("Local outbox typed claim required")
        event = claim.event
        if (
            event.event_kind not in RUNTIME_OUTBOX_KINDS
            or event.state != "claimed"
            or not isinstance(event.payload, dict)
            or digest(event.payload) != event.payload_digest
        ):
            raise PolicyViolation("Local outbox exact runtime observation required")
        key = event.idempotency_key
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 512
            or key != key.strip()
            or any(ord(character) < 32 for character in key)
        ):
            raise ValidationFailed("Local outbox bounded single-line key required")
        parse_digest(event.payload_digest)
        try:
            payload = f"{key}\t{event.payload_digest}\n".encode()
        except UnicodeError:
            raise ValidationFailed("Local outbox key must encode as UTF-8") from None
        if len(payload) > MAX_JOURNAL_RECORD_BYTES:
            raise ValidationFailed("Local outbox record byte bound exceeded")
        self._directory.append(("outbox-delivery.journal",), payload, pause=self._pause)
        return LocalDeliveryResult(
            "delivered",
            digest(
                {
                    "idempotency_key": claim.event.idempotency_key,
                    "payload_digest": claim.event.payload_digest,
                }
            ),
        )
