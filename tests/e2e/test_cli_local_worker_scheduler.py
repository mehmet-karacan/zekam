"""Dockerless local worker and scheduler command composition."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from typer.testing import CliRunner

from zekam.application.composition import build_context
from zekam.domain.canonical import digest
from zekam.infrastructure.local_analytics import RawAnalyticsEvent
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.interfaces.cli.main import app

runner = CliRunner()


def _init(home: Path) -> LocalCoreServices:
    result = runner.invoke(app, ["init", "--home", str(home)])
    assert result.exit_code == 0, result.stdout
    return LocalCoreServices.from_context(build_context(home=str(home)))


def test_worker_run_once_is_explicit_and_persists_terminal_result(tmp_path: Path) -> None:
    home = tmp_path / "worker-home"
    _init(home)
    submitted = runner.invoke(
        app,
        [
            "local-runtime",
            "submit-journal",
            "--home",
            str(home),
            "--idempotency-key",
            "worker-e2e",
            "--relative-path",
            "worker.log",
            "--line",
            "done",
        ],
    )
    assert submitted.exit_code == 0, submitted.stdout
    planned = runner.invoke(app, ["worker", "run-once", "--home", str(home)])
    assert planned.exit_code == 0
    assert json.loads(planned.stdout)["apply"] is False
    applied = runner.invoke(app, ["worker", "run-once", "--uygula", "--home", str(home)])
    assert applied.exit_code == 0, applied.stdout
    assert (home / "runtime" / "local-effects" / "worker.log").read_text().endswith("\tdone\n")
    status = runner.invoke(app, ["worker", "status", "--home", str(home)])
    assert status.exit_code == 0
    assert json.loads(status.stdout)["ready_jobs"] == 0
    assert json.loads(status.stdout)["running_jobs"] == 0


def test_scheduler_rebuild_report_and_reconcile_are_real_local_commands(tmp_path: Path) -> None:
    home = tmp_path / "scheduler-home"
    services = _init(home)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    services.analytics.append_segment(
        "scheduler-e2e",
        (
            RawAnalyticsEvent(
                event_id="11111111-1111-4111-8111-111111111111",
                event_type="runtime.outcome",
                occurred_at=now,
                project_ref="project:local",
                component="worker",
                component_version="1",
                adapter_version="1",
                dimensions=(("state", "completed"),),
                metrics=(("count", 1.0),),
                source_digest=digest({"fixture": "scheduler-local"}),
                work_ref="work:local",
                run_ref="run:local",
                session_ref="session:local",
            ),
        ),
    )
    rebuilt = runner.invoke(app, ["scheduler", "rebuild", "--uygula", "--home", str(home)])
    assert rebuilt.exit_code == 0, rebuilt.stdout
    report = runner.invoke(app, ["scheduler", "report", "--home", str(home)])
    assert report.exit_code == 0, report.stdout
    assert json.loads(report.stdout)["schema"] == "zekam-local-analytics-current/v1"
    reconciled = runner.invoke(app, ["scheduler", "reconcile", "--home", str(home)])
    assert reconciled.exit_code == 0
    assert json.loads(reconciled.stdout)["apply"] is False


def test_scheduler_report_represents_an_empty_analytics_home(tmp_path: Path) -> None:
    home = tmp_path / "empty-scheduler-home"
    _init(home)

    report = runner.invoke(app, ["scheduler", "report", "--home", str(home)])

    assert report.exit_code == 0, report.stdout
    document = json.loads(report.stdout)
    assert document["schema"] == "zekam-local-analytics-empty/v1"
    assert document["state"] == "empty"
    assert document["reports"] == {}
    assert document["read_only"] is True
    assert document["grants_authority"] is False
