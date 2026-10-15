"""Canonical job binding regression cases; deferred to the final test stage."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.canonical import digest
from zekam.domain.errors import ConcurrencyConflict, ConfigurationError, ValidationFailed
from zekam.infrastructure.sqlite import operational_migration as migration
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_backup import _logical_rows_digest
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

NOW = "2026-10-15T12:00:00+00:00"


def _world(tmp_path: Path):
    path = (tmp_path / "operational.db").resolve()
    schema.bootstrap_v7(path)
    store = SQLiteOperationalStore(path)
    with store.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest({"network": "deny"}),
            task_digest=digest("task"),
            sanitized_config={"network": "deny"},
        )
        project = uow.create_project(slug="bound", display_name="Bound")
        source = uow.bind_source(
            project_id=project.id, portable_ref="source:bound", source_kind="directory"
        )
        snapshot = uow.capture_source_snapshot(
            source_binding_id=source.id,
            revision_ref="revision-1",
            tree_digest=digest("tree"),
            content_digest=digest("content"),
            config_digest=digest("source-specific-profile"),
        )
        work = uow.create_work(
            project_id=project.id, kind="task", title="Bound Work", state="ready"
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            source_snapshot_id=snapshot.id,
            plan_digest=digest("plan"),
            budget={"seconds": 60},
        )
        step = uow.add_run_step(run_id=run.id, step_key="index", input_digest=digest("inputs"))
        uow.commit()
    binding = LocalJobBinding(
        project.id,
        work.id,
        work.revision,
        run.id,
        config.id,
        config.config_digest,
        run.plan_digest,
        step.id,
        snapshot.id,
    )
    return path, store, SQLiteLocalRuntimeStore(path), binding


def _claim(runtime: SQLiteLocalRuntimeStore, job_id: str | None):
    return runtime.claim_next(
        owner_id="binding-worker",
        owner_pid=os.getpid(),
        owner_token="binding-owner",
        lease_seconds=60,
        supported_operations=("research.run",),
        job_id=job_id,
        now=NOW,
    )


def test_atomic_bound_enqueue_exact_replay_and_marker_ownership(tmp_path: Path) -> None:
    path, _store, runtime, binding = _world(tmp_path)
    payload = {"operation": "research.run"}
    first, created = runtime.enqueue(
        idempotency_key="bound", payload=payload, binding=binding, available_at=NOW
    )
    replay, repeated = runtime.enqueue(
        idempotency_key="bound", payload=payload, binding=binding, available_at=NOW
    )
    assert created and not repeated and replay == first
    assert runtime.job_binding(first.id) == binding
    assert runtime.job_binding(first.id).body()["grants_authority"] is False
    with pytest.raises(ValidationFailed, match="runtime-owned"):
        runtime.enqueue(
            idempotency_key="forged", payload={"work_binding_digest": binding.binding_digest}
        )
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from local_job_binding").fetchone()[0] == 1
        assert db.execute("select count(*) from local_outbox").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("update local_job set payload_json='{}' where id=?", (first.id,))


@pytest.mark.parametrize(
    "field", ["project_id", "run_id", "source_snapshot_id", "plan_digest", "work_revision"]
)
def test_mismatched_tuple_rolls_back_job_and_outbox(tmp_path: Path, field: str) -> None:
    path, _store, runtime, binding = _world(tmp_path)
    invalid = replace(
        binding,
        **{
            field: binding.work_revision + 1
            if field == "work_revision"
            else digest("other")
            if field == "plan_digest"
            else str(uuid4())
        },
    )
    with pytest.raises(ValidationFailed, match="canonical binding rejected"):
        runtime.enqueue(
            idempotency_key="bad", payload={"operation": "research.run"}, binding=invalid
        )
    with sqlite3.connect(path) as db:
        for table in ("local_job", "local_outbox", "local_job_binding"):
            assert db.execute(f"select count(*) from {table}").fetchone()[0] == 0


@pytest.mark.parametrize("after_lease", [False, True])
def test_config_drift_blocks_lease_or_effect_claim(tmp_path: Path, after_lease: bool) -> None:
    path, store, runtime, binding = _world(tmp_path)
    job, _ = runtime.enqueue(
        idempotency_key="current",
        payload={"operation": "research.run"},
        binding=binding,
        available_at=NOW,
    )
    claimed = _claim(runtime, job.id) if after_lease else None
    with store.unit_of_work() as uow:
        uow.activate_config(
            config_digest=digest({"network": "changed"}),
            task_digest=digest("new-task"),
            sanitized_config={"network": "changed"},
        )
        uow.commit()
    with pytest.raises(ConcurrencyConflict, match="authority changed"):
        if after_lease:
            runtime.claim_effect(
                claimed,
                operation="research.run",
                effect_digest=digest("effect"),
                idempotency_key="effect",
                now=NOW,
            )
        else:
            _claim(runtime, job.id)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from local_effect_claim").fetchone()[0] == 0
        if after_lease:
            with pytest.raises(sqlite3.IntegrityError, match="current authority"):
                db.execute(
                    "insert into local_effect_claim values(?,?,?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        job.id,
                        claimed.lease.id,
                        claimed.lease.fencing_token,
                        "research.run",
                        digest("effect"),
                        "bypass",
                        NOW,
                    ),
                )


def test_unmigrated_store_rejects_bound_admission_without_implicit_upgrade(tmp_path: Path) -> None:
    _path, _store, _runtime, binding = _world(tmp_path)
    old = tmp_path / "old.db"
    runtime = SQLiteLocalRuntimeStore(old)
    with pytest.raises(ConfigurationError, match="schema v7"):
        runtime.enqueue(
            idempotency_key="bound", payload={"operation": "research.run"}, binding=binding
        )
    assert schema.status(old).schema_version == 3
    assert runtime.status().ready_jobs == 0


def test_v7_migration_preserves_v6_rows_and_rolls_back_interrupted_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v6.db"
    schema.bootstrap_v6(path)
    with sqlite3.connect(path) as db:
        logical = _logical_rows_digest(db)
        original = schema._original_rows_digest(db, 6)
        apply = schema._apply_migration

        def crash(connection, version):
            apply(connection, version)
            raise OSError("interrupted v7 DDL")

        monkeypatch.setattr(schema, "_apply_migration", crash)
        db.execute("begin immediate")
        with pytest.raises(OSError, match="interrupted v7"):
            migration._migrate_connection(
                db,
                source_logical_digest=logical,
                source_original_digest=original,
                source_version=6,
                target_version=7,
            )
        db.rollback()
        assert schema._validate_connection(db) == 6
        monkeypatch.setattr(schema, "_apply_migration", apply)
        db.execute("begin immediate")
        migration._migrate_connection(
            db,
            source_logical_digest=logical,
            source_original_digest=original,
            source_version=6,
            target_version=7,
        )
        db.commit()
        assert schema._original_rows_digest(db, 6) == original
        assert schema._validate_connection(db) == 7


def test_stale_ready_binding_does_not_starve_unbound_jobs(tmp_path: Path) -> None:
    path, store, runtime, binding = _world(tmp_path)
    stale, _ = runtime.enqueue(
        idempotency_key="stale",
        payload={"operation": "research.run"},
        binding=binding,
        available_at=NOW,
    )
    with store.unit_of_work() as uow:
        uow.activate_config(
            config_digest=digest("changed"),
            task_digest=digest("changed-task"),
            sanitized_config={"network": "deny"},
        )
        uow.commit()
    valid, _ = runtime.enqueue(
        idempotency_key="valid",
        payload={"operation": "research.run"},
        available_at=NOW,
    )
    claimed = _claim(runtime, None)
    assert claimed is not None and claimed.job.id == valid.id
    with sqlite3.connect(path) as db:
        assert (
            db.execute("select state from local_job where id=?", (stale.id,)).fetchone()[0]
            == "ready"
        )
        assert (
            db.execute("select count(*) from local_lease where job_id=?", (stale.id,)).fetchone()[0]
            == 0
        )


def test_pending_dependency_blocks_admission_until_predecessor_succeeds(tmp_path: Path) -> None:
    path, store, runtime, binding = _world(tmp_path)
    with store.unit_of_work() as uow:
        dependent = uow.add_run_step(
            run_id=binding.run_id,
            step_key="dependent",
            input_digest=digest("dependent"),
            dependencies=(binding.run_step_id,),
        )
        uow.commit()
    job, _ = runtime.enqueue(
        idempotency_key="dependent",
        payload={"operation": "research.run"},
        binding=replace(binding, run_step_id=dependent.id),
        available_at=NOW,
    )
    with pytest.raises(ConcurrencyConflict, match="authority changed"):
        _claim(runtime, job.id)
    assert _claim(runtime, None) is None
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from local_lease").fetchone()[0] == 0
    with store.unit_of_work() as uow:
        for before, after in (("pending", "ready"), ("ready", "running"), ("running", "succeeded")):
            uow.transition_run_step(
                step_id=binding.run_step_id,
                expected_status=before,
                to_status=after,
                evidence_digest=digest("predecessor-result"),
            )
        uow.commit()
    claimed = _claim(runtime, None)
    assert claimed is not None and claimed.job.id == job.id
