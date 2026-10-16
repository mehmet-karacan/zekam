"""Offline CLI regression sources; final collective test phase only."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from typer.testing import CliRunner

from zekam.application.composition import build_context
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.interfaces.cli.main import app


@pytest.mark.parametrize(
    "arguments",
    [
        ["local-core", "status"],
        ["local-runtime", "status"],
        ["scheduler", "status"],
        ["scheduler", "report"],
        ["doctor", "--category", "cognitive", "--json"],
        ["doctor", "--repair-plan", "--json"],
    ],
)
def test_status_and_plan_commands_do_not_recreate_deleted_key(tmp_path, arguments):
    home = tmp_path / "home"
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--home", str(home)])
    assert initialized.exit_code == 0, initialized.output
    key = home / "state" / "skill-receipt.key"
    key.unlink()
    result = runner.invoke(app, [*arguments, "--home", str(home)])
    assert isinstance(json.loads(result.output), dict), result.output
    assert not key.exists()


def test_paused_tick_returns_before_worker_and_key_initialization(tmp_path, monkeypatch):
    home = tmp_path / "home"
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--home", str(home)])
    assert initialized.exit_code == 0, initialized.output
    services = LocalCoreServices.from_context(build_context(home=home))
    services.improvement.set_evolution_control_state(
        "paused", reason="owner-pause", now=dt.datetime.now(dt.UTC)
    )
    key = home / "state" / "skill-receipt.key"
    key.unlink()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("paused tick constructed worker effects")

    monkeypatch.setattr("zekam.interfaces.cli.local_runtime._service", forbidden)
    result = runner.invoke(app, ["local-runtime", "tick", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert '"not-admitted"' in result.output
    assert not key.exists()
