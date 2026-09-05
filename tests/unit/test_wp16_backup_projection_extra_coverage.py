from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from zekam.application.obsidian_projection import build_obsidian_projection
from zekam.application.operational_store import OperationalSchemaStatus
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    NotFound,
    PolicyViolation,
    ValidationFailed,
)
from zekam.domain.markdown_projection import (
    ObsidianNoteKind,
    ObsidianProfile,
    ObsidianProjectionBundle,
    ObsidianProjectionRecord,
    ProjectionRecord,
    ProjectionSourceRef,
)
from zekam.domain.session_continuity import DataClassification, TruthClass
from zekam.infrastructure import local_backup
from zekam.infrastructure.sqlite import operational_backup
from zekam.infrastructure.sqlite import operational_migration as migration
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.storage.obsidian_projection_store import (
    LocalObsidianProjectionStore,
    StagedObsidianProjection,
)

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000201")


class _Admission:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.calls: list[str] = []

    def trusted_home(self) -> Path:
        return self.home

    def stop_new_admission(self) -> None:
        self.calls.append("stop")

    def drain_and_reap(self) -> None:
        self.calls.append("drain")

    def assert_no_admitted_authority(self) -> None:
        self.calls.append("assert")

    def release_admission(self) -> None:
        self.calls.append("release")

    def mark_recovery_required(self) -> None:
        self.calls.append("recovery")


def _bundle(entity_id: str = "work-1") -> ObsidianProjectionBundle:
    record = ProjectionRecord(
        "work",
        entity_id,
        f"Title {entity_id}",
        "active",
        f"Safe projection {entity_id}",
        (ProjectionSourceRef("work", entity_id, "rev-1", digest(entity_id)),),
    )
    typed = ObsidianProjectionRecord(
        record,
        ObsidianNoteKind.WORK,
        "yerel",
        PROJECT_ID,
        TruthClass.REPO_FACT,
        DataClassification.PUBLIC,
        NOW,
    )
    return build_obsidian_projection(
        (typed,),
        project_id=PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=digest("policy"),
    )


def test_local_backup_path_manifest_and_regular_file_guards(tmp_path: Path) -> None:
    for value in (None, "", "../x", "/x", "a/../b", 7, "x" * 4097):
        with pytest.raises(ValidationFailed):
            local_backup._safe_relative(value)
    assert local_backup._safe_relative("state/operational.db") == "state/operational.db"

    existing = tmp_path / "existing"
    existing.write_bytes(b"x")
    with pytest.raises(ValidationFailed):
        local_backup._destination(existing)
    with pytest.raises(ValidationFailed):
        local_backup._destination(Path("relative"))

    private = tmp_path / "private"
    private.write_bytes(b"payload")
    private.chmod(0o600)
    assert local_backup._regular(private).st_size == 7
    private.chmod(0o622)
    with pytest.raises(PolicyViolation):
        local_backup._regular(private)
    private.chmod(0o600)
    hardlink = tmp_path / "hardlink"
    os.link(private, hardlink)
    with pytest.raises(PolicyViolation):
        local_backup._regular(private)

    canonical = canonical_bytes({"a": 1})
    assert local_backup._strict_document(canonical) == {"a": 1}
    for raw in (b"", b'{"a":1,"a":2}', b'{"a": NaN}', b'{ "a": 1 }', b"\xff"):
        with pytest.raises(ValidationFailed):
            local_backup._strict_document(raw)


def test_local_backup_copy_and_bundle_failures_leave_no_partial(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "nested" / "target"
    source.write_bytes(b"immutable")
    source.chmod(0o600)
    local_backup._copy_regular(source, target, 0o400)
    assert target.read_bytes() == b"immutable"
    assert target.stat().st_mode & 0o777 == 0o400

    class _NotReady:
        def status(self) -> dict[str, bool]:
            return {"all_ready": False}

    destination = tmp_path / "bundle"
    with pytest.raises(PolicyViolation, match="not ready"):
        local_backup.create_bundle(cast(Any, _NotReady()), tmp_path, destination)
    assert not destination.exists()
    assert not tuple(tmp_path.glob(".bundle.*"))


def test_local_backup_verify_rejects_symlink_and_bad_manifest(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValidationFailed):
        local_backup.verify_bundle(link)

    manifest = real / "MANIFEST.json"
    manifest.write_bytes(b'{"schema":"wrong"}')
    manifest.chmod(0o400)
    with pytest.raises(ValidationFailed):
        local_backup.verify_bundle(real)


def test_operational_backup_real_create_restore_replay_and_restart(tmp_path: Path) -> None:
    source = tmp_path / "operational.db"
    backup = tmp_path / "backup.db"
    restored = tmp_path / "restored.db"
    schema.bootstrap(source)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "insert into project(id,slug,display_name,created_at) values(?,?,?,?)",
            ("p", "project", "Project", NOW.isoformat()),
        )

    adapter = operational_backup.SQLiteOperationalBackup(source)
    receipt = adapter.create_backup(str(backup))
    assert receipt.logical_digest == operational_backup.logical_database_digest(source)
    assert receipt.logical_digest == operational_backup.logical_database_digest(backup)
    assert not tuple(tmp_path.glob(".backup.db.partial-*"))
    with pytest.raises(ConfigurationError, match="overwrite"):
        adapter.create_backup(str(backup))
    restored_receipt = operational_backup.SQLiteOperationalBackup(source).restore_backup(
        str(backup), str(restored)
    )
    assert restored_receipt.logical_digest == receipt.logical_digest
    assert operational_backup.logical_database_digest(restored) == receipt.logical_digest
    assert schema.status(restored).schema_ok


def test_operational_backup_corruption_symlink_and_partial_cleanup(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    corrupt.chmod(0o600)
    destination = tmp_path / "never.db"
    with pytest.raises(ConfigurationError):
        operational_backup.SQLiteOperationalBackup(corrupt).create_backup(str(destination))
    assert not destination.exists()
    assert not tuple(tmp_path.glob(".never.db.partial-*"))

    link = tmp_path / "source-link.db"
    link.symlink_to(corrupt)
    with pytest.raises(ConfigurationError):
        operational_backup.SQLiteOperationalBackup(link).create_backup(str(destination))
    with pytest.raises(ConfigurationError):
        operational_backup.SQLiteOperationalBackup(corrupt).create_backup_anchored(
            str(destination), parent_descriptor=-1
        )
    with pytest.raises(ConfigurationError, match="header"):
        operational_backup._serialized_digest(b"broken", expected_version=3)


def test_operational_backup_descriptor_bounds_and_truncation(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(b"abc")
    descriptor = os.open(payload, os.O_RDONLY)
    try:
        assert operational_backup._read_all(descriptor, 3) == b"abc"
        with pytest.raises(ConfigurationError, match="bounds"):
            operational_backup._read_all(descriptor, 0)
        with pytest.raises(ConfigurationError, match="truncated"):
            operational_backup._read_all(descriptor, 4)
    finally:
        os.close(descriptor)


def test_migration_spool_target_scope_type_duplicate_and_limits(tmp_path: Path) -> None:
    absolute = tmp_path.resolve()
    valid = migration.MigrationSpoolTarget(absolute, "client", "session", "external")
    assert valid.home == absolute
    invalid_values: tuple[dict[str, Any], ...] = (
        {"home": Path("relative")},
        {"client_id": ""},
        {"session_id": 1},
        {"external_session_id": "x" * 513},
        {"client_id": "\ud800"},
    )
    for changes in invalid_values:
        with pytest.raises(ConfigurationError):
            replace(valid, **changes)
    duplicate = (valid, replace(valid, client_id="other"))
    with (
        pytest.raises(ConfigurationError, match="duplicate"),
        migration._spool_barrier(duplicate, trusted_home=absolute),
    ):
        pass
    mismatch = (replace(valid, home=(tmp_path / "other").resolve()),)
    with (
        pytest.raises(ConfigurationError, match="mismatch"),
        migration._spool_barrier(mismatch, trusted_home=absolute),
    ):
        pass


def test_migration_path_lock_concurrency_and_restart(tmp_path: Path) -> None:
    lock = tmp_path / "migration.lock"
    with (
        migration._migration_lock(lock),
        pytest.raises(ConcurrencyConflict),
        migration._migration_lock(lock),
    ):
        pass
    with migration._migration_lock(lock):
        assert lock.read_bytes() == b"0"
    unsafe = tmp_path / "unsafe.lock"
    unsafe.write_bytes(b"xx")
    unsafe.chmod(0o600)
    with (
        pytest.raises(ConfigurationError, match="identity"),
        migration._migration_lock(unsafe),
    ):
        pass
    link = tmp_path / "link.lock"
    link.symlink_to(lock)
    with pytest.raises(ConfigurationError), migration._migration_lock(link):
        pass
    with pytest.raises(ConfigurationError):
        migration._identity(Path("relative"))


def test_migration_precommit_failure_rolls_back_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "operational.db"
    backup = tmp_path / "before.db"
    schema.bootstrap(database)
    admission = _Admission(tmp_path)

    def fail_before_commit(*_: Any, **__: Any) -> None:
        raise ConfigurationError("forced precommit failure")

    monkeypatch.setattr(migration, "_migrate_connection", fail_before_commit)
    with pytest.raises(ConfigurationError, match="forced"):
        migration.migrate_v3_to_v4(
            database,
            backup,
            migration_lock=tmp_path / "migration.lock",
            admission=admission,
            spool_targets=(),
        )
    assert schema.status(database).schema_version == 3
    assert schema.status(backup).schema_version == 3
    assert admission.calls == ["stop", "drain", "assert", "release"]


def test_migration_postcommit_failure_marks_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "operational.db"
    backup = tmp_path / "before.db"
    schema.bootstrap(database)
    admission = _Admission(tmp_path)
    monkeypatch.setattr(
        "zekam.infrastructure.sqlite.operational_migration.schema.status",
        lambda _: OperationalSchemaStatus(True, 4, False, False),
    )
    with pytest.raises(ConfigurationError, match="postcommit"):
        migration.migrate_v3_to_v4(
            database,
            backup,
            migration_lock=tmp_path / "migration.lock",
            admission=admission,
            spool_targets=(),
        )
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "select value from zekam_meta where key='schema_version'"
            ).fetchone()[0]
            == "4"
        )
    assert admission.calls == ["stop", "drain", "assert", "recovery"]
    assert backup.exists()


def test_migration_rejects_wrong_container_and_admission_boundary(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="absolute"):
        migration.migrate_v3_to_v4(
            Path("db"),
            Path("backup"),
            migration_lock=Path("lock"),
            admission=cast(Any, object()),
            spool_targets=(),
        )
    database = tmp_path / "db"
    database.write_bytes(b"x")
    with pytest.raises(ConfigurationError, match="spool targets"):
        migration.migrate_v3_to_v4(
            database,
            tmp_path / "backup",
            migration_lock=tmp_path / "lock",
            admission=cast(Any, object()),
            spool_targets=cast(Any, []),
        )
    with pytest.raises(ConfigurationError, match="admission boundary"):
        migration.migrate_v3_to_v4(
            database,
            tmp_path / "backup",
            migration_lock=tmp_path / "lock",
            admission=cast(Any, object()),
            spool_targets=(),
        )


def test_obsidian_publish_replay_restart_and_stale_file_cleanup(tmp_path: Path) -> None:
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    first = _bundle("work-1")
    first_result = store.publish(store.stage(first))
    first_path = (
        Path(first_result.stable_vault) / "01_ACTIVE" / "CALISMA_OGELERI" / "work-work-1.md"
    )
    assert first_path.is_file()

    replay = store.stage(first)
    replay_result = store.publish(replay)
    assert replay_result.generation == first_result.generation
    assert not replay.staging_root.exists()

    second = _bundle("work-2")
    second_result = store.publish(store.stage(second))
    assert not first_path.exists()
    assert (
        Path(second_result.stable_vault) / "01_ACTIVE" / "CALISMA_OGELERI" / "work-work-2.md"
    ).is_file()
    restarted = LocalObsidianProjectionStore(tmp_path / "obsidian")
    verified = restarted.verify_current(
        "yerel",
        PROJECT_ID,
        ObsidianProfile.PUBLIC_SAFE,
        expected_projection_digest=second.projection_digest,
        expected_manifest_digest=second.manifest_digest,
        expected_receipt_digest=second.receipt_digest,
    )
    assert verified["status"] == "passed" and verified["grants_authority"] is False


def test_obsidian_stage_publish_path_and_symlink_guards(tmp_path: Path) -> None:
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    bundle = _bundle()
    staged = store.stage(bundle)
    with pytest.raises(PolicyViolation, match="generation"):
        store.publish(replace(staged, generation="0" * 64))
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(PolicyViolation, match="profile root"):
        store.publish(StagedObsidianProjection(bundle, outside, staged.generation))
    staged.staging_root.joinpath("evil").symlink_to(tmp_path / "outside")
    with pytest.raises(PolicyViolation, match="symlink"):
        store.publish(staged)

    with pytest.raises(ValidationFailed):
        store._profile_root("Bad Realm", PROJECT_ID, ObsidianProfile.PUBLIC_SAFE, create=True)
    with pytest.raises(ValidationFailed):
        store._profile_root(
            "yerel", cast(Any, "not-uuid"), ObsidianProfile.PUBLIC_SAFE, create=True
        )
    with pytest.raises(NotFound):
        LocalObsidianProjectionStore(tmp_path / "missing")._profile_root(
            "yerel", PROJECT_ID, ObsidianProfile.PUBLIC_SAFE, create=False
        )


def test_obsidian_corruption_and_current_pointer_fail_closed(tmp_path: Path) -> None:
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    bundle = _bundle()
    published = store.publish(store.stage(bundle))
    profile = tmp_path / "obsidian" / "yerel" / str(PROJECT_ID) / "public-safe"
    pointer = profile / "CURRENT.json"
    pointer.write_bytes(b"not-json")
    with pytest.raises(ValidationFailed, match="pointer"):
        store.verify_current(
            "yerel",
            PROJECT_ID,
            ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )

    pointer.write_bytes(
        canonical_bytes(
            {
                "schema": "incomplete",
                "generation": published.generation,
            }
        )
    )
    with pytest.raises(ValidationFailed, match="exact schema"):
        store.verify_current(
            "yerel",
            PROJECT_ID,
            ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )

    store.publish(store.stage(bundle))
    generation = profile / "generations" / published.generation
    note = next(path for path in generation.rglob("*.md"))
    note.write_text("corrupted", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="digest drift"):
        store.verify_current(
            "yerel",
            PROJECT_ID,
            ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )


def test_obsidian_manifest_path_and_stable_marker_security(tmp_path: Path) -> None:
    for value in ("", "../x", "/x", "C:\\x", "a\\b"):
        with pytest.raises(PolicyViolation):
            LocalObsidianProjectionStore._write_file(tmp_path, value, b"x")

    profile = tmp_path / "profile"
    generation = tmp_path / "generation"
    profile.mkdir()
    generation.mkdir()
    stable = profile / "GUNCEL_BELLEK"
    stable.mkdir()
    marker = stable / ".zekam-managed-files.json"
    marker.write_text(json.dumps({"schema": "wrong", "files": []}), encoding="utf-8")
    with pytest.raises(ValidationFailed, match="marker schema"):
        LocalObsidianProjectionStore._publish_stable_vault(profile, generation)
    marker.unlink()
    stable.rmdir()
    stable.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PolicyViolation, match="GUNCEL_BELLEK"):
        LocalObsidianProjectionStore._publish_stable_vault(profile, generation)
