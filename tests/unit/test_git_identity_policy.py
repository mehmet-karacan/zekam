from __future__ import annotations

import os
import subprocess
from pathlib import Path

from scripts.git_kimlik_politikasi import (
    EXPECTED_EMAIL,
    EXPECTED_NAME,
    commit_findings,
    message_findings,
    sanitize_history_message,
)


def test_message_policy_rejects_attribution_and_non_ascii() -> None:
    assert message_findings("Anlamli Turkce ASCII mesaj\n") == ()
    assert "ai-attribution-forbidden" in message_findings(
        "Anlamli mesaj\n\nCo-authored-by: Claude <noreply@example.test>\n"
    )
    assert "commit-message-non-ascii" in message_findings("Turkce ş mesaj")


def test_history_filter_removes_attribution_and_transliterates() -> None:
    result = sanitize_history_message(
        "Özellik: güvenli akış\n\nCo-authored-by: Claude <noreply@example.test>\n"
    )
    assert result == "Ozellik: guvenli aks"
    assert result.isascii()
    assert "Claude" not in result


def test_commit_metadata_policy_checks_author_and_committer(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repository, check=True)
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(("git", "add", "README.md"), cwd=repository, check=True)
    environment = os.environ | {
        "GIT_AUTHOR_NAME": EXPECTED_NAME,
        "GIT_AUTHOR_EMAIL": EXPECTED_EMAIL,
        "GIT_COMMITTER_NAME": EXPECTED_NAME,
        "GIT_COMMITTER_EMAIL": EXPECTED_EMAIL,
    }
    subprocess.run(
        ("git", "commit", "-q", "-m", "Test kaydini ekle"),
        cwd=repository,
        check=True,
        env=environment,
    )
    previous = Path.cwd()
    try:
        os.chdir(repository)
        assert commit_findings("HEAD") == ()
    finally:
        os.chdir(previous)
