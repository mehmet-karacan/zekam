"""Test agaci icerik digest'i: icerik-bagli, baglanti izlemez, sinirli (native yardimci yol)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.unit_test_runner.tree_fingerprint import (
    digest_test_tree,
    fingerprint_tree,
)


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _link_dir(link: Path, target: Path) -> bool:
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            )
            return result.returncode == 0
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


def test_digest_follows_content_not_mtime(tmp_path: Path) -> None:
    _write(tmp_path, "src/test/java/ATest.java", "class ATest {}")
    first = digest_test_tree(tmp_path, [""])
    assert list(first) == ["src/test/java/ATest.java"]
    os.utime(tmp_path / "src/test/java/ATest.java", (1, 1))
    assert digest_test_tree(tmp_path, [""]) == first  # mtime tazelik kaniti degil
    _write(tmp_path, "src/test/java/ATest.java", "class ATest { int x; }")
    changed = digest_test_tree(tmp_path, [""])
    assert changed != first
    assert fingerprint_tree(changed) != fingerprint_tree(first)


def test_multi_module_and_missing_test_dirs(tmp_path: Path) -> None:
    _write(tmp_path, "a/src/test/java/ATest.java", "a")
    (tmp_path / "b").mkdir()
    tree = digest_test_tree(tmp_path, ["a", "b"])
    assert list(tree) == ["a/src/test/java/ATest.java"]
    assert digest_test_tree(tmp_path, ["b"]) == {}  # test agaci yok: bos, hata degil


def test_linked_directories_are_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, "Secret.java", "disarida")
    _write(tmp_path / "proje", "src/test/java/ATest.java", "a")
    if not _link_dir(tmp_path / "proje" / "src" / "test" / "link", outside):
        pytest.skip("symlink/junction kurulamadi")
    tree = digest_test_tree(tmp_path / "proje", [""])
    assert list(tree) == ["src/test/java/ATest.java"]


def test_linked_test_root_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, "java/ATest.java", "disarida")
    root = tmp_path / "proje"
    (root / "src").mkdir(parents=True)
    if not _link_dir(root / "src" / "test", outside):
        pytest.skip("symlink/junction kurulamadi")
    with pytest.raises(Exception, match=r"baglanti|Symlink|junction|disina"):
        digest_test_tree(root, [""])


def test_file_count_bound_is_explicit_not_silent(tmp_path: Path) -> None:
    for index in range(4):
        _write(tmp_path, f"src/test/java/T{index}.java", str(index))
    with pytest.raises(ValidationFailed, match="siniri"):
        digest_test_tree(tmp_path, [""], max_files=3)
