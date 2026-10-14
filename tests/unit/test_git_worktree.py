"""Bounded Git worktree evidence tests."""

from __future__ import annotations

from pathlib import Path

from zekam.infrastructure.git import worktree


def test_worktree_git_runner_uses_bounded_reader(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def bounded(root: Path, *arguments: str) -> str:
        calls.append((root, arguments))
        return "bounded patch\n"

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("worktree diff must not use subprocess.run")

    monkeypatch.setattr(worktree, "run_read_only", bounded)
    monkeypatch.setattr(worktree.subprocess, "run", forbidden)

    result = worktree._run(tmp_path, "diff", "--no-color", "HEAD")

    assert result.returncode == 0
    assert result.stdout == "bounded patch\n"
    assert calls == [(tmp_path, ("diff", "--no-color", "HEAD"))]
