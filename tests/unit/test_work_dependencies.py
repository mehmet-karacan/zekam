"""Canonical Work dependency history, cycles and effect admission (v8)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, ConcurrencyConflict, ValidationFailed
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite import operational_migration as migration
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_backup import _logical_rows_digest
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


def _world(tmp_path: Path):
    path = tmp_path / "operational.db"
    schema.bootstrap_v8(path)
    SQLiteLocalRuntimeStore(path)
    store = SQLiteOperationalStore(path)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="dependencies", display_name="Dependencies")
        works = [
            uow.create_work(project_id=project.id, kind="task", title=f"Work {i}", state="ready")
            for i in range(3)
        ]
        uow.commit()
    return path, store, project, works


def _complete(uow, work_id: str) -> None:
    work = uow.get_work(work_id)
    for state in ("active", "verification", "completed"):
        work = uow.transition_work(
            work_item_id=work.id,
            expected_revision=work.revision,
            to_state=state,
            payload_digest=digest("payload"),
            event_digest=digest((work.id, state)),
            evidence_digest=digest("fixture-evidence") if state == "completed" else None,
        )


def test_v8_pins_and_old_schema_do_not_auto_migrate(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    schema.bootstrap_v7(path)
    with SQLiteOperationalStore(path).unit_of_work() as uow:
        with pytest.raises(ConfigurationError, match="migration-required"):
            uow.list_work_dependencies(str(uuid4()))
    assert schema.status(path).schema_version == 7
    assert schema.V8_MIGRATION_LEDGER[:-1] == schema.V7_MIGRATION_LEDGER
    assert schema.V8_MIGRATION_NAME == "operational-work-dependency-v8"
    assert (
        schema.V8_MIGRATION_DIGEST
        == "sha256:ed1fc9a1716d111c59491e67e625d2bb333894dd6983eaf8b20c019ce5de44ce"
    )
    assert (
        schema.V8_SCHEMA_DIGEST
        == "sha256:412a2986eadfd3d80938d80502816fa9481757391cdade90e6fdd37f4d28ee46"
    )


def test_dependency_cycle_rejection_rolls_back_revision_even_if_caller_catches(
    tmp_path: Path,
) -> None:
    path, store, _project, (a, b, c) = _world(tmp_path)
    with store.unit_of_work() as uow:
        uow.change_work_dependency(
            work_item_id=a.id, depends_on_work_id=b.id, expected_revision=1, action="added"
        )
        uow.change_work_dependency(
            work_item_id=b.id, depends_on_work_id=c.id, expected_revision=1, action="added"
        )
        with pytest.raises(ValidationFailed, match="cycle/scope/state"):
            uow.change_work_dependency(
                work_item_id=c.id, depends_on_work_id=a.id, expected_revision=1, action="added"
            )
        assert uow.get_work(c.id).revision == 1
        assert len(uow.list_work_events(c.id)) == 1
        assert uow.list_work_dependencies(c.id) == ()
        uow.commit()
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("delete from work_dependency_event")


def test_v7_to_v8_core_migration_preserves_all_old_rows(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    schema.bootstrap_v7(path)
    with SQLiteOperationalStore(path).unit_of_work() as uow:
        uow.create_project(slug="preserved", display_name="Preserved")
        uow.commit()
    with sqlite3.connect(path) as connection:
        logical = _logical_rows_digest(connection)
        original = schema._original_rows_digest(connection, 7)
        connection.execute("begin immediate")
        migration._migrate_connection(
            connection,
            source_logical_digest=logical,
            source_original_digest=original,
            source_version=7,
            target_version=8,
        )
        connection.commit()
        assert schema._original_rows_digest(connection, 7) == original
    result = schema.status(path)
    assert result.schema_version == 8 and result.schema_ok and result.integrity_ok


def test_dependency_revision_scope_removal_and_completion_guards(tmp_path: Path) -> None:
    _path, store, _project, (a, b, _c) = _world(tmp_path)
    with store.unit_of_work() as uow:
        foreign_project = uow.create_project(slug="foreign", display_name="Foreign")
        foreign = uow.create_work(
            project_id=foreign_project.id, kind="task", title="Foreign", state="ready"
        )
        with pytest.raises(ValidationFailed, match="self/cross-project"):
            uow.change_work_dependency(
                work_item_id=a.id,
                depends_on_work_id=foreign.id,
                expected_revision=1,
                action="added",
            )
        updated = uow.change_work_dependency(
            work_item_id=a.id, depends_on_work_id=b.id, expected_revision=1, action="added"
        )
        with pytest.raises(ValidationFailed, match="revision drift"):
            uow.change_work_dependency(
                work_item_id=a.id, depends_on_work_id=b.id, expected_revision=1, action="removed"
            )
        assert uow.list_work_dependencies(a.id)[0]["satisfied"] is False
        with pytest.raises(ValidationFailed):
            uow.transition_work(
                work_item_id=a.id,
                expected_revision=updated.revision,
                to_state="active",
                payload_digest=digest("p"),
                event_digest=digest("blocked-active"),
            )
        removed = uow.change_work_dependency(
            work_item_id=a.id,
            depends_on_work_id=b.id,
            expected_revision=updated.revision,
            action="removed",
        )
        assert uow.list_work_dependencies(a.id) == ()
        uow.change_work_dependency(
            work_item_id=a.id,
            depends_on_work_id=b.id,
            expected_revision=removed.revision,
            action="added",
        )
        _complete(uow, b.id)
        assert uow.list_work_dependencies(a.id)[0]["satisfied"] is True
        _complete(uow, a.id)
        uow.commit()


def test_dependency_waiting_job_does_not_starve_queue_and_claims_after_completion(
    tmp_path: Path,
) -> None:
    path, store, project, (a, b, _c) = _world(tmp_path)
    with store.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest({}), task_digest=digest("task"), sanitized_config={}
        )
        updated = uow.change_work_dependency(
            work_item_id=a.id, depends_on_work_id=b.id, expected_revision=1, action="added"
        )
        run = uow.create_run(
            work_item_id=a.id,
            config_revision_id=config.id,
            plan_digest=digest("plan"),
            budget={"seconds": 60},
        )
        uow.commit()
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    binding = LocalJobBinding(
        project.id, a.id, updated.revision, run.id, config.id, config.config_digest, run.plan_digest
    )
    waiting, _ = runtime.enqueue(
        idempotency_key="waiting",
        payload={"operation": "probe", "effect": {}},
        binding=binding,
        max_attempts=1,
    )
    other, _ = runtime.enqueue(
        idempotency_key="runnable", payload={"operation": "probe", "effect": {}}, max_attempts=1
    )
    kwargs = dict(
        owner_id="fixture",
        owner_pid=os.getpid(),
        owner_token=str(uuid4()),
        lease_seconds=30,
        supported_operations=("probe",),
    )
    with pytest.raises(ConcurrencyConflict):
        runtime.claim_next(**kwargs, job_id=waiting.id)
    selected = runtime.claim_next(**kwargs)
    assert selected is not None and selected.job.id == other.id
    with store.unit_of_work() as uow:
        _complete(uow, b.id)
        uow.commit()
    selected = runtime.claim_next(**kwargs, job_id=waiting.id)
    assert selected is not None and selected.job.id == waiting.id
