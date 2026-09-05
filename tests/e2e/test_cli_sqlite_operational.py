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
