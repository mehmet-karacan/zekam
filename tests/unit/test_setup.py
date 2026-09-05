"""Yeni makine setup plani ve dry-run CLI davranisi."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.setup import SETUP_PLAN_SCHEMA, build_setup_plan, setup_plan_digest
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.unit


def test_windows_setup_uses_schannel_before_database_changes() -> None:
    plan = build_setup_plan(platform="win32")

    assert plan[0].argv == ("git", "config", "--global", "http.sslBackend", "schannel")
    assert plan[1].step_id == "local-home-bootstrap"
    assert plan[1].argv == ("init", "--persistence", "sqlite")
    assert plan[-1].step_id == "final-local-core-doctor"
    assert plan[-1].mutates is False


def test_non_windows_setup_does_not_change_git_tls_backend() -> None:
    plan = build_setup_plan(platform="linux")

    assert all(step.step_id != "windows-git-ca" for step in plan)


def test_setup_plan_is_exact_home_and_digest_bound(tmp_path: Path) -> None:
    home = (tmp_path / "home").resolve()
    plan = build_setup_plan(platform="linux", home=home)

    assert plan == build_setup_plan(platform="linux", home=home)
    assert plan[0].argv == ("init", "--persistence", "sqlite", "--home", str(home))
    assert plan[1].argv == (
        "doctor",
        "--category",
        "sqlite",
        "--json",
        "--home",
        str(home),
    )
    assert setup_plan_digest(plan) == setup_plan_digest(plan)
    assert "postgresql" not in repr(plan).lower()
    assert not any(step.argv[:2] in {("policy", "init"), ("scheduler", "init")} for step in plan)


def test_setup_json_is_dry_run_by_default() -> None:
    result = CliRunner().invoke(app, ["setup", "--json"])

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["schema"] == SETUP_PLAN_SCHEMA
    assert document["apply"] is False
    assert document["guarantees"] == {
        "fresh_home_publish": "atomic",
        "replay": "idempotent",
        "apply_binding": "exact-plan-digest",
        "network": "not-required",
        "docker": "not-required",
    }
    assert document["plan_digest"].startswith("sha256:")
    assert document["steps"]


def test_setup_apply_rejects_missing_or_stale_plan_digest_before_effect() -> None:
    runner = CliRunner()

    missing = runner.invoke(app, ["setup", "--uygula", "--json"])
    stale = runner.invoke(
        app,
        ["setup", "--uygula", "--plan-digest", "sha256:" + "0" * 64, "--json"],
    )

    assert missing.exit_code == stale.exit_code == 64
    assert "exact --plan-digest" in missing.stderr
    assert "exact --plan-digest" in stale.stderr
