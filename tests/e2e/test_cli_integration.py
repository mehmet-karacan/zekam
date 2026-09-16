"""CLI integration dry-run, claimed apply and terminal replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import zekam.application.client_integrations as client_integrations
import zekam.interfaces.cli.integration as integration_commands
from zekam.application.config import load_settings
from zekam.interfaces.cli.main import app


def test_user_policy_sync_requires_shown_digest_and_replays_terminal_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_integrations, "_user_inventory", lambda _root, **_kwargs: ())
    native_home = tmp_path / "native-home"
    native_home.mkdir()
    monkeypatch.setattr(integration_commands.Path, "home", lambda: native_home)
    home = tmp_path / "home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    planned = runner.invoke(
        app,
        [
            "integration",
            "sync",
            "--scope",
            "user",
            "--enable",
            "codex",
            "--home",
            str(home),
            "--json",
        ],
    )
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["apply"] is False
    assert plan["provider_calls"] == 0
    assert load_settings(home=home, environ={}).cli.integrations.codex is False

    arguments = [
        "integration",
        "sync",
        "--scope",
        "user",
        "--enable",
        "codex",
        "--plan-digest",
        plan["plan_digest"],
        "--uygula",
        "--home",
        str(home),
        "--json",
    ]
    applied = runner.invoke(app, arguments)
    assert applied.exit_code == 0, applied.output
    receipt = json.loads(applied.output)
    assert receipt["operational_terminal_evidence_digest"] == receipt["receipt_digest"]
    assert load_settings(home=home, environ={}).cli.integrations.codex is True

    replay = runner.invoke(app, arguments)
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output)["plan_digest"] == plan["plan_digest"]

    mismatched = runner.invoke(
        app,
        [
            "integration",
            "sync",
            "--scope",
            "user",
            "--disable",
            "codex",
            "--plan-digest",
            plan["plan_digest"],
            "--uygula",
            "--home",
            str(home),
            "--json",
        ],
    )
    assert mismatched.exit_code != 0
    assert load_settings(home=home, environ={}).cli.integrations.codex is True
