"""Doctor Git fast-forward plan and adapter tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from zekam.application.doctor_repair import apply_git_fast_forward, plan_git_fast_forward
from zekam.domain.errors import PolicyViolation

pytestmark = pytest.mark.unit


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _repositories(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    writer = tmp_path / "writer"
    local = tmp_path / "local"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(writer)], check=True, capture_output=True)
    _git(writer, "config", "user.name", "Zekam Test")
    _git(writer, "config", "user.email", "zekam-test@example.invalid")
    (writer / "state.txt").write_text("one\n", encoding="utf-8")
    _git(writer, "add", "state.txt")
    _git(writer, "commit", "-m", "initial")
    _git(writer, "push", "-u", "origin", "HEAD")
    subprocess.run(["git", "clone", str(remote), str(local)], check=True, capture_output=True)
    _git(local, "config", "user.name", "Zekam Test")
    _git(local, "config", "user.email", "zekam-test@example.invalid")
    return writer, local


def _advance(writer: Path, value: str) -> str:
    (writer / "state.txt").write_text(value + "\n", encoding="utf-8")
    _git(writer, "add", "state.txt")
    _git(writer, "commit", "-m", f"advance-{value}")
    _git(writer, "push")
    return _git(writer, "rev-parse", "HEAD")


def test_exact_cached_remote_head_fast_forwards_clean_worktree(tmp_path: Path) -> None:
    writer, local = _repositories(tmp_path)
    expected = _advance(writer, "two")
    _git(local, "fetch", "origin")
    plan = plan_git_fast_forward(local)

    result = apply_git_fast_forward(local, plan=plan, plan_digest=plan.plan_digest)

    assert plan.required
    assert plan.blocked_reasons == ()
    assert result.changed
    assert result.new_head == expected
    assert _git(local, "status", "--porcelain") == ""


def test_dirty_worktree_blocks_fast_forward(tmp_path: Path) -> None:
    writer, local = _repositories(tmp_path)
    _advance(writer, "two")
    _git(local, "fetch", "origin")
    (local / "local.txt").write_text("dirty\n", encoding="utf-8")

    plan = plan_git_fast_forward(local)

    assert plan.required
    assert "worktree-dirty" in plan.blocked_reasons
    with pytest.raises(PolicyViolation, match="bloke"):
        apply_git_fast_forward(local, plan=plan, plan_digest=plan.plan_digest)


def test_remote_drift_after_plan_is_rejected_without_merge(tmp_path: Path) -> None:
    writer, local = _repositories(tmp_path)
    _advance(writer, "two")
    _git(local, "fetch", "origin")
    plan = plan_git_fast_forward(local)
    old_head = _git(local, "rev-parse", "HEAD")
    _advance(writer, "three")

    with pytest.raises(PolicyViolation, match="Remote HEAD plan sonrasinda degisti"):
        apply_git_fast_forward(local, plan=plan, plan_digest=plan.plan_digest)

    assert _git(local, "rev-parse", "HEAD") == old_head
