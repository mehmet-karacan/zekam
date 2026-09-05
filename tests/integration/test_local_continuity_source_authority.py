"""Dormant Gate-A portable-plan and local-sidecar integration."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from zekam.application.home import HomeLayout
from zekam.application.local_continuity_source_authority import (
    BACKUP_RESTORE_READY,
    PortableSourcePlanRecord,
    SourceAuthorityResult,
)
from zekam.application.local_continuity_source_plan import ContinuitySourceRecipe
from zekam.application.mutation_admission import (
    _GATE_A_LOCK,
    _GATE_A_STATES,
    _GateASourceCapability,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_continuity_source_plan import (
    BoundedContinuitySource,
    publish_portable_source_plan,
    read_portable_source_plan,
)
from zekam.infrastructure.sqlite import local_continuity_source_authority as authority_module
from zekam.infrastructure.sqlite.local_continuity_source_authority import (
    DDL_DIGEST,
    SCHEMA_FINGERPRINT,
    SQLiteLocalSourceAuthority,
    local_source_authority_path,
)
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest
from zekam.infrastructure.sqlite.operational_migration import migrate_v3_to_v4
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

POLICY = {"runtime": {"network_default": "deny"}}
SOURCE_REF = "src/example.py"


@pytest.fixture
def authority(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "source"
    (root / "src").mkdir(parents=True)
    (root / SOURCE_REF).write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Gate A Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "gate-a@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "add", SOURCE_REF], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)

    home = tmp_path / "home"
    HomeLayout(home).ensure()
    path = home / "state" / "operational.db"
    bootstrap(path)
    operational = SQLiteOperationalStore(path)
    with operational.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest(POLICY),
            task_digest=digest("gate-a-task"),
            sanitized_config=POLICY,
        )
        project = uow.create_project(slug="gate-a", display_name="Gate A")
        binding = uow.bind_source(
            project_id=project.id,
            portable_ref="project/gate-a",
            source_kind="git",
        )
        uow.commit()
    HomeLayout(home).ensure_project(project.id)
    realm = str(uuid4())
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into project_knowledge_realm values(?,?,?)",
            (project.id, realm, dt.datetime.now(dt.UTC).isoformat()),
        )
    recipe = ContinuitySourceRecipe(
        project.id,
        realm,
        binding.id,
        (SOURCE_REF,),
        config.task_digest,
        config.config_digest,
    )
    adapter = BoundedContinuitySource(root, recipe)
    plan = adapter.capture()
    snapshot = adapter.apply(operational, plan, expected_plan_digest=plan.content_digest)
    (home / "backups").mkdir(mode=0o700)

    class Admission:
        def trusted_home(self) -> Path:
            return home

        def stop_new_admission(self) -> None:
            pass

        def drain_and_reap(self) -> None:
            pass

        def assert_no_admitted_authority(self) -> None:
            pass

        def release_admission(self) -> None:
            pass

        def mark_recovery_required(self) -> None:
            raise AssertionError

    migrate_v3_to_v4(
        path,
        home / "backups" / "gate-a-v3.sqlite3",
        migration_lock=home / "state" / "operational-migration.lock",
        admission=Admission(),
        spool_targets=(),
    )
    record = PortableSourcePlanRecord(snapshot.id, plan)
    return {
        "root": root,
        "home": home,
        "path": path,
        "operational": operational,
        "recipe": recipe,
        "plan": plan,
        "snapshot": snapshot,
        "record": record,
    }


def _rows(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "local_source_authority_meta",
                "local_source_authority_migration",
                "local_source_binding_revision",
                "local_source_binding_head",
            )
        }


def test_unselected_sibling_directory_does_not_spuriously_drift_root_identity(
    authority: dict[str, Any],
) -> None:
    adapter = BoundedContinuitySource(authority["root"], authority["recipe"])
    (authority["root"] / "unselected-sibling").mkdir()
    assert adapter.capture().content_digest == authority["plan"].content_digest


def _execute(
    store: SQLiteLocalSourceAuthority,
    authority: dict[str, Any],
    *,
    previous_revision_digest: str | None,
    rebind: bool,
) -> SourceAuthorityResult:
    command = ("continuity", "source-rebind" if rebind else "source-bind")
    capability = object.__new__(_GateASourceCapability)
    with _GATE_A_LOCK:
        _GATE_A_STATES[capability] = (command, "INPUTS_VALID")
    return store.execute(
        capability=capability,
        record=authority["record"],
        source=BoundedContinuitySource(authority["root"], authority["recipe"]),
        device_id="macbook",
        root=authority["root"],
        previous_revision_digest=previous_revision_digest,
        rebind=rebind,
    )


def test_publish_replay_and_read_reconstruct_exact_plan(authority: dict[str, Any]) -> None:
    record = authority["record"]
    before = logical_database_digest(authority["path"])
    name = publish_portable_source_plan(authority["home"], record)
    assert name == record.plan.content_digest[7:] + ".json"
    path = authority["home"] / "projeler" / record.plan.recipe.project_id / "baglantilar" / name
    original = path.read_bytes()
    assert publish_portable_source_plan(authority["home"], record) == name
    assert path.read_bytes() == original
    assert (
        read_portable_source_plan(
            authority["home"], record.plan.recipe.project_id, record.plan.content_digest
        )
        == record
    )
    assert str(authority["root"]).encode() not in original
    assert logical_database_digest(authority["path"]) == before


def test_bind_then_rebind_is_append_only_and_fenced(authority: dict[str, Any]) -> None:
    record = authority["record"]
    publish_portable_source_plan(authority["home"], record)
    store = SQLiteLocalSourceAuthority(authority["home"], authority["path"])
    first = _execute(store, authority, previous_revision_digest=None, rebind=False)
    assert first.body()["backup_restore_ready"] is False
    assert first.generation == 1
    assert _rows(store.path) == {
        "local_source_authority_meta": 1,
        "local_source_authority_migration": 1,
        "local_source_binding_revision": 1,
        "local_source_binding_head": 1,
    }
    second = _execute(store, authority, previous_revision_digest=first.revision_digest, rebind=True)
    assert second.generation == 2 and second.revision_digest != first.revision_digest
    assert _rows(store.path)["local_source_binding_revision"] == 2
    assert _rows(store.path)["local_source_binding_head"] == 2
    before = store.path.read_bytes()
    assert (
        _execute(store, authority, previous_revision_digest=first.revision_digest, rebind=True)
        == second
    )
    assert store.path.read_bytes() == before
    third = _execute(store, authority, previous_revision_digest=second.revision_digest, rebind=True)
    assert third.generation == 3
    with pytest.raises(PolicyViolation):
        _execute(store, authority, previous_revision_digest=first.revision_digest, rebind=True)
    assert _rows(store.path)["local_source_binding_revision"] == 3


def test_sidecar_schema_metadata_permissions_and_no_wal(authority: dict[str, Any]) -> None:
    result = _execute(
        SQLiteLocalSourceAuthority(authority["home"], authority["path"]),
        authority,
        previous_revision_digest=None,
        rebind=False,
    )
    path = local_source_authority_path(authority["home"])
    assert path.stat().st_mode & 0o777 == 0o600
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()
    with sqlite3.connect(path) as db:
        meta = db.execute(
            "select schema_digest,local_instance_id from local_source_authority_meta"
        ).fetchone()
        ledger = db.execute(
            "select version,name,checksum from local_source_authority_migration"
        ).fetchone()
        assert meta is not None and meta[0] == SCHEMA_FINGERPRINT and len(meta[1]) == 36
        assert ledger == (1, "source-authority-v1", DDL_DIGEST)
        assert db.execute("pragma foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "update local_source_binding_revision set generation=2 where revision_digest=?",
                (result.revision_digest,),
            )


def test_binding_failure_does_not_change_operational_database(authority: dict[str, Any]) -> None:
    before = logical_database_digest(authority["path"])
    store = SQLiteLocalSourceAuthority(authority["home"], authority["path"])
    with pytest.raises(PolicyViolation):
        _execute(store, authority, previous_revision_digest="sha256:" + "0" * 64, rebind=True)
    assert logical_database_digest(authority["path"]) == before
    assert not store.path.exists()


def test_concurrent_rebind_same_predecessor_has_one_success(authority: dict[str, Any]) -> None:
    store = SQLiteLocalSourceAuthority(authority["home"], authority["path"])
    first = _execute(store, authority, previous_revision_digest=None, rebind=False)

    def rebind() -> str:
        candidate = SQLiteLocalSourceAuthority(authority["home"], authority["path"])
        try:
            return _execute(
                candidate, authority, previous_revision_digest=first.revision_digest, rebind=True
            ).revision_digest
        except PolicyViolation:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _value: rebind(), range(2)))
    assert outcomes.count("rejected") == 1
    assert len(set(outcomes) - {"rejected"}) == 1
    assert _rows(store.path)["local_source_binding_revision"] == 2


def test_commit_unknown_is_classified_from_exact_durable_revision(
    authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteLocalSourceAuthority(authority["home"], authority["path"])
    first = _execute(store, authority, previous_revision_digest=None, rebind=False)
    original: Any = authority_module.__dict__["_connect"]

    class CommitUnknown:
        def __init__(self, database: sqlite3.Connection) -> None:
            self.database = database

        def __getattr__(self, name: str) -> Any:
            return getattr(self.database, name)

        def commit(self) -> None:
            self.database.commit()
            raise OSError("untrusted commit detail")

        def close(self) -> None:
            self.database.close()

    def uncertain(path: Path, *, readonly: bool) -> Any:
        database = original(path, readonly=readonly)
        return database if readonly else CommitUnknown(database)

    monkeypatch.setattr(
        "zekam.infrastructure.sqlite.local_continuity_source_authority._connect",
        uncertain,
    )
    result = _execute(store, authority, previous_revision_digest=first.revision_digest, rebind=True)
    assert result.generation == 2
    assert _rows(store.path)["local_source_binding_revision"] == 2


def test_corrupt_or_extra_schema_object_rejects_without_new_revision(
    authority: dict[str, Any],
) -> None:
    store = SQLiteLocalSourceAuthority(authority["home"], authority["path"])
    first = _execute(store, authority, previous_revision_digest=None, rebind=False)
    with sqlite3.connect(store.path) as db:
        db.execute("create table unexpected(value text)")
    with pytest.raises(PolicyViolation):
        _execute(store, authority, previous_revision_digest=first.revision_digest, rebind=True)
    assert _rows(store.path)["local_source_binding_revision"] == 1


def test_portable_conflict_and_symlink_are_rejected(authority: dict[str, Any]) -> None:
    record = authority["record"]
    name = record.plan.content_digest[7:] + ".json"
    parent = authority["home"] / "projeler" / record.plan.recipe.project_id / "baglantilar"
    target = parent / name
    target.write_bytes(b"{}")
    with pytest.raises(PolicyViolation):
        publish_portable_source_plan(authority["home"], record)
    target.unlink()
    victim = authority["home"] / "victim"
    victim.write_bytes(b"victim")
    os.symlink(victim, target)
    with pytest.raises(PolicyViolation):
        publish_portable_source_plan(authority["home"], record)
    assert victim.read_bytes() == b"victim"


def test_portable_replacement_before_commit_rolls_back(
    authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cast(
        Callable[[Path, PortableSourcePlanRecord], str],
        authority_module.__dict__["publish_portable_source_plan"],
    )

    def replace_after_publish(home: Path, record: PortableSourcePlanRecord) -> str:
        name = original(home, record)
        target = home / "projeler" / record.plan.recipe.project_id / "baglantilar" / name
        replacement = target.with_name("replacement.json")
        replacement.write_bytes(b"{}")
        os.replace(replacement, target)
        return name

    monkeypatch.setattr(
        "zekam.infrastructure.sqlite.local_continuity_source_authority.publish_portable_source_plan",
        replace_after_publish,
    )
    store = SQLiteLocalSourceAuthority(authority["home"], authority["path"])
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _execute(store, authority, previous_revision_digest=None, rebind=False)
    assert _rows(store.path)["local_source_binding_revision"] == 0


def test_late_durable_classification_emits_no_result(
    authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0]
    monkeypatch.setattr(
        "zekam.infrastructure.local_continuity_source_plan.time.monotonic_ns", lambda: now[0]
    )
    store = SQLiteLocalSourceAuthority(authority["home"], authority["path"])
    classify = store._classify

    def late(candidate: Any) -> Any:
        result = classify(candidate)
        now[0] = 20_000_000_001
        return result

    monkeypatch.setattr(store, "_classify", late)
    with pytest.raises(PolicyViolation):
        _execute(store, authority, previous_revision_digest=None, rebind=False)
    assert _rows(store.path)["local_source_binding_revision"] == 1


def test_sidecar_symlink_and_unexpected_side_file_reject_before_write(
    authority: dict[str, Any],
) -> None:
    store = SQLiteLocalSourceAuthority(authority["home"], authority["path"])
    os.symlink(authority["home"] / "missing-victim", store.path)
    with pytest.raises(PolicyViolation):
        _execute(store, authority, previous_revision_digest=None, rebind=False)
    store.path.unlink()
    Path(str(store.path) + "-shm").write_bytes(b"")
    with pytest.raises(PolicyViolation):
        _execute(store, authority, previous_revision_digest=None, rebind=False)
    assert not store.path.exists()


def test_backup_readiness_cannot_be_enabled_by_binding(authority: dict[str, Any]) -> None:
    assert BACKUP_RESTORE_READY is False
    assert "source-authority.sqlite3" not in str(authority["home"] / "projeler")
