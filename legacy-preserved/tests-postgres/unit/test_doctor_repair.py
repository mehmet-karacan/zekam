"""Doctor Git fast-forward plan and adapter tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.doctor_repair import (
    DatabaseMigrationPlan,
    DoctorRepairPlan,
    apply_git_fast_forward,
    plan_git_fast_forward,
)
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.postgres.migrations import (
    AppliedMigration,
    DriftFinding,
    DriftKind,
    Migration,
    MigrationStatus,
)
from zekam.interfaces.cli.main import app

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


def _migration(version: int, tmp_path: Path, checksum: str = "a" * 64) -> Migration:
    return Migration(
        version=version,
        name=f"change_{version}",
        path=tmp_path / f"{version:04d}_change_{version}.sql",
        checksum=checksum,
    )


def test_doctor_plan_selects_one_exact_migration_before_routine_repair(
    tmp_path: Path,
) -> None:
    writer, local = _repositories(tmp_path)
    del writer
    git = plan_git_fast_forward(local)
    first = _migration(2, tmp_path)
    second = _migration(3, tmp_path, checksum="b" * 64)
    status = MigrationStatus(
        head=1,
        applied=(AppliedMigration(1, "base", "c" * 64),),
        pending=(first, second),
        drift=(),
    )
    migration_plan = DatabaseMigrationPlan(status=status, blocked_reasons=())
    plan = DoctorRepairPlan(git=git, migrations=migration_plan, routines=None)

    assert migration_plan.next_migration == first
    assert plan.required_steps == ("postgres-migration-upgrade",)
    assert plan.next_step == "postgres-migration-upgrade"
    assert plan.as_dict()["migrations"]["target"] == {
        "version": 2,
        "name": "change_2",
        "checksum": "a" * 64,
    }


def test_migration_drift_is_fail_closed_and_changes_plan_digest(tmp_path: Path) -> None:
    pending = _migration(2, tmp_path)
    clean = DatabaseMigrationPlan(
        status=MigrationStatus(head=1, applied=(), pending=(pending,), drift=()),
        blocked_reasons=(),
    )
    drifted = DatabaseMigrationPlan(
        status=MigrationStatus(
            head=1,
            applied=(),
            pending=(pending,),
            drift=(DriftFinding(DriftKind.CHECKSUM_MISMATCH, 1, "changed"),),
        ),
        blocked_reasons=("migration-drift",),
    )

    assert clean.plan_digest != drifted.plan_digest
    assert drifted.required
    assert not drifted.as_dict()["applicable"]


def test_doctor_cli_exposes_explicit_bounded_prepare_option() -> None:
    result = CliRunner().invoke(app, ["doctor", "--help"])

    assert result.exit_code == 0
    assert "--hazirla" in result.stdout
    assert "pending migration ve routine" in result.stdout
