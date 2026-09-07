from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.domain.canonical import digest
from zekam.domain.errors import ConcurrencyConflict, ConfigurationError
from zekam.infrastructure.local_core_services import validate_local_sqlite_store
from zekam.infrastructure.local_file_security import (
    private_directory,
    restrict_private_file,
    restrict_private_tree,
)
from zekam.infrastructure.sqlite import operational_migration as migration
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.evolution_authority import evolution_authority_schema_digest
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_backup import (
    SQLiteOperationalBackup,
    _logical_rows_digest,
)
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


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


def _secure_v3(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = (tmp_path / "secure-home").resolve()
    home.mkdir()
    restrict_private_tree(home)
    database = home / "operational-v3.db"
    schema.bootstrap(database)
    restrict_private_file(database)
    return home, database, home / "operational-before-v5.db"


def _bind_spool(database: Path) -> None:
    now = "2026-09-06T00:00:00+00:00"
    value_digest = digest("v5-windows-spool-binding")
    realm = "018f0000-0000-7000-8000-000000000002"
    with sqlite3.connect(database) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute(
            "insert into project(id,slug,display_name,created_at) values('project','p','P',?)",
            (now,),
        )
        connection.execute(
            "insert into project_knowledge_realm values('project',?,?)", (realm, now)
        )
        connection.execute(
            "insert into source_binding values('source','project','source:p','directory',1,?)",
            (now,),
        )
        connection.execute(
            "insert into source_snapshot values('snapshot','source','rev',?,?,?,?)",
            (value_digest, value_digest, value_digest, now),
        )
        connection.execute(
            "insert into session(id,client_id,device_id,project_id,status,opened_at)"
            " values('session','codex','device','project','open',?)",
            (now,),
        )
        connection.execute(
            "insert into continuity_session_binding values("
            "'session','external','project',?,null,null,'codex','device','snapshot',?,?,?,?,?)",
            (realm, value_digest, value_digest, value_digest, value_digest, now),
        )


def test_fresh_v5_contains_registered_evolution_authority_schema_without_grant(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "operational-v5.db").resolve()

    result = schema.bootstrap_v5(database)

    assert result.schema_version == 5
    assert result.schema_ok is True
    assert result.integrity_ok is True
    assert schema.V5_MIGRATION_LEDGER[-1] == (
        5,
        "operational-evolution-authority-v5",
        schema.V5_MIGRATION_DIGEST,
    )
    assert evolution_authority_schema_digest() == (
        "sha256:bbc25fab86e90ef24ff90725e352d0e90a0509497801625ffffdc3d786e6f877"
    )
    with sqlite3.connect(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "select name from sqlite_master where type='table' and name like 'evolution_%'"
            )
        }
        assert tables == {
            "evolution_grant_approval",
            "evolution_standing_grant",
            "evolution_grant_revocation",
            "evolution_review_decision",
            "evolution_child_reservation",
            "evolution_child_effect_claim",
            "evolution_runtime_attestation",
            "evolution_terminal_readback",
            "evolution_child_terminal",
        }
        assert connection.execute("select count(*) from evolution_standing_grant").fetchone() == (
            0,
        )


def test_default_schema_remains_v3_until_external_migration_admission(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "operational-default.db").resolve()
    result = schema.bootstrap(database)
    assert schema.SCHEMA_VERSION == 3
    assert result.schema_version == 3


def test_local_runtime_reopens_admitted_v5_without_downgrade(tmp_path: Path) -> None:
    database = (tmp_path / "operational-v5-runtime.db").resolve()
    schema.bootstrap_v5(database)

    runtime = SQLiteLocalRuntimeStore(database)

    assert runtime.status().ready_jobs == 0
    assert schema.status(database).schema_version == 5


@pytest.mark.parametrize("payload", [b"", b"foreign-file"])
def test_local_runtime_never_bootstraps_an_invalid_existing_file(
    tmp_path: Path, payload: bytes
) -> None:
    database = (tmp_path / "invalid-existing.db").resolve()
    database.write_bytes(payload)

    with pytest.raises(ConfigurationError):
        SQLiteLocalRuntimeStore(database)

    assert database.read_bytes() == payload


def test_admitted_migration_core_moves_v3_to_v5_atomically(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "operational-v3.db").resolve()
    schema.bootstrap(database)
    with sqlite3.connect(database) as connection:
        source_logical = _logical_rows_digest(connection)
        source_original = schema._original_rows_digest(connection, 3)
        connection.execute("begin immediate")
        migration._migrate_connection(
            connection,
            source_logical_digest=source_logical,
            source_original_digest=source_original,
            target_version=5,
        )
        connection.commit()
    result = schema.status(database)
    assert result.schema_version == 5
    assert result.schema_ok is True


def test_public_windows_migration_moves_v3_to_v5_with_fencing_and_backup(
    tmp_path: Path,
) -> None:
    home, database, backup = _secure_v3(tmp_path)
    lock = home / "operational-migration.lock"
    admission = _Admission(home)
    with sqlite3.connect(database) as connection:
        connection.execute("insert into local_runtime_config values(1,1000)")
        connection.commit()

    receipt = migration.migrate_v3_to_v5(
        database,
        backup,
        migration_lock=lock,
        admission=admission,
        spool_targets=(),
    )

    assert receipt.status.schema_version == 5
    assert receipt.status.schema_ok is True
    assert receipt.status.integrity_ok is True
    assert schema.status(backup).schema_version == 3
    assert SQLiteLocalRuntimeStore(database, existing_only=True).status().ready_jobs == 0
    assert SQLiteOperationalStore(database).unit_of_work() is not None
    assert validate_local_sqlite_store("operational", database) == {
        "schema_version": 5,
        "schema_digest": schema.V5_SCHEMA_DIGEST,
    }
    assert admission.calls == ["stop", "drain", "assert", "release"]
    assert admission.closed is False
    assert admission.recovery is False


@pytest.mark.skipif(os.name != "nt", reason="Windows-native migration boundary")
def test_public_windows_migration_rejects_target_outside_trusted_home(
    tmp_path: Path,
) -> None:
    home, _, _ = _secure_v3(tmp_path)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    restrict_private_tree(outside)
    database = outside / "operational.db"
    schema.bootstrap(database)
    restrict_private_file(database)
    admission = _Admission(home)

    with pytest.raises(ConfigurationError, match="outside trusted home"):
        migration.migrate_v3_to_v5(
            database,
            outside / "backup.db",
            migration_lock=outside / "migration.lock",
            admission=admission,
            spool_targets=(),
        )

    assert admission.calls == []
    assert schema.status(database).schema_version == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows-native migration boundary")
def test_public_windows_migration_rejects_nonprivate_scoped_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _, _ = _secure_v3(tmp_path)
    scoped = home / "scoped"
    scoped.mkdir()
    restrict_private_tree(scoped)
    database = scoped / "operational.db"
    schema.bootstrap(database)
    restrict_private_file(database)
    original = private_directory

    def deny_scoped_parent(path: Path, mode: int = 0o700) -> bool:
        return False if path == scoped else original(path, mode)

    monkeypatch.setattr(migration, "private_directory", deny_scoped_parent)
    admission = _Admission(home)
    with pytest.raises(ConfigurationError, match="scoped parent ACL"):
        migration.migrate_v3_to_v5(
            database,
            scoped / "backup.db",
            migration_lock=scoped / "migration.lock",
            admission=admission,
            spool_targets=(),
        )

    assert admission.calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows-native migration boundary")
def test_public_windows_migration_lock_contention_releases_admission(
    tmp_path: Path,
) -> None:
    home, database, backup = _secure_v3(tmp_path)
    lock = home / "migration.lock"
    admission = _Admission(home)

    with (
        migration._windows_migration_lock(lock),
        pytest.raises(ConcurrencyConflict, match="already active"),
    ):
        migration.migrate_v3_to_v5(
            database,
            backup,
            migration_lock=lock,
            admission=admission,
            spool_targets=(),
        )

    assert admission.calls == ["stop", "release"]
    assert schema.status(database).schema_version == 3
    assert not backup.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows-native migration boundary")
def test_public_windows_precommit_failure_rolls_back_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, database, backup = _secure_v3(tmp_path)
    original = migration._migrate_connection

    def interrupted(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)  # type: ignore[arg-type]
        raise OSError("injected precommit interruption")

    monkeypatch.setattr(migration, "_migrate_connection", interrupted)
    admission = _Admission(home)
    with pytest.raises(OSError, match="precommit interruption"):
        migration.migrate_v3_to_v5(
            database,
            backup,
            migration_lock=home / "migration.lock",
            admission=admission,
            spool_targets=(),
        )

    assert schema.status(database).schema_version == 3
    assert schema.status(backup).schema_version == 3
    assert admission.calls == ["stop", "drain", "assert", "release"]
    assert admission.recovery is False

    monkeypatch.setattr(migration, "_migrate_connection", original)
    retry_admission = _Admission(home)
    receipt = migration.migrate_v3_to_v5(
        database,
        backup,
        migration_lock=home / "migration.lock",
        admission=retry_admission,
        spool_targets=(),
    )
    assert receipt.status.schema_version == 5
    assert retry_admission.calls == ["stop", "drain", "assert", "release"]


@pytest.mark.skipif(os.name != "nt", reason="Windows-native migration boundary")
def test_public_windows_postcommit_readback_failure_marks_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, database, backup = _secure_v3(tmp_path)

    def fail_readback(candidate: Path) -> object:
        raise ConfigurationError(f"injected readback failure: {candidate.name}")

    monkeypatch.setattr(schema, "status", fail_readback)
    admission = _Admission(home)
    with pytest.raises(ConfigurationError, match="readback failure"):
        migration.migrate_v3_to_v5(
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
        assert schema._validate_connection(connection) == 5
    with sqlite3.connect(backup) as connection:
        assert schema._validate_connection(connection) == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows-native migration boundary")
def test_public_windows_nonempty_spool_requires_existing_private_lock(
    tmp_path: Path,
) -> None:
    home, database, backup = _secure_v3(tmp_path)
    admission = _Admission(home)
    target = migration.MigrationSpoolTarget(home, "codex", "session", "external")

    with pytest.raises(ConfigurationError, match="private regular file"):
        migration.migrate_v3_to_v5(
            database,
            backup,
            migration_lock=home / "migration.lock",
            admission=admission,
            spool_targets=(target,),
        )

    assert admission.calls == ["stop", "drain", "assert", "release"]
    assert schema.status(database).schema_version == 3
    assert not backup.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows-native migration boundary")
def test_public_windows_nonempty_spool_binding_is_fenced_and_migrated(
    tmp_path: Path,
) -> None:
    home, database, backup = _secure_v3(tmp_path)
    _bind_spool(database)
    restrict_private_file(database)
    spool = ClientLifecycleSpool(home, client_id="codex")
    spool._ensure_write_directories()
    spool.lock_path.write_bytes(b"0")
    restrict_private_file(spool.lock_path)
    target = migration.MigrationSpoolTarget(home, "codex", "session", "external")
    admission = _Admission(home)

    receipt = migration.migrate_v3_to_v5(
        database,
        backup,
        migration_lock=home / "migration.lock",
        admission=admission,
        spool_targets=(target,),
    )

    assert receipt.status.schema_version == 5
    assert admission.calls == ["stop", "drain", "assert", "release"]
    assert schema.status(backup).schema_version == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows-native migration boundary")
def test_public_windows_source_replacement_is_detected_before_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, database, backup = _secure_v3(tmp_path)
    displaced = home / "displaced-v3.db"
    original_connect = migration._connect_existing_writer

    def replace_before_writer(path: Path) -> sqlite3.Connection:
        path.replace(displaced)
        SQLiteOperationalBackup(displaced).create_backup(str(path))
        return original_connect(path)

    monkeypatch.setattr(migration, "_connect_existing_writer", replace_before_writer)
    admission = _Admission(home)
    with pytest.raises(ConfigurationError, match="identity drift"):
        migration.migrate_v3_to_v5(
            database,
            backup,
            migration_lock=home / "migration.lock",
            admission=admission,
            spool_targets=(),
        )

    assert admission.calls == ["stop", "drain", "assert", "release"]
    assert schema.status(database).schema_version == 3
    assert schema.status(backup).schema_version == 3
