"""Proje-relative yol cozumu: traversal, symlink ve Windows junction/reparse reddi (M11)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.unit_test_engineering import normalize_relative_source

MAX_SMALL_FILE_BYTES = 4 * 1024 * 1024


def is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink() or os.path.isjunction(path):
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def resolve_inside(root: Path, relative: str) -> Path:
    """``root`` altindaki relative yolu cozer; baglanti/junction/traversal reddedilir.

    Bos ``relative`` kok dizinin kendisidir. Her ara bilesen tek tek denetlenir.
    """

    base = root.resolve(strict=True)
    if not base.is_dir():
        raise PolicyViolation("Proje koku dizin degil")
    if relative == "":
        return base
    normalize_relative_source(relative)
    if ":" in relative:
        raise PolicyViolation("Yol bileseni ':' tasiyamaz")
    current = base
    for part in relative.split("/"):
        current = current / part
        if os.path.lexists(current) and is_link_or_junction(current):
            raise PolicyViolation("Symlink/junction yol bileseni reddedildi")
    resolved = current.resolve(strict=False)
    if not resolved.is_relative_to(base):
        raise PolicyViolation("Yol proje kokunun disina cikiyor")
    return current


def read_file_digest(path: Path, *, max_bytes: int = MAX_SMALL_FILE_BYTES) -> tuple[bytes, str]:
    """Sinirli okuma; (icerik, sha256 digest)."""

    if is_link_or_junction(path) or not path.is_file():
        raise ValidationFailed("Dosya yok ya da baglanti")
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValidationFailed("Dosya boyut sinirini asiyor")
    return data, digest_of_bytes(data)
