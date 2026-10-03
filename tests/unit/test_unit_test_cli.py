from datetime import UTC, datetime

from typer.testing import CliRunner

from zekam.application.composition import build_context
from zekam.application.unit_test_outcome import (
    EXIT_CLARIFICATION,
    EXIT_ENVIRONMENT_MISSING,
)
from zekam.domain.unit_test_engineering import UnitTestBudget, UnitTestRequest
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
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


def test_plan_reports_maven_readiness_without_running_provider_or_maven(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "test",
            "plan",
            "--project-root",
            str(tmp_path),
            "--project-id",
            "project-1",
            "--source-binding-id",
            "binding-1",
            "--source-revision",
            "revision-1",
            "--source",
            "src/main/java/Foo.java",
            "--percent",
            "90",
        ],
    )
    assert result.exit_code == 0
    assert '"provider_calls": 0' in result.stdout
    assert '"status": "not-supported"' in result.stdout


def test_pause_is_fail_closed_until_durable_control_is_composed() -> None:
    result = runner.invoke(app, ["test", "pause", "sha256:" + "a" * 64])
    assert result.exit_code == EXIT_ENVIRONMENT_MISSING
    assert "migration-required" in result.stdout


def test_pause_resume_cancel_cli_writes_durable_terminals(tmp_path) -> None:
    context = build_context(home=tmp_path)
    database = context.settings.database.sqlite_path(context.home)
    schema.bootstrap_v6(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="demo", display_name="Demo")
        request = UnitTestRequest.with_defaults(
            project_id=project.id,
            source_binding_id="binding-1",
            source_revision="revision-1",
            source_files=["src/main/java/Foo.java"],
            percent="80",
            budget=UnitTestBudget(2, 60, 600),
        )
        uow.unit_test_ledger().register_request(request, now=datetime.now(UTC))
        uow.commit()

    paused = runner.invoke(app, ["test", "pause", request.request_digest, "--home", str(tmp_path)])
    assert paused.exit_code == 0, paused.stdout
    assert '"status": "recorded"' in paused.stdout
    resumed = runner.invoke(
        app, ["test", "resume", request.request_digest, "--home", str(tmp_path)]
    )
    assert resumed.exit_code == 0, resumed.stdout
    assert '"status": "ready"' in resumed.stdout
    cancelled = runner.invoke(
        app, ["test", "cancel", request.request_digest, "--home", str(tmp_path)]
    )
    assert cancelled.exit_code == 0, cancelled.stdout
    assert '"reason": "user-cancelled"' in cancelled.stdout
