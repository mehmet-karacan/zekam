"""Bounded Jira issue history diff parser tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application import project_history_query as history
from zekam.infrastructure.git.source_reader import GitCommandError

pytestmark = pytest.mark.unit


def test_parse_log_entries_rejects_empty_record_gap() -> None:
    valid = "a" * 40 + "\x1f\x1f2026-10-15T00:00:00+00:00\x1fSKYRSM-5659 konu"

    with pytest.raises(GitCommandError, match="Git log kaydi eksik"):
        history._parse_log_entries(valid + "\x1e\x1e" + valid + "\x1e")


def test_parse_log_entries_accepts_single_trailing_separator() -> None:
    valid = "a" * 40 + "\x1f\x1f2026-10-15T00:00:00+00:00\x1fSKYRSM-5659 konu"

    assert history._parse_log_entries(valid + "\x1e") == [valid]


def test_parse_log_entries_rejects_malformed_bounded_tail() -> None:
    valid = "a" * 40 + "\x1f\x1f2026-10-15T00:00:00+00:00\x1fSKYRSM-5659 konu"

    with pytest.raises(GitCommandError, match="Git log kaydi beklenen sekilde degil"):
        history._parse_log_entries(valid + "\x1e" + "malformed" + "\x1e")


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


def test_history_log_uses_observed_commit_instead_of_moving_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = "a" * 40
    calls: list[tuple[str, ...]] = []

    def fake_git(_root: Path, *arguments: str) -> str:
        calls.append(arguments)
        if arguments == ("rev-parse", "HEAD"):
            return observed
        if arguments == ("rev-parse", "--is-shallow-repository"):
            return "false"
        if arguments[0] == "log":
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(history, "run_read_only", fake_git)
    result = history.query_issue_history(tmp_path, "SKYRSM-5659")
    assert result.head == observed
    assert next(item for item in calls if item[0] == "log")[1] == observed


def test_history_rejects_invalid_head_before_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(history, "run_read_only", lambda *_args: "invalid-head")
    with pytest.raises(GitCommandError, match="HEAD kimligi"):
        history.query_issue_history(tmp_path, "SKYRSM-5659")


def test_history_does_not_drop_invalid_parent(tmp_path: Path) -> None:
    entry = "a" * 40 + "\x1finvalid-parent\x1f2026-10-15T00:00:00+00:00\x1fSKYRSM-5659 subject"
    with pytest.raises(GitCommandError, match="parent kimligi"):
        history._commit(tmp_path, entry, "SKYRSM-5659")


def test_history_redacts_secret_before_hunk_character_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synthetic = "gh" + "p_" + "a" * 36
    command: list[str] = []

    def fake_git(_root: Path, *arguments: str) -> str:
        command.extend(arguments)
        return "@@ -1 +1 @@\n+" + synthetic + "\n"

    monkeypatch.setattr(history, "MAX_HUNK_CHARS", 20)
    monkeypatch.setattr(history, "run_read_only", fake_git)
    hunks, truncated, redacted, _ = history._hunks(tmp_path, "a" * 40, "b" * 40, "a.txt", 20)
    assert hunks is None
    assert truncated and redacted
    assert "--no-textconv" in command


def test_history_redacts_subject_before_character_truncation(tmp_path: Path) -> None:
    synthetic = "gh" + "p_" + "a" * 36
    subject = "SKYRSM-5659 " + "x" * (history.MAX_SUBJECT_CHARS - 20) + " " + synthetic
    entry = "a" * 40 + "\x1f\x1f2026-10-15T00:00:00+00:00\x1f" + subject
    result = history._commit(tmp_path, entry, "SKYRSM-5659")
    assert result.subject_redacted
    assert result.subject == "[redacted: secret-pattern]"
