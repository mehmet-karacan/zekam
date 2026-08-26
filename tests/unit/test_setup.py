"""Yeni makine setup plani ve dry-run CLI davranisi."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from zekam.application.setup import build_setup_plan
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.unit


def test_windows_setup_uses_schannel_before_database_changes() -> None:
    plan = build_setup_plan(platform="win32")

    assert plan[0].argv == ("git", "config", "--global", "http.sslBackend", "schannel")
    assert plan[1].step_id == "home-layout"
    assert plan[-1].step_id == "final-doctor"
    assert plan[-1].mutates is False


def test_non_windows_setup_does_not_change_git_tls_backend() -> None:
    plan = build_setup_plan(platform="linux")

    assert all(step.step_id != "windows-git-ca" for step in plan)


def test_setup_json_is_dry_run_by_default() -> None:
    result = CliRunner().invoke(app, ["setup", "--json"])

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["schema"] == "zekam-setup-plan/v1"
    assert document["apply"] is False
    assert document["steps"]
