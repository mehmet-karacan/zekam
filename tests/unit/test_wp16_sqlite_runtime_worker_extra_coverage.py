# mypy: disable-error-code="assignment,arg-type,misc"
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import replace as dc_replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest

from zekam.application.execution import ExecutionHost
from zekam.application.model_benchmark_service import load_fixture_registry
from zekam.application.worker import (
    SchedulerGateway,
    Worker,
    WorkerSettings,
    build_worker,
    resolve_handlers,
    run_codex_lifecycle_bootstrap_once,
    run_codex_lifecycle_once,
    run_projection_close_once,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    NotFound,
    PolicyViolation,
    ValidationFailed,
)
from zekam.domain.model_benchmark import BenchmarkPlan, BenchmarkSuite, SuiteKind
from zekam.domain.optimization import (
    MetricAggregation,
    MetricDirection,
    MetricRole,
    MetricSpec,
)
from zekam.domain.runtime import JobKind, JobState
from zekam.infrastructure.sqlite import local_learning as local_learning_schema
from zekam.infrastructure.sqlite import local_model_benchmark as benchmark_module
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.local_improvement import (
    MAX_BODY_BYTES,
    AttemptReservation,
    EvaluationReceipt,
    ImprovementCandidate,
    ImprovementChangeClass,
    SQLiteLocalImprovementStore,
    _body,
    _document,
    _instant,
    _normalized_identity,
    _parse_time,
    _safe,
    _source,
    _text,
)
from zekam.infrastructure.sqlite.local_model_benchmark import (
    DryRunReport,
    LocalBenchmarkTask,
    LocalGraderContract,
    SQLiteLocalBenchmarkLab,
    _canonical_document,
    blind_pair,
    dry_run,
    parse_score,
)
from zekam.infrastructure.sqlite.local_model_benchmark import (
    _instant as benchmark_instant,
)
from zekam.infrastructure.sqlite.local_runtime import (
    SQLiteLocalRuntimeStore,
    _bounded_int,
    _digest,
    _moment,
    _payload_json,
    _process_probe_value,
    _required,
)
from zekam.infrastructure.sqlite.operational_store import (
    SQLiteOperationalStore,
    _canonical_uuid,
    _exact_positive_int,
    _required_text,
    _row_work,
    _validate_digest,
    _work_payload,
)

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(minutes=10)
NAIVE = NOW.replace(tzinfo=None)
D = digest("wp16-sqlite-runtime-worker-extra")
U = tuple(UUID(int=value) for value in range(1, 20))


def _replace(instance: Any, **changes: Any) -> Any:
    return dc_replace(instance, **changes)


def test_local_improvement_private_parsers_and_size_security(tmp_path: Path) -> None:
    assert "prevent" in _normalized_identity("prevents prevented preventing")
    assert _normalized_identity("the") == "lexical-set-v2:"
    assert _instant(NOW).endswith("+00:00")
    assert _parse_time(NOW.isoformat()) == NOW
    assert _safe("safe-ref", "ref") == "safe-ref"
    assert _text("bounded", "text") == "bounded"
    for value in (None, "", "password-value", "x" * 4097):
        with pytest.raises(ValidationFailed):
            _text(value, "text")
    for value in (None, "../x", "api-key", "white space"):
        with pytest.raises(ValidationFailed):
            _safe(value, "ref")
    for value in (None, "bad", NAIVE):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _instant(value) if isinstance(value, dt.datetime) else _parse_time(value)
    with pytest.raises(ValidationFailed, match="body exceeds"):
        _body({"value": "x" * MAX_BODY_BYTES})
    assert _document(canonical_json({"a": 1})) == {"a": 1}
    for raw in (None, "", '{"a":1,"a":2}', '{"a":NaN}', '{"b":2, "a":1}', "[]"):
        with pytest.raises(PolicyViolation):
            _document(raw)
    relative = Path("relative.sqlite3")
    with pytest.raises(ValidationFailed, match="source path"):
        _source(relative, D)
    source = tmp_path / "source.sqlite3"
    source.write_bytes(b"not sqlite")
    source.chmod(0o644)
    with pytest.raises(PolicyViolation, match="identity"):
        _source(source.resolve(), D)


def _metric() -> MetricSpec:
    return MetricSpec(
        "quality.mean",
        "quality",
        "ratio",
        MetricDirection.MAXIMIZE,
        MetricRole.PRIMARY,
        "benchmark",
        target_value=0.9,
        minimum_meaningful_delta=0.05,
        regression_tolerance=0.0,
        aggregation=MetricAggregation.MEAN,
    )


def _candidate(**changes: Any) -> ImprovementCandidate:
    values: dict[str, Any] = {
        "candidate_id": U[0],
        "objective": "repair local cache",
        "observed_problem": "repeatable cache failure",
        "failure_card_digest": D,
        "baseline_aggregate_digest": D,
        "hypothesis": "atomic rebuild prevents stale reads",
        "patch_digest": D,
        "change_class": ImprovementChangeClass.AUTO_SAFE,
        "allowed_resources": ("local-cache",),
        "metric_specs": (_metric(),),
        "regression_guards": ("no-regression",),
        "evaluation_plan_digest": D,
        "max_iterations": 2,
        "max_provider_calls": 2,
        "max_tokens": 100,
        "max_cost_micros": 100,
        "wall_clock_seconds": 60,
        "rollback_plan": "restore immutable generation",
        "proposer_ref": "proposer-one",
        "source_revision": "revision-one",
        "created_at": NOW,
    }
    values.update(changes)
    return ImprovementCandidate(**values)


def test_local_improvement_candidate_exact_type_canonical_and_budget_matrix() -> None:
    candidate = _candidate()
    assert candidate.candidate_digest.startswith("sha256:")
    assert candidate.novelty_digest.startswith("sha256:")
    variants: tuple[dict[str, Any], ...] = (
        {"candidate_id": "not-uuid"},
        {"change_class": "AUTO_SAFE"},
        {"allowed_resources": []},
        {"allowed_resources": ()},
        {"allowed_resources": ("z", "a")},
        {"regression_guards": ()},
        {"allowed_resources": ("force-push",)},
        {"metric_specs": ()},
        {"metric_specs": (_replace(_metric(), metric_id="unknown"),)},
        {"metric_specs": (_replace(_metric(), target_value=1),)},
        {"max_iterations": True},
        {"max_iterations": 0},
        {"max_provider_calls": 4097},
        {"max_tokens": 0},
        {"wall_clock_seconds": 86_401},
        {"created_at": NAIVE},
    )
    for changed in variants:
        with pytest.raises((ValidationFailed, PolicyViolation, TypeError, ValueError)):
            _candidate(**changed)
    for values in ((D, -1, 1, 1), (D, 0, 0, 1), (D, 0, 1, 0), (D, True, 1, 1)):
        with pytest.raises(ValidationFailed):
            AttemptReservation(*values)
    with pytest.raises(ValidationFailed):
        EvaluationReceipt(D, "unknown", D)


def test_local_improvement_store_front_door_and_private_parent_guards(tmp_path: Path) -> None:
    learning = tmp_path / "learning.db"
    benchmark = tmp_path / "benchmark.db"
    store = SQLiteLocalImprovementStore(
        (tmp_path / "improvement.db").resolve(), learning.resolve(), benchmark.resolve()
    )
    tmp_path.chmod(0o755)
    with pytest.raises(PolicyViolation, match="private parent"):
        store.bootstrap()
    tmp_path.chmod(0o700)
    store.bootstrap()
    with pytest.raises(ValidationFailed, match="Exact improvement candidate"):
        store.propose(cast(Any, object()))
    with pytest.raises(ValidationFailed, match="Exact improvement evaluation input"):
        store.complete_evaluation(
            cast(Any, object()),
            D,
            D,
            artifact_before_digest=D,
            artifact_after_digest=D,
            actual_provider_calls=0,
            actual_tokens=0,
            actual_cost_micros=0,
            finished_at=NOW,
        )
    with pytest.raises(ValidationFailed, match="rollout input"):
        store.record_rollout(D, D, stage="prod", success=True, now=NOW)
    with pytest.raises(ValidationFailed, match="rollout input"):
        store.record_rollout(D, D, stage="shadow", success=cast(Any, 1), now=NOW)
    with pytest.raises(ValidationFailed, match="approval"):
        store.review(D, D, reviewer_ref="reviewer", approved=cast(Any, 1), now=NOW)
    with pytest.raises(ValidationFailed, match="rollback status"):
        store.rollback(D, success=cast(Any, 1), now=NOW)
    with pytest.raises(PolicyViolation, match="prior exact activation"):
        store.rollback(D, success=True, now=NOW)


def test_local_improvement_source_schema_and_method_type_gates(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    learning = private / "learning.db"
    benchmark = private / "benchmark.db"
    with sqlite3.connect(learning) as db:
        db.executescript(local_learning_schema._SCHEMA)
    with sqlite3.connect(benchmark) as db:
        db.executescript(benchmark_module._SCHEMA)
    learning.chmod(0o600)
    benchmark.chmod(0o600)
    store = SQLiteLocalImprovementStore(
        (private / "improvement.db").resolve(), learning.resolve(), benchmark.resolve()
    )
    store.bootstrap()
    with pytest.raises(PolicyViolation, match="failure card"):
        store._failure_card(D)
    with pytest.raises(PolicyViolation, match="stored WP-11 aggregate"):
        store._aggregate(D)
    with pytest.raises(ValidationFailed, match="Exact improvement reservation"):
        store.claim_attempt(cast(Any, object()), now=NOW)
    with pytest.raises(ValidationFailed, match="validator manifest"):
        store.freeze_validators(cast(Any, object()), cast(Any, object()))
    candidate = _candidate()
    with pytest.raises(ValidationFailed, match="usage invalid"):
        store.complete_evaluation(
            candidate,
            D,
            D,
            artifact_before_digest=D,
            artifact_after_digest=D,
            actual_provider_calls=cast(Any, True),
            actual_tokens=0,
            actual_cost_micros=0,
            finished_at=NOW,
        )
    with pytest.raises(PolicyViolation, match="metric vector"):
        store._values({"quality": {"mean": 1}}, candidate.metric_specs)
    with sqlite3.connect(store.path) as db:
        db.execute("create table unexpected_schema_object(id integer)")
    with pytest.raises(PolicyViolation, match="schema drift"):
        store.audit()


def _benchmark_contracts() -> tuple[
    Any, BenchmarkSuite, BenchmarkPlan, LocalBenchmarkTask, LocalGraderContract
]:
    registry = load_fixture_registry()
    fixture = registry.fixtures[0]
    suite = BenchmarkSuite("extra", 1, SuiteKind.GENERAL, (fixture.fixture_digest,))
    plan = BenchmarkPlan("model", suite.suite_digest, D, D, registry.registry_digest, repetitions=5)
    grader = LocalGraderContract("grader", 1, D, ("correctness",))
    task = LocalBenchmarkTask("task", 1, fixture.fixture_digest, D, D, grader.grader_digest, 5, 10)
    return registry, suite, plan, task, grader


def test_local_benchmark_contract_dry_run_blind_and_parser_guards() -> None:
    registry, suite, plan, task, grader = _benchmark_contracts()
    assert benchmark_instant(NOW).endswith("+00:00")
    with pytest.raises(ValidationFailed):
        benchmark_instant(NAIVE)
    assert dry_run(plan, suite, max_calls=10).call_count == 10
    with pytest.raises(ValidationFailed, match="Exact benchmark"):
        dry_run(cast(Any, object()), suite, max_calls=10)
    with pytest.raises(PolicyViolation, match="plan/suite"):
        dry_run(_replace(plan, suite_digest=D), suite, max_calls=10)
    for value in (0, 1, -0.1, 1.1, float("nan"), "1"):
        with pytest.raises(ValidationFailed):
            parse_score(value)
    packet = blind_pair(D, "model-a", digest("a"), "model-b", digest("b"))
    assert len(packet.aliases) == 2
    with pytest.raises(PolicyViolation, match="distinct"):
        blind_pair(D, "same", digest("a"), "same", digest("b"))
    for raw in ('{"a":1,"a":2}', '{"a":NaN}', '{"b":2, "a":1}', "[]"):
        with pytest.raises(PolicyViolation):
            _canonical_document(raw)
    for changed in (
        {"version": 0},
        {"repetitions": 4},
        {"repetitions": 101},
        {"timeout_seconds": 0},
    ):
        with pytest.raises(ValidationFailed):
            _replace(task, **changed)
    for changed in ({"version": 0}, {"dimensions": ()}, {"dimensions": ("a", "a")}):
        with pytest.raises(ValidationFailed):
            _replace(grader, **changed)
    with pytest.raises(PolicyViolation):
        DryRunReport(D, 1, 2, 1)
    assert registry.registry_digest


def _benchmark_lab(tmp_path: Path) -> SQLiteLocalBenchmarkLab:
    private = tmp_path / "benchmark-private"
    private.mkdir(mode=0o700, parents=True)
    lab = SQLiteLocalBenchmarkLab((private / "lab.db").resolve(), (private / "artifacts").resolve())
    lab.bootstrap()
    return lab


def test_local_benchmark_lab_plan_replay_claim_and_artifact_fail_closed(tmp_path: Path) -> None:
    registry, suite, plan, task, grader = _benchmark_contracts()
    lab = _benchmark_lab(tmp_path)
    with pytest.raises(PolicyViolation, match="binding missing"):
        lab.ensure_plan(registry=registry, suite=suite, plan=plan)
    with pytest.raises(PolicyViolation, match="Task/grader"):
        lab.register_contracts(_replace(task, grader_digest=D), grader, plan=plan, suite=suite)
    with pytest.raises(PolicyViolation, match="exact plan"):
        lab.register_contracts(task, grader, plan=_replace(plan, repetitions=6), suite=suite)
    lab.register_contracts(task, grader, plan=plan, suite=suite)
    plan_id, created = lab.ensure_plan(registry=registry, suite=suite, plan=plan)
    assert created and lab.ensure_plan(registry=registry, suite=suite, plan=plan) == (
        plan_id,
        False,
    )
    with pytest.raises(ValidationFailed, match="Exact benchmark contracts"):
        lab.ensure_plan(registry=cast(Any, object()), suite=suite, plan=plan)
    with pytest.raises(PolicyViolation, match="remote"):
        lab.ensure_plan(registry=registry, suite=suite, plan=_replace(plan, remote_execution=True))
    with pytest.raises(ValidationFailed, match="repetition"):
        lab.claim_tested(plan=plan, fixture=registry.fixtures[0], repetition=0)
    with pytest.raises(PolicyViolation, match="prepared"):
        empty = _benchmark_lab(tmp_path / "other")
        empty.claim_tested(plan=plan, fixture=registry.fixtures[0], repetition=1)
    with pytest.raises(PolicyViolation, match="relation invalid"):
        lab.bind_artifacts(response_digest=D, raw_digest=D, normalized_digest=D)
    with pytest.raises(PolicyViolation, match="pair missing"):
        lab.artifact_pair(D)


def test_local_benchmark_claim_failure_receipt_and_artifact_replay_paths(tmp_path: Path) -> None:
    registry, suite, plan, task, grader = _benchmark_contracts()
    lab = _benchmark_lab(tmp_path)
    lab.register_contracts(task, grader, plan=plan, suite=suite)
    plan_id, _ = lab.ensure_plan(registry=registry, suite=suite, plan=plan)
    fixture = registry.fixtures[0]
    claim_id = lab.claim_tested(plan=plan, fixture=fixture, repetition=1)
    with pytest.raises(PolicyViolation, match="cannot be reissued"):
        lab.claim_tested(plan=plan, fixture=fixture, repetition=1)
    with pytest.raises(PolicyViolation, match="Tested receipt"):
        lab.complete_tested(claim_id=U[8], result=cast(Any, SimpleNamespace()))
    with pytest.raises(PolicyViolation, match="Verifier receipt"):
        lab.complete_verifier(claim_id=U[8], verdict=cast(Any, SimpleNamespace()))
    with pytest.raises(ValidationFailed, match="phase"):
        lab.retain_failure(
            plan_id=plan_id,
            claim_id=claim_id,
            fixture_digest=fixture.fixture_digest,
            repetition=1,
            phase="other",
            category="timeout",
        )
    with pytest.raises(ValidationFailed, match="repetition"):
        lab.retain_failure(
            plan_id=plan_id,
            claim_id=claim_id,
            fixture_digest=fixture.fixture_digest,
            repetition=0,
            phase="tested",
            category="timeout",
        )
    failure = lab.retain_failure(
        plan_id=plan_id,
        claim_id=claim_id,
        fixture_digest=fixture.fixture_digest,
        repetition=1,
        phase="tested",
        category="timeout",
    )
    assert failure == lab.retain_failure(
        plan_id=plan_id,
        claim_id=claim_id,
        fixture_digest=fixture.fixture_digest,
        repetition=1,
        phase="tested",
        category="timeout",
    )
    for kind, payload in (("bad", b"x"), ("raw", b""), ("raw", b"password=secret")):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            lab.store_artifact(kind, payload)
    value = lab.store_artifact("raw", b"bounded artifact")
    assert lab.store_artifact("raw", b"bounded artifact") == value
    assert not lab.trial_receipt_matches(
        plan_id=plan_id,
        tested_claim_id=U[10],
        verifier_claim_id=U[11],
        verdict=cast(Any, SimpleNamespace()),
        result=cast(Any, SimpleNamespace()),
    )


def test_local_benchmark_private_directory_file_identity_and_schema_drift(tmp_path: Path) -> None:
    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    lab = SQLiteLocalBenchmarkLab((root / "lab.db").resolve(), (root / "artifacts").resolve())
    with pytest.raises(PolicyViolation, match="private directory"):
        lab.bootstrap()
    root.chmod(0o700)
    lab.bootstrap()
    lab.path.chmod(0o644)
    with pytest.raises(PolicyViolation, match="identity"):
        lab.counts()
    lab.path.chmod(0o600)
    with sqlite3.connect(lab.path) as db:
        db.execute("create table schema_drift(id integer)")
    with pytest.raises(PolicyViolation, match="schema drift"):
        lab.counts()


def _operational(tmp_path: Path) -> SQLiteOperationalStore:
    path = tmp_path / "operational.db"
    operational_schema.bootstrap(path)
    return SQLiteOperationalStore(path)


def test_operational_private_strict_decoders_and_corruption() -> None:
    for value in (None, "", " padded ", 1):
        with pytest.raises(ValidationFailed):
            _required_text(value, "value")
    for value in (None, 0, -1, True, 1.0, "1"):
        with pytest.raises(ValidationFailed):
            _exact_positive_int(value, "value")
    for value in (None, "bad"):
        with pytest.raises((ValidationFailed, ValueError)):
            _validate_digest(value, "value")
    for value in (None, "BAD", "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"):
        with pytest.raises(ValidationFailed):
            _canonical_uuid(value, "value")
    assert _work_payload(None)["summary"] == ""
    for value in (
        [],
        {"unknown": 1},
        {"summary": 1},
        {"acceptance_criteria": "x"},
        {"acceptance_criteria": [""]},
    ):
        with pytest.raises(ValidationFailed):
            _work_payload(value)
    for row in (
        {"payload_json": "{"},
        {"payload_json": "[]"},
        {"payload_json": '{"summary":1,"acceptance_criteria":[]}'},
    ):
        with pytest.raises(ConfigurationError):
            _row_work(row)


def test_operational_uow_replay_validation_rollback_and_restart(tmp_path: Path) -> None:
    store = _operational(tmp_path)
    with store.unit_of_work() as uow:
        with pytest.raises(ValidationFailed, match="Config digest"):
            uow.activate_config(config_digest=D, task_digest=D, sanitized_config={"x": 1})
        config_body = {"database": "sqlite"}
        config = uow.activate_config(
            config_digest=digest(config_body), task_digest=D, sanitized_config=config_body
        )
        with pytest.raises(ValidationFailed, match="replay"):
            uow.activate_config(
                config_digest=digest(config_body),
                task_digest=digest("other"),
                sanitized_config=config_body,
            )
        project = uow.create_project(slug="demo", display_name="Demo")
        with pytest.raises(ValidationFailed, match="replay"):
            uow.create_project(slug="demo", display_name="Changed")
        with pytest.raises(ValidationFailed, match="bulunamadi"):
            uow.resolve_project("missing")
        other = uow.create_project(slug="other", display_name="Other")
        uow.add_project_alias(project_id=project.id, alias="alias")
        with pytest.raises(ValidationFailed, match="baska project"):
            uow.add_project_alias(project_id=other.id, alias="alias")
        with pytest.raises(ValidationFailed, match="Source kind"):
            uow.bind_source(project_id=project.id, portable_ref="repo/demo", source_kind="database")
        binding = uow.bind_source(
            project_id=project.id, portable_ref="repo/demo", source_kind="git"
        )
        with pytest.raises(ValidationFailed, match="replay"):
            uow.bind_source(
                project_id=project.id, portable_ref=binding.portable_ref, source_kind="directory"
            )
        work = uow.create_work(
            project_id=project.id, kind="task", title="Task", state="ready", payload_digest=D
        )
        with pytest.raises(ValidationFailed, match="target state"):
            uow.transition_work(
                work_item_id=work.id,
                expected_revision=1,
                to_state="unknown",
                payload_digest=D,
                event_digest=D,
            )
        with pytest.raises(ValidationFailed, match="bulunamadi"):
            uow.transition_work(
                work_item_id=str(U[8]),
                expected_revision=1,
                to_state="active",
                payload_digest=D,
                event_digest=D,
            )
        config_id = config.id
        uow.commit()
    restarted = _operational(tmp_path)
    with restarted.unit_of_work() as uow:
        assert uow.resolve_project("demo").id == project.id
        run = uow.create_run(
            work_item_id=work.id, config_revision_id=config_id, plan_digest=D, budget={}
        )
        with pytest.raises(ValidationFailed, match="duplicate dependency"):
            uow.add_run_step(
                run_id=run.id, step_key="step", input_digest=D, dependencies=(str(U[0]), str(U[0]))
            )
        with pytest.raises(ValidationFailed, match="Run bulunamadi"):
            uow.get_run(str(U[9]))
        with pytest.raises(ValidationFailed, match="status"):
            uow.record_bootstrap_receipt(
                receipt_digest=D, plan_digest=D, task_digest=D, status="partial"
            )
        uow.record_bootstrap_receipt(
            receipt_digest=D, plan_digest=D, task_digest=D, status="completed"
        )
        with pytest.raises(ValidationFailed, match="replay"):
            uow.record_bootstrap_receipt(
                receipt_digest=D, plan_digest=D, task_digest=D, status="failed"
            )
    with restarted.unit_of_work() as uow:
        assert uow.resolve_project("demo").id == project.id


def test_operational_model_artifact_validation_and_transaction_rollback(tmp_path: Path) -> None:
    store = _operational(tmp_path)
    with store.unit_of_work() as uow:
        with pytest.raises(ValidationFailed, match="modality"):
            uow.register_model(canonical_id="model", access_name="model", modality="text")
        model = uow.register_model(canonical_id="model", access_name="model", modality="chat")
        with pytest.raises(ValidationFailed, match="replay"):
            uow.register_model(canonical_id="model", access_name="changed", modality="chat")
        with pytest.raises(ValidationFailed, match="health"):
            uow.record_model_health(
                model_revision_id=model.id, status="healthy", evidence_digest=D, latency_ms=None
            )
        for media, size in (("text", 1), ("text/plain", -1), ("text/plain", 65 * 1024 * 1024)):
            with pytest.raises(ValidationFailed):
                uow.register_artifact(
                    artifact_digest=D,
                    media_type=media,
                    size_bytes=size,
                    classification="internal",
                )
    with store.unit_of_work() as uow:
        assert uow.list_projects() == ()


def test_operational_knowledge_note_relation_materialization_and_archive_guards(
    tmp_path: Path,
) -> None:
    store = _operational(tmp_path)
    realm = str(U[0])
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="knowledge", display_name="Knowledge")
        scope = f"project:{project.id}"
        with pytest.raises(ValidationFailed, match="project bulunamadi"):
            uow.register_knowledge_note(
                realm_id=realm,
                project_id=str(U[8]),
                owner_scope=f"project:{U[8]}",
                portable_ref="projeler/missing/notlar/user/a.md",
                note_kind="note",
                authorship="user",
                classification="internal",
                content_digest=D,
            )
        for changes in (
            {"note_kind": "unknown"},
            {"authorship": "machine"},
            {"classification": "secret"},
            {"state": "archived"},
        ):
            values: dict[str, Any] = {
                "realm_id": realm,
                "project_id": project.id,
                "owner_scope": scope,
                "portable_ref": "projeler/knowledge/notlar/user/a.md",
                "note_kind": "note",
                "authorship": "user",
                "classification": "internal",
                "content_digest": D,
                **changes,
            }
            with pytest.raises(ValidationFailed):
                uow.register_knowledge_note(**values)
        first = uow.register_knowledge_note(
            realm_id=realm,
            project_id=project.id,
            owner_scope=scope,
            portable_ref="projeler/knowledge/notlar/user/a.md",
            note_kind="note",
            authorship="user",
            classification="internal",
            content_digest=digest("first"),
        )
        second = uow.register_knowledge_note(
            realm_id=realm,
            project_id=project.id,
            owner_scope=scope,
            portable_ref="projeler/knowledge/notlar/generated/b.md",
            note_kind="note",
            authorship="generated",
            classification="public",
            content_digest=digest("second"),
        )
        with pytest.raises(ValidationFailed, match="bulunamadi"):
            uow.confirm_knowledge_note(
                note_id="missing", expected_content_digest=D, evidence_digest=D
            )
        with pytest.raises(ValidationFailed, match="content drift"):
            uow.confirm_knowledge_note(
                note_id=first.id, expected_content_digest=D, evidence_digest=D
            )
        first = uow.confirm_knowledge_note(
            note_id=first.id,
            expected_content_digest=first.content_digest,
            evidence_digest=digest("materialized-first"),
        )
        second = uow.confirm_knowledge_note(
            note_id=second.id,
            expected_content_digest=second.content_digest,
            evidence_digest=digest("materialized-second"),
        )
        with pytest.raises(ValidationFailed, match="evidence replay"):
            uow.confirm_knowledge_note(
                note_id=first.id,
                expected_content_digest=first.content_digest,
                evidence_digest=digest("changed-evidence"),
            )
        with pytest.raises(ValidationFailed, match="self"):
            uow.relate_knowledge_notes(
                from_note_id=first.id,
                to_note_id=first.id,
                relation_kind="related",
                source_digest=D,
                verified=True,
            )
        with pytest.raises(ValidationFailed, match="verified"):
            uow.relate_knowledge_notes(
                from_note_id=first.id,
                to_note_id=second.id,
                relation_kind="related",
                source_digest=D,
                verified=False,
            )
        relation = uow.relate_knowledge_notes(
            from_note_id=first.id,
            to_note_id=second.id,
            relation_kind="related",
            source_digest=D,
            verified=True,
        )
        assert relation.verified
        with pytest.raises(ValidationFailed, match="bulunamadi"):
            uow.archive_knowledge_note(
                note_id="missing", expected_content_digest=D, archived_ref="archive/project/x/a.md"
            )
        with pytest.raises(ValidationFailed, match="content drift"):
            uow.archive_knowledge_note(
                note_id=first.id,
                expected_content_digest=D,
                archived_ref=f"archive/project/{project.id}/a.md",
            )
        archived = uow.archive_knowledge_note(
            note_id=first.id,
            expected_content_digest=first.content_digest,
            archived_ref=f"archive/project/{project.id}/a.md",
        )
        assert archived.state == "archived"
        with pytest.raises(ValidationFailed, match="replay ref"):
            uow.archive_knowledge_note(
                note_id=first.id,
                expected_content_digest=first.content_digest,
                archived_ref=f"archive/project/{project.id}/changed.md",
            )


def test_operational_exact_replays_and_work_transition_boundaries(tmp_path: Path) -> None:
    store = _operational(tmp_path)
    realm = str(U[1])
    with store.unit_of_work() as uow:
        config_body = {"profile": "replay"}
        config = uow.activate_config(
            config_digest=digest(config_body), task_digest=D, sanitized_config=config_body
        )
        assert (
            uow.activate_config(
                config_digest=digest(config_body), task_digest=D, sanitized_config=config_body
            )
            == config
        )
        project = uow.create_project(slug="replay", display_name="Replay")
        assert uow.create_project(slug="replay", display_name="Replay") == project
        uow.add_project_alias(project_id=project.id, alias="replay-alias")
        uow.add_project_alias(project_id=project.id, alias="replay-alias")
        binding = uow.bind_source(
            project_id=project.id, portable_ref="repo/replay", source_kind="git"
        )
        assert (
            uow.bind_source(project_id=project.id, portable_ref="repo/replay", source_kind="git")
            == binding
        )
        evidence = digest("work-evidence")
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Replay work",
            state="ready",
            payload={"summary": "replay", "acceptance_criteria": ["exact"]},
            evidence_digest=evidence,
        )
        active = uow.transition_work(
            work_item_id=work.id,
            expected_revision=1,
            to_state="active",
            payload_digest=digest({"summary": "replay", "acceptance_criteria": ["exact"]}),
            event_digest=digest("active-event"),
            evidence_digest=evidence,
        )
        assert active.state == "active" and active.revision == 2
        with pytest.raises(ValidationFailed, match="transition yasak"):
            uow.transition_work(
                work_item_id=active.id,
                expected_revision=2,
                to_state="ready",
                payload_digest=D,
                event_digest=D,
            )
        model = uow.register_model(
            canonical_id="replay-model", access_name="local", modality="chat"
        )
        assert (
            uow.register_model(canonical_id="replay-model", access_name="local", modality="chat")
            == model
        )
        revision = uow.observe_model_revision(
            model_identity_id=model.id,
            provider_fingerprint_digest=D,
            observed_revision="v1",
        )
        assert (
            uow.observe_model_revision(
                model_identity_id=model.id,
                provider_fingerprint_digest=D,
                observed_revision="v1",
            )
            == revision
        )
        artifact = uow.register_artifact(
            artifact_digest=digest("artifact-replay"),
            media_type="text/plain",
            size_bytes=7,
            classification="internal",
        )
        assert (
            uow.register_artifact(
                artifact_digest=artifact.digest,
                media_type="TEXT/PLAIN",
                size_bytes=7,
                classification="internal",
            )
            == artifact
        )
        with pytest.raises(ValidationFailed, match="replay payload drift"):
            uow.register_artifact(
                artifact_digest=artifact.digest,
                media_type="text/plain",
                size_bytes=8,
                classification="internal",
            )
        scope = f"project:{project.id}"
        note = uow.register_knowledge_note(
            realm_id=realm,
            project_id=project.id,
            owner_scope=scope,
            portable_ref="projeler/replay/notlar/user/a.md",
            note_kind="note",
            authorship="user",
            classification="internal",
            content_digest=digest("note-replay"),
        )
        with pytest.raises(ValidationFailed, match="realm binding drift"):
            uow.register_knowledge_note(
                realm_id=str(U[2]),
                project_id=project.id,
                owner_scope=scope,
                portable_ref="projeler/replay/notlar/user/realm-drift.md",
                note_kind="note",
                authorship="user",
                classification="internal",
                content_digest=digest("realm-drift"),
            )
        assert (
            uow.register_knowledge_note(
                realm_id=realm,
                project_id=project.id,
                owner_scope=scope,
                portable_ref=note.portable_ref,
                note_kind="note",
                authorship="user",
                classification="internal",
                content_digest=note.content_digest,
            )
            == note
        )
        materialized = uow.confirm_knowledge_note(
            note_id=note.id,
            expected_content_digest=note.content_digest,
            evidence_digest=digest("note-materialized"),
        )
        assert uow.confirm_knowledge_note(
            note_id=note.id,
            expected_content_digest=note.content_digest,
            evidence_digest=digest("note-materialized"),
        ).materialized
        other = uow.register_knowledge_note(
            realm_id=realm,
            project_id=project.id,
            owner_scope=scope,
            portable_ref="projeler/replay/notlar/generated/b.md",
            note_kind="note",
            authorship="generated",
            classification="internal",
            content_digest=digest("other-note"),
        )
        with pytest.raises(ValidationFailed, match="materialized note"):
            uow.archive_knowledge_note(
                note_id=other.id,
                expected_content_digest=other.content_digest,
                archived_ref=f"archive/project/{project.id}/not-materialized.md",
            )
        other = uow.confirm_knowledge_note(
            note_id=other.id,
            expected_content_digest=other.content_digest,
            evidence_digest=digest("other-materialized"),
        )
        relation = uow.relate_knowledge_notes(
            from_note_id=materialized.id,
            to_note_id=other.id,
            relation_kind="supports",
            source_digest=D,
            verified=True,
        )
        assert (
            uow.relate_knowledge_notes(
                from_note_id=materialized.id,
                to_note_id=other.id,
                relation_kind="supports",
                source_digest=D,
                verified=True,
            )
            == relation
        )
        with pytest.raises(ValidationFailed, match="replay payload drift"):
            uow.relate_knowledge_notes(
                from_note_id=materialized.id,
                to_note_id=other.id,
                relation_kind="supports",
                source_digest=digest("relation-drift"),
                verified=True,
            )
        archive_ref = f"archive/project/{project.id}/a.md"
        archived = uow.archive_knowledge_note(
            note_id=materialized.id,
            expected_content_digest=materialized.content_digest,
            archived_ref=archive_ref,
        )
        assert (
            uow.archive_knowledge_note(
                note_id=archived.id,
                expected_content_digest=archived.content_digest,
                archived_ref=archive_ref,
            )
            == archived
        )
        receipt = digest("bootstrap-replay")
        uow.record_bootstrap_receipt(
            receipt_digest=receipt, plan_digest=D, task_digest=D, status="completed"
        )
        uow.record_bootstrap_receipt(
            receipt_digest=receipt, plan_digest=D, task_digest=D, status="completed"
        )
        session = uow.open_session(client_id="client", device_id="device", project_id=project.id)
        session_note = uow.register_knowledge_note(
            realm_id=realm,
            project_id=project.id,
            owner_scope=f"session:{session.id}",
            portable_ref="projeler/replay/notlar/user/session.md",
            note_kind="note",
            authorship="user",
            classification="internal",
            content_digest=digest("session-note"),
        )
        assert session_note.owner_scope == f"session:{session.id}"
        corrupt = uow.register_knowledge_note(
            realm_id=realm,
            project_id=project.id,
            owner_scope=scope,
            portable_ref="projeler/replay/notlar/user/corrupt-state.md",
            note_kind="note",
            authorship="user",
            classification="internal",
            content_digest=digest("corrupt-state"),
        )
        corrupt = uow.confirm_knowledge_note(
            note_id=corrupt.id,
            expected_content_digest=corrupt.content_digest,
            evidence_digest=digest("corrupt-materialized"),
        )
        uow._db().execute("pragma ignore_check_constraints=on")
        uow._db().execute("drop trigger knowledge_note_guard_update")
        uow._db().execute("update knowledge_note set state='corrupt' where id=?", (corrupt.id,))
        with pytest.raises(ValidationFailed, match="archive state"):
            uow.archive_knowledge_note(
                note_id=corrupt.id,
                expected_content_digest=corrupt.content_digest,
                archived_ref=f"archive/project/{project.id}/corrupt-state.md",
            )


def _runtime(tmp_path: Path, **changes: Any) -> SQLiteLocalRuntimeStore:
    return SQLiteLocalRuntimeStore(tmp_path / "runtime.db", **changes)


def test_local_runtime_strict_helpers_bootstrap_and_config_restart(tmp_path: Path) -> None:
    assert _moment(NOW.isoformat()) == NOW
    for value in (None, "", "x" * 513):
        if value is None:
            continue
        with pytest.raises(ValidationFailed):
            _required(value, "value")
    for value in (None, 1):
        with pytest.raises(ValidationFailed):
            _digest(cast(Any, value), "value")
    for value in (True, 0, 11, 1.0):
        with pytest.raises(ValidationFailed):
            _bounded_int(cast(Any, value), "value", minimum=1, maximum=10)
    for value in ("", "not-time", NAIVE.isoformat()):
        with pytest.raises(ValidationFailed):
            _moment(value)
    with pytest.raises(ValidationFailed):
        _payload_json(cast(Any, []))
    with pytest.raises(ValidationFailed, match="1 MiB"):
        _payload_json({"x": "x" * 1_048_576})
    for observed in (1, "", " padded ", "x" * 513):
        with pytest.raises(ValidationFailed):
            _process_probe_value(lambda _pid, value=observed: cast(Any, value), 1)
    with pytest.raises(ValidationFailed, match="existing_only"):
        _runtime(tmp_path, existing_only=cast(Any, 1))
    with pytest.raises(PolicyViolation, match="existing current"):
        _runtime(tmp_path, existing_only=True)
    store = _runtime(tmp_path, max_pending_outbox=3)
    assert SQLiteLocalRuntimeStore(store.path, existing_only=True).max_pending_outbox == 3
    with pytest.raises(PolicyViolation, match="config drift"):
        SQLiteLocalRuntimeStore(store.path, max_pending_outbox=4)
    with pytest.raises(ValidationFailed, match="open_only"):
        store.recovery_cases(open_only=cast(Any, 1))


def test_local_runtime_job_claim_receipt_replay_and_terminal_guards(tmp_path: Path) -> None:
    store = _runtime(tmp_path)
    with pytest.raises(ValidationFailed, match="timeout"):
        store.enqueue(
            idempotency_key="bad-time",
            payload={},
            available_at=NOW.isoformat(),
            timeout_at=NOW.isoformat(),
        )
    job, created = store.enqueue(
        idempotency_key="job", payload={"kind": "test"}, available_at=NOW.isoformat()
    )
    assert created
    with pytest.raises(ConcurrencyConflict, match="payload drift"):
        store.enqueue(
            idempotency_key="job", payload={"kind": "changed"}, available_at=NOW.isoformat()
        )
    with pytest.raises(ValidationFailed, match="duplicate"):
        store.claim_next(
            owner_id="w",
            owner_pid=1,
            owner_token="t",
            lease_seconds=10,
            resources=("r", "r"),
            now=NOW.isoformat(),
        )
    work = store.claim_next(
        owner_id="w", owner_pid=1, owner_token="t", lease_seconds=10, now=NOW.isoformat()
    )
    assert work is not None and work.job.id == job.id
    with pytest.raises(NotFound):
        store.heartbeat(
            "missing",
            owner_id="w",
            owner_token="t",
            fencing_token=1,
            lease_seconds=10,
            now=NOW.isoformat(),
        )
    claim, made = store.claim_effect(
        work, operation="write", effect_digest=D, idempotency_key="effect", now=NOW.isoformat()
    )
    assert made
    with pytest.raises(ConcurrencyConflict, match="idempotency drift"):
        store.claim_effect(
            work, operation="other", effect_digest=D, idempotency_key="effect", now=NOW.isoformat()
        )
    with pytest.raises(ValidationFailed, match="status"):
        store.record_receipt(claim, status=cast(Any, "partial"), evidence_digest=D)
    forged = _replace(claim, effect_digest=digest("forged"))
    with pytest.raises(ConcurrencyConflict, match="exact claim"):
        store.record_receipt(forged, status="completed", evidence_digest=D)
    receipt = store.record_receipt(
        claim, status="completed", evidence_digest=D, now=NOW.isoformat()
    )
    assert receipt.status == "completed"
    assert (
        store.record_receipt(claim, status="completed", evidence_digest=D, now=NOW.isoformat())
        == receipt
    )
    with pytest.raises(ConcurrencyConflict, match="replay"):
        store.record_receipt(claim, status="failed", evidence_digest=D, now=NOW.isoformat())
    with pytest.raises(ValidationFailed, match="terminal state"):
        store.finish(work, state=cast(Any, "done"), evidence_digest=D)
    with pytest.raises(PolicyViolation, match="evidence"):
        store.finish(work, state="completed")
    with pytest.raises(ValidationFailed, match="evidence recovery"):
        store.finish(work, state="recovery-required", evidence_digest=D)
    terminal = store.finish(
        work, state="completed", evidence_digest=D, now=(NOW + dt.timedelta(seconds=1)).isoformat()
    )
    assert terminal.state == "completed"
    with pytest.raises(NotFound):
        store.destroy_terminal("missing")


def test_local_runtime_schedule_outbox_and_no_claim_recovery_guards(tmp_path: Path) -> None:
    store = _runtime(tmp_path)
    scheduled, created = store.schedule_once(
        slot_key="slot-1",
        schedule_digest=D,
        idempotency_key="scheduled",
        payload={"kind": "maintenance"},
        now=NOW.isoformat(),
    )
    assert created
    assert store.schedule_once(
        slot_key="slot-1",
        schedule_digest=D,
        idempotency_key="scheduled",
        payload={"kind": "maintenance"},
        now=NOW.isoformat(),
    ) == (scheduled, False)
    with pytest.raises(ConcurrencyConflict, match="replay payload drift"):
        store.schedule_once(
            slot_key="slot-1",
            schedule_digest=D,
            idempotency_key="changed",
            payload={"kind": "maintenance"},
            now=NOW.isoformat(),
        )
    store.enqueue(idempotency_key="fresh", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker", owner_pid=1, owner_token="token", lease_seconds=10, now=NOW.isoformat()
    )
    assert work is not None
    with pytest.raises(PolicyViolation, match="receiptless claim"):
        store.finish(work, state="recovery-required", now=NOW.isoformat())
    with pytest.raises(ValidationFailed, match="typed claim"):
        store.record_outbox_receipt(cast(Any, object()), status="delivered", evidence_digest=D)
    events = store.pending_outbox()
    assert events
    with pytest.raises(ValidationFailed, match="Outbox id exact"):
        store.claim_outbox(
            supported_kinds=(events[0].event_kind,),
            owner_id="owner",
            owner_pid=1,
            owner_token="token",
            lease_seconds=10,
            outbox_id=" padded ",
            now=NOW.isoformat(),
        )


def test_local_runtime_schedule_collision_and_outbox_unknown_recovery(tmp_path: Path) -> None:
    store = _runtime(tmp_path)
    store.schedule_once(
        slot_key="slot",
        schedule_digest=D,
        idempotency_key="scheduled-job",
        payload={"kind": "scheduled"},
        now=NOW.isoformat(),
    )
    with pytest.raises(ConcurrencyConflict, match="digest drift"):
        store.schedule_once(
            slot_key="slot",
            schedule_digest=digest("changed-schedule"),
            idempotency_key="scheduled-job",
            payload={"kind": "scheduled"},
            now=NOW.isoformat(),
        )
    with pytest.raises(ConcurrencyConflict, match="zaten kullanilmis"):
        store.schedule_once(
            slot_key="different-slot",
            schedule_digest=D,
            idempotency_key="scheduled-job",
            payload={"kind": "scheduled"},
            now=NOW.isoformat(),
        )
    event = store.pending_outbox()[0]
    assert (
        store.claim_outbox(
            supported_kinds=(event.event_kind,),
            owner_id="outbox-worker",
            owner_pid=42,
            owner_token="process-token",
            lease_seconds=30,
            outbox_id="missing",
            now=NOW.isoformat(),
        )
        is None
    )
    claim = store.claim_outbox(
        supported_kinds=(event.event_kind,),
        owner_id="outbox-worker",
        owner_pid=42,
        owner_token="process-token",
        lease_seconds=30,
        outbox_id=event.id,
        now=NOW.isoformat(),
    )
    assert claim is not None
    with pytest.raises(ValidationFailed, match="status"):
        store.record_outbox_receipt(
            claim, status=cast(Any, "partial"), evidence_digest=D, now=NOW.isoformat()
        )
    forged = _replace(claim, owner_token="forged")
    with pytest.raises(ConcurrencyConflict, match="exact claim"):
        store.record_outbox_receipt(
            forged, status="unknown", evidence_digest=D, now=NOW.isoformat()
        )
    unknown = store.record_outbox_receipt(
        claim, status="unknown", evidence_digest=D, now=NOW.isoformat()
    )
    assert unknown.state == "recovery-required"
    assert (
        store.record_outbox_receipt(claim, status="unknown", evidence_digest=D, now=NOW.isoformat())
        == unknown
    )
    with pytest.raises(ConcurrencyConflict, match="replay drift"):
        store.record_outbox_receipt(
            claim,
            status="unknown",
            evidence_digest=digest("changed-receipt"),
            now=NOW.isoformat(),
        )
    case = next(
        case for case in store.recovery_cases() if case.case_kind == "outbox-delivery-unknown"
    )
    with pytest.raises(ValidationFailed, match="outcome"):
        store.resolve_recovery(
            case.id, outcome=cast(Any, "unknown"), evidence_digest=D, now=NOW.isoformat()
        )
    with pytest.raises(ValidationFailed, match="uyumsuz"):
        store.resolve_recovery(case.id, outcome="completed", evidence_digest=D, now=NOW.isoformat())
    resolution = store.resolve_recovery(
        case.id, outcome="delivered", evidence_digest=D, now=NOW.isoformat()
    )
    assert resolution.outcome == "delivered"
    assert (
        store.resolve_recovery(case.id, outcome="delivered", evidence_digest=D, now=NOW.isoformat())
        == resolution
    )
    with pytest.raises(ConcurrencyConflict, match="replay drift"):
        store.resolve_recovery(
            case.id,
            outcome="delivered",
            evidence_digest=digest("different-resolution"),
            now=NOW.isoformat(),
        )


def test_local_runtime_receiptless_recovery_and_probe_validation(tmp_path: Path) -> None:
    store = _runtime(tmp_path)
    store.enqueue(idempotency_key="uncertain", payload={}, available_at=NOW.isoformat())
    work = store.claim_next(
        owner_id="w", owner_pid=10, owner_token="token", lease_seconds=1, now=NOW.isoformat()
    )
    assert work is not None
    store.claim_effect(
        work,
        operation="write",
        effect_digest=D,
        idempotency_key="uncertain-effect",
        now=NOW.isoformat(),
    )
    with pytest.raises(PolicyViolation, match="Unresolved"):
        store.finish(work, state="completed", evidence_digest=D, now=NOW.isoformat())
    recovered = store.finish(work, state="recovery-required", now=NOW.isoformat())
    assert recovered.state == "recovery-required" and store.recovery_cases()
    with pytest.raises(ValidationFailed, match="callable"):
        store.recover_outbox(process_token_for=cast(Any, "bad"))
    with pytest.raises(ValidationFailed, match="callable"):
        store.recover_orphans(process_token_for=cast(Any, "bad"))
    with pytest.raises(NotFound, match="Recovery case"):
        store.resolve_recovery("missing", outcome="failed", evidence_digest=D)
    with pytest.raises(NotFound, match="Local job"):
        store.reconcile_recovery("missing")


def test_local_runtime_reconcile_and_quarantine_authority_boundaries(tmp_path: Path) -> None:
    store = _runtime(tmp_path)
    job, _ = store.enqueue(idempotency_key="ordinary", payload={}, available_at=NOW.isoformat())
    with pytest.raises(PolicyViolation, match="recovery-required"):
        store.reconcile_recovery(job.id, now=NOW.isoformat())
    work = store.claim_next(
        owner_id="worker",
        owner_pid=7,
        owner_token="token",
        lease_seconds=30,
        now=NOW.isoformat(),
    )
    assert work is not None
    with pytest.raises(ConcurrencyConflict, match="live fence"):
        store.quarantine(
            _replace(work, lease=_replace(work.lease, owner_token="forged")),
            evidence_digest=D,
            now=NOW.isoformat(),
        )
    store.claim_effect(
        work,
        operation="write",
        effect_digest=D,
        idempotency_key="claimed-effect",
        now=NOW.isoformat(),
    )
    with pytest.raises(PolicyViolation, match="Effect claim"):
        store.quarantine(work, evidence_digest=D, now=NOW.isoformat())


def test_worker_capacity_empty_run_finish_recovery_and_composition_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = WorkerSettings(
        "worker", ("read",), poll_seconds=0.0001, max_queue_depth=1, max_workers=1, max_iterations=1
    )
    host = Mock(spec=ExecutionHost)
    host.acquire_work.return_value = None
    worker = Worker(
        cast(ExecutionHost, host), settings, handlers={str(JobKind.READ_ONLY): lambda _work: D}
    )
    assert not worker.tick(now=NOW, queue_depth=2).accepted_work
    assert worker.tick(now=NOW).skipped_reason == "kuyruk bos"
    monkeypatch.setattr("zekam.application.worker.time.sleep", lambda _value: None)
    assert len(worker.run()) == 1
    with pytest.raises(PolicyViolation, match="explicit handler"):
        build_worker(object(), U[0], settings=settings, handlers={})
    with pytest.raises(PolicyViolation, match="Scheduled-only"):
        build_worker(
            object(), U[0], settings=settings, handlers={"x": lambda _work: D}, consume_queue=False
        )
    with pytest.raises(PolicyViolation, match="handler tanimsiz"):
        resolve_handlers(("missing",), registry={})
    for capabilities, runner in (
        (("wrong",), run_codex_lifecycle_once),
        (("wrong",), run_codex_lifecycle_bootstrap_once),
        (("wrong",), run_projection_close_once),
    ):
        with pytest.raises(PolicyViolation):
            kwargs: dict[str, Any] = {"settings": _replace(settings, capabilities=capabilities)}
            if runner is not run_projection_close_once:
                kwargs["home"] = Path("/tmp")
            runner(object(), U[0], **kwargs)


def test_worker_schedule_dry_run_skip_missing_failure_and_success_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = WorkerSettings("scheduled", ("read",))
    scheduler = Mock(spec=SchedulerGateway)
    definitions = tuple(
        (U[index], SimpleNamespace(job_name=name), None)
        for index, name in enumerate(
            (
                "ordinary-no-handler",
                "diagnostic-trace-purge",
                "handler-fails",
                "handler-succeeds",
                "trigger-skipped",
            ),
            start=1,
        )
    )
    scheduler.definitions.return_value = definitions
    scheduler.is_running.return_value = False
    scheduler.known_keys.return_value = frozenset()
    plans = iter(
        (
            SimpleNamespace(should_run=True, missed=0, reason="due"),
            SimpleNamespace(should_run=False, missed=1, reason="skip"),
            SimpleNamespace(should_run=True, missed=0, reason="due"),
            SimpleNamespace(should_run=True, missed=0, reason="due"),
            SimpleNamespace(should_run=True, missed=0, reason="due"),
        )
    )
    monkeypatch.setattr(
        "zekam.application.worker.plan_trigger", lambda *_args, **_kwargs: next(plans)
    )
    scheduler.record_trigger.side_effect = (U[10], U[11], U[12], U[13], None)
    worker = Worker(
        Mock(spec=ExecutionHost),
        settings,
        scheduler=cast(SchedulerGateway, scheduler),
        scheduled_handlers={
            "handler-fails": lambda _now: (_ for _ in ()).throw(RuntimeError("failed")),
            "handler-succeeds": lambda _now: "done",
        },
        consume_queue=False,
    )
    assert worker._run_schedules(NOW) == ("ordinary-no-handler", "handler-succeeds")
    assert scheduler.record_incident.call_count == 3

    scheduler.definitions.return_value = ((U[1], SimpleNamespace(job_name="due"), None),)
    scheduler.is_running.return_value = False
    scheduler.known_keys.return_value = frozenset()
    monkeypatch.setattr(
        "zekam.application.worker.plan_trigger",
        lambda *_args, **_kwargs: SimpleNamespace(should_run=True),
    )
    planned = worker.plan(now=NOW)
    assert planned.triggered_jobs == ("due",)


def test_worker_process_terminal_visibility_failure_paths() -> None:
    job = SimpleNamespace(id=U[0], kind=JobKind.READ_ONLY)
    work = SimpleNamespace(job=job)
    host = Mock(spec=ExecutionHost)
    host.ledger.claims_for_job.return_value = ()
    host.finish.side_effect = [False, False]
    worker = Worker(
        cast(ExecutionHost, host),
        WorkerSettings("worker", ("read",)),
        handlers={str(JobKind.READ_ONLY): lambda _work: D},
    )
    with pytest.raises(PolicyViolation, match="terminal finish ve recovery"):
        worker._process(cast(Any, work), NOW)
    assert worker._active == 0

    host.finish.side_effect = [RuntimeError("uncertain"), RuntimeError("recovery failed")]
    with pytest.raises(PolicyViolation, match="recovery-required kaydi"):
        worker._process(cast(Any, work), NOW)
    assert worker._active == 0


def _worker_job(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": U[0],
        "project_id": U[1],
        "work_item_id": U[2],
        "plan_id": U[3],
        "step_id": "step",
        "assignment_id": U[4],
        "run_id": U[5],
        "state": JobState.RUNNING,
        "payload": {"entry_digest": D},
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_worker_codex_child_empty_recovery_and_identity_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = WorkerSettings("child", ("client.lifecycle.codex-drain",))
    repo = Mock()
    spool = Mock()
    spool.pending.return_value = ()
    repo.next_codex_lifecycle_job_id.return_value = None
    monkeypatch.setattr(
        "zekam.application.worker.legacy_repository", lambda *_args, **_kwargs: repo
    )
    monkeypatch.setattr(
        "zekam.application.client_lifecycle_spool.ClientLifecycleSpool",
        lambda *_args, **_kwargs: spool,
    )
    assert run_codex_lifecycle_once(object(), U[0], home=tmp_path, settings=settings) is None

    spool.pending.return_value = (SimpleNamespace(entry_digest=D),)
    repo.committed_admission_exists.return_value = True
    monkeypatch.setattr(
        "zekam.application.client_lifecycle_composition.recover_committed_codex_delivery",
        lambda **_kwargs: None,
    )
    with pytest.raises(PolicyViolation, match="ACK recovery"):
        run_codex_lifecycle_once(object(), U[0], home=tmp_path, settings=settings)

    repo.committed_admission_exists.return_value = False
    repo.next_codex_lifecycle_job_id.return_value = U[0]
    host = Mock()
    host.jobs.get.return_value = _worker_job(work_item_id=None)
    monkeypatch.setattr("zekam.application.worker.ExecutionHost", lambda *_args, **_kwargs: host)
    with pytest.raises(PolicyViolation, match="identity eksik"):
        run_codex_lifecycle_once(object(), U[0], home=tmp_path, settings=settings)


def test_worker_bootstrap_and_projection_empty_identity_and_spool_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bootstrap_settings = WorkerSettings("parent", ("client.lifecycle.codex-bootstrap",))
    spool = Mock()
    spool.pending.return_value = ()
    monkeypatch.setattr(
        "zekam.application.client_lifecycle_spool.ClientLifecycleSpool",
        lambda *_args, **_kwargs: spool,
    )
    assert (
        run_codex_lifecycle_bootstrap_once(
            object(), U[0], home=tmp_path, settings=bootstrap_settings
        )
        is None
    )
    spool.pending.return_value = (SimpleNamespace(entry_digest=D),)
    repo = Mock()
    repo.next_bootstrap_job_id.return_value = None
    monkeypatch.setattr(
        "zekam.application.worker.legacy_repository", lambda *_args, **_kwargs: repo
    )
    assert (
        run_codex_lifecycle_bootstrap_once(
            object(), U[0], home=tmp_path, settings=bootstrap_settings
        )
        is None
    )
    repo.next_bootstrap_job_id.return_value = U[0]
    host = Mock()
    host.jobs.get.return_value = _worker_job(payload={"entry_digest": digest("other")})
    monkeypatch.setattr("zekam.application.worker.ExecutionHost", lambda *_args, **_kwargs: host)
    with pytest.raises(PolicyViolation, match="spool head drift"):
        run_codex_lifecycle_bootstrap_once(
            object(), U[0], home=tmp_path, settings=bootstrap_settings
        )

    service = Mock()
    service.assert_release_ready = Mock()
    service.next_ready_job_id.return_value = None
    monkeypatch.setattr(
        "zekam.application.projection_close_runtime.ProjectionCloseRuntimeService",
        lambda *_args, **_kwargs: service,
    )
    close_settings = WorkerSettings("close", ("client.lifecycle.projection-close",))
    assert run_projection_close_once(object(), U[0], settings=close_settings) is None
    service.next_ready_job_id.return_value = U[0]
    host.jobs.get.return_value = _worker_job(plan_id=None)
    with pytest.raises(PolicyViolation, match="identity eksik"):
        run_projection_close_once(object(), U[0], settings=close_settings)


def test_worker_child_bootstrap_and_close_failure_terminalization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spool = Mock()
    spool.pending.return_value = (SimpleNamespace(entry_digest=D),)
    monkeypatch.setattr(
        "zekam.application.client_lifecycle_spool.ClientLifecycleSpool",
        lambda *_args, **_kwargs: spool,
    )
    job = _worker_job()
    work = SimpleNamespace(job=job)
    host = Mock()
    host.jobs.get.return_value = job
    host.jobs.claim_exact.return_value = work
    host.ledger.claims_for_job.return_value = ()
    host.finish.return_value = False
    monkeypatch.setattr("zekam.application.worker.ExecutionHost", lambda *_args, **_kwargs: host)

    child_repo = Mock()
    child_repo.committed_admission_exists.return_value = False
    child_repo.next_codex_lifecycle_job_id.return_value = job.id
    monkeypatch.setattr(
        "zekam.application.worker.legacy_repository", lambda *_args, **_kwargs: child_repo
    )
    binder = Mock()
    binder.bind_child_envelope.side_effect = RuntimeError("bind")
    monkeypatch.setattr(
        "zekam.application.client_runtime_bootstrap.ClaimedLifecycleBootstrapService",
        lambda *_args, **_kwargs: binder,
    )
    with pytest.raises(PolicyViolation, match="terminal finish"):
        run_codex_lifecycle_once(
            object(),
            U[0],
            home=tmp_path,
            settings=WorkerSettings("child", ("client.lifecycle.codex-drain",)),
        )

    parent_repo = Mock()
    parent_repo.next_bootstrap_job_id.return_value = job.id
    parent_repo.current_for_bootstrap_job.return_value = object()
    monkeypatch.setattr(
        "zekam.application.worker.legacy_repository", lambda *_args, **_kwargs: parent_repo
    )
    binder.materialize.side_effect = RuntimeError("materialize")
    host.finish.return_value = False
    with pytest.raises(PolicyViolation, match="bootstrap worker terminal"):
        run_codex_lifecycle_bootstrap_once(
            object(),
            U[0],
            home=tmp_path,
            settings=WorkerSettings("parent", ("client.lifecycle.codex-bootstrap",)),
        )

    service = Mock()
    service.assert_release_ready = Mock()
    service.next_ready_job_id.return_value = job.id
    service.execute.side_effect = RuntimeError("close")
    monkeypatch.setattr(
        "zekam.application.projection_close_runtime.ProjectionCloseRuntimeService",
        lambda *_args, **_kwargs: service,
    )
    host.finish.return_value = False
    with pytest.raises(PolicyViolation, match="Projection close worker terminal"):
        run_projection_close_once(
            object(),
            U[0],
            settings=WorkerSettings("close", ("client.lifecycle.projection-close",)),
        )
