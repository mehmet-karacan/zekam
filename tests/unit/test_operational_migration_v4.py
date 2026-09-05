"""Exact v3-to-v4 external-boundary migration tests."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from zekam.application.operational_store import (
    OperationalBackupReceipt,
    OperationalSchemaStatus,
)
from zekam.domain.errors import ConcurrencyConflict, ConfigurationError
from zekam.infrastructure.sqlite import operational_migration as migration
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.operational_backup import (
    SQLiteOperationalBackup,
    logical_database_digest,
)

pytestmark = pytest.mark.unit


class _Admission:
    def __init__(self, home: Path | None = None) -> None:
        self.calls: list[str] = []
        self.closed = False
        self.recovery = False
        self.home = home or Path(__file__).parents[2]

    def trusted_home(self) -> Path:
        return self.home

    def stop_new_admission(self) -> None:
        assert not self.closed
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


def _migrate(
    path: Path, backup: Path, admission: _Admission
) -> migration.OperationalMigrationReceipt:
    return migration.migrate_v3_to_v4(
        path,
        backup,
        migration_lock=path.parent / "operational-migration.lock",
        admission=admission,
        spool_targets=(),
    )


def _binding(path: Path) -> None:
    now = "2026-09-03T00:00:00+00:00"
    sha = "sha256:" + "1" * 64
    realm = "018f0000-0000-7000-8000-000000000002"
    with sqlite3.connect(path) as connection:
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
            (sha, sha, sha, now),
        )
        connection.execute(
            "insert into session(id,client_id,device_id,project_id,status,opened_at)"
            " values('session','codex','device','project','open',?)",
            (now,),
        )
        connection.execute(
            "insert into continuity_session_binding values("
            "'session','external','project',?,null,null,'codex','device','snapshot',?,?,?,?,?)",
            (realm, sha, sha, sha, sha, now),
        )


def test_exact_v3_migrates_once_with_verified_nonoverwrite_backup(tmp_path: Path) -> None:
    path = tmp_path / "operational.db"
    backup = tmp_path / "before-v4.db"
    schema.bootstrap(path)
    before = logical_database_digest(path)
    before_ledger = {}
    with sqlite3.connect(path) as connection:
        for table in ("schema_migration", "schema_revision"):
            before_ledger[table] = connection.execute(
                f"select version,name,checksum,applied_at from {table} order by version"
            ).fetchall()
    admission = _Admission()
    receipt = _migrate(path, backup, admission)
    assert receipt.status.schema_version == 4 and receipt.status.schema_ok
    assert receipt.source_v3_logical_digest == before == logical_database_digest(backup)
    assert schema.status(backup).schema_version == 3
    assert admission.calls == ["stop", "drain", "assert", "release"]
    assert not admission.closed and not admission.recovery
    with sqlite3.connect(path) as connection:
        for table in ("schema_migration", "schema_revision"):
            assert (
                connection.execute(
                    f"select version,name,checksum,applied_at from {table} "
                    "where version<4 order by version"
                ).fetchall()
                == before_ledger[table]
            )
            assert connection.execute(
                f"select count(*) from {table} where version=4"
            ).fetchone() == (1,)
    second = _Admission()
    with pytest.raises(ConfigurationError, match=r"schema-v3|overwrite"):
        _migrate(path, tmp_path / "second.db", second)
    assert schema.status(path).schema_version == 4


@pytest.mark.parametrize("version", [1, 2, 4])
def test_non_v3_sources_reject_without_schema_mutation(tmp_path: Path, version: int) -> None:
    path = tmp_path / f"v{version}.db"
    if version == 4:
        schema.bootstrap_v4(path)
    else:
        schema.bootstrap(path, target_version=version)
    before = path.read_bytes()
    admission = _Admission()
    with pytest.raises(ConfigurationError, match="schema-v3"):
        _migrate(path, tmp_path / "backup.db", admission)
    assert path.read_bytes() == before
    assert not admission.closed and not admission.recovery


def test_active_authority_rejects_before_backup_and_releases_admission(tmp_path: Path) -> None:
    path = tmp_path / "active.db"
    schema.bootstrap(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "insert into local_job(id,idempotency_key,payload_json,state,max_attempts,"
            "available_at,terminal_evidence_digest,created_at,updated_at)"
            " values('job','key','{}','running',1,?,null,?,?)",
            ("2026-09-03T00:00:00+00:00",) * 3,
        )
    admission = _Admission()
    with pytest.raises(ConfigurationError, match="active job"):
        _migrate(path, tmp_path / "backup.db", admission)
    assert not (tmp_path / "backup.db").exists()
    assert schema.status(path).schema_version == 3
    assert admission.calls[-1] == "release" and not admission.closed


def test_precommit_failure_rolls_back_and_retains_exact_v3_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.db"
    backup = tmp_path / "backup.db"
    schema.bootstrap(path)
    before = logical_database_digest(path)
    original = migration._migrate_connection

    def interrupted(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)  # type: ignore[arg-type]
        raise OSError("injected precommit interruption")

    monkeypatch.setattr(migration, "_migrate_connection", interrupted)
    admission = _Admission()
    with pytest.raises(OSError, match="interruption"):
        _migrate(path, backup, admission)
    assert schema.status(path).schema_version == 3
    assert logical_database_digest(path) == before == logical_database_digest(backup)
    assert not admission.closed and not admission.recovery


def test_postcommit_reopen_failure_keeps_external_admission_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "postcommit.db"
    schema.bootstrap(path)
    real_status = schema.status
    seen = 0

    def fail_after_commit(candidate: Path) -> OperationalSchemaStatus:
        nonlocal seen
        seen += 1
        if seen == 1:
            raise ConfigurationError("injected reopen failure")
        return real_status(candidate)

    monkeypatch.setattr(schema, "status", fail_after_commit)
    admission = _Admission()
    with pytest.raises(ConfigurationError, match="reopen failure"):
        _migrate(path, tmp_path / "backup.db", admission)
    assert real_status(path).schema_version == 4
    assert admission.closed and admission.recovery
    assert admission.calls[-1] == "recovery"


def test_migration_lock_rejects_symlink_without_database_mutation(tmp_path: Path) -> None:
    path = tmp_path / "operational.db"
    schema.bootstrap(path)
    target = tmp_path / "target.lock"
    target.write_text("0")
    link = tmp_path / "link.lock"
    os.symlink(target, link)
    admission = _Admission()
    with pytest.raises(ConfigurationError, match="lock path"):
        migration.migrate_v3_to_v4(
            path,
            tmp_path / "backup.db",
            migration_lock=link,
            admission=admission,
            spool_targets=(),
        )
    assert schema.status(path).schema_version == 3
    assert not admission.closed


def test_exact_empty_v4_backup_restores_only_to_new_dormant_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "v4.db"
    backup = tmp_path / "v4.backup.db"
    target = tmp_path / "restored.db"
    schema.bootstrap_v4(source)
    adapter = SQLiteOperationalBackup(source)
    created = adapter.create_backup(str(backup))
    restored = adapter.restore_v4_backup(str(backup), str(target))
    assert created.source_schema_version == restored.source_schema_version == 4
    assert schema.status(target).schema_version == 4
    assert logical_database_digest(source) == logical_database_digest(target)
    with pytest.raises(ConfigurationError, match="overwrite"):
        adapter.restore_v4_backup(str(backup), str(target))


def test_v4_backup_with_attachment_authority_cannot_be_restored(tmp_path: Path) -> None:
    source = tmp_path / "authority-v4.db"
    schema.bootstrap_v4(source)

    def sha(value: str) -> str:
        return "sha256:" + value * 64

    now = "2026-09-03T00:00:00+00:00"
    with sqlite3.connect(source) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute(
            "insert into project(id,slug,display_name,created_at) values('project','p','P',?)",
            (now,),
        )
        connection.execute(
            "insert into project_knowledge_realm values('project',"
            "'018f0000-0000-7000-8000-000000000002',?)",
            (now,),
        )
        connection.execute(
            "insert into source_binding values('source','project','source:p','directory',1,?)",
            (now,),
        )
        connection.execute(
            "insert into source_snapshot values('snapshot','source','rev',?,?,?,?)",
            (sha("1"), sha("2"), sha("3"), now),
        )
        connection.execute(
            "insert into session(id,client_id,device_id,project_id,status,opened_at)"
            " values('session','codex','device','project','open',?)",
            (now,),
        )
        connection.execute(
            "insert into continuity_session_binding values("
            "'session','external','project','018f0000-0000-7000-8000-000000000002',"
            "null,null,'codex','device','snapshot',"
            "?,?,?,?,?)",
            (sha("4"), sha("5"), sha("6"), sha("7"), now),
        )
        attachment_id = "018f0000-0000-7000-8000-000000000001"
        attachment_body = json.dumps(
            {
                "attachment_id": attachment_id,
                "client_contract_digest": sha("8"),
                "created_at": now,
                "hook_set_digest": sha("a"),
                "native_artifact_digest": sha("9"),
                "session_id": "session",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        connection.execute(
            "insert into continuity_hook_attachment values(?,?,?,?,?,?,?,?)",
            (
                attachment_id,
                "session",
                sha("8"),
                sha("9"),
                sha("a"),
                sha("b"),
                attachment_body,
                now,
            ),
        )
    backup = tmp_path / "authority.backup.db"
    SQLiteOperationalBackup(source).create_backup(str(backup))
    with pytest.raises(ConfigurationError, match="not dormant"):
        SQLiteOperationalBackup(source).restore_v4_backup(
            str(backup), str(tmp_path / "restored.db")
        )
    assert not (tmp_path / "restored.db").exists()


def test_every_known_binding_requires_exact_held_spool_coverage(tmp_path: Path) -> None:
    path = tmp_path / "bound.db"
    schema.bootstrap(path)
    _binding(path)
    missing = _Admission()
    with pytest.raises(ConfigurationError, match="spool coverage"):
        _migrate(path, tmp_path / "missing.backup.db", missing)
    assert schema.status(path).schema_version == 3
    home = tmp_path / "home"
    spool_lock = home / "global/runtime/client-lifecycle/codex/writer.lock"
    spool_lock.parent.mkdir(parents=True)
    spool_lock.write_bytes(b"0")
    covered = _Admission(home)
    receipt = migration.migrate_v3_to_v4(
        path,
        tmp_path / "covered.backup.db",
        migration_lock=tmp_path / "migration.lock",
        admission=covered,
        spool_targets=(migration.MigrationSpoolTarget(home, "codex", "session", "external"),),
    )
    assert receipt.status.schema_version == 4
    assert spool_lock.is_file()


def test_external_migration_lock_contention_fails_without_waiting_or_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v3.db"
    schema.bootstrap(path)
    lock = tmp_path / "migration.lock"
    with migration._migration_lock(lock):
        admission = _Admission()
        with pytest.raises(ConcurrencyConflict):
            migration.migrate_v3_to_v4(
                path,
                tmp_path / "backup.db",
                migration_lock=lock,
                admission=admission,
                spool_targets=(),
            )
    assert schema.status(path).schema_version == 3


@pytest.mark.parametrize(
    ("state", "allowed"),
    [
        ("ready", True),
        ("completed", True),
        ("failed", True),
        ("cancelled", True),
        ("quarantined", True),
        ("running", False),
        ("recovery-required", False),
    ],
)
def test_job_quiescence_truth_table(tmp_path: Path, state: str, allowed: bool) -> None:
    path = tmp_path / f"{state}.db"
    schema.bootstrap(path)
    terminal = None if state in {"ready", "running"} else "sha256:" + "1" * 64
    with sqlite3.connect(path) as connection:
        connection.execute(
            "insert into local_job(id,idempotency_key,payload_json,state,max_attempts,"
            "available_at,terminal_evidence_digest,created_at,updated_at)"
            " values('job','key','{}',?,1,?,?,?,?)",
            (state, "2026-09-03T00:00:00+00:00", terminal) + ("2026-09-03T00:00:00+00:00",) * 2,
        )
    admission = _Admission()
    if allowed:
        assert _migrate(path, tmp_path / "backup.db", admission).status.schema_version == 4
    else:
        with pytest.raises(ConfigurationError, match="active job"):
            _migrate(path, tmp_path / "backup.db", admission)
        assert schema.status(path).schema_version == 3


def test_live_logical_drift_after_backup_rejects_before_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "drift.db"
    schema.bootstrap(path)
    original = SQLiteOperationalBackup.create_backup_anchored

    def mutate_after_backup(
        adapter: SQLiteOperationalBackup,
        destination: str,
        *,
        parent_descriptor: int,
    ) -> OperationalBackupReceipt:
        receipt = original(adapter, destination, parent_descriptor=parent_descriptor)
        with sqlite3.connect(path) as connection:
            connection.execute("insert into zekam_meta values('concurrent','drift')")
        return receipt

    monkeypatch.setattr(SQLiteOperationalBackup, "create_backup_anchored", mutate_after_backup)
    admission = _Admission()
    with pytest.raises(ConfigurationError, match="changed after backup"):
        _migrate(path, tmp_path / "backup.db", admission)
    assert schema.status(path).schema_version == 3
    assert not admission.closed


def test_unclaimed_pending_outbox_is_quiescent_but_pending_close_is_not(
    tmp_path: Path,
) -> None:
    pending = tmp_path / "pending-outbox.db"
    schema.bootstrap(pending)
    now = "2026-09-03T00:00:00+00:00"
    with sqlite3.connect(pending) as connection:
        connection.execute(
            "insert into local_job(id,idempotency_key,payload_json,state,max_attempts,"
            "available_at,terminal_evidence_digest,created_at,updated_at)"
            " values('job','job-key','{}','completed',1,?,?,?,?)",
            (now, "sha256:" + "1" * 64, now, now),
        )
        connection.execute(
            "insert into local_outbox values('outbox','job','outbox-key','job.completed','{}',?,?)",
            ("sha256:" + "2" * 64, now),
        )
        connection.execute(
            "insert into local_outbox_delivery(outbox_id,state,updated_at)"
            " values('outbox','pending',?)",
            (now,),
        )
    assert (
        _migrate(pending, tmp_path / "pending.backup.db", _Admission()).status.schema_version == 4
    )

    from tests.unit.test_operational_schema_v3 import _pending_close, _source

    closing = _source(tmp_path / "pending-close.db", 3)
    _pending_close(closing, control=True)
    with pytest.raises(ConfigurationError, match="pending snapshot close"):
        _migrate(closing, tmp_path / "closing.backup.db", _Admission())
    assert schema.status(closing).schema_version == 3


def test_invalid_spool_text_and_symlinked_backup_parent_fail_before_admission(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="spool target invalid"):
        migration.MigrationSpoolTarget(tmp_path, "codex", "\ud800", "external")
    path = tmp_path / "v3.db"
    schema.bootstrap(path)
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    os.symlink(real, linked)
    admission = _Admission()
    with pytest.raises(ConfigurationError, match="parent path unsafe"):
        migration.migrate_v3_to_v4(
            path,
            linked / "backup.db",
            migration_lock=tmp_path / "migration.lock",
            admission=admission,
            spool_targets=(),
        )
    assert admission.calls == []
    assert schema.status(path).schema_version == 3


def test_process_death_inside_v4_transaction_recovers_v3_and_exact_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "crash.db"
    backup = tmp_path / "crash.backup.db"
    schema.bootstrap(path)
    before = logical_database_digest(path)
    script = r"""
import os
import sys
from pathlib import Path
from zekam.infrastructure.sqlite import operational_migration as migration

class Admission:
    def trusted_home(self): return Path.cwd()
    def stop_new_admission(self): pass
    def drain_and_reap(self): pass
    def assert_no_admitted_authority(self): pass
    def release_admission(self): pass
    def mark_recovery_required(self): pass

original = migration._migrate_connection
def interrupted(connection, **kwargs):
    original(connection, **kwargs)
    os._exit(73)
migration._migrate_connection = interrupted
migration.migrate_v3_to_v4(
    Path(sys.argv[1]), Path(sys.argv[2]), migration_lock=Path(sys.argv[3]),
    admission=Admission(), spool_targets=(),
)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(path),
            str(backup),
            str(tmp_path / "migration.lock"),
        ],
        check=False,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=Path(__file__).parents[2],
        timeout=15,
    )
    assert result.returncode == 73, result.stderr.decode()
    schema.bootstrap(path, target_version=3)
    assert schema.status(path).schema_version == 3
    assert logical_database_digest(path) == before == logical_database_digest(backup)


def test_trusted_home_mismatch_and_missing_spool_lock_have_no_side_effect(
    tmp_path: Path,
) -> None:
    path = tmp_path / "operational.db"
    schema.bootstrap(path)
    _binding(path)
    home = tmp_path / "home"
    other = tmp_path / "other"
    home.mkdir()
    other.mkdir()
    before = path.read_bytes()
    target = migration.MigrationSpoolTarget(other, "codex", "session", "external")
    with pytest.raises(ConfigurationError, match="spool home mismatch"):
        migration.migrate_v3_to_v4(
            path,
            tmp_path / "backup.db",
            migration_lock=tmp_path / "migration.lock",
            admission=_Admission(home),
            spool_targets=(target,),
        )
    assert path.read_bytes() == before
    assert not (tmp_path / "backup.db").exists()
    assert not (other / "global/runtime/client-lifecycle/codex/writer.lock").exists()

    missing = migration.MigrationSpoolTarget(home, "codex", "session", "external")
    with pytest.raises(ConfigurationError, match="existing spool lock required"):
        migration.migrate_v3_to_v4(
            path,
            tmp_path / "backup.db",
            migration_lock=tmp_path / "migration.lock",
            admission=_Admission(home),
            spool_targets=(missing,),
        )
    assert path.read_bytes() == before
    assert not (home / "global/runtime/client-lifecycle/codex/writer.lock").exists()
