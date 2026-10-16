"""Actual canonical SQLite/CAS chain over replay clients and scripted measurements."""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from tests.unit.test_canonical_job_runtime import world as world
from tests.unit.unit_test_loop_support import Harness
from zekam.application.canonical_job_runtime import run_canonical_job
from zekam.application.unit_test_agents import WorkBinding
from zekam.application.unit_test_outcome import terminal_document
from zekam.application.unit_test_runtime import (
    _ShortTransactionLedger,
    native_source_content_digest,
    unit_test_job_input_body,
)
from zekam.application.unit_test_verifier_source import read_verifier_source
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, NotFound
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


@pytest.fixture
def canonical_loop(world, tmp_path):
    h = Harness(tmp_path / "loop-fixture")
    observed = h.environment.observe()
    with world.store.unit_of_work() as uow:
        snapshot = uow.capture_source_snapshot(
            source_binding_id=world.source.id,
            revision_ref=observed.source_revision,
            tree_digest=digest("loop-tree"),
            content_digest=native_source_content_digest(observed),
            config_digest=world.binding.config_digest,
        )
        work = uow.create_work(
            project_id=world.binding.project_id, kind="task", title="Loop source", state="ready"
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=world.binding.config_revision_id,
            plan_digest=world.binding.plan_digest,
            budget={"seconds": 120},
            source_snapshot_id=snapshot.id,
        )
        uow.commit()
    binding = replace(
        world.binding,
        work_item_id=work.id,
        work_revision=work.revision,
        run_id=run.id,
        source_snapshot_id=snapshot.id,
    )
    h.connection.close()
    h.ledger = _ShortTransactionLedger(world.store)
    h.objects = LocalContentAddressedStore(world.home / "artifacts/sha256")
    h.request = replace(
        h.request,
        project_id=binding.project_id,
        source_binding_id=world.source.id,
        source_revision=observed.source_revision,
        work_item_id=binding.work_item_id,
        run_id=binding.run_id,
        source_snapshot_id=binding.source_snapshot_id,
    )
    h.work = WorkBinding(uuid4(), UUID(binding.project_id), UUID(binding.work_item_id), uuid4())
    for adapter in h.adapters.values():
        adapter.objects = h.objects

    def registrar(key, size, media):
        with world.store.unit_of_work() as uow:
            uow.register_artifact(
                artifact_digest=key,
                size_bytes=size,
                media_type=media,
                classification="local-private",
            )
            uow.commit()

    h.gateway = replace(h._make_gateway(), artifact_registrar=registrar)
    runtime_binding = SimpleNamespace(
        approved_maven_plan_digest=digest("maven-plan"),
        realm_id=h.work.realm_id,
        coordinator_assignment_id=h.work.coordinator_assignment_id,
        model_id=None,
        verifier_model_id=None,
        provider_profile=None,
        remote_model=False,
        plan_id=None,
        step_id=None,
    )
    inputs = unit_test_job_input_body(h.request, runtime_binding, h.approvals, h.limits)

    def execute(_cancel):
        service = replace(h.service(), artifact_registrar=registrar)
        outcome = service.run(h.request, approvals=h.approvals, limits=h.limits)
        return {
            "schema": "zekam-unit-test-run/v1",
            "execution_input": inputs,
            "request_digest": outcome.request_digest,
            "terminal": terminal_document(outcome.terminal),
            "attempts": outcome.attempts,
            "executed_attempts": outcome.executed_attempts,
            "detail": list(outcome.detail),
            "provider_calls": 0,
            "provider_calls_state": "local-only",
            "exit_code": 0,
        }

    result = run_canonical_job(
        world.home,
        binding=binding,
        operation="test.run",
        input_digest=digest(inputs),
        execute=execute,
        validate_inputs=lambda: None,
        execution_key="loop-fixture",
    )
    return SimpleNamespace(world=world, harness=h, result=result, binding=binding)


def test_verifier_source_reads_actual_canonical_terminal_without_upgrading_replay(canonical_loop):
    w = canonical_loop
    report = read_verifier_source(w.world.home, w.result["canonical_job_id"])
    assert report["canonical_terminal_verified"] is True
    assert report["quality_review_recorded"] is True
    assert report["independent_execution_observed"] is False
    assert report["independent_quality_verified"] is False
    assert report["current_source_verified"] is False
    assert report["publication_authorized"] is False
    assert report["provenance"]["model_independence"] == "unproven"
    assert report["parser_report_facts"]["state"] == "not-captured"
    snapshot = w.world.runtime.job_snapshot(w.result["canonical_job_id"])
    assert read_verifier_source(w.world.home, snapshot["idempotency_key"]) == report


@pytest.mark.parametrize(
    "field",
    ["measurement_record_digest", "final_verifier_execution_digest", "terminal_evidence_digest"],
)
def test_verifier_source_missing_exact_cas_never_creates_new_execution(canonical_loop, field):
    w = canonical_loop
    report = read_verifier_source(w.world.home, w.result["canonical_job_id"])
    calls = sum(adapter.calls for adapter in w.harness.adapters.values())
    w.harness.objects._paths(report[field])[0].unlink()
    with pytest.raises((NotFound, OSError)):
        read_verifier_source(w.world.home, w.result["canonical_job_id"])
    assert sum(adapter.calls for adapter in w.harness.adapters.values()) == calls


def test_loop_measurement_record_and_binding_are_registered(canonical_loop):
    w = canonical_loop
    report = read_verifier_source(w.world.home, w.result["canonical_job_id"])
    record = json.loads(w.harness.objects.get(report["measurement_record_digest"]))
    from zekam.infrastructure.sqlite.operational_schema import _connect

    connection = _connect(w.world.database, read_only=True)
    try:
        for key in (
            report["measurement_record_digest"],
            record["binding_digest"],
            report["terminal_evidence_digest"],
        ):
            row = connection.execute(
                "select size_bytes,classification from artifact_ref where digest=?", (key,)
            ).fetchone()
            assert row["size_bytes"] == w.harness.objects.stat(key).size_bytes
            assert row["classification"] == "local-private"
    finally:
        connection.close()


def test_verifier_source_rejects_non_run_completed_job(world):
    result = run_canonical_job(
        world.home,
        binding=world.binding,
        operation="project.index",
        input_digest=digest("input"),
        execute=lambda _cancel: {"passed": True},
        validate_inputs=lambda: None,
    )
    with pytest.raises(PolicyViolation, match="test.run"):
        read_verifier_source(world.home, result["canonical_job_id"])


def test_verifier_source_does_not_adopt_legacy_passed_json_as_terminal_proof(world):
    result = run_canonical_job(
        world.home,
        binding=world.binding,
        operation="test.run",
        input_digest=digest("legacy-input"),
        execute=lambda _cancel: {"schema": "zekam-unit-test-run/v1", "passed": True},
        validate_inputs=lambda: None,
    )
    with pytest.raises(PolicyViolation, match="execution input missing"):
        read_verifier_source(world.home, result["canonical_job_id"])
