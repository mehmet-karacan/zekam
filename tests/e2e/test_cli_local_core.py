from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from zekam.interfaces.cli.main import app


def _run(*arguments: str) -> Result:
    return CliRunner().invoke(app, list(arguments))


def test_init_composes_all_local_stores_and_restart_is_stable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = _run("init", "--home", str(home))
    assert first.exit_code == 0, first.stdout
    status = _run("local-core", "status", "--home", str(home))
    assert status.exit_code == 0, status.stdout
    document = json.loads(status.stdout)
    assert document["all_ready"] is True
    assert set(document["databases"]) == {
        "operational",
        "learning",
        "registry",
        "benchmark",
        "routing",
        "improvement",
        "source_authority",
    }
    assert document["databases"]["source_authority"]["required"] is False
    second = _run("init", "--home", str(home))
    assert second.exit_code == 0, second.stdout
    assert _run("local-core", "status", "--home", str(home)).exit_code == 0


def test_real_setup_is_digest_bound_replayable_and_does_not_need_docker(tmp_path: Path) -> None:
    home = tmp_path / "disposable-home"
    tool_bin = tmp_path / "path-without-docker"
    tool_bin.mkdir()
    git = shutil.which("git")
    assert git is not None
    if os.name == "nt":
        isolated_path = str(Path(git).resolve().parent)
    else:
        (tool_bin / "git").symlink_to(Path(git).resolve())
        isolated_path = str(tool_bin)
    environment = os.environ.copy()
    environment["PATH"] = isolated_path
    assert shutil.which("docker", path=environment["PATH"]) is None
    prefix = (sys.executable, "-m", "zekam.interfaces.cli.main", "setup")

    planned = subprocess.run(
        (*prefix, "--home", str(home), "--json"),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert planned.returncode == 0, planned.stderr
    plan = json.loads(planned.stdout)
    assert plan["schema"] == "zekam-setup-plan/v2"
    assert "postgresql" not in planned.stdout.lower()

    arguments = (
        *prefix,
        "--home",
        str(home),
        "--uygula",
        "--plan-digest",
        plan["plan_digest"],
        "--json",
    )
    first = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    replay = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert first.returncode == replay.returncode == 0, first.stderr + replay.stderr
    assert json.loads(first.stdout)["plan_digest"] == plan["plan_digest"]
    assert json.loads(replay.stdout)["status"] == "completed"
    assert (home / "state" / "learning.db").is_file()
    assert (home / "modeller" / "registry" / "models.db").is_file()


def test_standard_doctor_checks_all_composed_local_store_fingerprints(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert _run("init", "--home", str(home)).exit_code == 0

    report = _run("doctor", "--home", str(home), "--json")
    document = json.loads(report.stdout)
    local = next(
        item for item in document["results"] if item["check_id"] == "sqlite.local-core-stores"
    )
    assert local["status"] == "passed"
    assert set(local["evidence"]["databases"]) == {
        "operational",
        "learning",
        "registry",
        "benchmark",
        "routing",
        "improvement",
        "source_authority",
    }
    assert local["evidence"]["analytics"]["semantic_ok"] is True
    assert local["evidence"]["analytics"]["semantic_fingerprint"].startswith("sha256:")


@pytest.mark.parametrize(
    ("relative", "store_name"),
    (
        ("state/learning.db", "learning"),
        ("state/improvement.db", "improvement"),
        ("modeller/registry/models.db", "registry"),
        ("benchmarklar/benchmark.db", "benchmark"),
        ("modeller/routing/routing.db", "routing"),
    ),
)
def test_standard_doctor_rejects_each_local_store_wrong_schema(
    tmp_path: Path,
    relative: str,
    store_name: str,
) -> None:
    home = tmp_path / "home"
    assert _run("init", "--home", str(home)).exit_code == 0

    with sqlite3.connect(home / relative) as database:
        database.execute("create table injected_schema_drift(value text)")
    broken = _run("doctor", "--home", str(home), "--json")
    assert broken.exit_code == 2
    broken_document = json.loads(broken.stdout)
    local = next(
        item
        for item in broken_document["results"]
        if item["check_id"] == "sqlite.local-core-stores"
    )
    assert local["status"] == "failed"
    assert local["evidence"]["databases"][store_name]["schema_ok"] is False
    assert {item["code"] for item in local["findings"]} == {"sqlite.local-store-semantic-drift"}


def test_doctor_local_store_check_is_read_only_and_fails_closed_on_corrupt_bytes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert _run("init", "--home", str(home)).exit_code == 0
    database_paths = tuple(path for path in home.rglob("*.db") if path.is_file())
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in database_paths}

    healthy = _run("doctor", "--home", str(home), "--category", "sqlite", "--json")
    assert healthy.exit_code == 0, healthy.stdout
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in database_paths} == before

    learning = home / "state" / "learning.db"
    learning.write_bytes(b"not-a-sqlite-database")
    blocked = _run("doctor", "--home", str(home), "--json")
    assert blocked.exit_code == 2
    document = json.loads(blocked.stdout)
    local = next(
        item for item in document["results"] if item["check_id"] == "sqlite.local-core-stores"
    )
    assert local["evidence"]["databases"]["learning"]["integrity"] is False


def test_doctor_fails_closed_on_analytics_semantic_drift(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert _run("init", "--home", str(home)).exit_code == 0
    lock = home / "analytics" / ".writer.lock"
    if os.name == "nt":
        icacls = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "icacls.exe"
        weakened = subprocess.run(
            [str(icacls), str(lock), "/grant", "*S-1-1-0:W", "/Q"],
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert weakened.returncode == 0, weakened.stderr
    else:
        lock.chmod(0o644)

    blocked = _run("doctor", "--home", str(home), "--json")
    assert blocked.exit_code == 2
    document = json.loads(blocked.stdout)
    local = next(
        item for item in document["results"] if item["check_id"] == "sqlite.local-core-stores"
    )
    assert local["evidence"]["analytics"]["ready"] is False
    assert local["evidence"]["analytics"]["repairable"] is False


def test_knowledge_ingest_is_durable_replayable_and_conflict_safe(tmp_path: Path) -> None:
    home = tmp_path / "home"
    note = tmp_path / "note.md"
    note.write_text("# Local note\n\nDurable content.\n", encoding="utf-8")
    assert _run("init", "--home", str(home)).exit_code == 0
    arguments = (
        "knowledge",
        "ingest",
        str(note),
        "--slug",
        "local-note",
        "--uygula",
        "--json",
        "--home",
        str(home),
    )
    first = _run(*arguments)
    second = _run(*arguments)
    assert first.exit_code == second.exit_code == 0
    assert json.loads(first.stdout)["note_id"] == json.loads(second.stdout)["note_id"]
    materialized = home / "inbox" / "user" / "global" / "local-note.md"
    assert materialized.read_bytes() == note.read_bytes()
    with sqlite3.connect(home / "state" / "operational.db") as db:
        assert db.execute("select count(*) from artifact_ref").fetchone()[0] == 1
        assert db.execute("select count(*) from knowledge_note").fetchone()[0] == 1
        assert (
            db.execute("select count(*) from knowledge_note where materialized=1").fetchone()[0]
            == 1
        )
    materialized.write_text("drift\n", encoding="utf-8")
    rejected = _run(*arguments)
    assert rejected.exit_code != 0
    with sqlite3.connect(home / "state" / "operational.db") as db:
        assert db.execute("select count(*) from knowledge_note").fetchone()[0] == 1


def test_removed_remote_commands_and_current_local_surfaces_are_advertised_correctly(
    tmp_path: Path,
) -> None:
    result = _run("--help")
    assert result.exit_code == 0
    for command in ("memory", "loop", "oracle", "trace"):
        assert f" {command} " not in result.stdout
    assert " model " in result.stdout
    assert " local-core " in result.stdout
    assert " opencode " in result.stdout
    assert " resume " in result.stdout
    assert " capabilities " in result.stdout


def test_doctor_applies_only_the_exact_current_local_repair_plan(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert _run("init", "--home", str(home)).exit_code == 0
    (home / "state" / "learning.db").replace(home / "state" / "learning.saved")

    planned = _run(
        "doctor",
        "--home",
        str(home),
        "--category",
        "sqlite",
        "--repair-plan",
        "--json",
    )
    assert planned.exit_code == 2, planned.stdout
    plan = json.loads(planned.stdout)["doctor_repair_plan"]
    assert plan["action"] == "bootstrap-missing-local-stores"
    assert plan["missing"] == ["learning"]

    stale = _run(
        "doctor",
        "--home",
        str(home),
        "--category",
        "sqlite",
        "--uygula",
        "--plan-digest",
        "sha256:" + "0" * 64,
        "--json",
    )
    assert stale.exit_code != 0
    assert not (home / "state" / "learning.db").exists()

    applied = _run(
        "doctor",
        "--home",
        str(home),
        "--category",
        "sqlite",
        "--uygula",
        "--plan-digest",
        plan["plan_digest"],
        "--json",
    )
    assert applied.exit_code == 0, applied.stdout
    result = json.loads(applied.stdout)["doctor_repair_result"]
    assert result["step"] == "bootstrap-missing-local-stores"
    assert result["after"]["all_ready"] is True
    assert result["receipt_id"].startswith("sha256:")

    (home / "modeller" / "registry" / "models.db").replace(
        home / "modeller" / "registry" / "models.saved"
    )
    prepared = _run("doctor", "--home", str(home), "--category", "sqlite", "--hazirla", "--json")
    assert prepared.exit_code == 0, prepared.stdout
    receipt = json.loads(prepared.stdout)["doctor_prepare_results"]
    assert len(receipt) == 1 and receipt[0]["after"]["all_ready"] is True
