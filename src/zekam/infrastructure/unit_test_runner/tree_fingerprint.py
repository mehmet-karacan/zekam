"""Hedef modullerin test agaci icerik digest'i (native yardimci yol, W05).

Native ajan test dosyalarini kendi araclariyla yazar; Zekam olcum oncesi ve sonrasi bu agacin
icerik digest'ini okur. Boylece "hangi test icerigiyle olculdu" kaniti ve olcum sirasinda test
degisimi (drift) mtime'a degil icerige dayanir. Baglanti/junction/reparse dosyalari ve dizinleri
izlenmez; sinirlar asilirsa sessizce kirpilmaz, ``ValidationFailed`` olur.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.unit_test_runner.path_safety import (
    is_link_or_junction,
    read_file_digest,
    resolve_inside,
)

MAX_TEST_TREE_FILES = 5000


def digest_test_tree(
    root: Path, module_dirs: Iterable[str], *, max_files: int = MAX_TEST_TREE_FILES
) -> dict[str, str]:
    """``<modul>/src/test`` altindaki her dosyanin icerik digest'i (proje-relative, posix)."""

    result: dict[str, str] = {}
    base = root.resolve(strict=True)
    for module in module_dirs:
        relative = f"{module}/src/test" if module else "src/test"
        directory = resolve_inside(base, relative)
        if not os.path.lexists(directory):
            continue
        if is_link_or_junction(directory) or not directory.is_dir():
            raise ValidationFailed("Test agaci koku dizin degil ya da baglanti")
        for current, dirnames, filenames in os.walk(directory, followlinks=False):
            current_path = Path(current)
            dirnames[:] = sorted(
                name for name in dirnames if not is_link_or_junction(current_path / name)
            )
            for name in sorted(filenames):
                path = current_path / name
                if is_link_or_junction(path):
                    continue
                if len(result) >= max_files:
                    raise ValidationFailed("Test agaci dosya siniri asildi")
                key = path.relative_to(base).as_posix()
                result[key] = read_file_digest(path)[1]
    return result


def fingerprint_tree(tree: dict[str, str]) -> str:
    """Agacin tek digest'i (sirasiz-kararli)."""

    return digest({"contract": "zekam-unit-test-tree/v1", "files": sorted(tree.items())})
