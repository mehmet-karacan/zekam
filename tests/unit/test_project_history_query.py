"""Bounded Jira issue history diff parser tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application import project_history_query as history
from zekam.infrastructure.git.source_reader import GitCommandError

pytestmark = pytest.mark.unit


def test_diff_files_rejects_incomplete_nul_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        history,
        "run_read_only",
        lambda _root, *arguments: "M\0src/main.py\0orphan-status\0",
    )

    with pytest.raises(GitCommandError, match="name-status kaydi eksik"):
        history._diff_files(tmp_path, "a" * 40, "b" * 40)


def test_diff_files_rejects_empty_path_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        history,
        "run_read_only",
        lambda _root, *arguments: "M\0\0",
    )

    with pytest.raises(GitCommandError, match="name-status kaydi eksik"):
        history._diff_files(tmp_path, "a" * 40, "b" * 40)


def test_diff_files_keeps_bounded_valid_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_git(_root: Path, *arguments: str) -> str:
        if arguments[1] == "--name-status":
            return "M\0src/main.py\0A\0src/new.py\0"
        return "@@ -1 +1 @@\n+safe\n"

    monkeypatch.setattr(history, "run_read_only", fake_git)

    files_total, files = history._diff_files(tmp_path, "a" * 40, "b" * 40)

    assert files_total == 2
    assert [(item.status, item.path, item.hunks) for item in files] == [
        ("M", "src/main.py", "@@ -1 +1 @@\n+safe"),
        ("A", "src/new.py", "@@ -1 +1 @@\n+safe"),
    ]
