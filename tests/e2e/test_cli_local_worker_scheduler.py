"""Dockerless local worker and scheduler command composition."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.composition import build_context
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.local_analytics import RawAnalyticsEvent
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.infrastructure.local_file_security import private_regular
from zekam.interfaces.cli import local_runtime as local_runtime_cli
from zekam.interfaces.cli.main import app

runner = CliRunner()


def _init(home: Path) -> LocalCoreServices:
    result = runner.invoke(app, ["init", "--home", str(home)])
    assert result.exit_code == 0, (result.stdout, result.stderr, result.exception)
    return LocalCoreServices.from_context(build_context(home=str(home)))


def test_skill_runtime_authority_is_private_and_stable_across_composition(
    tmp_path: Path,
) -> None:
    home = tmp_path / "skill-authority-home"
    first = _init(home)
    body = {"schema": "test-skill-attestation/v1", "value": digest("value")}
    first_attestation = first.skill_runtime_signer.attest(body)

    second = LocalCoreServices.from_context(build_context(home=str(home)))

    assert second.skill_runtime_signer.attest(body) == first_attestation
    assert private_regular(home / "state" / "skill-receipt.key")


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


def test_os_supervisor_tick_runs_typed_job_effect_and_terminal_receipt(tmp_path: Path) -> None:
    home = tmp_path / "supervisor-home"
    _init(home)

    first = runner.invoke(app, ["local-runtime", "tick", "--home", str(home)])
    assert first.exit_code == 0, first.stdout
    document = json.loads(first.stdout)
    assert document["schema"] == "zekam-os-supervisor-tick-receipt/v1"
    assert document["job_created"] is True
    assert document["terminal_state"] == "completed"
    assert document["terminal_evidence_digest"].startswith("sha256:")
    assert document["daily_job_created"] is True
    assert document["daily_terminal_state"] == "completed"
    assert document["daily_terminal_evidence_digest"].startswith("sha256:")
    assert document["provider_calls"] == document["network_calls"] == 0
    journal = home / "runtime/local-effects/evolution/maintenance-reconcile.jsonl"
    assert journal.is_file()
    daily_notes = tuple((home / "global/generated/daylog").glob("*.md"))
    assert len(daily_notes) == 1

    replay = runner.invoke(app, ["local-runtime", "tick", "--home", str(home)])
    assert replay.exit_code == 0, replay.stdout
    replay_document = json.loads(replay.stdout)
    assert replay_document["job_created"] is False
    assert replay_document["daily_job_created"] is False
    assert replay_document["daily_terminal_state"] == "not-due"
    assert replay_document["daily_terminal_evidence_digest"] is None
    assert replay_document["terminal_state"] == "completed"
    assert journal.read_text(encoding="utf-8").count("\n") == 1
    assert tuple((home / "global/generated/daylog").glob("*.md")) == daily_notes


def test_evolution_pause_resume_disable_controls_real_tick_admission(tmp_path: Path) -> None:
    home = tmp_path / "evolution-control-home"
    _init(home)

    preview = runner.invoke(app, ["evolve", "pause", "--home", str(home)])
    assert preview.exit_code == 0, preview.stdout
    assert json.loads(preview.stdout)["apply"] is False

    paused = runner.invoke(
        app, ["evolve", "pause", "--uygula", "--home", str(home)]
    )
    assert paused.exit_code == 0, paused.stdout
    assert json.loads(paused.stdout)["result"]["state"] == "paused"
    tick = runner.invoke(app, ["local-runtime", "tick", "--home", str(home)])
    assert tick.exit_code == 0, tick.stdout
    stopped = json.loads(tick.stdout)
    assert stopped["state"] == "paused"
    assert stopped["job_created"] is False
    assert stopped["terminal_state"] == "not-admitted"

    resumed = runner.invoke(
        app, ["evolve", "resume", "--uygula", "--home", str(home)]
    )
    assert resumed.exit_code == 70
    assert "admission hazir degil" in resumed.stderr
    still_stopped = runner.invoke(app, ["local-runtime", "tick", "--home", str(home)])
    assert still_stopped.exit_code == 0, still_stopped.stdout
    assert json.loads(still_stopped.stdout)["state"] == "paused"

    disabled = runner.invoke(
        app, ["evolve", "disable", "--uygula", "--home", str(home)]
    )
    assert disabled.exit_code == 0, disabled.stdout
    assert json.loads(disabled.stdout)["result"]["state"] == "disabled"
    rejected = runner.invoke(
        app, ["evolve", "resume", "--uygula", "--home", str(home)]
    )
    assert rejected.exit_code == 70


def test_os_supervisor_combines_missed_local_days_into_one_catch_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "supervisor-catch-up-home"
    services = _init(home)
    services.learning.propose_hygiene(
        digest("historical-learning"),
        "stale",
        now=dt.datetime(2026, 9, 1, 12, tzinfo=dt.UTC),
    )
    monkeypatch.setattr(
        local_runtime_cli, "latest_due_learning_day", lambda _now: dt.date(2026, 9, 4)
    )
    first = runner.invoke(app, ["local-runtime", "tick", "--home", str(home)])
    assert first.exit_code == 0, first.stdout
    first_payload = next(
        (home / "global/generated/daylog").glob("2026-09-04-*.md")
    ).read_text(encoding="utf-8")
    assert '"start_day":"2026-09-01"' in first_payload

    monkeypatch.setattr(
        local_runtime_cli, "latest_due_learning_day", lambda _now: dt.date(2026, 9, 7)
    )
    caught_up = runner.invoke(app, ["local-runtime", "tick", "--home", str(home)])

    assert caught_up.exit_code == 0, caught_up.stdout
    receipt = json.loads(caught_up.stdout)
    assert receipt["daily_previous_completed_day"] == "2026-09-04"
    assert receipt["daily_due_day"] == "2026-09-07"
    assert receipt["daily_terminal_state"] == "completed"
    active = tuple((home / "global/generated/daylog").glob("*.md"))
    assert len(active) == 2
    payload = next(path for path in active if path.name.startswith("2026-09-07-")).read_text(
        encoding="utf-8"
    )
    assert '"start_day":"2026-09-05"' in payload
    assert '"day":"2026-09-07"' in payload
    assert '"timezone":"Europe/Istanbul"' in payload


def test_daily_watermark_rejects_forged_completed_slot_without_effect_receipt(
    tmp_path: Path,
) -> None:
    home = tmp_path / "forged-daily-watermark-home"
    services = _init(home)
    job, _ = services.runtime.schedule_once(
        slot_key="learning-daily:2099-01-01",
        schedule_digest=digest("forged-daily-schedule"),
        idempotency_key="forged-daily-watermark",
        payload={"operation": "forged", "effect": {}},
    )
    with sqlite3.connect(services.operational_path) as connection:
        connection.execute(
            "update local_job set state='completed',terminal_evidence_digest=? where id=?",
            (digest("forged-terminal"), job.id),
        )
    with pytest.raises(PolicyViolation, match="watermark"):
        services.runtime.latest_completed_learning_day()


def test_os_tick_claims_its_exact_slot_job_ahead_of_older_supported_backlog(
    tmp_path: Path,
) -> None:
    home = tmp_path / "exact-supervisor-home"
    _init(home)
    submitted = runner.invoke(
        app,
        [
            "local-runtime", "submit-journal", "--home", str(home),
            "--idempotency-key", "older-journal", "--relative-path", "older.log",
            "--line", "must-remain-ready",
        ],
    )
    assert submitted.exit_code == 0
    older_job = json.loads(submitted.stdout)["job_id"]

    tick = runner.invoke(app, ["local-runtime", "tick", "--home", str(home)])
    assert tick.exit_code == 0, tick.stdout
    document = json.loads(tick.stdout)
    assert document["claimed_job_id"] == document["job_id"]
    assert document["terminal_state"] == "completed"
    assert not (home / "runtime/local-effects/older.log").exists()
    snapshot = LocalCoreServices.from_context(
        build_context(home=str(home))
    ).runtime.job_snapshot(older_job)
    assert snapshot is not None and snapshot["state"] == "ready"
