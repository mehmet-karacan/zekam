"""Yardimci scriptler icin bounded regular-file okumalari."""

from __future__ import annotations

import stat
from pathlib import Path


def _validate_limit(max_bytes: int) -> None:
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes non-negative int olmali")


def _regular_file(path: Path) -> None:
    metadata = path.lstat()
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if path.is_symlink() or (reparse_flag and attributes & reparse_flag):
        raise OSError("regular dosya yerine link veya reparse noktasi")
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("regular dosya gerekli")
    if metadata.st_size < 0:
        raise OSError("dosya boyutu gecersiz")


def read_bounded_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Dosyayi limit+1 stream ile oku ve link/growth durumunda reddet."""

    _validate_limit(max_bytes)
    _regular_file(path)
    if path.stat().st_size > max_bytes:
        raise OSError("dosya boyut sinirini asiyor")
    with path.open("rb") as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise OSError("dosya okuma sirasinda buyudu")
    return payload


def read_bounded_text(path: Path, *, max_bytes: int, encoding: str = "utf-8") -> str:
    """Bounded byte akisini strict metin olarak decode et."""

    return read_bounded_bytes(path, max_bytes=max_bytes).decode(encoding, errors="strict")
