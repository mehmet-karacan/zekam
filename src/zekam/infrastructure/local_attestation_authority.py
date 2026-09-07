"""Private persistent key loader for one narrowly scoped local attestation authority."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from zekam.application.local_attestation import LocalAttestationSigner, LocalAttestationVerifier
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_file_security import (
    private_directory,
    private_regular,
    restrict_private_file,
)

_KEY_BYTES = 32


def load_or_create_local_attestation_authority(
    path: Path,
) -> tuple[LocalAttestationSigner, LocalAttestationVerifier]:
    if type(path) is not type(Path()) or not path.is_absolute() or path == Path(path.anchor):
        raise ValidationFailed("Local attestation authority exact absolute file required")
    if not private_directory(path.parent):
        raise PolicyViolation("Local attestation authority private parent required")
    created_identity: tuple[int, int, int] | None = None
    if not path.exists():
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            payload = secrets.token_bytes(_KEY_BYTES)
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OSError("Local attestation authority short write")
                written += count
            os.fsync(descriptor)
            created = os.fstat(descriptor)
            created_identity = (created.st_dev, created.st_ino, created.st_size)
        finally:
            os.close(descriptor)
        restrict_private_file(path)
    if not private_regular(path):
        raise PolicyViolation("Local attestation authority private regular file required")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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
            raise PolicyViolation("Local attestation authority read made no bounded progress")
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    identity = (after.st_dev, after.st_ino, after.st_size)
    if (
        len(payload) != _KEY_BYTES
        or (created_identity is not None and created_identity != identity)
        or (before.st_dev, before.st_ino, before.st_size) != identity
        or identity != (current.st_dev, current.st_ino, current.st_size)
    ):
        raise PolicyViolation("Local attestation authority identity/content drift")
    return LocalAttestationSigner(payload), LocalAttestationVerifier(payload)
