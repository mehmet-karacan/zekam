"""WP-08 exact-v1 migration and offline snapshot admission regression tests."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zekam.application.local_runtime import LocalClaimedWork, LocalJob, LocalLease
from zekam.domain.canonical import digest
from zekam.domain.errors import ConcurrencyConflict, ConfigurationError
from zekam.infrastructure.sqlite import operational_backup as backup_module
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.continuity_schema import SCHEMA_V2_SQL
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_backup import (
    SQLiteOperationalBackup,
    logical_database_digest,
    original_v1_data_digest,
)

pytestmark = pytest.mark.unit

NOW = "2026-09-02T12:00:00+00:00"
PROJECT_ID = "018f0000-0000-7000-8000-000000000001"
REALM_ID = "018f0000-0000-7000-8000-000000000002"


def _v1(path: Path) -> Path:
    schema.bootstrap(path, target_version=1)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute(
            "insert into project(id,slug,display_name,created_at) values(?,?,?,?)",
            (PROJECT_ID, "upgrade-fixture", "Original user rows", NOW),
        )
        connection.execute(
            "insert into work_item(id,project_id,kind,title,state,revision,created_at)"
            " values('work',?,'task','Preserved work','ready',1,?)",
            (PROJECT_ID, NOW),
        )
        connection.execute(
            "insert into source_binding values('source',?,'source:fixture','directory',1,?)",
            (PROJECT_ID, NOW),
        )
        connection.execute(
            "insert into source_snapshot values('snapshot','source','revision:one',?,?,?,?)",
            (digest("tree"), digest("content"), digest("config"), NOW),
        )
        connection.execute(
            "insert into session(id,client_id,device_id,project_id,status,opened_at)"
            " values('session','client','device',?,'open',?)",
            (PROJECT_ID, NOW),
        )
        connection.execute(
            "insert into session_event values('event','session','SESSION_START',?,?)",
            (digest("event"), NOW),
        )
        connection.execute(
            "insert into artifact_ref values(?,'text/plain',12,'internal',?)",
            (digest("artifact"), NOW),
        )
        connection.execute("insert into zekam_meta values('user-setting','preserve')")
    return path


def _rows(path: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return [tuple(row) for row in connection.execute(query)]


def _job(connection: sqlite3.Connection, *, state: str = "completed") -> None:
    evidence = None if state in {"ready", "running"} else digest("terminal")
    connection.execute(
        "insert into local_job(id,idempotency_key,payload_json,state,max_attempts,"
        "available_at,terminal_evidence_digest,created_at,updated_at)"
        " values('job','job-key','{}',?,1,?,?,?,?)",
        (state, NOW, evidence, NOW, NOW),
    )


def _execution_state(path: Path, kind: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        _job(connection, state="running" if kind in {"lease", "running-job"} else "completed")
        if kind == "lease":
            connection.execute(
                "insert into local_lease values('old-lease','job','old-worker',123,"
                "'copied-owner-token',1,?,?)",
                (NOW, "2026-09-02T12:10:00+00:00"),
            )
        if kind in {"running-run", "unknown-run"}:
            connection.execute(
                "insert into config_revision values('config',?,?,'{}',1,?)",
                (digest("runtime-config"), digest("task"), NOW),
            )
            connection.execute(
                "insert into run(id,work_item_id,source_snapshot_id,config_revision_id,status,"
                "budget_json,plan_digest,created_at,updated_at)"
                " values('run','work','snapshot','config',?,'{}',?,?,?)",
                (kind.removesuffix("-run"), digest("run-plan"), NOW, NOW),
            )
        if kind in {"receiptless", "unknown-receipt", "recovery"}:
            connection.execute(
                "insert into local_effect_claim values('claim','job','old-lease',1,"
                "'write',?,'effect-key',?)",
                (digest("effect"), NOW),
            )
            if kind == "unknown-receipt":
                connection.execute(
                    "insert into local_effect_receipt values('receipt','claim','unknown',?,?)",
                    (digest("unknown"), NOW),
                )
            if kind == "recovery":
                connection.execute(
                    "insert into local_recovery_case values('case','job','claim',null,"
                    "'effect-unknown',?,'open',?,null)",
                    (digest("recovery"), NOW),
                )
        if kind in {"pending-delivery", "claimed-delivery", "missing-delivery-receipt"}:
            connection.execute(
                "insert into local_outbox values('outbox','job','outbox-key',"
                "'job.completed','{}',?,?)",
                (digest({}), NOW),
            )
            if kind == "pending-delivery":
                connection.execute(
                    "insert into local_outbox_delivery(outbox_id,state,updated_at)"
                    " values('outbox','pending',?)",
                    (NOW,),
                )
            else:
                state = "claimed" if kind == "claimed-delivery" else "delivered"
                connection.execute(
                    "insert into local_outbox_delivery values('outbox',?,1,'claim',"
                    "'old-worker',123,'copied-owner-token',?,?)",
                    (state, "2026-09-02T12:10:00+00:00", NOW),
                )


def test_v1_bytes_checksum_and_schema_fingerprint_are_immutable() -> None:
    assert len(schema.V1_SCHEMA_SQL.encode()) == 36995
    assert "sha256:" + hashlib.sha256(schema.V1_SCHEMA_SQL.encode()).hexdigest() == (
        "sha256:d91114ad970241a779d183f9646616b6d5b04d0af8d2e01451473a0c5d6d769e"
    )
    assert schema.V1_SCHEMA_DIGEST == (
        "sha256:67ea597d286df31d5fe14a66003879a733e50a1cad25c7e8a7bcdcadc2839f20"
    )
    assert schema.V1_MIGRATION_NAME == "operational-authority-v1"
    assert schema._SCHEMA == schema.V1_SCHEMA_SQL


def test_status_is_version_aware_read_only_and_bootstrap_requires_explicit_upgrade(
    tmp_path: Path,
) -> None:
    path = _v1(tmp_path / "operational.db")
    before = path.read_bytes()
    current = schema.status(path)
    assert current.schema_version == 1 and current.schema_ok and current.integrity_ok
    assert path.read_bytes() == before
    assert schema.bootstrap(path, target_version=1) == current
    with pytest.raises(ConfigurationError, match="migration-required"):
        schema.bootstrap(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("version", [0, 4, -1, True, "2", None])
def test_invalid_bootstrap_version_never_creates_a_database(
    tmp_path: Path, version: object
) -> None:
    path = tmp_path / "invalid.db"
    with pytest.raises(ConfigurationError, match="unsupported"):
        schema.bootstrap(path, target_version=version)  # type: ignore[arg-type]
    assert not path.exists()


def test_fresh_v2_and_upgraded_v1_match_exact_manifest_and_preserve_original_rows(
    tmp_path: Path,
) -> None:
    existing = _v1(tmp_path / "existing.db")
    original = original_v1_data_digest(existing)
    ledger = _rows(existing, "select * from schema_migration")
    revisions = _rows(existing, "select * from schema_revision")
    fresh = tmp_path / "fresh.db"
    assert schema.bootstrap(fresh, target_version=2).schema_version == 2
    assert schema.upgrade(existing, target_version=2).schema_version == 2
    assert schema.status(existing) == schema.status(fresh)
    assert original_v1_data_digest(existing) == original
    assert _rows(existing, "select * from schema_migration where version=1") == ledger
    assert _rows(existing, "select * from schema_revision where version=1") == revisions
    for table in ("schema_migration", "schema_revision"):
        query = f"select version,name,checksum from {table} order by version"
        assert _rows(existing, query) == _rows(fresh, query) == list(schema.V2_MIGRATION_LEDGER)
    before_replay = logical_database_digest(existing)
    assert schema.upgrade(existing, target_version=2).schema_ok
    assert logical_database_digest(existing) == before_replay


@pytest.mark.parametrize(
    "statement",
    [
        "update zekam_meta set value='01' where key='schema_version'",
        "update zekam_meta set value='9' where key='schema_version'",
        "update zekam_meta set value='sha256:bad' where key='schema_digest'",
        "update schema_migration set checksum='sha256:bad' where version=1",
        "delete from schema_revision where version=1",
        "create index unexpected_project_index on project(display_name)",
        "insert into work_item(id,project_id,kind,title,state,revision,created_at)"
        " values('orphan','absent','task','Orphan','ready',1,'now')",
    ],
)
def test_unknown_drift_and_foreign_key_corruption_reject_without_repair(
    tmp_path: Path, statement: str
) -> None:
    path = _v1(tmp_path / "drift.db")
    with sqlite3.connect(path) as connection:
        connection.execute(statement)
    before = path.read_bytes()
    assert not schema.status(path).schema_ok
    with pytest.raises(ConfigurationError):
        schema.upgrade(path, target_version=2)
    assert path.read_bytes() == before
    assert _rows(path, "select count(*) from schema_migration where version=2") == [(0,)]


@pytest.mark.parametrize("fault", ["ddl", "ledger"])
def test_upgrade_failure_rolls_back_ddl_ledgers_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    path = _v1(tmp_path / "interrupted.db")
    before = logical_database_digest(path)
    original = schema._apply_migration

    def interrupted(connection: sqlite3.Connection, version: int) -> None:
        if fault == "ddl":
            schema._execute_script(connection, SCHEMA_V2_SQL)
        else:
            original(connection, version)
        raise OSError("injected upgrade interruption")

    monkeypatch.setattr(schema, "_apply_migration", interrupted)
    with pytest.raises(OSError, match="interruption"):
        schema.upgrade(path, target_version=2)
    assert schema.status(path).schema_version == 1
    assert schema.status(path).schema_ok
    assert logical_database_digest(path) == before


def test_real_process_death_during_upgrade_recovers_exact_v1(tmp_path: Path) -> None:
    path = _v1(tmp_path / "process-death.db")
    before = logical_database_digest(path)
    script = """
import os
import sys
from pathlib import Path
from zekam.infrastructure.sqlite import operational_schema as schema
original = schema._execute_script
def interrupted(connection, sql):
    connection.execute('pragma cache_size=1')
    connection.execute('pragma cache_spill=1')
    original(connection, sql)
    if sql == schema.SCHEMA_V2_SQL:
        os._exit(73)
schema._execute_script = interrupted
schema.upgrade(Path(sys.argv[1]), target_version=2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 73, result.stderr.decode()
    # A hot rollback journal is recovered only by an explicit writable open;
    # read-only status must not perform journal recovery or migration itself.
    schema.bootstrap(path, target_version=1)
    assert schema.status(path).schema_version == 1
    assert schema.status(path).schema_ok
    assert logical_database_digest(path) == before


def test_fresh_v2_failure_rolls_back_the_v1_scaffold_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fresh-interrupted.db"
    original = schema._apply_migration

    def interrupted(connection: sqlite3.Connection, version: int) -> None:
        original(connection, version)
        if version == 2:
            raise OSError("fresh v2 interrupted")

    monkeypatch.setattr(schema, "_apply_migration", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        schema.bootstrap(path, target_version=2)
    assert _rows(path, "select count(*) from sqlite_master where name not like 'sqlite_%'") == [
        (0,)
    ]


def test_online_backup_pins_one_snapshot_while_wal_writer_changes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _v1(tmp_path / "source.db")
    with sqlite3.connect(source) as connection:
        assert connection.execute("pragma journal_mode=wal").fetchone() == ("wal",)
    original_digest = logical_database_digest(source)
    original = backup_module._logical_rows_digest
    changed = False

    def concurrent_write(connection: sqlite3.Connection) -> str:
        nonlocal changed
        value = original(connection)
        if not changed:
            changed = True
            with sqlite3.connect(source) as writer:
                writer.execute("update work_item set title='Concurrent new title' where id='work'")
        return value

    monkeypatch.setattr(backup_module, "_logical_rows_digest", concurrent_write)
    snapshot = tmp_path / "snapshot.db"
    receipt = SQLiteOperationalBackup(source).create_backup(str(snapshot))
    assert receipt.logical_digest == original_digest == logical_database_digest(snapshot)
    assert _rows(source, "select title from work_item") == [("Concurrent new title",)]
    assert _rows(snapshot, "select title from work_item") == [("Preserved work",)]


def test_upgrade_rolls_back_any_original_user_row_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _v1(tmp_path / "row-drift.db")
    before = logical_database_digest(path)
    original = schema._apply_migration

    def mutate_original(connection: sqlite3.Connection, version: int) -> None:
        original(connection, version)
        connection.execute("update work_item set title='unexpected mutation' where id='work'")

    monkeypatch.setattr(schema, "_apply_migration", mutate_original)
    with pytest.raises(ConfigurationError, match="original v1 row parity"):
        schema.upgrade(path, target_version=2)
    assert schema.status(path).schema_version == 1
    assert logical_database_digest(path) == before


def test_competing_upgrades_serialize_and_revalidate(tmp_path: Path) -> None:
    path = _v1(tmp_path / "concurrent.db")
    original = original_v1_data_digest(path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _: schema.upgrade(path, target_version=2), range(16)))
    assert all(result.schema_ok and result.schema_version == 2 for result in results)
    assert _rows(path, "select count(*) from schema_migration") == [(2,)]
    assert _rows(path, "select count(*) from schema_revision") == [(2,)]
    assert original_v1_data_digest(path) == original


def test_waiting_upgrade_checks_writer_state_after_transaction_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _v1(tmp_path / "writer-race.db")
    writer = sqlite3.connect(path)
    writer.execute("begin immediate")
    _job(writer, state="running")
    connected = threading.Event()
    original_connect = schema._connect

    def observed_connect(candidate: Path, *, read_only: bool = False) -> sqlite3.Connection:
        connection = original_connect(candidate, read_only=read_only)
        connected.set()
        return connection

    monkeypatch.setattr(schema, "_connect", observed_connect)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(schema.upgrade, path, target_version=2)
            assert connected.wait(5)
            assert not future.done()
            writer.commit()
            with pytest.raises(ConfigurationError, match="quiescent active job"):
                future.result(timeout=5)
    finally:
        writer.close()
    assert schema.status(path).schema_version == 1


@pytest.mark.parametrize(
    "kind",
    [
        "lease",
        "running-job",
        "running-run",
        "unknown-run",
        "receiptless",
        "unknown-receipt",
        "recovery",
        "claimed-delivery",
        "missing-delivery-receipt",
    ],
)
def test_upgrade_rejects_live_or_unresolved_authority_without_cleaning_rows(
    tmp_path: Path, kind: str
) -> None:
    path = _v1(tmp_path / "authority.db")
    _execution_state(path, kind)
    before = logical_database_digest(path)
    with pytest.raises(ConfigurationError, match="recovery-required"):
        schema.upgrade(path, target_version=2)
    assert logical_database_digest(path) == before
    assert schema.status(path).schema_version == 1


@pytest.mark.parametrize("version", [1, 2])
def test_supported_snapshot_restore_is_immutable_and_preserves_original_rows(
    tmp_path: Path, version: int
) -> None:
    source = _v1(tmp_path / "source.db")
    if version == 2:
        schema.upgrade(source, target_version=2)
    original = original_v1_data_digest(source)
    backup = tmp_path / "snapshot.db"
    target = tmp_path / "restored.db"
    adapter = SQLiteOperationalBackup(source)
    created = adapter.create_backup(str(backup))
    before = backup.read_bytes()
    restored = adapter.restore_backup(str(backup), str(target), target_version=2)
    assert created.source_schema_version == restored.source_schema_version == version
    assert created.source_schema_digest == schema.SCHEMA_DIGESTS[version]
    assert created.logical_digest == restored.logical_digest == logical_database_digest(backup)
    assert schema.status(target).schema_ok and schema.status(target).schema_version == 2
    assert original_v1_data_digest(target) == original
    assert backup.read_bytes() == before
    if version == 1:
        assert logical_database_digest(target) != created.logical_digest
    else:
        assert logical_database_digest(target) == created.logical_digest


def test_restore_upgrade_failure_never_publishes_or_mutates_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _v1(tmp_path / "snapshot.db")
    target = tmp_path / "restored.db"
    before = snapshot.read_bytes()
    original = schema._apply_migration

    def fail_after_upgrade(connection: sqlite3.Connection, version: int) -> None:
        original(connection, version)
        raise OSError("restore upgrade interrupted")

    monkeypatch.setattr(schema, "_apply_migration", fail_after_upgrade)
    with pytest.raises(OSError, match="interrupted"):
        SQLiteOperationalBackup(snapshot).restore_backup(
            str(snapshot), str(target), target_version=2
        )
    assert snapshot.read_bytes() == before
    assert not target.exists()
    assert not list(tmp_path.glob(".restored.db.partial-*"))


def test_concurrent_restores_have_one_non_overwriting_winner(tmp_path: Path) -> None:
    snapshot = _v1(tmp_path / "snapshot.db")
    target = tmp_path / "restored.db"
    original = original_v1_data_digest(snapshot)

    def restore(_index: int) -> str:
        try:
            SQLiteOperationalBackup(snapshot).restore_backup(
                str(snapshot), str(target), target_version=2
            )
            return "published"
        except ConfigurationError as exc:
            assert "overwrite" in str(exc)
            return "conflict"

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = tuple(executor.map(restore, range(4)))
    assert results.count("published") == 1
    assert results.count("conflict") == 3
    assert schema.status(target).schema_ok
    assert original_v1_data_digest(target) == original


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "kind", ["lease", "receiptless", "recovery", "claimed-delivery", "pending-delivery"]
)
def test_active_or_unresolved_snapshot_is_not_published_as_restored_home(
    tmp_path: Path, version: int, kind: str
) -> None:
    source = _v1(tmp_path / "source.db")
    if version == 2:
        schema.upgrade(source, target_version=2)
    _execution_state(source, kind)
    snapshot = tmp_path / "snapshot.db"
    target = tmp_path / "restored.db"
    adapter = SQLiteOperationalBackup(source)
    adapter.create_backup(str(snapshot))
    before = snapshot.read_bytes()
    with pytest.raises(ConfigurationError, match="recovery-required"):
        adapter.restore_backup(str(snapshot), str(target), target_version=2)
    assert not target.exists()
    assert snapshot.read_bytes() == before
    assert not list(tmp_path.glob(".restored.db.partial-*"))


def test_pending_v2_close_request_blocks_restore_admission(tmp_path: Path) -> None:
    source = _v1(tmp_path / "source.db")
    schema.upgrade(source, target_version=2)
    with sqlite3.connect(source) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute(
            "insert into project_knowledge_realm values(?,?,?)", (PROJECT_ID, REALM_ID, NOW)
        )
        connection.execute(
            "insert into continuity_session_binding values('session','external',?,?,null,null,"
            "'client','device','snapshot',?,?,?,?,?)",
            (
                PROJECT_ID,
                REALM_ID,
                digest("task"),
                digest("plan"),
                digest("policy"),
                digest("binding"),
                NOW,
            ),
        )
        connection.execute(
            "insert into session_event_detail"
            " values('event','session',1,null,'event-key',?,null,'{}')",
            (digest("event"),),
        )
        connection.execute(
            "insert into continuity_checkpoint"
            " values(?,'session','checkpoint-key',1,?,'snapshot',?,null,'{}',?)",
            (digest("checkpoint"), digest("event"), digest("context"), NOW),
        )
        connection.execute(
            "insert into continuity_close_request values(?,'session',?,1,'{}',?)",
            (digest("close"), digest("checkpoint"), NOW),
        )
    snapshot = tmp_path / "snapshot.db"
    target = tmp_path / "restored.db"
    adapter = SQLiteOperationalBackup(source)
    adapter.create_backup(str(snapshot))
    with pytest.raises(ConfigurationError, match="pending snapshot close"):
        adapter.restore_backup(str(snapshot), str(target), target_version=2)
    assert not target.exists()


@pytest.mark.parametrize("restoring", [False, True])
def test_backup_publish_race_never_overwrites_a_competing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restoring: bool
) -> None:
    source = _v1(tmp_path / "source.db")
    target = tmp_path / "destination.db"
    original_fsync = backup_module._fsync

    def publish_competitor(path: Path) -> None:
        original_fsync(path)
        if path.name.startswith(".destination.db.partial-") and not target.exists():
            target.write_bytes(b"user-created-concurrently")

    monkeypatch.setattr(backup_module, "_fsync", publish_competitor)
    adapter = SQLiteOperationalBackup(source)
    with pytest.raises(ConfigurationError, match="overwrite"):
        if restoring:
            adapter.restore_backup(str(source), str(target), target_version=2)
        else:
            adapter.create_backup(str(target))
    assert target.read_bytes() == b"user-created-concurrently"


@pytest.mark.parametrize("version", [1, 2])
def test_old_owner_and_effect_claim_cannot_reacquire_authority_after_upgrade_restore(
    tmp_path: Path, version: int
) -> None:
    source = _v1(tmp_path / "source.db")
    with sqlite3.connect(source) as connection:
        _job(connection)
        connection.execute(
            "insert into local_effect_claim"
            " values('old-claim','job','old-lease',1,'write',?,'old-key',?)",
            (digest("old-effect"), NOW),
        )
        connection.execute(
            "insert into local_effect_receipt values('old-receipt','old-claim','completed',?,?)",
            (digest("old-result"), NOW),
        )
    if version == 2:
        schema.upgrade(source, target_version=2)
    stale = LocalClaimedWork(
        LocalJob("job", "running", "job-key", 1, 1, {}),
        LocalLease(
            "old-lease",
            "job",
            "old-worker",
            123,
            "copied-owner-token",
            1,
            "2026-09-02T12:10:00+00:00",
        ),
    )
    snapshot = tmp_path / "snapshot.db"
    target = tmp_path / "restored.db"
    adapter = SQLiteOperationalBackup(source)
    adapter.create_backup(str(snapshot))
    adapter.restore_backup(str(snapshot), str(target))
    with pytest.raises(ConfigurationError, match="migration-required"):
        SQLiteLocalRuntimeStore(source)
    assert schema.status(source).schema_version == version
    runtime = SQLiteLocalRuntimeStore(target)
    with pytest.raises(ConcurrencyConflict):
        runtime.claim_effect(
            stale,
            operation="write",
            effect_digest=digest("old-effect"),
            idempotency_key="old-key",
            now=NOW,
        )
    with pytest.raises(ConcurrencyConflict):
        runtime.finish(stale, state="completed", evidence_digest=digest("old-result"), now=NOW)
    assert _rows(target, "select count(*) from local_effect_claim") == [(1,)]
    assert _rows(target, "select count(*) from local_lease") == [(0,)]


@pytest.mark.parametrize("kind", ["corrupt", "unknown", "ledger-drift"])
def test_restore_rejects_bad_snapshots_and_existing_destination_without_overwrite(
    tmp_path: Path, kind: str
) -> None:
    snapshot = tmp_path / "snapshot.db"
    if kind == "corrupt":
        snapshot.write_bytes(b"not sqlite")
    else:
        _v1(snapshot)
        with sqlite3.connect(snapshot) as connection:
            if kind == "unknown":
                connection.execute("update zekam_meta set value='4' where key='schema_version'")
            else:
                connection.execute("update schema_migration set checksum='drift'")
    target = tmp_path / "restored.db"
    before = snapshot.read_bytes()
    with pytest.raises(ConfigurationError, match="integrity/schema"):
        SQLiteOperationalBackup(snapshot).restore_backup(str(snapshot), str(target))
    assert not target.exists() and snapshot.read_bytes() == before
    target.write_bytes(b"user-file")
    with pytest.raises(ConfigurationError, match="overwrite"):
        SQLiteOperationalBackup(snapshot).restore_backup(str(snapshot), str(target))
    assert target.read_bytes() == b"user-file"
