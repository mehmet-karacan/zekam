"""Private local key storage for trusted skill runtime receipts."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from zekam.application.skill_runtime import SkillRuntimeSigner, SkillRuntimeVerifier
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_file_security import (
    private_directory,
    private_regular,
    restrict_private_file,
)

_KEY_BYTES = 32


def _stable_identity(info: os.stat_result) -> tuple[int, ...]:
    """Use handle-stable fields on Windows, where pathlib inode values can drift."""

    if os.name == "nt":
        return (info.st_dev, info.st_size)
    return (info.st_dev, info.st_ino, info.st_size)


def load_or_create_skill_runtime_authority(
    path: Path,
) -> tuple[SkillRuntimeSigner, SkillRuntimeVerifier]:
    if type(path) is not type(Path()) or not path.is_absolute() or path == Path(path.anchor):
        raise ValidationFailed("Skill runtime authority exact absolute file ister")
    if not private_directory(path.parent):
        raise PolicyViolation("Skill runtime authority private parent ister")
    created_identity: tuple[int, ...] | None = None
    descriptor: int | None = None
    if not path.exists():
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(path, flags, 0o600)
        try:
            payload = secrets.token_bytes(_KEY_BYTES)
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OSError("Skill runtime authority short write")
                written += count
            os.fsync(descriptor)
            try:
                restrict_private_file(path)
            except OSError as exc:
                raise PolicyViolation("Skill runtime authority identity/content drift") from exc
            secured = os.fstat(descriptor)
            created_identity = _stable_identity(secured)
            os.lseek(descriptor, 0, os.SEEK_SET)
        except Exception:
            os.close(descriptor)
            raise
    if not private_regular(path):
        if descriptor is not None:
            os.close(descriptor)
        raise PolicyViolation("Skill runtime authority private regular file ister")
    if descriptor is None:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
        )
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = _KEY_BYTES + 1
        for _attempt in range(_KEY_BYTES + 2):
            if remaining == 0:
                break
            try:
                chunk = os.read(descriptor, remaining)
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        else:
            raise PolicyViolation("Skill runtime authority read made no bounded progress")
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    drift = []
    if len(payload) != _KEY_BYTES:
        drift.append(f"length-{len(payload)}")
    if created_identity is not None and created_identity != _stable_identity(before):
        drift.append("created-handle")
    if _stable_identity(before) != _stable_identity(after):
        drift.append("opened-handle")
    if os.name != "nt" and _stable_identity(after) != _stable_identity(current):
        drift.append("path-handle")
    if drift:
        raise PolicyViolation(
            "Skill runtime authority identity/content drift: " + ",".join(drift)
        )
    return SkillRuntimeSigner(payload), SkillRuntimeVerifier(payload)
