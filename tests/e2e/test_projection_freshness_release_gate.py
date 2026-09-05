"""Projection-aware terminal transition guard at the public CLI boundary."""

from __future__ import annotations

from typer.testing import CliRunner

from zekam.interfaces.cli.main import app


def test_removed_raw_transition_command_cannot_bypass_projection_close() -> None:
    result = CliRunner().invoke(
        app,
        [
            "work",
            "transition",
            "zekam",
            "work-id",
            "completed",
            "--kanit",
            "test=local-suite",
            "--uygula",
        ],
    )

    assert result.exit_code == 2
    assert "No such command 'transition'" in result.output
