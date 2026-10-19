"""Explicit one-shot operational v3 -> v5 local upgrade regression tests.

Evolution bootstrap (grant, supervisor, standing grant) bu yolda ASLA aktive
gectirilmez: admission yalniz mevcut paused/disabled + quiescent sinirlarini
kullanir. Migration bizzat `migrate_v3_to_v5` zinciridir; CLI `current+1`
degil, plan'daki explicit target_version (5) ile router yapar.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.composition import build_context
from zekam.application.unit_test_migration import UnitTestLedgerMigrationAdmission
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.local_file_security import (
    restrict_private_file,
    restrict_private_tree,
)
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.local_improvement import SQLiteLocalImprovementStore
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest
from zekam.interfaces.cli import db as db_cli
from zekam.interfaces.cli.main import app

NOW = "2026-10-19T00:00:00+00:00"
RUNNER = CliRunner()


def _improvement_store(home: Path) -> SQLiteLocalImprovementStore:
    return SQLiteLocalImprovementStore(
        home / "state" / "improvement.db",
        home / "state" / "learning.db",
        home / "benchmarklar" / "benchmark.db",
    )


def _secure_home(home: Path) -> Path:
    """Private home + salt v3 operational DB + admitted runtime config.

    Gercek `zekam init`'li v3 home'u modelledir: operational.db mevcut,
    `local_runtime_config` satiri mevcut, runtime/ dizini private.
    """
    home.mkdir(parents=True, exist_ok=True)
    restrict_private_tree(home)
    (home / "runtime").mkdir(parents=True, exist_ok=True)
    (home / "state").mkdir(parents=True, exist_ok=True)
    database = home / "state" / "operational.db"
    schema.bootstrap(database)
    with sqlite3.connect(database) as connection:
        connection.execute("insert into local_runtime_config values(1,1000)")
    restrict_private_file(database)
    return database


def _add_project_row(database: Path, project_id: str = "project") -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute(
            "insert into project(id,slug,display_name,created_at) values(?,?,?,?)",
            (project_id, project_id, project_id, NOW),
        )
    restrict_private_file(database)


def _pause(home: Path) -> None:
    store = _improvement_store(home)
    if not (home / "state" / "improvement.db").exists():
        store.bootstrap()
    store.set_evolution_control_state(
        "paused",
        reason="test-paused",
        now=dt.datetime.now(dt.UTC),
    )


def _upgrade(*args: str, home: Path) -> object:
    return RUNNER.invoke(app, ["db", "upgrade", *args, "--home", str(home)])


def _plan(home: Path) -> dict[str, object]:
    result = _upgrade(home=home)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


class TestV3Routing:
    def test_v3_planner_targets_v5_not_current_plus_one(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        database = _secure_home(home)
        _pause(home)
        context = build_context(home=home)
        planner = db_cli._MIGRATION_PLANNERS[3]
        plan = planner(context, database)
        assert plan["source_version"] == 3
        assert plan["target_version"] == 5  # explicit 3 -> 5, not 3 + 1 = 4
        assert plan["database_ref"] == "ZEKAM_HOME/state/operational.db"
        assert plan["migration_lock_ref"] == "ZEKAM_HOME/runtime/operational-v5-migration.lock"
        body = {key: value for key, value in plan.items() if key != "plan_digest"}
        assert plan["plan_digest"] == digest(body)
        assert db_cli._migration_label(3) == "v3 -> v5 (operational-evolution-authority-v5)"
        # dormant v4 still has no CLI upgrade path and reports the generic label
        assert db_cli._migration_label(4) == "v4 -> v9"


class TestAdmissionTargetConstraints:
    def test_target_5_is_admitted(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        _secure_home(home)
        _pause(home)
        context = build_context(home=home)
        admission = UnitTestLedgerMigrationAdmission(context, target_version=5)
        admission.stop_new_admission()

    @pytest.mark.parametrize("target", [3, 4, 10, "5", None, 5.0])
    def test_unsupported_targets_are_rejected(self, tmp_path: Path, target: object) -> None:
        context = build_context(home=tmp_path / "home")
        with pytest.raises(PolicyViolation, match="target unsupported"):
            UnitTestLedgerMigrationAdmission(context, target_version=target)  # type: ignore[arg-type]

    def test_default_target_is_6_for_existing_chain(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        _secure_home(home)
        _pause(home)
        context = build_context(home=home)
        # 5..9 zincirinin established default'i degismez
        UnitTestLedgerMigrationAdmission(context)


class TestPausedRequirement:
    def test_observing_control_is_refused(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        _secure_home(home)
        _improvement_store(home).bootstrap()
        # default control state is observing; admission must refuse
        context = build_context(home=home)
        admission = UnitTestLedgerMigrationAdmission(context, target_version=5)
        with pytest.raises(PolicyViolation, match="paused or disabled"):
            admission.stop_new_admission()

    def test_paused_control_and_quiescent_runtime_are_accepted(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        _secure_home(home)
        _improvement_store(home).bootstrap()
        _pause(home)
        context = build_context(home=home)
        admission = UnitTestLedgerMigrationAdmission(context, target_version=5)
        admission.stop_new_admission()
        admission.drain_and_reap()
        # fresh v3 runtime is quiescent: no jobs, outbox or recovery cases
        admission.assert_no_admitted_authority()


class TestDryRunPlan:
    def test_dry_run_yields_exact_deterministic_v3_plan(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        database = _secure_home(home)
        _pause(home)
        plan = _plan(home)
        source_digest = logical_database_digest(database)
        expected_body = {
            "schema": "zekam-operational-local-upgrade-migration-plan/v1",
            "source_version": 3,
            "target_version": 5,
            "database_ref": "ZEKAM_HOME/state/operational.db",
            "backup_ref": (
                f"ZEKAM_HOME/backups/operational-v3-before-v5-{source_digest[7:19]}.db"
            ),
            "migration_lock_ref": "ZEKAM_HOME/runtime/operational-v5-migration.lock",
            "source_logical_digest": source_digest,
            "spool_target_count": 0,
            "control_state": "paused-or-disabled-required",
            "provider_calls": 0,
            "network_calls": 0,
            "grants_authority": False,
        }
        assert plan == expected_body | {"plan_digest": digest(expected_body)}
        # deterministic: a second dry-run yields the identical plan
        assert _plan(home)["plan_digest"] == plan["plan_digest"]
        # dry-run is read-only: no backup file, schema untouched, no provider/network
        backups = list((home / "backups").glob("*")) if (home / "backups").exists() else []
        assert backups == []
        assert schema.status(database).schema_version == 3
        assert plan["provider_calls"] == 0
        assert plan["network_calls"] == 0

    def test_dry_run_with_user_data_reports_spool_count_zero(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        database = _secure_home(home)
        _add_project_row(database)
        _pause(home)
        plan = _plan(home)
        assert plan["source_version"] == 3
        assert plan["target_version"] == 5
        assert plan["source_logical_digest"] == logical_database_digest(database)
        assert plan["spool_target_count"] == 0
        assert plan["grants_authority"] is False


class TestApplyDigestAdmission:
    def test_stale_digest_is_rejected(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        database = _secure_home(home)
        _pause(home)
        result = _upgrade("--uygula", "--plan-digest", digest("stale-plan"), home=home)
        assert result.exit_code == 70
        assert "Exact migration plan digest gerekli" in result.stderr
        assert schema.status(database).schema_version == 3
        assert not (home / "backups").exists()

    def test_missing_digest_is_rejected(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        database = _secure_home(home)
        _pause(home)
        result = _upgrade("--uygula", home=home)
        assert result.exit_code == 70
        assert "Exact migration plan digest gerekli" in result.stderr
        assert schema.status(database).schema_version == 3
        assert not (home / "backups").exists()

    def test_digest_mismatch_after_source_change_is_rejected(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        database = _secure_home(home)
        _pause(home)
        original_digest = str(_plan(home)["plan_digest"])
        _add_project_row(database, project_id="project-2")
        result = _upgrade("--uygula", "--plan-digest", original_digest, home=home)
        assert result.exit_code == 70
        assert "Exact migration plan digest gerekli" in result.stderr
        # re-read plan now reflects the new source digest; old digest is stale
        assert str(_plan(home)["plan_digest"]) != original_digest
        assert schema.status(database).schema_version == 3
        assert not (home / "backups").exists()

    def test_not_paused_control_is_rejected_before_migration(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        database = _secure_home(home)
        _improvement_store(home).bootstrap()
        plan = _plan(home)
        result = _upgrade("--uygula", "--plan-digest", str(plan["plan_digest"]), home=home)
        assert result.exit_code == 70
        assert "paused or disabled" in result.stderr
        assert schema.status(database).schema_version == 3
        # admission failed before the backup writer ran
        backups = list((home / "backups").glob("*.db")) if (home / "backups").exists() else []
        assert backups == []


class TestApplyV3ToV5:
    def _apply(self, home: Path) -> dict[str, object]:
        plan = _plan(home)
        result = _upgrade("--uygula", "--plan-digest", str(plan["plan_digest"]), home=home)
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    def test_apply_moves_v3_to_v5_with_private_backup(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        database = _secure_home(home)
        _add_project_row(database)
        _pause(home)
        plan = _plan(home)
        source_digest = logical_database_digest(database)
        receipt = self._apply(home)
        assert receipt == {
            "schema": "zekam-operational-local-upgrade-migration-receipt/v1",
            "plan_digest": plan["plan_digest"],
            "source_version": 3,
            "target_version": 5,
            "schema_ok": True,
            "integrity_ok": True,
            "backup_ref": str(plan["backup_ref"]),
            "source_logical_digest": source_digest,
            "provider_calls": 0,
            "network_calls": 0,
            "grants_authority": False,
        }
        assert schema.status(database).schema_version == 5
        with sqlite3.connect(database) as connection:
            assert connection.execute("select count(*) from project").fetchone()[0] == 1
        backup = home / "backups" / str(plan["backup_ref"]).split("/")[-1]
        assert backup.is_file()
        assert schema.status(backup).schema_version == 3
        assert logical_database_digest(backup) == source_digest
        # control remains paused; the local path never resumes or re-enables
        assert _improvement_store(home).evolution_control_status()["state"] == "paused"

    def test_apply_registers_no_grant_and_activates_no_evolution(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        database = _secure_home(home)
        _pause(home)
        receipt = self._apply(home)
        assert receipt["grants_authority"] is False
        assert receipt["provider_calls"] == 0
        assert receipt["network_calls"] == 0
        with sqlite3.connect(database) as connection:
            grant_tables = {
                str(row[0])
                for row in connection.execute(
                    "select name from sqlite_master"
                    " where type='table' and name like 'evolution_%'"
                )
            }
            assert "evolution_standing_grant" in grant_tables
            # v5 schema exists but no standing grant row was registered
            assert connection.execute(
                "select count(*) from evolution_standing_grant"
            ).fetchone()[0] == 0
        # no supervisor install side effect: evolution_bootstrap was not invoked
        assert _improvement_store(home).evolution_control_status()["state"] == "paused"


class TestUnsupportedSource:
    def test_v4_apply_is_rejected(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        restrict_private_tree(home)
        database = home / "state" / "operational.db"
        schema.bootstrap_v4(database)
        restrict_private_file(database)
        result = _upgrade("--uygula", home=home)
        assert result.exit_code == 70
        assert "migration uygulanabilir" in result.stderr
        assert schema.status(database).schema_version == 4

    def test_v4_dry_run_reports_label_only(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(parents=True)
        restrict_private_tree(home)
        database = home / "state" / "operational.db"
        schema.bootstrap_v4(database)
        restrict_private_file(database)
        result = _upgrade(home=home)
        assert result.exit_code == 0
        assert "v4 -> v9" in result.stdout
        assert "Dry-run" in result.stdout
