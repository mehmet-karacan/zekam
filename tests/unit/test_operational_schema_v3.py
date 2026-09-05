"""Version-relative v3 migration/restore evidence on disposable databases only."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest
from tests.unit.test_operational_schema_upgrade import (
    NOW,
    PROJECT_ID,
    REALM_ID,
    _execution_state,
    _job,
    _rows,
    _v1,
)

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConfigurationError
from zekam.infrastructure.sqlite import operational_backup as backup_module
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.continuity_control_schema import SCHEMA_V3_SQL
from zekam.infrastructure.sqlite.continuity_schema import SCHEMA_V2_SQL
from zekam.infrastructure.sqlite.operational_backup import (
    SQLiteOperationalBackup,
    logical_database_digest,
    original_data_digest,
)

pytestmark = pytest.mark.unit


def _v2(path: Path) -> Path:
    """Seed historical v2 tables, not a relabelled current-schema fixture."""
    _v1(path)
    schema.upgrade(path, target_version=2)
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute("insert into project_knowledge_realm values(?,?,?)", (PROJECT_ID, REALM_ID, NOW))
        db.execute(
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
        db.execute(
            "insert into session_event_detail"
            " values('event','session',1,null,'event-key',?,?,'{}')",
            (digest("event"), digest("spool-1")),
        )
        db.execute(
            "insert into continuity_checkpoint"
            " values(?,'session','checkpoint-key',1,?,'snapshot',?,?,'{}',?)",
            (digest("checkpoint"), digest("event"), digest("context"), digest("spool-1"), NOW),
        )
        db.execute(
            "insert into context_manifest values(?,'session',?,1,0,'{}',?)",
            (digest("context"), digest("checkpoint"), NOW),
        )
        db.execute(
            "insert into hydration_receipt values(?,'session',?,'hydrate-key',?)",
            (digest("hydration"), digest("context"), NOW),
        )
    assert schema.status(path).schema_version == 2 and schema.status(path).schema_ok
    return path


def _source(path: Path, version: int) -> Path:
    if version == 1:
        return _v1(path)
    _v2(path)
    if version == 3:
        schema.upgrade(path)
    return path


def _pending_close(path: Path, *, control: bool = False) -> None:
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into continuity_close_request values(?,'session',?,1,'{}',?)",
            (digest("close"), digest("checkpoint"), NOW),
        )
        db.execute("update session set status='closing' where id='session'")
        if control:
            body = {
                "session_id": "session",
                "binding_digest": digest("binding"),
                "request_digest": digest("close"),
                "client_id": "client",
                "device_id": "device",
                "external_session_id": "external",
                "spool_digest": digest("spool-2"),
                "observation_digest": digest("observation"),
                "delivery_id": "control-delivery",
                "spool_sequence": 2,
                "previous_spool_digest": digest("spool-1"),
                "external_event_type": "SessionEnd",
                "internal_event_type": "post_close",
                "disposition": "advisory-post-close",
                "created_at": NOW,
                "grants_authority": False,
                "approval_inherited": False,
            }
            db.execute(
                "insert into continuity_control_event values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    digest(body),
                    body["session_id"],
                    body["binding_digest"],
                    body["request_digest"],
                    body["client_id"],
                    body["device_id"],
                    body["external_session_id"],
                    body["spool_digest"],
                    body["observation_digest"],
                    body["delivery_id"],
                    body["spool_sequence"],
                    body["previous_spool_digest"],
                    body["external_event_type"],
                    body["internal_event_type"],
                    body["disposition"],
                    canonical_json(body),
                    NOW,
                ),
            )


def test_v2_sql_and_fingerprint_are_frozen_at_reviewed_v3_baseline() -> None:
    assert len(SCHEMA_V2_SQL.encode()) == 18433
    assert schema.V2_MIGRATION_DIGEST == (
        "sha256:a4efb21d80a634c6fe8b42030c19d7ec25de2cc8b6bafeb230cec09744aaafaf"
    )
    assert (
        "sha256:" + hashlib.sha256(SCHEMA_V2_SQL.encode()).hexdigest() == schema.V2_MIGRATION_DIGEST
    )
    assert schema.V2_SCHEMA_DIGEST == (
        "sha256:812d64b984d774154a710b6bead73f065004bccb4a5d633b9aa4c64a42d5914d"
    )
    assert schema._expected_schema_fingerprint(2) == schema.V2_SCHEMA_DIGEST
    assert schema.SCHEMA_VERSION == 3
    assert schema.MIGRATION_LEDGER[:2] == schema.V2_MIGRATION_LEDGER


def test_existing_v2_requires_explicit_upgrade_and_keeps_every_v2_row(tmp_path: Path) -> None:
    path = _v2(tmp_path / "v2.db")
    original = original_data_digest(path, source_version=2)
    ledger = _rows(path, "select * from schema_migration order by version")
    revisions = _rows(path, "select * from schema_revision order by version")
    before = path.read_bytes()
    assert schema.status(path).schema_version == 2
    with pytest.raises(ConfigurationError, match="migration-required"):
        schema.bootstrap(path)
    assert path.read_bytes() == before
    assert schema.upgrade(path).schema_version == 3
    assert original_data_digest(path, source_version=2) == original
    assert _rows(path, "select * from schema_migration where version<=2 order by version") == ledger
    assert (
        _rows(path, "select * from schema_revision where version<=2 order by version") == revisions
    )
    current = logical_database_digest(path)
    assert schema.upgrade(path).schema_ok
    assert logical_database_digest(path) == current


@pytest.mark.parametrize("source_version", [0, 1, 2])
def test_fresh_and_historical_paths_apply_each_version_in_one_ordered_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_version: int
) -> None:
    path = tmp_path / "ordered.db"
    if source_version:
        _source(path, source_version)
    seen = []
    original = schema._apply_migration

    def record(db: sqlite3.Connection, version: int) -> None:
        assert db.in_transaction
        seen.append(version)
        original(db, version)

    monkeypatch.setattr(schema, "_apply_migration", record)
    result = schema.bootstrap(path) if source_version == 0 else schema.upgrade(path)
    assert result.schema_version == 3 and result.schema_ok and result.integrity_ok
    assert seen == list(range(source_version + 1, 4))
    for table in ("schema_migration", "schema_revision"):
        assert _rows(path, f"select version,name,checksum from {table} order by version") == list(
            schema.MIGRATION_LEDGER
        )


@pytest.mark.parametrize("version", [0, 4, -1, True, False, "3", None])
def test_invalid_upgrade_and_restore_targets_are_rejected_before_mutation(
    tmp_path: Path, version: object
) -> None:
    source = _v2(tmp_path / "v2.db")
    before = source.read_bytes()
    target = tmp_path / "absent-parent" / "restored.db"
    with pytest.raises(ConfigurationError, match="unsupported"):
        schema.upgrade(source, target_version=version)  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="unsupported"):
        SQLiteOperationalBackup(source).restore_backup(
            str(source),
            str(target),
            target_version=version,  # type: ignore[arg-type]
        )
    assert source.read_bytes() == before
    assert not target.parent.exists()


@pytest.mark.parametrize("source_version,target_version", [(2, 1), (3, 1), (3, 2)])
def test_downgrade_never_rewrites_or_publishes(
    tmp_path: Path, source_version: int, target_version: int
) -> None:
    source = _source(tmp_path / "source.db", source_version)
    before = source.read_bytes()
    target = tmp_path / "restored.db"
    with pytest.raises(ConfigurationError, match="downgrade"):
        schema.upgrade(source, target_version=target_version)
    with pytest.raises(ConfigurationError, match="downgrade"):
        schema.bootstrap(source, target_version=target_version)
    with pytest.raises(ConfigurationError, match="downgrade"):
        SQLiteOperationalBackup(source).restore_backup(
            str(source), str(target), target_version=target_version
        )
    assert source.read_bytes() == before
    assert not target.exists()
    assert not list(tmp_path.glob(".restored.db.partial-*"))


@pytest.mark.parametrize(
    "statement",
    [
        "update schema_migration set checksum='drift' where version=2",
        "delete from schema_revision where version=2",
        "update zekam_meta set value='02' where key='schema_version'",
        "update zekam_meta set value='4' where key='schema_version'",
        "create index unreviewed_v2_index on context_manifest(session_id)",
        "drop trigger continuity_event_chain_guard",
    ],
)
def test_v2_drift_is_not_repaired_by_v3_upgrade(tmp_path: Path, statement: str) -> None:
    path = _v2(tmp_path / "drift.db")
    with sqlite3.connect(path) as db:
        db.execute(statement)
    before = path.read_bytes()
    assert not schema.status(path).schema_ok
    with pytest.raises(ConfigurationError):
        schema.upgrade(path)
    assert path.read_bytes() == before
    assert _rows(path, "select count(*) from schema_migration where version=3") == [(0,)]


@pytest.mark.parametrize("source_version", [0, 1, 2])
@pytest.mark.parametrize("stage", ["ddl", "ledger"])
def test_v3_failure_rolls_back_the_entire_requested_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_version: int, stage: str
) -> None:
    path = tmp_path / "interrupted.db"
    before = None
    if source_version:
        _source(path, source_version)
        before = logical_database_digest(path)
    original = schema._apply_migration

    def interrupted(db: sqlite3.Connection, version: int) -> None:
        if version == 3 and stage == "ddl":
            schema._execute_script(db, SCHEMA_V3_SQL)
        else:
            original(db, version)
        if version == 3:
            raise OSError("v3 transaction interrupted")

    monkeypatch.setattr(schema, "_apply_migration", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        if source_version:
            schema.upgrade(path)
        else:
            schema.bootstrap(path)
    if source_version:
        assert schema.status(path).schema_version == source_version
        assert schema.status(path).schema_ok
        assert logical_database_digest(path) == before
    else:
        assert _rows(path, "select count(*) from sqlite_master where name not like 'sqlite_%'") == [
            (0,)
        ]


@pytest.mark.parametrize("changed", ["v2-row", "migration-timestamp", "revision-timestamp"])
def test_v3_cannot_mutate_historical_v2_rows_or_ledger_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    path = _v2(tmp_path / "historical.db")
    before = logical_database_digest(path)
    original = schema._apply_migration

    def mutate(db: sqlite3.Connection, version: int) -> None:
        original(db, version)
        if changed == "v2-row":
            db.execute(
                "insert into hydration_receipt values(?,'session',?,'unexpected-hydration',?)",
                (digest("unexpected"), digest("context"), NOW),
            )
        else:
            table = "schema_migration" if changed == "migration-timestamp" else "schema_revision"
            db.execute(f"update {table} set applied_at='changed' where version=2")

    monkeypatch.setattr(schema, "_apply_migration", mutate)
    with pytest.raises(ConfigurationError, match="original v2 row parity"):
        schema.upgrade(path)
    assert logical_database_digest(path) == before
    assert schema.status(path).schema_version == 2


@pytest.mark.parametrize("source_version", [1, 2])
def test_process_death_during_v3_recovers_exact_source_version(
    tmp_path: Path, source_version: int
) -> None:
    path = _source(tmp_path / "process-death.db", source_version)
    before = logical_database_digest(path)
    program = """
import os, sys
from pathlib import Path
from zekam.infrastructure.sqlite import operational_schema as schema
original = schema._execute_script
def killed(connection, sql):
    connection.execute('pragma cache_size=1')
    connection.execute('pragma cache_spill=1')
    original(connection, sql)
    if sql == schema.SCHEMA_V3_SQL:
        os._exit(73)
schema._execute_script = killed
schema.upgrade(Path(sys.argv[1]))
"""
    child = subprocess.run(
        [sys.executable, "-c", program, str(path)],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        timeout=15,
    )
    assert child.returncode == 73, child.stderr.decode()
    schema.bootstrap(path, target_version=source_version)
    assert logical_database_digest(path) == before


def test_concurrent_v2_upgrades_admit_one_v3_migration(tmp_path: Path) -> None:
    path = _v2(tmp_path / "concurrent.db")
    before = original_data_digest(path, source_version=2)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _: schema.upgrade(path), range(16)))
    assert all(result.schema_ok and result.schema_version == 3 for result in results)
    assert _rows(path, "select count(*) from schema_migration") == [(3,)]
    assert _rows(path, "select count(*) from schema_revision") == [(3,)]
    assert original_data_digest(path, source_version=2) == before


def test_v3_upgrade_rechecks_writer_authority_after_lock_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _v2(tmp_path / "writer.db")
    writer = sqlite3.connect(path)
    writer.execute("begin immediate")
    _job(writer, state="running")
    connected = threading.Event()
    original = schema._connect

    def notify(candidate: Path, *, read_only: bool = False) -> sqlite3.Connection:
        result = original(candidate, read_only=read_only)
        connected.set()
        return result

    monkeypatch.setattr(schema, "_connect", notify)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(schema.upgrade, path)
            assert connected.wait(5)
            assert not future.done()
            writer.commit()
            with pytest.raises(ConfigurationError, match="quiescent active job"):
                future.result(timeout=5)
    finally:
        writer.close()
    assert schema.status(path).schema_version == 2


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
def test_v2_unresolved_authority_cannot_be_upgraded(tmp_path: Path, kind: str) -> None:
    path = _v2(tmp_path / "authority.db")
    _execution_state(path, kind)
    before = logical_database_digest(path)
    with pytest.raises(ConfigurationError, match="recovery-required"):
        schema.upgrade(path)
    assert logical_database_digest(path) == before


@pytest.mark.parametrize(
    "source_version,target_version", [(1, 1), (1, 2), (1, 3), (2, 2), (2, 3), (3, 3)]
)
def test_backup_restore_preserves_version_relative_rows_and_immutable_source(
    tmp_path: Path, source_version: int, target_version: int
) -> None:
    source = _source(tmp_path / "source.db", source_version)
    original = original_data_digest(source, source_version=source_version)
    snapshot = tmp_path / "snapshot.db"
    target = tmp_path / "restored.db"
    adapter = SQLiteOperationalBackup(source)
    created = adapter.create_backup(str(snapshot))
    assert schema.status(snapshot).schema_version == source_version
    before = snapshot.read_bytes()
    restored = adapter.restore_backup(str(snapshot), str(target), target_version=target_version)
    assert restored.source_schema_version == created.source_schema_version == source_version
    assert created.source_schema_digest == schema.SCHEMA_DIGESTS[source_version]
    assert restored.logical_digest == created.logical_digest == logical_database_digest(snapshot)
    assert (
        schema.status(target).schema_version == target_version and schema.status(target).schema_ok
    )
    assert original_data_digest(target, source_version=source_version) == original
    assert snapshot.read_bytes() == before
    if source_version == target_version:
        assert logical_database_digest(target) == created.logical_digest
    else:
        assert logical_database_digest(target) != created.logical_digest


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize(
    "kind", ["lease", "receiptless", "unknown-receipt", "recovery", "pending-delivery"]
)
def test_v2_v3_restore_does_not_publish_unresolved_authority(
    tmp_path: Path, version: int, kind: str
) -> None:
    source = _source(tmp_path / "source.db", version)
    _execution_state(source, kind)
    snapshot = tmp_path / "snapshot.db"
    target = tmp_path / "restored.db"
    adapter = SQLiteOperationalBackup(source)
    adapter.create_backup(str(snapshot))
    before = snapshot.read_bytes()
    with pytest.raises(ConfigurationError, match="recovery-required"):
        adapter.restore_backup(str(snapshot), str(target))
    assert not target.exists() and snapshot.read_bytes() == before
    assert not list(tmp_path.glob(".restored.db.partial-*"))


@pytest.mark.parametrize("version", [2, 3])
def test_control_observation_does_not_make_pending_close_restorable(
    tmp_path: Path, version: int
) -> None:
    source = _source(tmp_path / "source.db", version)
    _pending_close(source, control=version == 3)
    snapshot = tmp_path / "snapshot.db"
    target = tmp_path / "restored.db"
    adapter = SQLiteOperationalBackup(source)
    adapter.create_backup(str(snapshot))
    assert logical_database_digest(snapshot) == logical_database_digest(source)
    if version == 3:
        assert _rows(snapshot, "select count(*) from continuity_control_event") == [(1,)]
    with pytest.raises(ConfigurationError, match="pending snapshot close"):
        adapter.restore_backup(str(snapshot), str(target))
    assert not target.exists()


def test_v3_snapshot_preserves_nonempty_control_rows_and_terminal_schema_joins(
    tmp_path: Path,
) -> None:
    """Exercise schema-row parity, not application close-receipt acceptance."""
    source = _source(tmp_path / "source.db", 3)
    payload = {
        "session_id": "session",
        "binding_digest": digest("binding"),
        "request_digest": digest("close"),
    }
    with sqlite3.connect(source) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into local_job(id,idempotency_key,payload_json,state,max_attempts,"
            "available_at,terminal_evidence_digest,created_at,updated_at)"
            " values('close-job','close-job-key',?,'completed',1,?,?,?,?)",
            (canonical_json(payload), NOW, digest("compile"), NOW, NOW),
        )
    _pending_close(source, control=True)
    with sqlite3.connect(source) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into local_outbox values('close-outbox','close-job','close-outbox-key',"
            "'continuity.compile',?,?,?)",
            (canonical_json(payload), digest(payload), NOW),
        )
        db.execute(
            "insert into local_outbox_delivery values('close-outbox','delivered',1,"
            "'delivery-claim','worker',1,'past-owner',?,?)",
            (NOW, NOW),
        )
        db.execute(
            "insert into local_outbox_receipt values('delivery-receipt','close-outbox',"
            "'delivery-claim',1,'delivered',?,?)",
            (digest("delivery"), NOW),
        )
        db.execute(
            "insert into continuity_outbox_binding values('close-outbox','session',"
            "'close-job','close',?,?)",
            (digest("close"), digest("close")),
        )
        db.execute(
            "insert into close_receipt values(?,?,'session',?,?,'close-outbox','[]',?)",
            (digest("receipt"), digest("close"), digest("checkpoint"), digest("context"), NOW),
        )
        db.execute(
            "update session set status='closed',closed_at=?,close_receipt_digest=?"
            " where id='session'",
            (NOW, digest("receipt")),
        )
    before = logical_database_digest(source)
    control_rows = _rows(source, "select * from continuity_control_event")
    assert len(control_rows) == 1
    target = tmp_path / "restored.db"
    SQLiteOperationalBackup(source).restore_backup(str(source), str(target))
    assert logical_database_digest(target) == before
    assert _rows(target, "select * from continuity_control_event") == control_rows
    assert _rows(target, "select count(*) from local_lease") == [(0,)]


def test_restore_detects_v2_row_loss_after_migration_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _v2(tmp_path / "snapshot.db")
    before = source.read_bytes()
    target = tmp_path / "restored.db"
    original = cast(Any, backup_module)._apply_forward_path

    def altered(connection: sqlite3.Connection, source_version: int, target_version: int) -> None:
        original(connection, source_version, target_version)
        connection.execute("update schema_revision set applied_at='changed' where version=2")

    monkeypatch.setattr(backup_module, "_apply_forward_path", altered)
    with pytest.raises(ConfigurationError, match="original v2 row parity"):
        SQLiteOperationalBackup(source).restore_backup(str(source), str(target))
    assert source.read_bytes() == before and not target.exists()


@pytest.mark.parametrize("source_version", [1, 2])
def test_restore_v3_migration_interruption_leaves_snapshot_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_version: int
) -> None:
    source = _source(tmp_path / "snapshot.db", source_version)
    target = tmp_path / "restored.db"
    before = source.read_bytes()
    original = schema._apply_migration

    def interrupted(db: sqlite3.Connection, version: int) -> None:
        original(db, version)
        if version == 3:
            raise OSError("restore-v3 interrupted")

    monkeypatch.setattr(schema, "_apply_migration", interrupted)
    with pytest.raises(OSError, match="restore-v3"):
        SQLiteOperationalBackup(source).restore_backup(str(source), str(target))
    assert source.read_bytes() == before and not target.exists()
    assert not list(tmp_path.glob(".restored.db.partial-*"))


def test_current_restore_has_exactly_one_non_overwriting_winner(tmp_path: Path) -> None:
    source = _source(tmp_path / "snapshot.db", 3)
    target = tmp_path / "restored.db"

    def restore(_index: int) -> bool:
        try:
            SQLiteOperationalBackup(source).restore_backup(str(source), str(target))
            return True
        except ConfigurationError as exc:
            assert "overwrite" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = tuple(executor.map(restore, range(4)))
    assert sum(results) == 1
    assert logical_database_digest(target) == logical_database_digest(source)
