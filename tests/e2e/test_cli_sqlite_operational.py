"""Fresh SQLite operational authority CLI acceptance."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from zekam.domain.canonical import digest
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.e2e


def _invoke(runner: CliRunner, *arguments: str) -> Result:
    return runner.invoke(app, list(arguments))


def test_local_project_and_work_are_durable_atomic_and_portable(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    source = tmp_path / "akilli-kasa-fixture"
    source.mkdir()
    marker = source / "README.md"
    marker.write_text("user-owned source\n", encoding="utf-8")

    initialized = _invoke(runner, "init", "--home", str(home), "--persistence", "sqlite")
    assert initialized.exit_code == 0, initialized.stdout
    added = _invoke(
        runner,
        "project",
        "add",
        str(source),
        "--home",
        str(home),
        "--slug",
        "akilli-kasa",
        "--alias",
        "kasa",
        "--uygula",
    )
    assert added.exit_code == 0, added.stdout

    created = _invoke(
        runner,
        "work",
        "create",
        "kasa",
        "Operational store kaniti",
        "--home",
        str(home),
        "--ozet",
        "Yerel authority E2E",
        "--numara",
        "AK-1",
        "--kriter",
        "Restart sonrasi bulunur",
        "--uygula",
    )
    assert created.exit_code == 0, created.stdout

    restarted_client = CliRunner()
    listed = _invoke(
        restarted_client,
        "work",
        "list",
        "--home",
        str(home),
        "--proje",
        "akilli-kasa",
        "--json",
    )
    assert listed.exit_code == 0, listed.stdout
    rows = json.loads(listed.stdout)
    assert rows == [
        {
            "id": rows[0]["id"],
            "external_number": "AK-1",
            "type": "task",
            "state": "proposed",
            "title": "Operational store kaniti",
            "summary": "Yerel authority E2E",
            "acceptance_criteria": ["Restart sonrasi bulunur"],
            "project_id": rows[0]["project_id"],
            "revision": 1,
            "evidence_digest": None,
        }
    ]

    duplicate = _invoke(
        runner,
        "work",
        "create",
        "kasa",
        "Duplicate olmamali",
        "--home",
        str(home),
        "--numara",
        "AK-1",
        "--uygula",
    )
    assert duplicate.exit_code == 70
    after_duplicate = _invoke(runner, "work", "list", "--home", str(home), "--json")
    assert len(json.loads(after_duplicate.stdout)) == 1

    database = home / "state" / "operational.db"
    with sqlite3.connect(database) as connection:
        source_refs = [
            row[0] for row in connection.execute("select portable_ref from source_binding")
        ]
        knowledge_table_count = connection.execute(
            "select count(*) from sqlite_master where type = 'table' "
            "and name in ('knowledge_chunk', 'knowledge_embedding')"
        ).fetchone()[0]
    assert source_refs == ["source:akilli-kasa"]
    assert str(tmp_path) not in source_refs[0]
    assert knowledge_table_count == 0
    assert marker.read_text(encoding="utf-8") == "user-owned source\n"


def test_work_show_history_and_revision_guarded_transition(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    source = tmp_path / "source"
    source.mkdir()
    assert _invoke(runner, "init", "--home", str(home), "--persistence", "sqlite").exit_code == 0
    assert (
        _invoke(
            runner,
            "project",
            "add",
            str(source),
            "--home",
            str(home),
            "--slug",
            "work-demo",
            "--uygula",
        ).exit_code
        == 0
    )
    assert (
        _invoke(
            runner,
            "work",
            "create",
            "work-demo",
            "Durum gecisi",
            "--home",
            str(home),
            "--uygula",
        ).exit_code
        == 0
    )
    listed = json.loads(_invoke(runner, "work", "list", "--home", str(home), "--json").stdout)
    work_id = listed[0]["id"]

    planned = _invoke(
        runner,
        "work",
        "transition",
        work_id,
        "--hedef",
        "ready",
        "--revision",
        "1",
        "--home",
        str(home),
        "--json",
    )
    assert planned.exit_code == 0, planned.stdout
    assert json.loads(planned.stdout)["apply"] is False

    applied = _invoke(
        runner,
        "work",
        "transition",
        work_id,
        "--hedef",
        "ready",
        "--revision",
        "1",
        "--home",
        str(home),
        "--uygula",
        "--json",
    )
    assert applied.exit_code == 0, applied.stdout
    assert json.loads(applied.stdout)["work"]["state"] == "ready"

    stale = _invoke(
        runner,
        "work",
        "transition",
        work_id,
        "--hedef",
        "active",
        "--revision",
        "1",
        "--home",
        str(home),
        "--uygula",
        "--json",
    )
    assert stale.exit_code != 0

    shown = _invoke(runner, "work", "show", work_id, "--home", str(home), "--json")
    assert shown.exit_code == 0, shown.stdout
    assert json.loads(shown.stdout)["revision"] == 2
    history = _invoke(runner, "work", "history", work_id, "--home", str(home), "--json")
    assert history.exit_code == 0, history.stdout
    assert [event["event_kind"] for event in json.loads(history.stdout)] == [
        "created",
        "transitioned",
    ]


def test_work_run_step_dependencies_and_checkpoint_are_durable(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    source = tmp_path / "source"
    source.mkdir()
    assert _invoke(runner, "init", "--home", str(home), "--persistence", "sqlite").exit_code == 0
    assert (
        _invoke(
            runner,
            "project",
            "add",
            str(source),
            "--home",
            str(home),
            "--slug",
            "run-demo",
            "--uygula",
        ).exit_code
        == 0
    )
    created = _invoke(
        runner,
        "work",
        "create",
        "run-demo",
        "Checkpointli is",
        "--home",
        str(home),
        "--uygula",
    )
    assert created.exit_code == 0, created.stdout
    work_id = json.loads(
        _invoke(runner, "work", "list", "--home", str(home), "--json").stdout
    )[0]["id"]

    store = SQLiteOperationalStore(home / "state" / "operational.db")
    with store.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest({"profile": "test"}),
            task_digest=digest("run-task"),
            sanitized_config={"profile": "test"},
        )
        uow.commit()

    preview = _invoke(
        runner,
        "work",
        "run-create",
        work_id,
        "--config-revision",
        config.id,
        "--plan-digest",
        digest("plan"),
        "--budget-json",
        '{"calls": 2, "marker": "synthetic-secret"}',
        "--home",
        str(home),
        "--json",
    )
    assert preview.exit_code == 0, preview.stdout
    assert "synthetic-secret" not in preview.stdout

    run = _invoke(
        runner,
        "work",
        "run-create",
        work_id,
        "--config-revision",
        config.id,
        "--plan-digest",
        digest("plan"),
        "--budget-json",
        '{"calls": 2}',
        "--home",
        str(home),
        "--uygula",
        "--json",
    )
    assert run.exit_code == 0, run.stdout
    run_id = json.loads(run.stdout)["run"]["id"]

    first_step = _invoke(
        runner,
        "work",
        "run-step",
        run_id,
        "scan",
        "--input-digest",
        digest("scan-input"),
        "--home",
        str(home),
        "--uygula",
        "--json",
    )
    assert first_step.exit_code == 0, first_step.stdout
    first_step_id = json.loads(first_step.stdout)["step"]["id"]
    second_step = _invoke(
        runner,
        "work",
        "run-step",
        run_id,
        "verify",
        "--input-digest",
        digest("verify-input"),
        "--depends-on",
        first_step_id,
        "--home",
        str(home),
        "--uygula",
        "--json",
    )
    assert second_step.exit_code == 0, second_step.stdout

    checkpoint = _invoke(
        runner,
        "work",
        "checkpoint",
        run_id,
        "--sequence",
        "1",
        "--payload-json",
        '{"step":"verify","state":"ready"}',
        "--home",
        str(home),
        "--uygula",
        "--json",
    )
    assert checkpoint.exit_code == 0, checkpoint.stdout
    checkpoint_body = json.loads(checkpoint.stdout)
    assert checkpoint_body["checkpoint"]["sequence"] == 1

    listed = _invoke(runner, "work", "checkpoints", run_id, "--home", str(home), "--json")
    assert listed.exit_code == 0, listed.stdout
    assert len(json.loads(listed.stdout)) == 1


def test_work_resume_reads_a_bounded_recent_window(tmp_path: Path) -> None:
    home = tmp_path / "home"
    database = home / "state" / "operational.db"
    database.parent.mkdir(parents=True)
    bootstrap(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="resume-demo", display_name="Resume Demo")
        for index in range(105):
            uow.create_work(
                project_id=project.id,
                kind="task",
                title=f"Acik is {index}",
                state="ready",
                payload_digest=digest({"index": index}),
            )
        uow.commit()

    result = _invoke(CliRunner(), "work", "resume", "--home", str(home), "--json")
    assert result.exit_code == 0, result.stdout
    document = json.loads(result.stdout)
    assert document["bounded"] is True
    assert len(document["open_items"]) == 100


def test_local_runtime_is_composed_in_real_cli_and_survives_restart(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    initialized = _invoke(runner, "init", "--home", str(home), "--persistence", "sqlite")
    assert initialized.exit_code == 0, initialized.stdout

    first = _invoke(runner, "local-runtime", "status", "--home", str(home))
    assert first.exit_code == 0, first.stdout
    status = json.loads(first.stdout)
    assert status == {
        "ready_jobs": 0,
        "running_jobs": 0,
        "recovery_jobs": 0,
        "quarantined_jobs": 0,
        "pending_outbox": 0,
        "claimed_outbox": 0,
        "recovery_outbox": 0,
        "open_recovery_cases": 0,
    }

    restarted = CliRunner()
    plan = _invoke(restarted, "local-runtime", "recover", "--home", str(home))
    assert plan.exit_code == 0, plan.stdout
    document = json.loads(plan.stdout)
    assert document["apply"] is False
    assert document["provider_calls"] == document["network_calls"] == 0


def _wait_for_line(path: Path, process: subprocess.Popen[str]) -> bytes:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists() and path.read_bytes().endswith(b"\n"):
            return path.read_bytes()
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"child exited before marker: {process.returncode}\n{stdout}\n{stderr}")
        time.sleep(0.02)
    process.kill()
    process.wait()
    pytest.fail("external marker was not durably written")


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL assertion is POSIX-only")
def test_cli_worker_kill_after_real_effect_recovers_without_duplicate(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    assert _invoke(runner, "init", "--home", str(home), "--persistence", "sqlite").exit_code == 0
    submitted = _invoke(
        runner,
        "local-runtime",
        "submit-journal",
        "--home",
        str(home),
        "--idempotency-key",
        "kill-effect",
        "--relative-path",
        "effect-call-count.log",
        "--line",
        "called",
    )
    assert submitted.exit_code == 0, submitted.stdout
    marker = home / "runtime" / "local-effects" / "effect-call-count.log"
    executable = str(Path(sys.executable).with_name("zekam"))
    child = subprocess.Popen(
        [
            executable,
            "local-runtime",
            "worker-once",
            "--home",
            str(home),
            "--pause-after-effect-ms",
            "60000",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_bytes = _wait_for_line(marker, child)
    child.kill()
    child.wait(timeout=5)
    assert child.returncode == -signal.SIGKILL

    restarted = _invoke(CliRunner(), "local-runtime", "worker-once", "--home", str(home))
    assert restarted.exit_code == 0, restarted.stdout
    document = json.loads(restarted.stdout)
    assert document["claimed_job_id"] is None
    assert document["startup"]["orphans"]["recovery_required"] == 1
    assert marker.read_bytes() == first_bytes
    assert marker.read_bytes().count(b"\n") == 1
    database = home / "state" / "operational.db"
    with sqlite3.connect(database) as connection:
        state = connection.execute("select state from local_job").fetchone()[0]
        claims = connection.execute("select count(*) from local_effect_claim").fetchone()[0]
        receipts = connection.execute("select count(*) from local_effect_receipt").fetchone()[0]
    assert (state, claims, receipts) == ("recovery-required", 1, 0)


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL assertion is POSIX-only")
def test_cli_outbox_kill_after_real_delivery_recovers_without_duplicate(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    assert _invoke(runner, "init", "--home", str(home), "--persistence", "sqlite").exit_code == 0
    assert (
        _invoke(
            runner,
            "local-runtime",
            "submit-journal",
            "--home",
            str(home),
            "--idempotency-key",
            "kill-outbox",
            "--relative-path",
            "unused.log",
            "--line",
            "unused",
        ).exit_code
        == 0
    )
    marker = home / "runtime" / "local-effects" / "outbox-delivery.journal"
    executable = str(Path(sys.executable).with_name("zekam"))
    child = subprocess.Popen(
        [
            executable,
            "local-runtime",
            "outbox-once",
            "--home",
            str(home),
            "--pause-after-delivery-ms",
            "60000",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_bytes = _wait_for_line(marker, child)
    child.kill()
    child.wait(timeout=5)
    assert child.returncode == -signal.SIGKILL

    restarted = _invoke(CliRunner(), "local-runtime", "outbox-once", "--home", str(home))
    assert restarted.exit_code == 0, restarted.stdout
    document = json.loads(restarted.stdout)
    assert document["claimed_outbox_id"] is None
    assert document["startup"]["recovered_outbox"] == 1
    assert marker.read_bytes() == first_bytes
    assert marker.read_bytes().count(b"\n") == 1
    database = home / "state" / "operational.db"
    with sqlite3.connect(database) as connection:
        delivery_state = connection.execute("select state from local_outbox_delivery").fetchone()[0]
        receipt_status = connection.execute("select status from local_outbox_receipt").fetchone()[0]
    assert (delivery_state, receipt_status) == ("recovery-required", "unknown")


def test_alias_collision_rolls_back_entire_project_registration(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.mkdir()
    second_source.mkdir()
    assert _invoke(runner, "init", "--home", str(home)).exit_code == 0
    assert (
        _invoke(
            runner,
            "project",
            "add",
            str(first_source),
            "--home",
            str(home),
            "--slug",
            "first",
            "--uygula",
        ).exit_code
        == 0
    )

    collision = _invoke(
        runner,
        "project",
        "add",
        str(second_source),
        "--home",
        str(home),
        "--slug",
        "second",
        "--alias",
        "first",
        "--uygula",
    )
    assert collision.exit_code == 70
    listed = _invoke(runner, "project", "list", "--home", str(home), "--json")
    assert [row["slug"] for row in json.loads(listed.stdout)] == ["first"]
