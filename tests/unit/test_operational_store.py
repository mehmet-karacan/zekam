"""WP-03 fresh operational store v1 contract tests."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from os import _exit
from pathlib import Path

import pytest

from zekam.application.operational_store import (
    ConfigRevisionRecord,
    OperationalProjectRecord,
    OperationalUnitOfWork,
    SourceBindingRecord,
    SourceSnapshotRecord,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite import operational_store as store_module
from zekam.infrastructure.sqlite import repository as legacy_schema
from zekam.infrastructure.sqlite.operational_backup import (
    SQLiteOperationalBackup,
    logical_database_digest,
)
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.unit


def _kill_process_before_operational_commit(database: str) -> None:
    store = SQLiteOperationalStore(Path(database))
    with store.unit_of_work() as uow:
        uow.create_project(slug="killed", display_name="Killed")
        _exit(91)


def _store(tmp_path: Path) -> tuple[Path, SQLiteOperationalStore]:
    path = tmp_path / "operational.db"
    bootstrap(path)
    return path, SQLiteOperationalStore(path)


def _authority(
    uow: OperationalUnitOfWork,
) -> tuple[
    ConfigRevisionRecord,
    OperationalProjectRecord,
    SourceBindingRecord,
    SourceSnapshotRecord,
]:
    config_body = {"database": {"backend": "sqlite"}, "network": "deny"}
    config = uow.activate_config(
        config_digest=digest(config_body),
        task_digest=digest("task-authority"),
        sanitized_config=config_body,
    )
    project = uow.create_project(slug="akilli-kasa", display_name="Akilli Kasa")
    uow.add_project_alias(project_id=project.id, alias="kasa")
    binding = uow.bind_source(
        project_id=project.id,
        portable_ref="github:mehmet-karacan/akilli-kasa",
        source_kind="git",
    )
    snapshot = uow.capture_source_snapshot(
        source_binding_id=binding.id,
        revision_ref="fae458661765c2e5228c84ea8e95aa6fded8b1c2",
        tree_digest=digest("tree"),
        content_digest=digest("dirty-worktree-manifest"),
        config_digest=digest("source-config"),
    )
    return config, project, binding, snapshot


def test_uow_persists_project_source_work_run_step_and_checkpoint(tmp_path: Path) -> None:
    path, store = _store(tmp_path)
    checkpoint_payload = {"completed_steps": ["inspect"], "next": "verify"}

    with store.unit_of_work() as uow:
        config, project, _, snapshot = _authority(uow)
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Operational authority",
            state="ready",
            payload_digest=digest("work-v1"),
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            source_snapshot_id=snapshot.id,
            plan_digest=digest("plan-v1"),
            budget={"max_seconds": 60, "max_tokens": 1000},
        )
        first = uow.add_run_step(
            run_id=run.id,
            step_key="inspect",
            input_digest=digest("inspect-input"),
        )
        uow.add_run_step(
            run_id=run.id,
            step_key="verify",
            input_digest=digest("verify-input"),
            dependencies=(first.id,),
        )
        checkpoint = uow.record_checkpoint(
            run_id=run.id,
            sequence=1,
            source_snapshot_id=snapshot.id,
            checkpoint_digest=digest(checkpoint_payload),
            payload=checkpoint_payload,
        )
        uow.commit()

    restarted = SQLiteOperationalStore(path)
    with restarted.unit_of_work() as uow:
        assert uow.get_work(work.id) == work
        assert uow.get_run(run.id) == run
        assert uow.list_checkpoints(run.id) == (checkpoint,)


def test_uow_without_commit_rolls_back_every_related_row(tmp_path: Path) -> None:
    path, store = _store(tmp_path)

    with store.unit_of_work() as uow:
        _authority(uow)

    with sqlite3.connect(path) as connection:
        assert connection.execute("select count(*) from project").fetchone()[0] == 0
        assert connection.execute("select count(*) from source_binding").fetchone()[0] == 0
        assert connection.execute("select count(*) from config_revision").fetchone()[0] == 0


def test_nested_uow_is_rejected_and_outer_transaction_survives(tmp_path: Path) -> None:
    _, store = _store(tmp_path)

    with store.unit_of_work() as outer:
        outer.create_project(slug="demo", display_name="Demo")
        with pytest.raises(ConfigurationError, match="Nested"), store.unit_of_work():
            pass
        outer.commit()


def test_source_binding_rejects_absolute_user_path(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="demo", display_name="Demo")
        with pytest.raises(ValidationFailed, match="mutlak yol"):
            uow.bind_source(
                project_id=project.id,
                portable_ref="/Users/example/private/project",
                source_kind="git",
            )


@pytest.mark.parametrize("sequence", [None, 0, -1, True, 1.0, "1"])
def test_checkpoint_sequence_rejects_wrong_types_and_boundaries(
    tmp_path: Path, sequence: object
) -> None:
    _, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        config, project, _, _ = _authority(uow)
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Checkpoint",
            state="ready",
            payload_digest=digest("work"),
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            plan_digest=digest("plan"),
            budget={},
        )
        with pytest.raises(ValidationFailed, match="pozitif integer"):
            uow.record_checkpoint(
                run_id=run.id,
                sequence=sequence,  # type: ignore[arg-type]
                checkpoint_digest=digest({}),
                payload={},
            )


def test_checkpoint_payload_digest_drift_is_rejected(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        config, project, _, _ = _authority(uow)
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Checkpoint",
            state="ready",
            payload_digest=digest("work"),
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            plan_digest=digest("plan"),
            budget={},
        )
        with pytest.raises(ValidationFailed, match="payload ile eslesmiyor"):
            uow.record_checkpoint(
                run_id=run.id,
                sequence=1,
                checkpoint_digest=digest("different"),
                payload={},
            )


def test_work_transition_is_revision_guarded_and_completed_requires_evidence(
    tmp_path: Path,
) -> None:
    _, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        _, project, _, _ = _authority(uow)
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Transitions",
            state="ready",
            payload_digest=digest("r1"),
        )
        active = uow.transition_work(
            work_item_id=work.id,
            expected_revision=1,
            to_state="active",
            payload_digest=digest("r2"),
            event_digest=digest("event-r2"),
        )
        with pytest.raises(ValidationFailed, match="revision drift"):
            uow.transition_work(
                work_item_id=work.id,
                expected_revision=1,
                to_state="blocked",
                payload_digest=digest("stale"),
                event_digest=digest("stale-event"),
            )
        verification = uow.transition_work(
            work_item_id=active.id,
            expected_revision=2,
            to_state="verification",
            payload_digest=digest("r3"),
            event_digest=digest("event-r3"),
        )
        with pytest.raises(ValidationFailed, match="evidence"):
            uow.transition_work(
                work_item_id=verification.id,
                expected_revision=3,
                to_state="completed",
                payload_digest=digest("r4"),
                event_digest=digest("event-r4"),
            )


def test_append_only_authority_tables_reject_update_and_delete(tmp_path: Path) -> None:
    path, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        _, project, _, _ = _authority(uow)
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Immutable",
            state="ready",
            payload_digest=digest("r1"),
        )
        uow.commit()

    with sqlite3.connect(path) as connection:
        for statement in (
            "update work_revision set state = 'blocked' where work_item_id = ?",
            "delete from work_event where work_item_id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement, (work.id,))


def test_legacy_minimum_profile_is_rejected_without_migrating_rows(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v1.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(legacy_schema._SCHEMA_V1)
        connection.execute(
            "insert into schema_migration(version, name, checksum, applied_at) values (1, ?, ?, ?)",
            (
                legacy_schema.MIGRATION_V1_NAME,
                legacy_schema.MIGRATION_V1_DIGEST,
                "2026-09-02T00:00:00+00:00",
            ),
        )
        connection.execute("insert into zekam_meta(key, value) values ('schema_version', '1')")
        connection.execute(
            "insert into project(id, slug, display_name, source_ref, created_at)"
            " values ('p1', 'demo', 'Demo', 'source:demo', '2026-09-02T00:00:00+00:00')"
        )
        connection.execute(
            "insert into work_item(id, project_id, kind, title, state, revision, created_at)"
            " values ('w1', 'p1', 'task', 'Legacy', 'ready', 1,"
            " '2026-09-02T00:00:00+00:00')"
        )

    with pytest.raises(ConfigurationError, match="migration ledger drift"):
        bootstrap(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("select slug, display_name from project").fetchone() == (
            "demo",
            "Demo",
        )
        assert connection.execute("select id, state, revision from work_item").fetchone() == (
            "w1",
            "ready",
            1,
        )
        assert (
            connection.execute(
                "select count(*) from pragma_table_info('project') where name = 'status'"
            ).fetchone()[0]
            == 0
        )


def test_two_writers_cannot_both_apply_same_expected_revision(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        _, project, _, _ = _authority(uow)
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Race",
            state="ready",
            payload_digest=digest("r1"),
        )
        uow.commit()

    def transition(label: str) -> str:
        try:
            with store.unit_of_work() as uow:
                uow.transition_work(
                    work_item_id=work.id,
                    expected_revision=1,
                    to_state="active",
                    payload_digest=digest(label),
                    event_digest=digest("event-" + label),
                )
                uow.commit()
            return "committed"
        except ValidationFailed:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(transition, ("left", "right")))

    assert sorted(results) == ["committed", "rejected"]


def test_process_kill_before_commit_leaves_no_partial_project(tmp_path: Path) -> None:
    path, store = _store(tmp_path)
    process = get_context("spawn").Process(
        target=_kill_process_before_operational_commit,
        args=(str(path),),
    )
    process.start()
    process.join(timeout=10)
    assert not process.is_alive()
    assert process.exitcode == 91

    with store.unit_of_work() as uow:
        assert uow.list_projects() == ()
        uow.commit()
    current = schema.status(path)
    assert current.integrity_ok and current.schema_ok


@pytest.mark.parametrize("value", (None, "", " padded "))
def test_operational_required_text_rejects_untyped_blank_and_noncanonical(
    value: object,
) -> None:
    with pytest.raises(ValidationFailed):
        store_module._required_text(value, "field")


@pytest.mark.parametrize("value", (None, True, 0, -1, 1.0))
def test_operational_positive_integer_is_exact(value: object) -> None:
    with pytest.raises(ValidationFailed):
        store_module._exact_positive_int(value, "field")


@pytest.mark.parametrize("value", (None, True, "broken"))
def test_operational_digest_validator_rejects_wrong_type_and_shape(value: object) -> None:
    with pytest.raises(ValidationFailed):
        store_module._validate_digest(value, "field")


@pytest.mark.parametrize(
    "value",
    (None, True, "not-a-uuid", "018F0000-0000-7000-8000-000000000001"),
)
def test_operational_uuid_validator_requires_canonical_lowercase(value: object) -> None:
    with pytest.raises(ValidationFailed):
        store_module._canonical_uuid(value, "field")


@pytest.mark.parametrize(
    "value",
    (
        [],
        {"unknown": 1},
        {"summary": 1},
        {"acceptance_criteria": "criterion"},
        {"acceptance_criteria": [""]},
    ),
)
def test_operational_work_payload_rejects_extra_and_untyped_fields(value: object) -> None:
    with pytest.raises(ValidationFailed):
        store_module._work_payload(value)


@pytest.mark.parametrize(
    "payload",
    ("not-json", "[]", '{"summary":1}', '{"acceptance_criteria":[1]}'),
)
def test_operational_row_projection_rejects_corrupt_payload(payload: str) -> None:
    row = {
        "id": "work",
        "project_id": "project",
        "kind": "task",
        "title": "title",
        "state": "ready",
        "revision": 1,
        "evidence_digest": None,
        "external_number": None,
        "payload_json": payload,
    }
    with pytest.raises(ConfigurationError):
        store_module._row_work(row)


def test_operational_uow_rejects_access_after_commit_and_explicit_rollback(
    tmp_path: Path,
) -> None:
    _, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        uow.create_project(slug="committed", display_name="Committed")
        uow.commit()
        with pytest.raises(ConfigurationError, match="aktif degil"):
            uow.list_projects()
    with store.unit_of_work() as uow:
        uow.create_project(slug="rolled-back", display_name="Rolled Back")
        uow.rollback()
    with store.unit_of_work() as uow:
        assert [item.slug for item in uow.list_projects()] == ["committed"]
        uow.commit()


def test_schema_v2_declares_minimum_session_model_and_receipt_families(tmp_path: Path) -> None:
    path, _ = _store(tmp_path)
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            ).fetchall()
        }
    assert {
        "bootstrap_receipt",
        "session",
        "session_event",
        "model_identity",
        "model_revision",
        "model_availability",
        "model_health_observation",
    } <= tables


def test_direct_terminal_run_and_step_without_receipt_or_evidence_are_rejected(
    tmp_path: Path,
) -> None:
    path, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        config, project, _, _ = _authority(uow)
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Terminal",
            state="ready",
            payload_digest=digest("work"),
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            plan_digest=digest("plan"),
            budget={},
        )
        uow.commit()

    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys = on")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("update run set status = 'succeeded' where id = ?", (run.id,))


def test_online_backup_restore_has_exact_logical_parity_and_restart(tmp_path: Path) -> None:
    path, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        _, project, _, _ = _authority(uow)
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Backup parity",
            state="ready",
            payload_digest=digest("work"),
        )
        uow.commit()

    backup_path = tmp_path / "snapshots" / "operational.db"
    restored_path = tmp_path / "restored" / "operational.db"
    adapter = SQLiteOperationalBackup(path)
    backup_receipt = adapter.create_backup(str(backup_path))
    restore_receipt = adapter.restore_backup(str(backup_path), str(restored_path))

    assert backup_receipt.logical_digest == restore_receipt.logical_digest
    assert backup_receipt.logical_digest == logical_database_digest(path)
    assert logical_database_digest(restored_path) == logical_database_digest(path)
    restarted = SQLiteOperationalStore(restored_path)
    with restarted.unit_of_work() as uow:
        assert uow.get_work(work.id) == work


def test_backup_rejects_corrupt_source_and_never_publishes_restore(tmp_path: Path) -> None:
    _, _ = _store(tmp_path)
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not-sqlite")
    destination = tmp_path / "restore.db"

    with pytest.raises(ConfigurationError, match="integrity/schema"):
        SQLiteOperationalBackup(corrupt).create_backup(str(destination))

    assert not destination.exists()


def test_backup_refuses_to_overwrite_existing_user_file(tmp_path: Path) -> None:
    path, _ = _store(tmp_path)
    destination = tmp_path / "existing.db"
    destination.write_bytes(b"user-content")

    with pytest.raises(ConfigurationError, match="overwrite"):
        SQLiteOperationalBackup(path).create_backup(str(destination))

    assert destination.read_bytes() == b"user-content"


def test_mid_bootstrap_exception_rolls_back_entire_operational_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fresh-fault.db"
    original_execute = schema._execute_script

    def fail_schema(connection: sqlite3.Connection, script: str) -> None:
        if script == schema._SCHEMA:
            connection.execute("create table injected_partial(id integer)")
            raise OSError("injected-mid-migration")
        original_execute(connection, script)

    monkeypatch.setattr(schema, "_execute_script", fail_schema)
    with pytest.raises(OSError, match="injected-mid-migration"):
        bootstrap(path)

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "select count(*) from sqlite_master where name not like 'sqlite_%'"
            ).fetchone()[0]
            == 0
        )


def test_operational_schema_drift_is_rejected_without_removing_unknown_object(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operational-drift.sqlite3"
    bootstrap(database)
    with sqlite3.connect(database) as connection:
        connection.execute("create index unauthorized_project_name on project(display_name)")
        connection.commit()

    with pytest.raises(ConfigurationError, match="schema manifest drift"):
        bootstrap(database)

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "select count(*) from sqlite_master where type = 'index' "
                "and name = 'unauthorized_project_name'"
            ).fetchone()[0]
            == 1
        )


def test_project_resolution_and_alias_slug_collisions_fail_atomically(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        first = uow.create_project(slug="first", display_name="First")
        uow.add_project_alias(project_id=first.id, alias="one")
        uow.commit()

    with store.unit_of_work() as uow:
        assert uow.resolve_project("one") == first
        assert uow.list_project_aliases(first.id) == ("one",)
        with pytest.raises(ValidationFailed, match="mevcut slug"):
            uow.add_project_alias(project_id=first.id, alias="first")

    with store.unit_of_work() as uow:
        second = uow.create_project(slug="second", display_name="Second")
        with pytest.raises(ValidationFailed, match="mevcut slug"):
            uow.add_project_alias(project_id=second.id, alias="first")

    with store.unit_of_work() as uow:
        assert [project.slug for project in uow.list_projects()] == ["first"]


def test_work_payload_shape_digest_and_duplicate_number_fail_closed(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="work-payload", display_name="Work Payload")
        first = uow.create_work(
            project_id=project.id,
            kind="task",
            title="First",
            state="proposed",
            payload={"summary": "Summary", "acceptance_criteria": ["Criterion"]},
            external_number="AK-1",
        )
        uow.commit()

    with store.unit_of_work() as uow:
        projected = uow.get_work(first.id)
        assert projected.summary == "Summary"
        assert projected.acceptance_criteria == ("Criterion",)
        assert projected.external_number == "AK-1"
        with pytest.raises(ValidationFailed, match="alan tipleri"):
            uow.create_work(
                project_id=project.id,
                kind="task",
                title="Wrong type",
                state="proposed",
                payload={"summary": None, "acceptance_criteria": []},
            )
        with pytest.raises(ValidationFailed, match="digest drift"):
            uow.create_work(
                project_id=project.id,
                kind="task",
                title="Drift",
                state="proposed",
                payload={"summary": "x", "acceptance_criteria": []},
                payload_digest=digest("different"),
            )

    with (
        pytest.raises(ValidationFailed, match="constraint ihlali"),
        store.unit_of_work() as uow,
    ):
        uow.create_work(
            project_id=project.id,
            kind="task",
            title="Duplicate",
            state="proposed",
            payload={"summary": "", "acceptance_criteria": []},
            external_number="AK-1",
        )
        uow.commit()

    with store.unit_of_work() as uow:
        assert [work.id for work in uow.list_work(project_id=project.id)] == [first.id]


def test_bootstrap_session_and_model_state_are_durable_and_type_strict(
    tmp_path: Path,
) -> None:
    path, store = _store(tmp_path)
    with store.unit_of_work() as uow:
        _, project, _, _ = _authority(uow)
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Session model",
            state="ready",
            payload_digest=digest("work"),
        )
        uow.record_bootstrap_receipt(
            receipt_digest=digest("receipt"),
            plan_digest=digest("plan"),
            task_digest=digest("task"),
            status="completed",
        )
        session = uow.open_session(
            client_id="codex",
            device_id="macbook-arm64",
            project_id=project.id,
            work_item_id=work.id,
        )
        uow.record_session_event(
            session_id=session.id,
            event_kind="opened",
            event_digest=digest("session-opened"),
        )
        model = uow.register_model(
            canonical_id="baai/bge-m3",
            access_name="local-bge-m3",
            modality="embedding",
        )
        revision = uow.observe_model_revision(
            model_identity_id=model.id,
            provider_fingerprint_digest=digest("bge-files"),
            observed_revision="local-current",
        )
        uow.record_model_availability(
            model_revision_id=revision.id,
            device_scope="macbook-arm64",
            client_scope="zekam",
            provider_scope="local",
            available=True,
        )
        uow.record_model_health(
            model_revision_id=revision.id,
            status="passed",
            evidence_digest=digest("probe"),
            latency_ms=42,
        )
        with pytest.raises(ValidationFailed, match="bool"):
            uow.record_model_availability(
                model_revision_id=revision.id,
                device_scope="macbook-arm64",
                client_scope="zekam",
                provider_scope="local",
                available=1,  # type: ignore[arg-type]
            )
        with pytest.raises(ValidationFailed, match="non-negative integer"):
            uow.record_model_health(
                model_revision_id=revision.id,
                status="passed",
                evidence_digest=digest("probe-2"),
                latency_ms=True,
            )
        uow.commit()

    with sqlite3.connect(path) as connection:
        assert connection.execute("select count(*) from bootstrap_receipt").fetchone()[0] == 1
        assert connection.execute("select count(*) from session_event").fetchone()[0] == 1
        model_health_count = connection.execute(
            "select count(*) from model_health_observation"
        ).fetchone()[0]
        assert model_health_count == 1
