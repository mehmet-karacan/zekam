"""Resolve OpenCode without executing npm's shell wrappers.

This resolves installation layout only; it does not attest CLI capabilities or
authorize a model call. Both historical and current npm package names occur in
supported installations.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from zekam.domain.errors import PolicyViolation, ValidationFailed

_MAX_SHIM_BYTES = 64 * 1024
_PACKAGES = (("@opencode", "cli"), ("opencode-ai",))


def resolve_opencode_executable(value: str = "opencode") -> Path:
    """Return the actual executable, rejecting missing/ambiguous shell shims."""

    found = shutil.which(value)
    if found is None:
        candidate = Path(value)
        if candidate.is_file():
            found = str(candidate)
    if found is None:
        raise ValidationFailed("OpenCode executable bulunamadi")
    try:
        path = Path(found).resolve(strict=True)
        if not path.is_file():
            raise ValidationFailed("OpenCode executable regular dosya olmali")
        if path.suffix.casefold() in {".cmd", ".bat", ".ps1"}:
            path = _resolve_npm_shim(path)
        if os.name == "nt" and path.suffix.casefold() != ".exe":
            raise PolicyViolation("Windows OpenCode adapter direct executable ister")
        return path
    except OSError as exc:
        raise ValidationFailed("OpenCode executable okunamadi") from exc


def _resolve_npm_shim(path: Path) -> Path:
    if path.stem.casefold() not in {"opencode", "opencode2"}:
        raise PolicyViolation("OpenCode shell shim adi desteklenmiyor")
    # Bound the read as well as the metadata check: a growing file cannot cause
    # an unbounded allocation. Never interpret or invoke the wrapper text.
    if path.stat().st_size > _MAX_SHIM_BYTES:
        raise PolicyViolation("OpenCode shell shim boyut sinirini asiyor")
    with path.open("rb") as stream:
        raw = stream.read(_MAX_SHIM_BYTES + 1)
    if len(raw) > _MAX_SHIM_BYTES:
        raise PolicyViolation("OpenCode shell shim boyut sinirini asiyor")
    try:
        text = raw.decode("utf-8-sig", errors="strict").replace("\\", "/").casefold()
    except UnicodeDecodeError as exc:
        raise PolicyViolation("OpenCode shell shim UTF-8 olmali") from exc
    packages = [
        parts for parts in _PACKAGES if f"node_modules/{'/'.join(parts)}/bin/opencode" in text
    ]
    if len(packages) != 1:
        raise PolicyViolation("OpenCode shell shim exact npm paketine cozumlenemedi")
    target = path.parent.joinpath("node_modules", *packages[0], "bin", "opencode.exe")
    if not target.is_file():
        raise ValidationFailed("OpenCode npm paketinde direct executable bulunamadi")
    return target.resolve(strict=True)
