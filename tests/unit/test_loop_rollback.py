"""Loop-owned inverse patch unit/integration-with-local-Git tests."""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from uuid import uuid4

from zekam.application.loop_rollback import LoopRollbackService
from zekam.infrastructure.git.loop_patch import GitLoopPatchAdapter

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
    (root / "target.txt").write_text("before\n", encoding="utf-8")
    (root / "user.txt").write_text("original\n", encoding="utf-8")
    _git(root, "add", "target.txt", "user.txt")
    _git(root, "commit", "-m", "baseline")
    return root


def test_regression_only_inverse_patch_restores_target_and_preserves_user_dirty(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    (root / "user.txt").write_text("user work\n", encoding="utf-8")
    adapter = GitLoopPatchAdapter(root)
    captured_baseline = adapter.capture_baseline(
        attempt_id=uuid4(),
        allowed_paths=("target.txt",),
        captured_at=NOW,
    )
    (root / "target.txt").write_text("loop regression\n", encoding="utf-8")
    captured_change = adapter.capture_change_set(
        captured_baseline,
        created_at=NOW + dt.timedelta(seconds=1),
    )
    service = LoopRollbackService(adapter)
    plan = service.prepare(
        baseline=captured_baseline.baseline,
        change_set=captured_change.change_set,
        reason_code="metric-regression",
        prepared_at=NOW + dt.timedelta(seconds=2),
    )
    receipt = service.execute(
        baseline=captured_baseline.baseline,
        captured=captured_change,
        change_set=captured_change.change_set,
        plan=plan,
        checked_at=NOW + dt.timedelta(seconds=3),
        applied_at=NOW + dt.timedelta(seconds=4),
    )

    assert (root / "target.txt").read_text(encoding="utf-8") == "before\n"
    assert (root / "user.txt").read_text(encoding="utf-8") == "user work\n"
    assert receipt.changed_resources == ("target.txt",)
    assert receipt.status == "applied"
    assert receipt.apply_check_digest.startswith("sha256:")


def test_loop_change_set_handles_exact_new_file_without_touching_other_paths(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    adapter = GitLoopPatchAdapter(root)
    baseline = adapter.capture_baseline(
        attempt_id=uuid4(),
        allowed_paths=("generated.txt",),
        captured_at=NOW,
    )
    (root / "generated.txt").write_text("generated\n", encoding="utf-8")
    captured = adapter.capture_change_set(baseline, created_at=NOW + dt.timedelta(seconds=1))
    service = LoopRollbackService(adapter)
    plan = service.prepare(
        baseline=baseline.baseline,
        change_set=captured.change_set,
        reason_code="validator-invalid",
        prepared_at=NOW + dt.timedelta(seconds=2),
    )
    service.execute(
        baseline=baseline.baseline,
        captured=captured,
        change_set=captured.change_set,
        plan=plan,
        checked_at=NOW + dt.timedelta(seconds=3),
        applied_at=NOW + dt.timedelta(seconds=4),
    )
    assert not (root / "generated.txt").exists()

