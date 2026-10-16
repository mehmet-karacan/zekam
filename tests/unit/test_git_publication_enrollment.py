"""Actual SQLite/CAS claims and private fixture files; no live producer calls."""

import json
import os
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.unit.test_git_publication_trust import trust_home  # noqa: F401
from zekam.application import git_publication_enrollment as subject
from zekam.application.git_publication_trust import (
    load_publication_trust,
    parse_publication_registry,
)
from zekam.application.local_runtime import LocalJobBinding, RESERVED_JOB_OPERATIONS
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import ConfigurationError, PolicyViolation
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


@pytest.fixture
def enrollment(trust_home):
    home, path, registry = trust_home
    path.unlink()  # This fixture owns the initial file; product never adopts it.
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\ngit_publication:\n  trust_digest: " + digest(registry) + "\n",
        encoding="utf-8",
    )
    settings = subject.load_settings(home=home)
    store = SQLiteOperationalStore(home / "state/operational.db")
    with store.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest(settings.sanitized()),
            task_digest=digest("task"),
            sanitized_config=settings.sanitized(),
        )
        project = uow.create_project(slug="demo", display_name="Demo")
        source = uow.bind_source(
            project_id=project.id, portable_ref="source:demo", source_kind="directory"
        )
        snapshot = uow.capture_source_snapshot(
            source_binding_id=source.id,
            revision_ref="revision-1",
            tree_digest=digest("tree"),
            content_digest=digest("content"),
            config_digest=config.config_digest,
        )
        work = uow.create_work(
            project_id=project.id, kind="task", title="Registry enrollment", state="ready"
        )
        uow.commit()
    plan = subject.build_trust_enrollment_plan(
        home, canonical_bytes(registry), project_id=project.id
    )
    with store.unit_of_work() as uow:
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            plan_digest=plan["plan_digest"],
            budget={"seconds": 120},
            source_snapshot_id=snapshot.id,
        )
        uow.commit()
    binding = LocalJobBinding(
        project.id,
        work.id,
        work.revision,
        run.id,
        config.id,
        config.config_digest,
        plan["plan_digest"],
        source_snapshot_id=snapshot.id,
    )
    return SimpleNamespace(
        home=home,
        path=path,
        registry=registry,
        plan=plan,
        binding=binding,
        runtime=SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True),
    )


def _enroll(f, validate=lambda: None):
    return subject.enroll_publication_trust(
        f.home, plan=f.plan, binding=f.binding, validate_inputs=validate
    )


def test_enrollment_exclusive_write_follows_actual_claim_and_has_receipt(enrollment, monkeypatch):
    f = enrollment
    original = os.open
    observed = []

    def claimed_open(path, *args, **kwargs):
        if path == f.path:
            connection = sqlite3.connect(f.home / "state/operational.db")
            try:
                count = connection.execute(
                    "select count(*) from local_effect_claim c join local_job j on j.id=c.job_id where c.operation=? and j.state='running'",
                    (subject.OPERATION,),
                ).fetchone()[0]
            finally:
                connection.close()
            assert count == 1
            observed.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(subject.os, "open", claimed_open)
    result = _enroll(f)
    assert len(observed) == 1
    assert f.path.read_bytes() == canonical_bytes(f.registry) + b"\n"
    assert load_publication_trust(f.home).trust_digest == f.plan["trust_digest"]
    snapshot = f.runtime.job_snapshot(result["canonical_job_id"])
    assert snapshot["state"] == "completed" and len(snapshot["effects"]) == 1
    assert snapshot["effects"][0]["receipt_status"] == "completed"
    assert result["signing_keys_created"] == 0 and result["publication_authorized"] is False
    assert result["producer_execution_verified"] is False and result["grants_authority"] is False
    replay = _enroll(f, validate=lambda: pytest.fail("completed replay must not readmit/write"))
    assert replay["canonical_replayed"] and len(observed) == 1


def test_missing_completed_registry_is_not_recreated(enrollment):
    f = enrollment
    _enroll(f)
    f.path.unlink()
    with pytest.raises(PolicyViolation, match="readback unavailable"):
        _enroll(f)
    assert not f.path.exists()


def test_admission_denial_and_foreign_work_never_write_registry(enrollment):
    f = enrollment

    def deny():
        raise PolicyViolation("admission denied")

    with pytest.raises(PolicyViolation, match="admission denied"):
        _enroll(f, validate=deny)
    assert not f.path.exists()
    with pytest.raises(PolicyViolation, match="binding drift"):
        subject.enroll_publication_trust(
            f.home,
            plan=f.plan,
            binding=replace(f.binding, project_id=str(uuid4())),
            validate_inputs=lambda: None,
        )
    assert not f.path.exists()


def test_existing_registry_is_not_adopted_even_if_exact(enrollment):
    f = enrollment
    f.path.write_bytes(canonical_bytes(f.registry) + b"\n")
    with pytest.raises(PolicyViolation, match="cannot adopt"):
        _enroll(f)
    assert f.path.read_bytes() == canonical_bytes(f.registry) + b"\n"


def test_config_or_registry_change_invalidates_plan_before_write(enrollment):
    f = enrollment
    (f.home / "config.yaml").write_text("schema: zekam-config/v1\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="current settings"):
        _enroll(f)
    assert not f.path.exists()


def test_registry_parser_rejects_duplicate_json_and_private_material(enrollment):
    registry = enrollment.registry
    duplicate = json.dumps(registry).replace('"revision": 1', '"revision": 2, "revision": 1')
    with pytest.raises(ConfigurationError):
        parse_publication_registry(duplicate.encode())
    registry["producers"][0]["private_key"] = "never-accepted"
    with pytest.raises(ConfigurationError):
        parse_publication_registry(canonical_bytes(registry))


def test_initial_enrollment_does_not_silently_rotate(enrollment):
    f = enrollment
    registry = f.registry | {"revision": 2}
    with pytest.raises(PolicyViolation, match="rotation is separate"):
        subject.build_trust_enrollment_plan(
            f.home, canonical_bytes(registry), project_id=f.binding.project_id
        )
    assert not f.path.exists()
    assert subject.OPERATION in RESERVED_JOB_OPERATIONS


def test_interrupted_write_does_not_get_adopted_by_a_second_attempt(enrollment, monkeypatch):
    f = enrollment
    original = subject._read_effect

    def interrupt(*_args):
        raise OSError("fixture interrupted after file write")

    monkeypatch.setattr(subject, "_read_effect", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        _enroll(f)
    before = f.path.read_bytes()
    monkeypatch.setattr(subject, "_read_effect", original)
    with pytest.raises(PolicyViolation):
        _enroll(f)
    assert f.path.read_bytes() == before


def test_pre_and_post_effect_admission_distinguish_own_completed_write(enrollment):
    f = enrollment
    observations = []
    result = _enroll(f, validate=lambda: observations.append(f.path.exists()))
    assert observations[0] is False and observations[-1] is True
    assert result["file_readback_verified"] is True
    assert f.runtime.job_snapshot(result["canonical_job_id"])["state"] == "completed"


def test_post_effect_file_drift_prevents_terminal_success(enrollment):
    f = enrollment

    def drift():
        if f.path.exists():
            with f.path.open("ab") as stream:
                stream.write(b" ")

    with pytest.raises(PolicyViolation, match="registry readback drift"):
        _enroll(f, validate=drift)
    with pytest.raises(PolicyViolation):
        _enroll(f)


def test_new_plan_cannot_reset_completed_registry_history_when_file_is_missing(enrollment):
    f = enrollment
    _enroll(f)
    f.path.unlink()
    with pytest.raises(PolicyViolation, match="already has canonical history"):
        subject.build_trust_enrollment_plan(
            f.home, canonical_bytes(f.registry), project_id=f.binding.project_id
        )
    assert not f.path.exists()
