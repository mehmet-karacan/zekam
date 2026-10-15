"""Yarisli regular dosya okumalari icin ortak bounded stream yardimcilari."""

from __future__ import annotations

import stat
from pathlib import Path


def read_bounded_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Regular, link/reparse olmayan dosyayi en fazla ``max_bytes`` okur."""

    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes pozitif tamsayi olmali")
    metadata = path.lstat()
    reparse = bool(
        int(getattr(metadata, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )
    if (
        stat.S_ISLNK(metadata.st_mode)
        or reparse
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size < 0
        or metadata.st_size > max_bytes
    ):
        raise OSError("bounded regular file policy")
    with path.open("rb") as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise OSError("bounded regular file grew beyond limit")
    return payload


def read_bounded_text(path: Path, *, max_bytes: int) -> str:
    """UTF-8 strict bounded regular file metni okur."""

    return read_bounded_bytes(path, max_bytes=max_bytes).decode("utf-8", errors="strict")
