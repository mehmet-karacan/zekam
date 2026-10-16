"""Canonical index/test integration on a real operational SQLite/CAS fixture."""

from types import SimpleNamespace

import pytest

from zekam.application import canonical_job_runtime as subject
from zekam.application import project_rag_runtime as rag
from zekam.application import unit_test_runtime as measurement
from zekam.application.home import HomeLayout
from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.canonical import digest
from zekam.domain.errors import NotFound, PolicyViolation
from zekam.domain.unit_test_engineering import UnitTestBudget, UnitTestRequest
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap_v7
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


def _native_source_fixture(
    world, *, failed=False, execution_key="nondefault-measurement", with_report_facts=False
):
    """Synthetic measurer; real native ledger, canonical checkpoint and CAS writes."""
    from zekam.application.unit_test_loop_state import EnvironmentObservation, MeasuredRun
    from zekam.application.unit_test_measurement import (
        MeasurementBinding,
        MeasurementScope,
        MeasurementEvidence,
        RunStatus,
        TestRunKind,
        ToolchainIdentity,
        FreshnessVerdict,
        assemble_measurement,
    )
    from zekam.domain.unit_test_engineering import CoverageMetric, CoverageObservation

    request = measurement.native_request(
        UnitTestRequest.with_defaults(
            project_id=world.binding.project_id,
            source_binding_id=world.source.id,
            source_revision=world.observed.source_revision,
            source_files=("src/main/A.java",),
            percent="80",
            budget=UnitTestBudget(3, 60, 180),
            work_item_id=world.binding.work_item_id,
            run_id=world.binding.run_id,
            source_snapshot_id=world.binding.source_snapshot_id,
        )
    )
    observed = EnvironmentObservation(
        world.observed.source_revision,
        world.observed.production_digests,
        world.observed.build_config_digest,
        digest("launcher"),
    )

    def measure(request, *, attempt_id, run_id, candidate_digest, **_kwargs):
        from zekam.domain.canonical import digest_of_bytes

        report = (
            '<testsuite tests="1" failures="' + str(int(failed)) + '" errors="0" skipped="0">'
            '<testcase classname="p.ATest" name="fixture">'
            + ("<failure/>" if failed else "")
            + "</testcase></testsuite>"
        ).encode()
        bound = MeasurementBinding(
            request.request_digest,
            attempt_id,
            run_id,
            MeasurementScope.UNIT,
            request.source_revision,
            tuple(observed.production_digests.items()),
            (("A.class", digest("class")),),
            candidate_digest,
            observed.build_config_digest,
            world.binding.plan_digest,
            ToolchainIdentity("mvn", observed.launcher_digest, "3.9", "21", "21", ()),
            (("surefire:TEST-A.xml", digest_of_bytes(report)),),
        )
        evidence = MeasurementEvidence(
            RunStatus.COMPLETED,
            1 if failed else 0,
            TestRunKind.FAILED if failed else TestRunKind.PASSED,
            (
                ("discovered", 1),
                ("executed", 1),
                ("passed", 0 if failed else 1),
                ("failed", int(failed)),
                ("errors", 0),
                ("skipped", 0),
                ("aborted", 0),
            ),
            (
                CoverageObservation.measured(
                    "src/main/A.java", CoverageMetric.LINE, covered=9, missed=1
                ),
            ),
        )
        record = assemble_measurement(request, bound, evidence, freshness=FreshnessVerdict.FRESH)
        facts = None
        if with_report_facts:
            from zekam.infrastructure.unit_test_runner.report_facts import capture_report_facts

            outputs = SimpleNamespace(
                surefire={
                    "": (
                        SimpleNamespace(
                            relative="TEST-A.xml", digest=digest_of_bytes(report), data=report
                        ),
                    )
                },
                jacoco_xml={},
                exec_digests={},
            )
            facts = capture_report_facts(outputs, ("",), record)
        return MeasuredRun(
            record=record,
            report_facts=facts,
            report_facts_state="captured-parser-projection" if facts else "not-captured",
        )

    objects = LocalContentAddressedStore(world.home / "artifacts/sha256")
    ports = measurement.NativeMeasurementPorts(
        ledger=measurement._ShortTransactionLedger(world.store),
        objects=objects,
        measurer=SimpleNamespace(measure=measure),
        environment=SimpleNamespace(observe=lambda: observed),
        test_tree=lambda: {"src/test/ATest.java": digest("test")},
        plan_digest=world.binding.plan_digest,
    )
    return measurement.measure_native_job(
        request,
        ports,
        home=world.home,
        work_binding=world.binding,
        execution_key=execution_key,
    )


@pytest.mark.parametrize("failed", [False, True])
def test_native_source_reader_receipt_chain_does_not_claim_quality_or_authority(world, failed):
    from zekam.application.unit_test_source_evidence import read_native_measurement_source

    result = _native_source_fixture(world, failed=failed)
    report = read_native_measurement_source(world.home, result["canonical_job_id"])
    assert report["canonical_receipt_verified"] and report["attempt_receipt_verified"]
    assert report["attempt_state"] == ("failed" if failed else "completed")
    assert report["outcome"] == ("tests-failed" if failed else "target-met")
    assert report["build_exit_code"] == int(failed)
    assert report["execution_id"] == world.binding.run_id
    for field in (
        "current_source_verified",
        "raw_reports_verified",
        "independent_quality_verified",
        "publication_authorized",
        "grants_authority",
    ):
        assert report[field] is False
    snapshot = world.runtime.job_snapshot(result["canonical_job_id"])
    assert read_native_measurement_source(world.home, snapshot["idempotency_key"]) == report


def test_native_source_reader_rejects_unrelated_completed_job(world):
    from zekam.application.unit_test_source_evidence import read_native_measurement_source

    result = run(world, lambda _cancel: {"passed": True})
    with pytest.raises(PolicyViolation, match="test.measure"):
        read_native_measurement_source(world.home, result["canonical_job_id"])


@pytest.mark.parametrize("failed", [False, True])
def test_native_source_reader_loads_receipt_bound_report_facts(world, failed):
    from zekam.application.unit_test_source_evidence import read_native_measurement_source

    result = _native_source_fixture(world, failed=failed, with_report_facts=True)
    report = read_native_measurement_source(world.home, result["canonical_job_id"])
    facts = report["parser_report_facts"]
    assert facts["state"] == "verified-parser-projection"
    assert facts["combined"]["status"] == ("failed" if failed else "passed")
    assert facts["combined"]["counts"]["executed"] == 1
    assert facts["report_count"] == 1
    assert report["raw_reports_verified"] is False
    assert report["independent_quality_verified"] is False


def test_native_source_reader_missing_report_projection_is_not_silently_legacy(world):
    import json
    from zekam.application.unit_test_source_evidence import read_native_measurement_source

    result = _native_source_fixture(world, with_report_facts=True)
    objects = LocalContentAddressedStore(world.home / "artifacts/sha256")
    envelope = json.loads(objects.get(result["evidence_digest"]))
    key = envelope["evidence"]["report_facts_digest"]
    objects._paths(key)[0].unlink()
    with pytest.raises((OSError, NotFound)):
        read_native_measurement_source(world.home, result["canonical_job_id"])


@pytest.mark.parametrize("failed,with_report_facts", [(True, True), (False, False)])
def test_publication_test_source_rejects_failed_or_legacy_measurement_before_source_access(
    world, monkeypatch, failed, with_report_facts
):
    from uuid import uuid4
    from zekam.application import git_publication_test_source as publication
    from zekam.application.git_publication import GitPublicationPlan, GitPublicationTarget

    result = _native_source_fixture(world, failed=failed, with_report_facts=with_report_facts)
    plan = GitPublicationPlan(
        uuid4(),
        uuid4(),
        world.binding,
        GitPublicationTarget(
            digest("root"), "origin", digest("remote"), "refs/heads/main", "a" * 40
        ),
        digest("pending-test"),
        digest("pending-review"),
    )
    monkeypatch.setattr(
        publication,
        "resolve_project_source",
        lambda *_args: pytest.fail("Unqualified measurement must not access project source"),
    )
    with pytest.raises(PolicyViolation, match="successful exact source scope"):
        publication.read_publication_test_source(world.home, result["canonical_job_id"], plan=plan)


def test_native_source_reader_rejects_missing_attempt_cas(world):
    from zekam.application.unit_test_source_evidence import read_native_measurement_source

    result = _native_source_fixture(world)
    objects = LocalContentAddressedStore(world.home / "artifacts/sha256")
    objects._paths(result["evidence_digest"])[0].unlink()
    with pytest.raises((OSError, NotFound)):
        read_native_measurement_source(world.home, result["canonical_job_id"])


def test_native_source_reader_rejects_result_to_attempt_drift(world):
    from zekam.application.unit_test_source_evidence import _linked_measurement

    result = _native_source_fixture(world)
    objects = LocalContentAddressedStore(world.home / "artifacts/sha256")
    import json

    with world.store.unit_of_work() as uow:
        ledger = uow.unit_test_ledger()
        attempt = ledger.get_attempt(result["attempt_id"])
        request = ledger.get_request(result["request_digest"])
        receipt = ledger.get_attempt_receipt(attempt.attempt_id)
        observations = ledger.list_observations(attempt.attempt_id)
    envelope = json.loads(objects.get(receipt.evidence_digest))
    candidate = json.loads(objects.get(attempt.candidate_digest))
    for field, bad in (
        ("ordinal", True),
        ("build_exit_code", 1),
        ("freshness", "stale"),
        ("test_counts", {"passed": 999}),
    ):
        with pytest.raises(PolicyViolation):
            _linked_measurement(
                result | {field: bad},
                envelope,
                candidate,
                request=request,
                attempt=attempt,
                receipt=receipt,
                observations=observations,
            )


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b"[]"])
def test_native_source_reader_requires_strict_object_json(world, raw):
    from zekam.application.unit_test_source_evidence import _object

    objects = LocalContentAddressedStore(world.home / "artifacts/sha256")
    key = objects.put(raw, media_type="application/json").digest
    with pytest.raises(PolicyViolation):
        _object(objects, key)


def test_native_source_reader_enforces_object_read_budget_before_get(world, monkeypatch):
    from zekam.application import unit_test_source_evidence as source

    result = _native_source_fixture(world)
    monkeypatch.setattr(source, "MAX_BYTES", 1)
    with pytest.raises(PolicyViolation, match="read budget"):
        source.read_native_measurement_source(world.home, result["canonical_job_id"])


@pytest.fixture
def world(tmp_path):
    home = HomeLayout(tmp_path / "home").ensure().root
    database = home / "state" / "operational.db"
    bootstrap_v7(database)
    runtime = SQLiteLocalRuntimeStore(database)
    store = SQLiteOperationalStore(database)
    observed = SimpleNamespace(
        source_revision="revision-1",
        production_digests={"src/main/A.java": digest("class A {}")},
        build_config_digest=digest("pom"),
    )
    with store.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest({}),
            task_digest=digest("task"),
            sanitized_config={},
        )
        project = uow.create_project(slug="demo", display_name="Demo")
        source = uow.bind_source(
            project_id=project.id, portable_ref="source:demo", source_kind="directory"
        )
        snapshot = uow.capture_source_snapshot(
            source_binding_id=source.id,
            revision_ref=observed.source_revision,
            tree_digest=digest("tree"),
            content_digest=measurement.native_source_content_digest(observed),
            config_digest=config.config_digest,
        )
        work = uow.create_work(project_id=project.id, kind="task", title="Canonical", state="ready")
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            plan_digest=digest("plan"),
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
        run.plan_digest,
        source_snapshot_id=snapshot.id,
    )
    return SimpleNamespace(
        home=home,
        database=database,
        store=store,
        runtime=runtime,
        binding=binding,
        source=source,
        observed=observed,
    )


def run(world, execute, operation="project.index", validate=lambda: None):
    return subject.run_canonical_job(
        world.home,
        binding=world.binding,
        operation=operation,
        input_digest=digest("input"),
        execute=execute,
        validate_inputs=validate,
    )


def test_real_claim_checkpoint_receipt_and_exact_cas_replay_without_write_lock(world):
    calls = []

    def execute(cancel):
        calls.append(True)
        with world.store.unit_of_work() as uow:
            uow.create_project(slug="independent-writer", display_name="Writer")
            uow.commit()
        return {"status": "observed", "receipt_ref": digest("fixture")}

    first = run(world, execute)
    replay = run(world, execute)
    assert len(calls) == 1 and replay["canonical_replayed"] is True
    assert first["canonical_job_id"] == replay["canonical_job_id"]
    snapshot = world.runtime.job_snapshot(first["canonical_job_id"])
    assert snapshot["state"] == "completed" and snapshot["attempt_count"] == 1
    assert len(snapshot["effects"]) == 1
    assert snapshot["effects"][0]["receipt_status"] == "completed"
    with world.store.unit_of_work() as uow:
        checkpoints = uow.list_checkpoints(world.binding.run_id)
        assert len(checkpoints) == 1
        uow.commit()


def test_unknown_effect_is_not_redispatched(world):
    calls = []

    def interrupted(cancel):
        calls.append(True)
        raise RuntimeError("fixture interruption after possible effect")

    with pytest.raises(RuntimeError):
        run(world, interrupted)
    with pytest.raises(PolicyViolation, match="recovery required"):
        run(world, interrupted)
    assert len(calls) == 1
    assert world.runtime.status().recovery_jobs == 1
    assert len(world.runtime.recovery_cases()) == 1


def test_generic_consumer_cannot_take_index_or_measurement_jobs(world):
    for operation in (
        "project.index",
        "test.measure",
        "skill.trial",
        "skill.audit.verify",
        "skill.audit.review",
        "skill.evaluation.aggregate",
        "skill.evaluation.native-project",
        "skill.invocation.native-record",
        "skill.native.execute",
        "skill.invocation.from-execution",
        "skill.outcome.native-verify",
        "skill.outcome.native-record",
    ):
        world.runtime.enqueue(
            idempotency_key=operation, payload={"operation": operation}, binding=world.binding
        )
    import os

    assert (
        world.runtime.claim_next(
            owner_id="generic",
            owner_pid=os.getpid(),
            owner_token="fixture",
            lease_seconds=60,
        )
        is None
    )


def test_completed_replay_survives_config_change_and_rejects_missing_cas(world):
    first = run(world, lambda cancel: {"observed": True})
    with world.store.unit_of_work() as uow:
        uow.activate_config(
            config_digest=digest({"revision": 2}),
            task_digest=digest("new-task"),
            sanitized_config={"revision": 2},
        )
        uow.commit()
    assert run(world, lambda cancel: pytest.fail("no redispatch"))["canonical_replayed"] is True
    snapshot = world.runtime.job_snapshot(first["canonical_job_id"])
    objects = LocalContentAddressedStore(world.home / "artifacts" / "sha256")
    objects._paths(snapshot["terminal_evidence_digest"])[0].unlink()
    with pytest.raises(NotFound):
        run(world, lambda cancel: pytest.fail("missing CAS must not cause redispatch"))


def test_native_measurement_pins_production_bytes_before_dispatch(world, monkeypatch):
    request = UnitTestRequest.with_defaults(
        project_id=world.binding.project_id,
        source_binding_id=world.source.id,
        source_revision=world.observed.source_revision,
        source_files=("src/main/A.java",),
        percent="80",
        budget=UnitTestBudget(3, 60, 180),
        work_item_id=world.binding.work_item_id,
        run_id=world.binding.run_id,
        source_snapshot_id=world.binding.source_snapshot_id,
    )
    ports = SimpleNamespace(
        plan_digest=world.binding.plan_digest,
        environment=SimpleNamespace(observe=lambda: world.observed),
    )
    calls = []

    def measure(request, ports, *, cancel):
        calls.append(True)
        return SimpleNamespace(
            to_document=lambda: {"outcome": "fixture-observation", "exit_code": 0}
        )

    monkeypatch.setattr(measurement, "measure_native", measure)
    world.observed.production_digests = {"src/main/A.java": digest("modified without commit")}
    with pytest.raises(PolicyViolation, match="source snapshot drift"):
        measurement.measure_native_job(request, ports, home=world.home, work_binding=world.binding)
    assert not calls and world.runtime.status().ready_jobs == 0
    world.observed.production_digests = {"src/main/A.java": digest("class A {}")}
    result = measurement.measure_native_job(
        request, ports, home=world.home, work_binding=world.binding
    )
    assert result["canonical_job_id"] and len(calls) == 1
    measurement.measure_native_job(request, ports, home=world.home, work_binding=world.binding)
    assert len(calls) == 1


def test_index_entry_uses_canonical_receipt_and_replays_without_source_access(world, monkeypatch):
    monkeypatch.setattr(rag, "_registered_project", lambda *a: world.binding.project_id)
    monkeypatch.setattr(
        rag,
        "build_index_job_plan",
        lambda *a, **k: {
            "plan_digest": world.binding.plan_digest,
            "source_revision": world.observed.source_revision,
            "tree_digest": digest("tree"),
        },
    )
    monkeypatch.setattr(rag, "resolve_project_source", lambda *a: world.home)
    from zekam.application.config import EmbeddingRoute

    monkeypatch.setattr(rag, "project_embedding_route", lambda *a: EmbeddingRoute.LOCAL)
    calls = []

    def index(*args, **kwargs):
        calls.append(True)
        return {"generation_digest": digest("generation"), "chunk_count": 1}

    monkeypatch.setattr(rag, "_index", index)
    result = rag.index_registered_project(world.home, "demo", work_binding=world.binding)
    assert result["canonical_job_id"] and len(calls) == 1
    monkeypatch.setattr(
        rag, "build_index_job_plan", lambda *a, **k: pytest.fail("historical replay")
    )
    replay = rag.index_registered_project(world.home, "demo", work_binding=world.binding)
    assert replay["canonical_replayed"] is True and len(calls) == 1


def test_lease_keeper_stays_active_during_post_effect_source_validation(world, monkeypatch):
    import threading

    renewed = threading.Event()
    heartbeat = SQLiteLocalRuntimeStore.heartbeat

    def track(self, *args, **kwargs):
        result = heartbeat(self, *args, **kwargs)
        renewed.set()
        return result

    monkeypatch.setattr(subject, "_HEARTBEAT_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(SQLiteLocalRuntimeStore, "heartbeat", track)
    validations = []

    def validate():
        validations.append(True)
        if len(validations) > 1:
            assert renewed.wait(5), "Lease must renew while post-effect validation is running"

    def execute(cancel):
        renewed.clear()
        return {"observed": True}

    result = run(world, execute, validate=validate)
    assert result["canonical_replayed"] is False


def test_index_cli_dry_run_and_exact_binding_forwarding(world, monkeypatch):
    import json

    from typer.testing import CliRunner

    from zekam.application.config import EmbeddingRoute
    from zekam.interfaces.cli import project as cli

    plan = {
        "plan_digest": world.binding.plan_digest,
        "project_id": world.binding.project_id,
        "source_revision": world.observed.source_revision,
        "tree_digest": digest("tree"),
    }
    monkeypatch.setattr(cli, "build_index_job_plan", lambda *a, **k: dict(plan))
    monkeypatch.setattr(cli, "project_embedding_route", lambda *a: EmbeddingRoute.LOCAL)
    calls = []

    def index(*args, **kwargs):
        calls.append(kwargs)
        return {"generation_digest": digest("generation"), "chunk_count": 1}

    monkeypatch.setattr(cli, "index_registered_project", index)
    args = [
        "index",
        "demo",
        "--home",
        str(world.home),
        "--work-run",
        world.binding.run_id,
        "--work-revision",
        "1",
        "--json",
    ]
    runner = CliRunner()
    dry = runner.invoke(cli.app, [*args, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    observed = json.loads(dry.output)["work_binding"]
    assert observed["binding_digest"] == world.binding.binding_digest and not calls
    denied = runner.invoke(cli.app, args)
    assert denied.exit_code != 0 and not calls
    applied = runner.invoke(
        cli.app,
        [
            *args,
            "--plan-digest",
            world.binding.plan_digest,
            "--work-binding-digest",
            world.binding.binding_digest,
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert len(calls) == 1 and calls[0]["work_binding"] == world.binding


def test_index_plan_is_provider_free_and_changes_with_source_bytes(world, monkeypatch):
    from zekam.application.config import EmbeddingRoute, KnowledgeSettings

    root = world.home.parent / "source"
    root.mkdir()
    source = root / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(rag, "resolve_project_source", lambda *a: root)
    monkeypatch.setattr(rag, "_registered_project", lambda *a: world.binding.project_id)
    monkeypatch.setattr(rag, "load_smart_binding", lambda *a: None)
    monkeypatch.setattr(
        rag,
        "load_settings",
        lambda **k: SimpleNamespace(
            knowledge=KnowledgeSettings(embedding_route=EmbeddingRoute.LOCAL),
        ),
    )
    monkeypatch.setattr(rag, "_provider", lambda *a, **k: pytest.fail("plan must be provider-free"))
    before = rag.build_index_job_plan(world.home, "demo")
    source.write_text("value = 2\n", encoding="utf-8")
    after = rag.build_index_job_plan(world.home, "demo")
    assert before["plan_digest"] != after["plan_digest"]
    assert before["tree_digest"] != after["tree_digest"]
    assert before["provider_calls"] == after["provider_calls"] == 0
    assert before["grants_authority"] is False


def test_explicit_batch_loop_uses_short_ledger_and_canonical_receipt(world, monkeypatch):
    import datetime as dt
    from uuid import UUID, uuid4

    from zekam.application.unit_test_plateau import LoopLimits, UnitTestApprovals
    from zekam.domain.unit_test_engineering import UnitTestStopReason, UnitTestTerminal

    request = UnitTestRequest.with_defaults(
        project_id=world.binding.project_id,
        source_binding_id=world.source.id,
        source_revision=world.observed.source_revision,
        source_files=("src/main/A.java",),
        percent="80",
        budget=UnitTestBudget(3, 60, 180),
        work_item_id=world.binding.work_item_id,
        run_id=world.binding.run_id,
        source_snapshot_id=world.binding.source_snapshot_id,
    )
    executable = world.home / "fixture-opencode.exe"
    executable.write_bytes(b"fixture executable; never launched")
    runtime_binding = measurement.UnitTestRuntimeBinding(
        realm_id=uuid4(),
        project_id=UUID(world.binding.project_id),
        work_item_id=UUID(world.binding.work_item_id),
        coordinator_assignment_id=uuid4(),
        project_root=world.home,
        object_store_root=world.home / "artifacts" / "sha256",
        lock_dir=world.home / "locks",
        opencode_executable=executable,
        approved_maven_plan_digest=world.binding.plan_digest,
    )
    limits = LoopLimits()
    approvals = UnitTestApprovals(
        write_tests=True, build=True, approved_budget=request.budget, approved_limits=limits
    )
    monkeypatch.setattr(
        measurement,
        "inspect_native_source_content",
        lambda *a: measurement.native_source_content_digest(world.observed),
    )
    calls = []

    def compose(request, *, ledger, binding, artifact_registrar, control):
        assert isinstance(ledger, measurement._ShortTransactionLedger)

        def execute(request, *, approvals, limits):
            calls.append(True)
            ledger.register_request(request, now=dt.datetime.now(dt.UTC))
            with world.store.unit_of_work() as independent:
                independent.create_project(slug=f"batch-writer-{len(calls)}", display_name="Writer")
                independent.commit()
            terminal = UnitTestTerminal(
                request.request_digest, UnitTestStopReason.USER_PAUSED, None
            )
            ledger.record_terminal(terminal, now=dt.datetime.now(dt.UTC))
            return SimpleNamespace(
                request_digest=request.request_digest,
                terminal=terminal,
                attempts=0,
                executed_attempts=0,
                detail=(),
            )

        return SimpleNamespace(
            loop=SimpleNamespace(run=execute), gateway=SimpleNamespace(remote=False)
        )

    monkeypatch.setattr(measurement, "compose_unit_test_runtime", compose)
    args = {
        "home": world.home,
        "store": world.store,
        "binding": runtime_binding,
        "work_binding": world.binding,
        "approvals": approvals,
        "limits": limits,
        "validate_context": lambda: None,
    }
    first = measurement.run_unit_test_job(request, **args)
    replay = measurement.run_unit_test_job(request, **args)
    assert first["canonical_job_id"] == replay["canonical_job_id"]
    assert replay["canonical_replayed"] is True and len(calls) == 1
    assert first["provider_calls"] == 0
    snapshot = world.runtime.job_snapshot(first["canonical_job_id"])
    assert snapshot["effects"][0]["operation"] == "test.run"
    assert snapshot["effects"][0]["receipt_status"] == "completed"
    resumed = measurement.run_unit_test_job(request, **args, allow_resume=True)
    assert resumed["canonical_job_id"] != first["canonical_job_id"] and len(calls) == 2


def test_technical_lease_interruption_never_records_final_user_cancelled():
    import datetime as dt

    from zekam.application.unit_test_loop import _Session
    from zekam.domain.unit_test_engineering import UnitTestStopReason

    recorded = []
    session = _Session.__new__(_Session)
    session.control = SimpleNamespace(technical_interruption=True)
    session.state = SimpleNamespace(to_payload=lambda: {})
    session.rd = digest("request")
    session.s = SimpleNamespace(
        ledger=SimpleNamespace(record_terminal=lambda terminal, **k: recorded.append(terminal))
    )
    session._tick = lambda: None
    session._put = lambda body: digest(body)
    session._now = lambda: dt.datetime.now(dt.UTC)
    session._outcome = lambda terminal, detail: terminal
    session._stop(UnitTestStopReason.USER_CANCELLED, ("cancel-during-process",))
    assert len(recorded) == 1
    assert recorded[0].stop_reason is UnitTestStopReason.RECOVERY_REQUIRED
    assert recorded[0].final is False


def test_binding_drift_during_effect_cancels_technical_lease_and_requires_recovery(
    world, monkeypatch
):
    monkeypatch.setattr(subject, "_HEARTBEAT_INTERVAL_SECONDS", 0.02)

    def execute(cancellation):
        with world.store.unit_of_work() as uow:
            uow.activate_config(
                config_digest=digest({"revision": 2}),
                task_digest=digest("new-task"),
                sanitized_config={"revision": 2},
            )
            uow.commit()
        assert cancellation.wait(5), "Canonical authority drift must interrupt the lease"
        return {"observed": True}

    with pytest.raises(PolicyViolation, match="interrupted"):
        run(world, execute)
    assert world.runtime.status().recovery_jobs == 1
    with world.store.unit_of_work() as uow:
        assert uow.list_checkpoints(world.binding.run_id) == ()
        uow.commit()


@pytest.mark.parametrize("native,expected_modules", [(True, ("",)), (False, ())])
def test_canonical_source_plan_uses_execution_modes_module_scope(
    world, monkeypatch, native, expected_modules
):
    import json

    from typer.testing import CliRunner

    from zekam.interfaces.cli import unit_test as cli

    request = UnitTestRequest.with_defaults(
        project_id=world.binding.project_id,
        source_binding_id=world.source.id,
        source_revision=world.observed.source_revision,
        source_files=("src/main/A.java",),
        percent="80",
        budget=UnitTestBudget(3, 60, 180),
    )
    monkeypatch.setattr(cli, "_plan_request", lambda **k: (request, {}))
    monkeypatch.setattr(
        cli, "_execution_plan", lambda *a: {"status": "ready", "plan_digest": digest("maven")}
    )
    modules = []

    def inspect(request, root, target_modules):
        modules.append(target_modules)
        return measurement.native_source_content_digest(world.observed)

    monkeypatch.setattr(cli, "inspect_native_source_content", inspect)
    args = ["plan", "--canonical-source", "--project-root", str(world.home)]
    if native:
        args.append("--native")
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["source_content_digest"]
    assert modules == [expected_modules]


def _completion_fixture_with_incomplete_proof(world, defect):
    import os
    from uuid import uuid4

    from zekam.domain.canonical import canonical_bytes

    operation = "project.index"
    input_digest = digest("input")
    job, _ = world.runtime.enqueue(
        idempotency_key=subject.canonical_job_key(
            world.binding.binding_digest, operation, input_digest
        ),
        payload={"operation": operation, "input_digest": input_digest},
        binding=world.binding,
    )
    work = world.runtime.claim_next(
        owner_id="proof-fixture",
        owner_pid=os.getpid(),
        owner_token="fixture-owner",
        lease_seconds=60,
        supported_operations=(operation,),
        job_id=job.id,
    )
    effect_digest = digest(
        {"operation": operation, "binding": world.binding.binding_digest, "input": input_digest}
    )
    claim, _ = world.runtime.claim_effect(
        work,
        operation=operation,
        effect_digest=digest("wrong-effect") if defect == "effect" else effect_digest,
        idempotency_key=f"job:{job.id}:canonical-effect",
    )
    content = canonical_bytes({"observed": True})
    objects = LocalContentAddressedStore(world.home / "artifacts" / "sha256")
    artifact = objects.put(content, media_type="application/json")
    payload = {
        "schema": "zekam-canonical-job-checkpoint/v1",
        "job_id": job.id,
        "claim_id": claim.id,
        "operation": operation,
        "input_digest": input_digest,
        "binding_digest": world.binding.binding_digest,
        "result_digest": artifact.digest,
        "grants_authority": False,
    }
    changes = {
        "claim": ("claim_id", str(uuid4())),
        "binding": ("binding_digest", digest("other")),
        "input": ("input_digest", digest("other")),
        "result": ("result_digest", digest("other")),
        "operation": ("operation", "test.measure"),
        "grant": ("grants_authority", True),
        "schema": ("schema", "other"),
        "oversized": ("extra", "x" * 17000),
    }
    if defect in changes:
        key, value = changes[defect]
        payload[key] = value
    with world.store.unit_of_work() as uow:
        if defect != "artifact-missing":
            uow.register_artifact(
                artifact_digest=artifact.digest,
                media_type="text/plain" if defect == "artifact-media" else "application/json",
                size_bytes=len(content) + (1 if defect == "artifact-size" else 0),
                classification="local-private",
            )
        if defect != "checkpoint-missing":
            run_id = world.binding.run_id
            if defect == "run":
                other = uow.create_run(
                    work_item_id=world.binding.work_item_id,
                    config_revision_id=world.binding.config_revision_id,
                    plan_digest=world.binding.plan_digest,
                    budget={"seconds": 60},
                    source_snapshot_id=world.binding.source_snapshot_id,
                )
                run_id = other.id
            uow.record_checkpoint(
                run_id=run_id,
                sequence=1,
                checkpoint_digest=digest(payload),
                payload=payload,
                source_snapshot_id=world.binding.source_snapshot_id,
            )
            if defect in {"duplicate", "oversized-duplicate"}:
                extra = payload | {
                    "extra": "x" * 17000 if defect == "oversized-duplicate" else "duplicate"
                }
                uow.record_checkpoint(
                    run_id=run_id,
                    sequence=2,
                    checkpoint_digest=digest(extra),
                    payload=extra,
                    source_snapshot_id=world.binding.source_snapshot_id,
                )
        uow.commit()
    world.runtime.record_receipt(claim, status="completed", evidence_digest=artifact.digest)
    world.runtime.finish(work, state="completed", evidence_digest=artifact.digest)
    return job.id


@pytest.mark.parametrize(
    "defect",
    [
        "checkpoint-missing",
        "artifact-missing",
        "artifact-size",
        "artifact-media",
        "claim",
        "binding",
        "input",
        "result",
        "operation",
        "grant",
        "schema",
        "duplicate",
        "oversized-duplicate",
        "oversized",
        "run",
        "effect",
    ],
)
def test_replay_rejects_incomplete_or_mismatched_completion_proof_without_redispatch(world, defect):
    import sqlite3

    from zekam.infrastructure.sqlite.operational_backup import _logical_rows_digest

    job_id = _completion_fixture_with_incomplete_proof(world, defect)
    with sqlite3.connect(world.database) as connection:
        before = _logical_rows_digest(connection)
    with pytest.raises(PolicyViolation):
        run(world, lambda cancellation: pytest.fail("Invalid proof cannot redispatch"))
    assert world.runtime.job_snapshot(job_id)["state"] == "completed"
    with sqlite3.connect(world.database) as connection:
        assert _logical_rows_digest(connection) == before
