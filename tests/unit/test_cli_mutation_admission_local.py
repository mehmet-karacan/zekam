from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.mutation_admission import _issue_gate_a_source_capability
from zekam.domain.errors import PolicyViolation
from zekam.interfaces.cli.main import app


def test_gate_a_capability_cannot_be_issued_outside_exact_cli_leaf() -> None:
    with pytest.raises(PolicyViolation):
        _issue_gate_a_source_capability(("continuity", "source-bind"), confirmed=True)


def test_active_local_mutations_remain_explicit(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    source = tmp_path / "source"
    source.mkdir()
    dry_run = runner.invoke(app, ["project", "add", str(source), "--home", str(home)])
    assert dry_run.exit_code == 0
    listed = runner.invoke(app, ["project", "list", "--home", str(home), "--json"])
    assert listed.exit_code == 0
    assert json.loads(listed.stdout) == []
    applied = runner.invoke(
        app,
        ["project", "add", str(source), "--home", str(home), "--uygula"],
    )
    assert applied.exit_code == 0, applied.stdout

    alias_added = runner.invoke(
        app,
        [
            "project",
            "alias-add",
            "source",
            "temporary",
            "--home",
            str(home),
            "--uygula",
        ],
    )
    assert alias_added.exit_code == 0, alias_added.stdout
    remove_dry_run = runner.invoke(
        app,
        ["project", "alias-remove", "source", "temporary", "--home", str(home)],
    )
    assert remove_dry_run.exit_code == 0, remove_dry_run.stdout
    assert (
        "temporary"
        in json.loads(
            runner.invoke(
                app, ["project", "resolve", "source", "--home", str(home), "--json"]
            ).stdout
        )["aliases"]
    )
    alias_removed = runner.invoke(
        app,
        [
            "project",
            "alias-remove",
            "source",
            "temporary",
            "--home",
            str(home),
            "--uygula",
        ],
    )
    assert alias_removed.exit_code == 0, alias_removed.stdout
    assert (
        "temporary"
        not in json.loads(
            runner.invoke(
                app, ["project", "resolve", "source", "--home", str(home), "--json"]
            ).stdout
        )["aliases"]
    )


def test_retired_provider_commands_are_not_advertised() -> None:
    help_result = CliRunner().invoke(app, ["--help"])
    assert help_result.exit_code == 0
    for command in ("memory", "loop", "oracle", "trace"):
        assert f" {command} " not in help_result.stdout
    assert " model " in help_result.stdout
    assert " local-core " in help_result.stdout
    assert " opencode " in help_result.stdout

    lifecycle_help = CliRunner().invoke(app, ["opencode", "--help"])
    assert lifecycle_help.exit_code == 0
    assert " event " in lifecycle_help.stdout
    assert " pre-compact " in lifecycle_help.stdout
    assert " forward " not in lifecycle_help.stdout
