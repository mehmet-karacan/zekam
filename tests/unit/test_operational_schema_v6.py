from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError
from zekam.domain.unit_test_engineering import (
    CoverageMetric,
    CoveragePolicy,
    RatioThreshold,
    UnitTestAttempt,
    UnitTestBudget,
    UnitTestRequest,
)
from zekam.infrastructure.doctor.sqlite_checks import PersistenceCheck
from zekam.infrastructure.local_core_services import validate_local_sqlite_store
from zekam.infrastructure.local_file_security import restrict_private_file, restrict_private_tree
from zekam.infrastructure.sqlite import operational_migration as migration
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_backup import (
    SQLiteOperationalBackup,
    _logical_rows_digest,
    logical_database_digest,
)
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.sqlite.unit_test_ledger import MIGRATION_NAME, UNIT_TEST_LEDGER_DDL

NOW = dt.datetime(2026, 10, 2, 12, tzinfo=dt.UTC)
SRC = "mod-a/src/main/java/p/Service.java"
LEDGER_TABLES = {
    "unit_test_request",
    "unit_test_attempt",
    "unit_test_attempt_receipt",
    "unit_test_observation",
    "unit_test_terminal",
}
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-native migration boundary")


class _Admission:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.calls: list[str] = []
        self.closed = False
        self.recovery = False

    def trusted_home(self) -> Path:
        return self.home

    def stop_new_admission(self) -> None:
        self.closed = True
        self.calls.append("stop")

    def drain_and_reap(self) -> None:
        assert self.closed
        self.calls.append("drain")

    def assert_no_admitted_authority(self) -> None:
        assert self.closed
        self.calls.append("assert")

    def release_admission(self) -> None:
        self.closed = False
        self.calls.append("release")

    def mark_recovery_required(self) -> None:
        self.recovery = True
        self.calls.append("recovery")


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute("select name from sqlite_master where type='table'")
        }


def _secure_v5(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Private home with a populated, exact v5 database (v3 -> v5 through the real path)."""

    home = (tmp_path / "secure-home").resolve()
    home.mkdir()
    restrict_private_tree(home)
    database = home / "operational.db"
    schema.bootstrap_v5(database)
    restrict_private_file(database)
    with sqlite3.connect(database) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute(
            "insert into project(id,slug,display_name,created_at) values('p1','demo','Demo',?)",
            (NOW.isoformat(),),
        )
        connection.execute("insert into local_runtime_config values(1,1000)")
    return home, database, home / "operational-before-v6.db"


def _request() -> UnitTestRequest:
    return UnitTestRequest(
        project_id="p1",
        source_binding_id="bind-1",
        source_revision="rev1",
        source_files=(SRC,),
        metric=CoverageMetric.LINE,
        threshold=RatioThreshold.from_percent("90"),
        policy=CoveragePolicy.PER_FILE,
        budget=UnitTestBudget(3, 60, 600),
    )


def _attempt(request: UnitTestRequest) -> UnitTestAttempt:
    return UnitTestAttempt(
        "att-1",
        request.request_digest,
        1,
        None,
        digest({"p": 1}),
        digest({"c": 1}),
        "key-1",
        ((SRC.replace("main", "test"), digest({"t": 1})),),
    )


def test_v6_pins_name_digest_and_fingerprint() -> None:
    assert schema.V6_MIGRATION_NAME == MIGRATION_NAME == "operational-unit-test-engineering-v6"
    assert schema.V6_MIGRATION_DIGEST == (
        "sha256:76e49a7df409f1681ba09be0e4e9e8bb6cf8bfd70755d7ba225f6cbd31f8b208"
    )
    assert schema.V6_SCHEMA_DIGEST == (
        "sha256:6d39a1d08715cab55beac6d03b9e47f90b72c3f4c1432e59a568201c658c6888"
    )
    assert schema.SCHEMA_DIGESTS[6] == schema.V6_SCHEMA_DIGEST
    assert schema.V6_MIGRATION_LEDGER[:5] == schema.V5_MIGRATION_LEDGER
    assert schema.V6_MIGRATION_LEDGER[-1] == (6, MIGRATION_NAME, schema.V6_MIGRATION_DIGEST)
    assert frozenset({3, 5, 6}) == schema.RUNTIME_SCHEMA_VERSIONS
    # historical pins are untouched by the deliberate v6 addition
    assert schema.V5_SCHEMA_DIGEST == (
        "sha256:b76c64e6394b72fb93a7307b24661c5db142dcbd471eb5b890145722fec72117"
    )
    assert schema.V5_MIGRATION_DIGEST == (
        "sha256:901d39f0fd9da799065f4f38701d26e972bb9f4c57ab538f278b8eab22a9d36d"
    )
    assert schema.SCHEMA_VERSION == 3
    assert "create table" in UNIT_TEST_LEDGER_DDL


def test_fresh_v6_has_ledger_tables_and_exact_ledger_rows(tmp_path: Path) -> None:
    database = (tmp_path / "operational-v6.db").resolve()

    result = schema.bootstrap_v6(database)

    assert (result.schema_version, result.schema_ok, result.integrity_ok) == (6, True, True)
    assert _tables(database) >= LEDGER_TABLES
    with sqlite3.connect(database) as connection:
        assert [
            tuple(row)
            for row in connection.execute("select version,name,checksum from schema_migration")
        ] == list(schema.V6_MIGRATION_LEDGER)
        assert connection.execute(
            "select value from zekam_meta where key='schema_digest'"
        ).fetchone() == (schema.V6_SCHEMA_DIGEST,)
    with pytest.raises(ConfigurationError, match="empty destination"):
        schema.bootstrap_v6(database)


def test_default_bootstrap_stays_v3_and_v5_stays_without_ledger(tmp_path: Path) -> None:
    default = (tmp_path / "default.db").resolve()
    v5 = (tmp_path / "v5.db").resolve()

    assert schema.bootstrap(default).schema_version == 3
    assert schema.bootstrap_v5(v5).schema_version == 5

    assert not LEDGER_TABLES & _tables(default)
    assert not LEDGER_TABLES & _tables(v5)


def test_core_migration_moves_v5_to_v6_preserving_every_v5_row(tmp_path: Path) -> None:
    _, database, _ = _secure_v5(tmp_path)
    with sqlite3.connect(database) as connection:
        source_logical = _logical_rows_digest(connection)
        source_original = schema._original_rows_digest(connection, 5)
        connection.execute("begin immediate")
        migration._migrate_connection(
            connection,
            source_logical_digest=source_logical,
            source_original_digest=source_original,
            target_version=6,
            source_version=5,
        )
        connection.commit()
        assert schema._original_rows_digest(connection, 5) == source_original

    result = schema.status(database)
    assert (result.schema_version, result.schema_ok, result.integrity_ok) == (6, True, True)
    assert _tables(database) >= LEDGER_TABLES
    with sqlite3.connect(database) as connection:
        assert connection.execute("select slug from project").fetchall() == [("demo",)]


def test_core_migration_is_not_idempotent_blind_and_rejects_wrong_pairs(
    tmp_path: Path,
) -> None:
    _, database, _ = _secure_v5(tmp_path)
    with sqlite3.connect(database) as connection:
        logical = _logical_rows_digest(connection)
        original = schema._original_rows_digest(connection, 5)
        connection.execute("begin immediate")
        with pytest.raises(ConfigurationError, match="source/target pair"):
            migration._migrate_connection(
                connection,
                source_logical_digest=logical,
                source_original_digest=original,
                target_version=6,
                source_version=3,
            )
        with pytest.raises(ConfigurationError, match="source/target pair"):
            migration._migrate_connection(
                connection,
                source_logical_digest=logical,
                source_original_digest=original,
                target_version=5,
                source_version=5,
            )
        connection.rollback()
    assert schema.status(database).schema_version == 5


def test_half_applied_ddl_is_rolled_back_and_v5_stays_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, database, _ = _secure_v5(tmp_path)
    before = logical_database_digest(database)
    original = schema._apply_migration

    def crash_after_ddl(connection: sqlite3.Connection, version: int) -> None:
        original(connection, version)
        raise OSError("injected crash after ledger DDL")

    monkeypatch.setattr(schema, "_apply_migration", crash_after_ddl)
    with sqlite3.connect(database) as connection:
        logical = _logical_rows_digest(connection)
        original_rows = schema._original_rows_digest(connection, 5)
        connection.execute("begin immediate")
        with pytest.raises(OSError, match="injected crash"):
            migration._migrate_connection(
                connection,
                source_logical_digest=logical,
                source_original_digest=original_rows,
                target_version=6,
                source_version=5,
            )
        connection.rollback()

    monkeypatch.setattr(schema, "_apply_migration", original)
    assert schema.status(database).schema_version == 5
    assert not LEDGER_TABLES & _tables(database)
    assert logical_database_digest(database) == before


def test_v5_database_has_no_ledger_access_until_migrated(tmp_path: Path) -> None:
    _, database, _ = _secure_v5(tmp_path)
    store = SQLiteOperationalStore(database)

    with store.unit_of_work() as uow, pytest.raises(ConfigurationError, match="v6 gerekli"):
        uow.unit_test_ledger()


def test_unit_of_work_ledger_survives_reopen_and_replay_is_noop(tmp_path: Path) -> None:
    database = (tmp_path / "operational-v6.db").resolve()
    schema.bootstrap_v6(database)
    request = _request()
    with SQLiteOperationalStore(database).unit_of_work() as uow:
        uow.create_project(slug="demo", display_name="Demo")
        project_id = uow.resolve_project("demo").id
        request = request.__class__(
            project_id=project_id,
            source_binding_id=request.source_binding_id,
            source_revision=request.source_revision,
            source_files=request.source_files,
            metric=request.metric,
            threshold=request.threshold,
            policy=request.policy,
            budget=request.budget,
        )
        ledger = uow.unit_test_ledger()
        assert ledger.register_request(request, now=NOW).replayed is False
        assert ledger.claim_attempt(_attempt(request), now=NOW).replayed is False
        uow.commit()

    # a new store and a new connection observe the durable claim; replays are no-ops
    with SQLiteOperationalStore(database).unit_of_work() as uow:
        ledger = uow.unit_test_ledger()
        assert ledger.unreceipted_attempts(request.request_digest) == ("att-1",)
        assert ledger.register_request(request, now=NOW).replayed is True
        assert ledger.claim_attempt(_attempt(request), now=NOW).replayed is True
        assert len(ledger.list_attempts(request.request_digest)) == 1
    assert schema.status(database).schema_ok is True


def test_v6_drift_is_detected_by_status(tmp_path: Path) -> None:
    database = (tmp_path / "operational-v6.db").resolve()
    schema.bootstrap_v6(database)
    with sqlite3.connect(database) as connection:
        connection.execute("drop trigger unit_test_request_no_update")

    result = schema.status(database)

    assert result.schema_version == 6
    assert result.schema_ok is False


def test_doctor_and_store_validation_recognize_v6(tmp_path: Path) -> None:
    database = (tmp_path / "operational-v6.db").resolve()
    schema.bootstrap_v6(database)

    check = PersistenceCheck(database, "ZEKAM_HOME/state/operational.db").run()

    assert check.status.value == "passed"
    assert check.evidence["schema_version"] == 6
    assert check.evidence["expected_schema_digest"] == schema.V6_SCHEMA_DIGEST
    assert check.evidence["supported_runtime_schema_versions"] == [3, 5, 6]
    assert validate_local_sqlite_store("operational", database, require_private_identity=False) == {
        "schema_version": 6,
        "schema_digest": schema.V6_SCHEMA_DIGEST,
    }
    assert SQLiteLocalRuntimeStore(database).status().ready_jobs == 0


def test_v5_database_remains_valid_for_doctor(tmp_path: Path) -> None:
    database = (tmp_path / "operational-v5.db").resolve()
    schema.bootstrap_v5(database)

    check = PersistenceCheck(database, "ZEKAM_HOME/state/operational.db").run()

    assert check.status.value == "passed"
    assert check.evidence["expected_schema_digest"] == schema.V5_SCHEMA_DIGEST


def test_unsupported_source_target_pair_rejected_before_admission(tmp_path: Path) -> None:
    home, database, backup = _secure_v5(tmp_path)
    admission = _Admission(home)

    with pytest.raises(ConfigurationError, match="source/target pair"):
        migration._migrate_v3_forward(
            database,
            backup,
            migration_lock=home / "migration.lock",
            admission=admission,
            spool_targets=(),
            target_version=6,
            source_version=3,
        )

    assert admission.calls == []
    assert schema.status(database).schema_version == 5


@windows_only
def test_public_migration_moves_v5_to_v6_with_backup_and_reopens(tmp_path: Path) -> None:
    home, database, backup = _secure_v5(tmp_path)
    before_logical = logical_database_digest(database)
    admission = _Admission(home)

    receipt = migration.migrate_v5_to_v6(
        database,
        backup,
        migration_lock=home / "migration.lock",
        admission=admission,
        spool_targets=(),
    )

    assert receipt.source_version == 5
    assert receipt.status.schema_version == 6
    assert receipt.status.schema_ok is True
    assert receipt.source_v3_logical_digest == before_logical == logical_database_digest(backup)
    assert schema.status(backup).schema_version == 5
    assert admission.calls == ["stop", "drain", "assert", "release"]
    assert admission.recovery is False
    # reopen: runtime, operational store and ledger all work on the migrated file
    assert SQLiteLocalRuntimeStore(database, existing_only=True).status().ready_jobs == 0
    with SQLiteOperationalStore(database).unit_of_work() as uow:
        assert uow.resolve_project("demo").slug == "demo"
        assert uow.unit_test_ledger().unreceipted_attempts(digest({"none": 1})) == ()


@windows_only
def test_public_migration_second_run_is_rejected_without_change(tmp_path: Path) -> None:
    home, database, backup = _secure_v5(tmp_path)
    migration.migrate_v5_to_v6(
        database,
        backup,
        migration_lock=home / "migration.lock",
        admission=_Admission(home),
        spool_targets=(),
    )
    migrated = logical_database_digest(database)
    admission = _Admission(home)

    with pytest.raises(ConfigurationError, match="exact schema-v5 required"):
        migration.migrate_v5_to_v6(
            database,
            home / "second-backup.db",
            migration_lock=home / "migration.lock",
            admission=admission,
            spool_targets=(),
        )

    assert logical_database_digest(database) == migrated
    assert schema.status(database).schema_version == 6
    assert admission.calls == ["stop", "drain", "assert", "release"]
    assert not (home / "second-backup.db").exists()


@windows_only
def test_public_precommit_failure_rolls_back_then_retry_reuses_verified_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, database, backup = _secure_v5(tmp_path)
    before = logical_database_digest(database)
    original = migration._migrate_connection

    def interrupted(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)  # type: ignore[arg-type]
        raise OSError("injected precommit interruption")

    monkeypatch.setattr(migration, "_migrate_connection", interrupted)
    admission = _Admission(home)
    with pytest.raises(OSError, match="precommit interruption"):
        migration.migrate_v5_to_v6(
            database,
            backup,
            migration_lock=home / "migration.lock",
            admission=admission,
            spool_targets=(),
        )

    assert schema.status(database).schema_version == 5
    assert not LEDGER_TABLES & _tables(database)
    assert logical_database_digest(database) == before
    assert schema.status(backup).schema_version == 5
    assert admission.calls == ["stop", "drain", "assert", "release"]
    assert admission.recovery is False

    monkeypatch.setattr(migration, "_migrate_connection", original)
    retry = _Admission(home)
    receipt = migration.migrate_v5_to_v6(
        database,
        backup,
        migration_lock=home / "migration.lock",
        admission=retry,
        spool_targets=(),
    )
    assert receipt.status.schema_version == 6
    assert logical_database_digest(backup) == before
    assert retry.calls == ["stop", "drain", "assert", "release"]


@windows_only
def test_public_postcommit_failure_marks_recovery_and_leaves_valid_v6_and_v5_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, database, backup = _secure_v5(tmp_path)

    def fail_readback(candidate: Path) -> object:
        raise ConfigurationError(f"injected readback failure: {candidate.name}")

    monkeypatch.setattr(schema, "status", fail_readback)
    admission = _Admission(home)
    with pytest.raises(ConfigurationError, match="readback failure"):
        migration.migrate_v5_to_v6(
            database,
            backup,
            migration_lock=home / "migration.lock",
            admission=admission,
            spool_targets=(),
        )

    assert admission.calls == ["stop", "drain", "assert", "recovery"]
    assert admission.closed is True
    assert admission.recovery is True
    with sqlite3.connect(database) as connection:
        assert schema._validate_connection(connection) == 6
    with sqlite3.connect(backup) as connection:
        assert schema._validate_connection(connection) == 5


@windows_only
def test_rollback_restores_the_pre_migration_backup_without_downgrade_in_place(
    tmp_path: Path,
) -> None:
    home, database, backup = _secure_v5(tmp_path)
    before = logical_database_digest(database)
    migration.migrate_v5_to_v6(
        database,
        backup,
        migration_lock=home / "migration.lock",
        admission=_Admission(home),
        spool_targets=(),
    )
    restored = home / "restored-v5.db"

    receipt = SQLiteOperationalBackup(backup).restore_backup(
        str(backup), str(restored), target_version=5
    )

    assert receipt.logical_digest == before
    assert schema.status(restored).schema_version == 5
    assert not LEDGER_TABLES & _tables(restored)
    # a v6 file can neither be restored as v5 (downgrade) nor can a restore replace the
    # explicit migration (v5 backup -> v6 target)
    with pytest.raises(ConfigurationError, match="downgrade forbidden"):
        SQLiteOperationalBackup(database).restore_backup(
            str(database), str(home / "downgraded.db"), target_version=5
        )
    with pytest.raises(ConfigurationError, match="cannot replace explicit"):
        SQLiteOperationalBackup(backup).restore_backup(
            str(backup), str(home / "forward.db"), target_version=6
        )
    same = SQLiteOperationalBackup(database).restore_backup(
        str(database), str(home / "copy-v6.db"), target_version=6
    )
    assert schema.status(home / "copy-v6.db").schema_version == 6
    assert same.logical_digest == logical_database_digest(database)


def test_no_legacy_database_driver_is_loaded_by_v6_path() -> None:
    code = ";".join(
        [
            "import sys, tempfile, pathlib",
            "from zekam.infrastructure.sqlite import operational_schema as s",
            "d = pathlib.Path(tempfile.mkdtemp()) / 'o.db'",
            "assert s.bootstrap_v6(d).schema_version == 6",
            "legacy = ('psycopg', 'psycopg2', 'asyncpg', 'pg8000', 'sqlalchemy')",
            "print(','.join(m for m in sys.modules if m.split('.')[0] in legacy))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.strip() == ""
