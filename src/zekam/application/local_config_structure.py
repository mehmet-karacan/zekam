"""Hassas config dosyalarindan yalniz yerel yapi bilgisini oku.

Bu API acik cagrilir; kaynak kesfi, indeks veya provider akisina bagli degildir.
Degerler ve icerik digest'i sonuc nesnesine hicbir zaman alinmaz.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from zekam.application.secret_detection import scan_text
from zekam.application.source_discovery import _is_symlink_or_junction
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import assert_safe_relative
from zekam.domain.session_continuity import DataClassification

MAX_LOCAL_CONFIG_BYTES = 2 * 1024 * 1024
MAX_LOCAL_CONFIG_LINES = 20_000
MAX_LOCAL_CONFIG_KEYS = 10_000
_SAFE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
_SENSITIVE_KEY = re.compile(
    r"(?:secret|credential|password|passwd|parola|api[-_.]?key|private[-_.]?key|token)",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(?P<key>[^:=\s]+)\s*[:=]")


@dataclass(frozen=True, slots=True)
class ConfigKey:
    name: str
    line_number: int
    group: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "line": self.line_number, "group": self.group}


@dataclass(frozen=True, slots=True)
class LocalConfigStructure:
    source_format: str
    keys: tuple[ConfigKey, ...]
    line_count: int
    classification: DataClassification = field(default=DataClassification.LOCAL_ONLY, init=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": self.source_format,
            "classification": self.classification.value,
            "line_count": self.line_count,
            "keys": [key.as_dict() for key in self.keys],
        }


def _source_format(relative_path: str) -> str:
    name = PurePosixPath(relative_path).name.casefold()
    if name == ".env" or name.startswith(".env."):
        return "env"
    if name.endswith(".properties"):
        return "properties"
    raise PolicyViolation("Yalniz env veya properties yapisi okunabilir")


def _read_bounded(root: Path, relative_path: str, max_bytes: int) -> bytes:
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or not 1 <= max_bytes <= MAX_LOCAL_CONFIG_BYTES
    ):
        raise ValidationFailed("Yerel config byte siniri gecersiz")
    assert_safe_relative(relative_path, "config relative path")
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise PolicyViolation("Config koku dizin olmali")
    candidate = resolved_root
    for part in PurePosixPath(relative_path).parts:
        candidate = candidate / part
        if _is_symlink_or_junction(candidate):
            raise PolicyViolation("Config yolu symlink veya junction kullanamaz")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
        before = resolved.stat()
    except (OSError, ValueError) as exc:
        raise PolicyViolation("Config kaynagi guvenli okunamadi") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise PolicyViolation("Config duzenli dosya ve byte siniri ister")

    payload = bytearray()
    try:
        with resolved.open("rb") as handle:
            while True:
                chunk = handle.read(min(64 * 1024, max_bytes + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > max_bytes or len(payload) > before.st_size:
                    raise PolicyViolation("Config okuma sirasinda degisti")
        after = resolved.stat()
    except OSError as exc:
        raise PolicyViolation("Config kaynagi guvenli okunamadi") from exc
    if (
        len(payload) != before.st_size
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise PolicyViolation("Config okuma sirasinda degisti")
    return bytes(payload)


def parse_local_config_structure(
    root: Path,
    relative_path: str,
    *,
    max_bytes: int = MAX_LOCAL_CONFIG_BYTES,
) -> LocalConfigStructure:
    """Yalniz explicit yerel cagriyla, degerleri disari cikarmadan yapiyi oku."""
    source_format = _source_format(relative_path)
    payload = _read_bounded(root, relative_path, max_bytes)
    try:
        lines = payload.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        raise ValidationFailed("Config UTF-8 olarak cozulemedi") from None
    if len(lines) > MAX_LOCAL_CONFIG_LINES:
        raise PolicyViolation("Config satir siniri asildi")

    keys: list[ConfigKey] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!")):
            continue
        if line.rstrip().endswith("\\"):
            raise ValidationFailed("Config devam satiri desteklenmiyor")
        match = _ASSIGNMENT.match(line)
        if match is None:
            raise ValidationFailed("Config satiri desteklenmeyen yapi tasiyor")
        name = match.group("key")
        if (
            _SAFE_KEY.fullmatch(name) is None
            or _SENSITIVE_KEY.search(name)
            or scan_text(name, relative_path="config.key")
        ):
            raise PolicyViolation("Config anahtar adi guvenli degil")
        group = (name.rpartition(".")[0] or None) if source_format == "properties" else None
        keys.append(ConfigKey(name=name, line_number=line_number, group=group))
        if len(keys) > MAX_LOCAL_CONFIG_KEYS:
            raise PolicyViolation("Config anahtar siniri asildi")
    return LocalConfigStructure(source_format, tuple(keys), len(lines))
