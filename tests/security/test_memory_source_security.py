from __future__ import annotations

import json
import subprocess
from pathlib import Path

from zekam.application.source_security import (
    GitSurface,
    SecretScanAllowance,
    SecretScanAllowlist,
    apply_secret_scan_allowlist,
    scan_git_security,
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "security-test@example.invalid")
    _git(root, "config", "user.name", "Security Test")
    return root


def test_tracked_secret_is_found_even_when_gitignore_later_matches(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    token = "ghp_" + "A" * 36
    (root / "local.env").write_text(f"token={token}\n", encoding="utf-8")
    _git(root, "add", "local.env")
    _git(root, "commit", "-m", "fixture")
    (root / ".gitignore").write_text("*.env\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    report = scan_git_security(root, include_history=False)
    assert any(item.relative_path == "local.env" for item in report.findings)
    assert token not in json.dumps(report.as_dict())


def test_removed_secret_remains_history_revoke_rotate_blocker(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    token = "ghp_" + "B" * 36
    (root / "credential.txt").write_text(token, encoding="utf-8")
    _git(root, "add", "credential.txt")
    _git(root, "commit", "-m", "fixture")
    (root / "credential.txt").unlink()
    _git(root, "add", "-u")
    _git(root, "commit", "-m", "remove")
    report = scan_git_security(root)
    assert report.requires_revoke_or_rotate is True
    assert any(item.surface is GitSurface.HISTORY for item in report.findings)
    assert token not in json.dumps(report.as_dict())


def test_staged_backup_artifact_blocks_without_reading_its_content(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "database.dump").write_bytes(b"\x00binary\x00")
    _git(root, "add", "database.dump")
    report = scan_git_security(root, include_history=False)
    assert report.passed is False
    assert [item.code for item in report.findings] == ["secret-or-backup-artifact"]
    assert report.findings[0].surface is GitSurface.STAGED


def test_allowlist_matches_only_exact_path_rule_fingerprint_and_not_backup(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    token = "ghp_" + "C" * 36
    (root / "fixture.txt").write_text(token, encoding="utf-8")
    _git(root, "add", "fixture.txt")
    report = scan_git_security(root, include_history=False)
    finding = report.findings[0]
    assert finding.rule_id is not None and finding.fingerprint is not None
    allowlist = SecretScanAllowlist(
        (
            SecretScanAllowance(
                surface="current",
                relative_path=finding.relative_path,
                rule_id=finding.rule_id,
                fingerprint=finding.fingerprint,
            ),
        )
    )
    filtered = apply_secret_scan_allowlist(report, allowlist)
    assert filtered.passed is True
    assert filtered.reviewed_allowance_count == 1
    (root / "database.dump").write_bytes(b"\x00fixture\x00")
    _git(root, "add", "database.dump")
    with_backup = apply_secret_scan_allowlist(
        scan_git_security(root, include_history=False), allowlist
    )
    assert [item.code for item in with_backup.findings] == ["secret-or-backup-artifact"]


def test_untracked_worktree_secret_is_scanned_before_staging(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    token = "ghp_" + "D" * 36
    (root / "new-source.py").write_text(f"value = '{token}'\n", encoding="utf-8")
    report = scan_git_security(root, include_history=False)
    assert report.worktree_file_count == 1
    assert report.findings[0].surface is GitSurface.WORKTREE
    assert token not in json.dumps(report.as_dict())
