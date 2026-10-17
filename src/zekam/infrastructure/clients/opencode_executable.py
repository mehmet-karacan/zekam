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
_DEFAULT_ALIASES = frozenset({"opencode", "opencode2"})


def resolve_opencode_executable(value: str = "opencode") -> Path:
    """Return the actual executable, rejecting missing/ambiguous shell shims."""

    found = shutil.which(value)
    if found is None:
        global_candidates = _global_npm_executables(value)
        if len(global_candidates) > 1:
            raise PolicyViolation("OpenCode global npm kurulumu belirsiz")
        if global_candidates:
            found = str(global_candidates[0])
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


def _global_npm_executables(value: str) -> tuple[Path, ...]:
    """Find the exact Windows npm global package layout without invoking npm.

    npm's command shim may be absent from PATH in a restricted host while the
    package's direct executable remains installed. Only the two supported
    package names and the default aliases are considered; an ambiguous global
    installation is reported by the caller instead of silently choosing one.
    """

    if os.name != "nt" or value.casefold() not in _DEFAULT_ALIASES:
        return ()
    roots: list[Path] = []
    for variable in ("APPDATA", "LOCALAPPDATA"):
        raw = os.environ.get(variable)
        if not raw:
            continue
        root = Path(raw) / "npm" / "node_modules"
        if root not in roots:
            roots.append(root)
    found: list[Path] = []
    for root in roots:
        for package in _PACKAGES:
            candidate = root.joinpath(*package, "bin", "opencode.exe")
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
    return tuple(found)


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
