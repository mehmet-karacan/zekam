"""Learning v3 archival keeps immutable history and uses canonical CLI receipts."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner
from tests.integration.test_memory_cli import _candidate, _store, NOW, REVIEWER_B

from zekam.application.memory_service import ReviewDecision
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.local_file_security import restrict_private_file
from zekam.infrastructure.sqlite.local_learning import (
    SQLiteLocalLearning, SCHEMA_V2_DIGEST, SCHEMA_DIGEST, _SCHEMA_V2,
)
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.integration


def _activate(store: SQLiteLocalLearning) -> tuple[str, str]:
    candidate = store.propose_memory(_candidate("archive-source", "Proje yerel calisir"), source_kind="receipt")
    review = store.review_memory(candidate, ReviewDecision(True, REVIEWER_B, "review"), now=NOW + dt.timedelta(seconds=1))
    revision = store.activate_memory(candidate, review, now=NOW + dt.timedelta(seconds=2))
    return str(store.list_records()[0]["memory_id"]), revision


def test_archival_cli_is_digest_bound_append_only_and_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = _store(tmp_path, home)
    memory_id, revision = _activate(store)
    with sqlite3.connect(store.path) as db:
        original_revision = db.execute("select * from memory_revision").fetchall()
    runner = CliRunner()
    args = ["memory", "archive", memory_id, "--home", str(home)]
    planned = runner.invoke(app, args)
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["expected_revision_digest"] == revision
    assert store.is_active(memory_id)
    refused = runner.invoke(app, args + ["--uygula", "--plan-digest", digest("wrong")])
    assert refused.exit_code != 0
    assert store.is_active(memory_id)
    applied = runner.invoke(app, args + ["--uygula", "--plan-digest", plan["plan_digest"]])
    assert applied.exit_code == 0, applied.output
    receipt = json.loads(applied.output)
    assert receipt["state"] == "archived"
    assert receipt["operational_terminal_evidence_digest"] == receipt["archive_digest"]
    assert not store.is_active(memory_id)
    assert store.active_records() == ()
    assert store.record_by_memory_id(memory_id)["state"] == "archived"
    assert store.list_records()[0]["state"] == "archived"
    assert store.memory_status()["revision_states"]["archived"] == 1
    replay = runner.invoke(app, args + ["--uygula", "--plan-digest", plan["plan_digest"]])
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output)["operational_job_id"] == receipt["operational_job_id"]
    with sqlite3.connect(store.path) as db:
        assert db.execute("select * from memory_revision").fetchall() == original_revision
        assert db.execute("select count(*) from memory_archival").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("delete from memory_archival")
    assert store.audit()["memory_archival"] == 1


def test_archival_refuses_stale_head_and_does_not_touch_active_record(tmp_path: Path) -> None:
    store = _store(tmp_path, tmp_path / "home")
    memory_id, _revision = _activate(store)
    with pytest.raises(PolicyViolation, match="head drift"):
        store.archive_memory(memory_id, digest("stale"), actor_ref="user", reason="archive", now=NOW + dt.timedelta(days=1))
    assert store.is_active(memory_id)
    assert store.audit()["memory_archival"] == 0


def test_v2_to_v3_requires_exact_plan_and_preserves_data_with_online_backup(tmp_path: Path) -> None:
    seeded = _store(tmp_path, tmp_path / "home")
    path = (seeded.path.parent / "legacy-v2.db").resolve()
    with sqlite3.connect(path) as db:
        db.executescript(_SCHEMA_V2)
        db.execute("insert into learning_schema values(1,2,?)", (SCHEMA_V2_DIGEST,))
    restrict_private_file(path)
    store = SQLiteLocalLearning(path, operational_path=seeded.operational_path)
    memory_id, revision = _activate(store)
    with pytest.raises(PolicyViolation, match="prepare v3"):
        store.archive_memory(memory_id, revision, actor_ref="user", reason="archive", now=NOW + dt.timedelta(days=1))
    backup = (tmp_path / "backups" / "v2.db").resolve()
    plan = store.migration_plan_v3(backup)
    assert plan["migration_required"] is True
    with pytest.raises(PolicyViolation, match="plan digest"):
        store.migrate_v2_to_v3(backup, authorized_plan_digest=digest("wrong"))
    assert not backup.exists()
    receipt = store.migrate_v2_to_v3(backup, authorized_plan_digest=plan["plan_digest"])
    assert receipt["target_schema_digest"] == SCHEMA_DIGEST
    assert store.migrate_v2_to_v3(backup, authorized_plan_digest=plan["plan_digest"]) == receipt
    assert store.is_active(memory_id)
    assert store.record_by_memory_id(memory_id)["revision_digest"] == revision
    with sqlite3.connect(backup) as db:
        assert db.execute("select version,schema_digest from learning_schema").fetchone() == (2, SCHEMA_V2_DIGEST)
    with sqlite3.connect(path) as db:
        assert db.execute("select version,schema_digest from learning_schema").fetchone() == (3, SCHEMA_DIGEST)
    assert store.audit()["memory_revision"] == 1


def test_skill_prepare_on_v3_is_noop_and_does_not_create_effect(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = _store(tmp_path, home)
    runner = CliRunner()
    before = store.path.read_bytes()
    with sqlite3.connect(store.operational_path) as db:
        jobs = db.execute("select count(*) from local_job").fetchone()[0]
    args = ["skill", "prepare", "--home", str(home)]
    planned = runner.invoke(app, args)
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["schema_version"] == 3
    assert plan["migration_required"] is False
    assert plan["applied"] is False
    refused = runner.invoke(app, args + ["--uygula", "--plan-digest", digest("wrong")])
    assert refused.exit_code != 0
    applied = runner.invoke(app, args + ["--uygula", "--plan-digest", plan["plan_digest"]])
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output) == plan
    assert store.path.read_bytes() == before
    with sqlite3.connect(store.operational_path) as db:
        assert db.execute("select count(*) from local_job").fetchone()[0] == jobs
    assert not (home / "backups" / "learning-v1-before-v2-4d5d9f79.db").exists()


@pytest.mark.parametrize("version", [2, 3])
def test_core_validator_checks_admitted_learning_schema_and_unique_indexes(tmp_path: Path, version: int) -> None:
    from zekam.infrastructure.local_core_services import validate_local_sqlite_store
    from zekam.domain.errors import ConfigurationError
    from zekam.infrastructure.sqlite.local_learning import _SCHEMA, _REQUIRED_V2_INDEX_SQL

    path = tmp_path / "learning.db"
    expected = SCHEMA_V2_DIGEST if version == 2 else SCHEMA_DIGEST
    with sqlite3.connect(path) as db:
        db.executescript(_SCHEMA_V2 if version == 2 else _SCHEMA)
        db.execute("insert into learning_schema values(1,?,?)", (version, expected))
    restrict_private_file(path)
    assert validate_local_sqlite_store("learning", path) == {
        "schema_version": version, "schema_digest": expected,
    }
    with sqlite3.connect(path) as db:
        name = next(iter(_REQUIRED_V2_INDEX_SQL))
        db.execute('drop index "' + name.replace('"', '""') + '"')
    with pytest.raises(ConfigurationError, match="schema validation"):
        validate_local_sqlite_store("learning", path)
