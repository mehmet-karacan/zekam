from typer.testing import CliRunner

from zekam.application.unit_test_outcome import (
    EXIT_CLARIFICATION,
    EXIT_ENVIRONMENT_MISSING,
)
from zekam.interfaces.cli.main import app

runner = CliRunner()


def test_test_plan_is_provider_free_and_shows_exact_budget() -> None:
    result = runner.invoke(
        app,
        [
            "test",
            "plan",
            "--project-id",
            "project-1",
            "--source-binding-id",
            "binding-1",
            "--source-revision",
            "sha256:" + "a" * 64,
            "--source",
            "src/main/java/Foo.java",
            "--percent",
            "90",
            "--max-attempts",
            "4",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert '"provider_calls": 0' in result.stdout
    assert '"max_attempts": 4' in result.stdout
    assert '"authorization": "not-granted"' in result.stdout


def test_natural_language_plan_does_not_guess_missing_target() -> None:
    result = runner.invoke(app, ["test", "plan", "--request", "unit testleri yaz"])
    assert result.exit_code == EXIT_CLARIFICATION
    assert "target-files-missing" in result.stdout


def test_run_needs_exact_plan_and_explicit_authorization() -> None:
    result = runner.invoke(app, ["test", "run", "--authorize"])
    assert result.exit_code != 0
    assert "exact-plan-digest-missing" in result.stdout


def test_pause_is_fail_closed_until_durable_control_is_composed() -> None:
    result = runner.invoke(app, ["test", "pause", "sha256:" + "a" * 64])
    assert result.exit_code == EXIT_ENVIRONMENT_MISSING
    assert "durable-control" in result.stdout
