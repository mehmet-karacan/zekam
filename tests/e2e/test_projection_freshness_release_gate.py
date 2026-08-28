"""Projection-aware terminal transition guard at the public CLI boundary."""

from __future__ import annotations

from typer.testing import CliRunner

from zekam.interfaces.cli.main import app


def test_raw_completed_transition_fails_before_opening_database() -> None:
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

    assert result.exit_code == 64
    assert "projection-aware close/release" in result.output
