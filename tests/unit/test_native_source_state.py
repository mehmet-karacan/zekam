"""Native continuity: ayni `M` durum satiri farkli icerigi 'identical' gostermemeli."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from zekam.application.local_continuity_native import read_source_state

pytestmark = pytest.mark.unit


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_worktree_digest_changes_when_dirty_file_content_changes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("v0\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "ilk")

    clean = read_source_state(root)
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    first = read_source_state(root)
    (root / "a.txt").write_text("v2\n", encoding="utf-8")
    second = read_source_state(root)
    (root / "new.txt").write_text("x\n", encoding="utf-8")
    untracked = read_source_state(root)

    assert clean["revision"] == first["revision"] == second["revision"]
    assert not clean["dirty"] and first["dirty"]
    assert first["dirty_path_count"] == second["dirty_path_count"] == 1
    digests = {
        clean["worktree_digest"],
        first["worktree_digest"],
        second["worktree_digest"],
        untracked["worktree_digest"],
    }
    assert len(digests) == 4
