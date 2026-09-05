"""Read-only Mac native identity inventory; never execute or copy a client.

Version is fixed artifact-pin metadata, not runtime version evidence. For the
known Codex Node package layout, inspect its native artifact without interpreting
or trusting the launcher program. No hook or executable-launch approval follows.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import stat
import struct
from pathlib import Path
from typing import Any

from zekam.application.local_client_identity import (
    MacNativeArtifactPin,
    NativeClientObservation,
    mac_native_pin,
)
from zekam.domain.errors import ConfigurationError, ValidationFailed

MAX_NATIVE_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_BYTES = 16384
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _reject(reason: str) -> ConfigurationError:
    return ConfigurationError(f"Local client inventory: {reason}")


def _absolute(path: Path) -> None:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or ".." in path.parts
        or any(ord(character) < 32 for character in str(path))
    ):
        raise ValidationFailed("Inventory requires typed canonical absolute paths")


def _path(path: Path) -> tuple[int, ...]:
    _absolute(path)
    identity: list[int] = []
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise _reject("symlink or non-directory ancestor")
        identity.extend((info.st_dev, info.st_ino, info.st_mode))
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise _reject("regular native or metadata file required")
    return (
        *identity,
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _native_identity(path: Path) -> tuple[tuple[int, ...], str]:
    before = _path(path)
    if not 32 <= before[-3] <= MAX_NATIVE_BYTES or not before[-4] & 0o111:
        raise _reject("native executable mode or size bound failed")
    with os.fdopen(os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK), "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != before[-6:-4]:
            raise _reject("native source changed before capture")
        header = source.read(32)
        if len(header) != 32:
            raise _reject("truncated native header")
        magic, cpu, subtype, kind, commands, command_bytes, _flags, reserved = struct.unpack(
            "<8I", header
        )
        if (
            (magic, cpu, subtype, kind, reserved) != (0xFEEDFACF, 0x0100000C, 0, 2, 0)
            or not 1 <= commands <= 4096
            or command_bytes < commands * 8
            or command_bytes > before[-3] - 32
        ):
            raise _reject("thin Mach-O arm64 executable required")
        fingerprint = hashlib.sha256(header)
        total = len(header)
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_NATIVE_BYTES:
                raise _reject("native executable byte bound exceeded")
            fingerprint.update(chunk)
        if total != before[-3] or _path(path) != before:
            raise _reject("native source changed during capture")
    return before, fingerprint.hexdigest()


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _reject("duplicate package metadata key")
        result[key] = value
    return result


def _package(path: Path, expected_version: str) -> tuple[tuple[int, ...], bytes]:
    before = _path(path)
    if not 1 <= before[-3] <= MAX_PACKAGE_BYTES:
        raise _reject("package metadata byte bound failed")
    with os.fdopen(os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != before[-6:-4]:
            raise _reject("package source changed")
        raw = stream.read(MAX_PACKAGE_BYTES + 1)
    if len(raw) != before[-3] or _path(path) != before:
        raise _reject("package source changed")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise _reject("package metadata malformed") from None
    if (
        not isinstance(document, dict)
        or document.get("name") != "@openai/codex"
        or document.get("version") != expected_version
    ):
        raise _reject("exact Codex package identity required")
    return before, raw


def _resolve(
    entrypoint: Path, pin: MacNativeArtifactPin
) -> tuple[Path, dict[Path, tuple[tuple[int, ...], bytes]]]:
    _absolute(entrypoint)
    resolved = entrypoint.resolve(strict=True)
    packages: dict[Path, tuple[tuple[int, ...], bytes]] = {}
    if pin.client_id == "codex" and resolved.suffix == ".js":
        if resolved.parts[-4:] != ("@openai", "codex", "bin", "codex.js"):
            raise _reject("unrecognized Codex launcher layout")
        _path(resolved)
        package_root = resolved.parent.parent
        platform_root = package_root / "node_modules/@openai/codex-darwin-arm64"
        # npm's platform alias package keeps name @openai/codex, not its alias.
        for path, version in (
            (package_root / "package.json", pin.version),
            (platform_root / "package.json", pin.version + "-darwin-arm64"),
        ):
            packages[path] = _package(path, version)
        resolved = platform_root / "vendor/aarch64-apple-darwin/bin/codex"
    _path(resolved)
    return resolved, packages


def inspect_macos_client(
    client_id: str, entrypoint: Path, observed_at: dt.datetime
) -> NativeClientObservation:
    """Inspect one exact native artifact, without executing even --version."""
    pin = mac_native_pin(client_id)
    observation = NativeClientObservation(pin, observed_at)
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise _reject("exact Darwin arm64 host required")
    try:
        _absolute(entrypoint)
        original_entrypoint = entrypoint.resolve(strict=True)
        native, packages = _resolve(entrypoint, pin)
        captured = _native_identity(native)
        if captured[1] != pin.native_sha256:
            raise _reject("installed native hash differs from exact inventory pin")
        if _native_identity(native) != captured:
            raise _reject("native identity changed across inventory observation")
        if entrypoint.resolve(strict=True) != original_entrypoint or _resolve(entrypoint, pin) != (
            native,
            packages,
        ):
            raise _reject("entrypoint or package identity changed across observation")
        return observation
    except (OSError, ValueError, TypeError, struct.error):
        raise _reject("native inventory capture failed") from None
