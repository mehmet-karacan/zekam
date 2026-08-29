"""Negative guards for user-owned source and exact inverse patch rollback."""

from __future__ import annotations

import datetime as dt
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application.loop_rollback import LoopRollbackService
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.git.loop_patch import GitLoopPatchAdapter

pytestmark = pytest.mark.security
NOW = dt.datetime(2026, 8, 29, 12, 0, tzinfo=dt.UTC)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
    )


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Zekam Test")
    for name in ("target.txt", "user.txt", "outside.txt"):
        (root / name).write_text(f"{name}-before\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    return root


def test_initial_dirty_allowed_path_cannot_become_loop_owned(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "target.txt").write_text("user dirty\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="dirty allowed path"):
        GitLoopPatchAdapter(root).capture_baseline(
            attempt_id=uuid4(), allowed_paths=("target.txt",), captured_at=NOW
        )


def test_outside_allowlist_write_blocks_change_set(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    adapter = GitLoopPatchAdapter(root)
    baseline = adapter.capture_baseline(
        attempt_id=uuid4(), allowed_paths=("target.txt",), captured_at=NOW
    )
    (root / "target.txt").write_text("loop edit\n", encoding="utf-8")
    (root / "outside.txt").write_text("outside edit\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="allowed path disinda"):
        adapter.capture_change_set(baseline, created_at=NOW + dt.timedelta(seconds=1))


def test_user_dirty_drift_after_change_set_blocks_apply_before_mutation(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "user.txt").write_text("user baseline\n", encoding="utf-8")
    adapter = GitLoopPatchAdapter(root)
    baseline = adapter.capture_baseline(
        attempt_id=uuid4(), allowed_paths=("target.txt",), captured_at=NOW
    )
    (root / "target.txt").write_text("loop edit\n", encoding="utf-8")
    captured = adapter.capture_change_set(baseline, created_at=NOW + dt.timedelta(seconds=1))
    service = LoopRollbackService(adapter)
    plan = service.prepare(
        baseline=baseline.baseline,
        change_set=captured.change_set,
        reason_code="metric-regression",
        prepared_at=NOW + dt.timedelta(seconds=2),
    )
    (root / "user.txt").write_text("user continued\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="User dirty baseline"):
        service.execute(
            baseline=baseline.baseline,
            captured=captured,
            change_set=captured.change_set,
            plan=plan,
            checked_at=NOW + dt.timedelta(seconds=3),
            applied_at=NOW + dt.timedelta(seconds=4),
        )
    assert (root / "target.txt").read_text(encoding="utf-8") == "loop edit\n"
    assert (root / "user.txt").read_text(encoding="utf-8") == "user continued\n"


def test_inverse_patch_digest_tamper_is_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    adapter = GitLoopPatchAdapter(root)
    baseline = adapter.capture_baseline(
        attempt_id=uuid4(), allowed_paths=("target.txt",), captured_at=NOW
    )
    (root / "target.txt").write_text("loop edit\n", encoding="utf-8")
    captured = adapter.capture_change_set(baseline, created_at=NOW + dt.timedelta(seconds=1))
    with pytest.raises(ValidationFailed, match="inverse patch digest drift"):
        replace(captured, inverse_patch=b"tampered patch")


def test_non_regression_reason_cannot_prepare_rollback(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    adapter = GitLoopPatchAdapter(root)
    baseline = adapter.capture_baseline(
        attempt_id=uuid4(), allowed_paths=("target.txt",), captured_at=NOW
    )
    (root / "target.txt").write_text("loop edit\n", encoding="utf-8")
    captured = adapter.capture_change_set(baseline, created_at=NOW + dt.timedelta(seconds=1))
    with pytest.raises(PolicyViolation, match="regression/invalid"):
        LoopRollbackService(adapter).prepare(
            baseline=baseline.baseline,
            change_set=replace(captured.change_set),
            reason_code="cleanup",
            prepared_at=NOW + dt.timedelta(seconds=2),
        )
