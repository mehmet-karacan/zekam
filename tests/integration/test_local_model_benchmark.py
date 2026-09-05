from __future__ import annotations

import datetime as dt
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from zekam.application.model_benchmark_service import (
    BenchmarkExecutionService,
    DeterministicLocalBenchmarkAdapter,
    LocalProcessBenchmarkAdapter,
    LocalProcessBenchmarkVerifier,
    _run_json_process,
    default_fixture_file,
    load_fixture_registry,
    trial_from_mapping,
)
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import (
    BENCHMARK_TASK_FAMILIES,
    BenchmarkFixture,
    BenchmarkPlan,
    BenchmarkSuite,
    BenchmarkTaskFamily,
    FixtureRegistry,
    SuiteKind,
    TrialResult,
    TrialStatus,
    VerifierIdentity,
    VerifierVerdict,
)
from zekam.infrastructure.sqlite.local_model_benchmark import (
    LocalBenchmarkTask,
    LocalGraderContract,
    SQLiteLocalBenchmarkLab,
    blind_pair,
    dry_run,
    parse_score,
)

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)


def _contracts() -> tuple[FixtureRegistry, BenchmarkSuite, BenchmarkPlan]:
    registry = load_fixture_registry()
    fixture = registry.fixtures[0]
    suite = BenchmarkSuite("local-lab", 1, SuiteKind.GENERAL, (fixture.fixture_digest,))
    plan = BenchmarkPlan(
        "tested-model",
        suite.suite_digest,
        digest("inventory"),
        digest("policy"),
        registry.registry_digest,
    )
    return registry, suite, plan


def _lab(tmp_path: Path) -> SQLiteLocalBenchmarkLab:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    lab = SQLiteLocalBenchmarkLab((root / "lab.db").resolve(), (root / "artifacts").resolve())
    lab.bootstrap()
    return lab


def _bind(
    lab: SQLiteLocalBenchmarkLab,
    registry: FixtureRegistry,
    suite: BenchmarkSuite,
    plan: BenchmarkPlan,
) -> None:
    grader = LocalGraderContract("exact-json", 1, digest("implementation"), ("correctness",))
    task = LocalBenchmarkTask(
        "task-one",
        1,
        registry.fixtures[0].fixture_digest,
        digest("prompt"),
        digest("hidden-key"),
        grader.grader_digest,
        plan.repetitions,
        10,
    )
    lab.register_contracts(task, grader, plan=plan, suite=suite)


def test_all_fourteen_task_families_have_digest_bound_task_manifests() -> None:
    grader = LocalGraderContract(
        "family-grader", 1, digest("family-implementation"), ("correctness",)
    )
    manifests = tuple(
        LocalBenchmarkTask(
            f"task-{family.value}",
            1,
            digest({"fixture": family.value}),
            digest({"prompt": family.value}),
            digest({"hidden": family.value}),
            grader.grader_digest,
            5,
            10,
            task_family=family,
            scoring_dimensions=grader.dimensions,
        )
        for family in BENCHMARK_TASK_FAMILIES
    )
    assert len(manifests) == 14
    assert {item.body()["task_family"] for item in manifests} == {
        item.value for item in BenchmarkTaskFamily
    }
    assert len({item.task_digest for item in manifests}) == 14


def test_task_family_and_task_specific_grader_fail_closed(tmp_path: Path) -> None:
    registry, suite, plan = _contracts()
    lab = _lab(tmp_path)
    grader = LocalGraderContract(
        "specific-grader", 1, digest("specific-implementation"), ("safety",)
    )
    task = LocalBenchmarkTask(
        "task-safety",
        1,
        registry.fixtures[0].fixture_digest,
        digest("prompt"),
        digest("hidden-key"),
        grader.grader_digest,
        plan.repetitions,
        10,
        task_family=BenchmarkTaskFamily.SAFETY_POLICY,
        scoring_dimensions=("correctness",),
    )
    with pytest.raises(PolicyViolation, match="dimensions drift"):
        lab.register_contracts(task, grader, plan=plan, suite=suite)
    with pytest.raises(ValidationFailed, match="family"):
        replace(task, task_family="safety-policy")  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed, match="dimensions"):
        replace(task, scoring_dimensions=("unknown",))


def test_durable_local_execution_replay_has_zero_additional_calls_and_restart(
    tmp_path: Path,
) -> None:
    registry, suite, plan = _contracts()
    lab = _lab(tmp_path)
    _bind(lab, registry, suite, plan)
    process = Path(__file__).parents[1] / "fixtures" / "local_benchmark_process.py"
    calls: list[str] = []

    def audit(phase: str, _identity: str, _request: str) -> None:
        calls.append(phase)

    oracle = DeterministicLocalBenchmarkAdapter(default_fixture_file().parent)
    adapter = LocalProcessBenchmarkAdapter(
        "tested-model",
        (sys.executable, str(process)),
        oracle,
        audit,
        artifact_sink=lab,
    )
    verifier = LocalProcessBenchmarkVerifier(
        VerifierIdentity("independent-model", "local:verifier", digest("verifier")),
        (sys.executable, str(process)),
        audit,
        artifact_sink=lab,
    )
    service = BenchmarkExecutionService(lab, registry)
    plan_id, trials = service.execute(
        suite=suite,
        plan=plan,
        adapter=adapter,
        verifier_adapter=verifier,
        claims=lab,
    )
    assert len(trials) == 5
    assert calls == [item for _ in range(5) for item in ("tested", "verifier")]
    before = tuple(calls)
    reopened = SQLiteLocalBenchmarkLab(lab.path, lab.artifact_root)
    replay_id, replay = BenchmarkExecutionService(reopened, registry).execute(
        suite=suite,
        plan=plan,
        adapter=adapter,
        verifier_adapter=verifier,
        claims=reopened,
    )
    assert replay_id == plan_id
    assert replay == trials
    assert tuple(calls) == before
    aggregate = BenchmarkExecutionService(reopened, registry).aggregate(
        plan_id,
        plan=plan,
        suite=suite,
        tested_model_id="tested-model",
        verifier=verifier.verifier,
    )
    assert aggregate.approved
    raw_digest, normalized_digest = reopened.artifact_pair(trials[0].response_digest)
    raw_bytes = (reopened.artifact_root / raw_digest.removeprefix("sha256:")).read_bytes()
    normalized_bytes = (
        reopened.artifact_root / normalized_digest.removeprefix("sha256:")
    ).read_bytes()
    assert b"zekam-benchmark-tested-result/v1" in raw_bytes
    assert b"zekam-benchmark-tested-result/v1" not in normalized_bytes
    assert raw_bytes != normalized_bytes
    assert reopened.counts() == {
        "benchmark_plan": 1,
        "call_claim": 10,
        "call_receipt": 10,
        "benchmark_trial": 5,
        "benchmark_failure": 0,
        "benchmark_aggregate": 1,
        "artifact": 3,
        "artifact_pair": 1,
    }


def test_task_grader_hidden_key_is_digest_only_and_dry_run_never_calls(tmp_path: Path) -> None:
    registry, suite, plan = _contracts()
    grader = LocalGraderContract("exact-json", 1, digest("implementation"), ("correctness",))
    task = LocalBenchmarkTask(
        "task-one",
        1,
        registry.fixtures[0].fixture_digest,
        digest("prompt"),
        digest("HIDDEN-ANSWER-MATERIAL"),
        grader.grader_digest,
        5,
        10,
    )
    lab = _lab(tmp_path)
    lab.register_contracts(task, grader, plan=plan, suite=suite)
    report = dry_run(plan, suite, max_calls=10)
    assert report.call_count == 10
    assert report.report_digest.startswith("sha256:")
    content = lab.path.read_bytes()
    assert b"HIDDEN-ANSWER-MATERIAL" not in content
    with pytest.raises(PolicyViolation, match="budget"):
        dry_run(plan, suite, max_calls=9)


def test_plan_requires_exact_task_prompt_hidden_key_and_grader_binding(tmp_path: Path) -> None:
    registry, suite, plan = _contracts()
    lab = _lab(tmp_path)
    with pytest.raises(PolicyViolation, match="binding missing"):
        lab.ensure_plan(registry=registry, suite=suite, plan=plan)
    _bind(lab, registry, suite, plan)
    grader = LocalGraderContract("exact-json", 1, digest("implementation"), ("correctness",))
    drift = LocalBenchmarkTask(
        "task-one",
        1,
        registry.fixtures[0].fixture_digest,
        digest("different-prompt"),
        digest("hidden-key"),
        grader.grader_digest,
        plan.repetitions,
        10,
    )
    with pytest.raises(ConcurrencyConflict, match="replay drift"):
        lab.register_contracts(drift, grader, plan=plan, suite=suite)


@pytest.mark.parametrize(
    "field,value",
    [
        ("repetition", "1"),
        ("parse_ok", 1),
        ("quality", "A"),
        ("quality", 1),
        ("quality", float("nan")),
        ("latency_ms", 1.0),
    ],
)
def test_trial_mapping_rejects_coercible_or_nonfinite_values(field: str, value: object) -> None:
    row = {
        "fixture_digest": digest("fixture"),
        "repetition": 1,
        "status": "passed",
        "parse_ok": True,
        "format_ok": True,
        "evidence_ok": True,
        "verifier_approved": True,
        "quality": 0.8,
        "reliability": 0.9,
        "latency_ms": 2,
        "input_tokens": 3,
        "output_tokens": 1,
        "estimated_cost": 0.0,
        "actual_cost": 0.0,
        "response_digest": digest("response"),
        "evidence_digest": digest("evidence"),
    }
    row[field] = value
    with pytest.raises((ValidationFailed, ValueError, TypeError)):
        trial_from_mapping(row)


def test_domain_trial_and_verifier_reject_integer_decision_scores() -> None:
    base = TrialResult(
        fixture_digest=digest("fixture"),
        repetition=1,
        status=TrialStatus.PASSED,
        parse_ok=True,
        format_ok=True,
        evidence_ok=True,
        verifier_approved=True,
        quality=0.8,
        reliability=0.9,
        latency_ms=1,
        input_tokens=1,
        output_tokens=1,
        retry_count=0,
        human_corrections=0,
        estimated_cost=0.0,
        actual_cost=0.0,
        response_digest=digest("response"),
        evidence_digest=digest("evidence"),
    )
    with pytest.raises(ValidationFailed, match="exact float"):
        replace(base, quality=1)
    with pytest.raises(ValidationFailed, match="exact float"):
        replace(base, reliability=1)
    with pytest.raises(ValidationFailed, match="exact bool"):
        VerifierVerdict(
            "tested",
            "verifier",
            "local:verifier",
            digest("response"),
            1,  # type: ignore[arg-type]
            digest("evidence"),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("estimated_cost", 0),
        ("estimated_cost", True),
        ("estimated_cost", float("nan")),
        ("estimated_cost", float("inf")),
        ("estimated_cost", -0.01),
        ("actual_cost", 0),
        ("actual_cost", False),
        ("actual_cost", float("nan")),
        ("actual_cost", float("inf")),
        ("actual_cost", -0.01),
    ],
)
def test_domain_trial_rejects_non_exact_nonfinite_or_negative_cost(
    field: str, value: object
) -> None:
    base = TrialResult(
        fixture_digest=digest("fixture"),
        repetition=1,
        status=TrialStatus.PASSED,
        parse_ok=True,
        format_ok=True,
        evidence_ok=True,
        verifier_approved=True,
        quality=0.8,
        reliability=0.9,
        latency_ms=1,
        input_tokens=1,
        output_tokens=1,
        retry_count=0,
        human_corrections=0,
        estimated_cost=0.0,
        actual_cost=None,
        response_digest=digest("response"),
        evidence_digest=digest("evidence"),
    )
    with pytest.raises(ValidationFailed):
        replace(base, **{field: value})  # type: ignore[arg-type]
    assert replace(base, actual_cost=0.0).cost == 0.0
    assert replace(base, actual_cost=None).cost == 0.0


def test_failure_is_retained_and_pending_claim_is_not_reissued(tmp_path: Path) -> None:
    registry, suite, plan = _contracts()
    lab = _lab(tmp_path)
    _bind(lab, registry, suite, plan)
    plan_id, _ = lab.ensure_plan(registry=registry, suite=suite, plan=plan)
    fixture = registry.fixtures[0]
    claim = lab.claim_tested(plan=plan, fixture=fixture, repetition=1)
    failure = lab.retain_failure(
        plan_id=plan_id,
        claim_id=claim,
        fixture_digest=fixture.fixture_digest,
        repetition=1,
        phase="tested",
        category="timeout",
    )
    assert failure.startswith("sha256:")
    with pytest.raises(PolicyViolation, match="cannot be reissued"):
        lab.claim_tested(plan=plan, fixture=fixture, repetition=1)
    assert lab.counts()["benchmark_failure"] == 1


def test_partial_claim_survives_restart_and_cannot_be_reissued(tmp_path: Path) -> None:
    registry, suite, plan = _contracts()
    lab = _lab(tmp_path)
    _bind(lab, registry, suite, plan)
    lab.ensure_plan(registry=registry, suite=suite, plan=plan)
    fixture = registry.fixtures[0]
    lab.claim_tested(plan=plan, fixture=fixture, repetition=1)
    reopened = SQLiteLocalBenchmarkLab(lab.path, lab.artifact_root)
    with pytest.raises(PolicyViolation, match="cannot be reissued"):
        reopened.claim_tested(plan=plan, fixture=fixture, repetition=1)
    assert reopened.counts()["call_claim"] == 1
    assert reopened.counts()["call_receipt"] == 0


def test_adapter_timeout_is_terminalized_as_durable_failed_trial(tmp_path: Path) -> None:
    registry, suite, plan = _contracts()
    lab = _lab(tmp_path)
    _bind(lab, registry, suite, plan)
    plan_id, _ = lab.ensure_plan(registry=registry, suite=suite, plan=plan)

    class TimeoutAdapter:
        execution_mode = "local"
        model_id = "tested-model"

        def __init__(self) -> None:
            self.calls = 0

        def invoke(
            self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, repetition: int
        ) -> TrialResult:
            del plan, fixture, repetition
            self.calls += 1
            raise subprocess.TimeoutExpired(("local-fake",), 1)

    class UnusedVerifier:
        execution_mode = "local"
        verifier = VerifierIdentity("independent-model", "local:verifier", digest("verifier"))

        def verify(
            self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, result: TrialResult
        ) -> VerifierVerdict:
            del plan, fixture, result
            raise AssertionError("verifier must not run after tested timeout")

    adapter = TimeoutAdapter()
    service = BenchmarkExecutionService(lab, registry)
    for _ in range(5):
        with pytest.raises(PolicyViolation, match="failure retained"):
            service.execute(
                suite=suite,
                plan=plan,
                adapter=adapter,
                verifier_adapter=UnusedVerifier(),
                claims=lab,
            )
    retained = lab.list_trials(plan_id)
    assert len(retained) == 5
    assert all(item.status is TrialStatus.TIMEOUT for item in retained)
    assert all(item.failure_category == "timeout" for item in retained)
    assert lab.counts()["benchmark_failure"] == 5
    assert adapter.calls == 5
    _, replay = service.execute(
        suite=suite,
        plan=plan,
        adapter=adapter,
        verifier_adapter=UnusedVerifier(),
        claims=lab,
    )
    assert len(replay) == 5
    assert adapter.calls == 5


def test_verifier_failure_is_terminalized_and_not_replayed(tmp_path: Path) -> None:
    registry, suite, plan = _contracts()
    lab = _lab(tmp_path)
    _bind(lab, registry, suite, plan)
    process = Path(__file__).parents[1] / "fixtures" / "local_benchmark_process.py"
    tested_calls: list[str] = []
    adapter = LocalProcessBenchmarkAdapter(
        "tested-model",
        (sys.executable, str(process)),
        DeterministicLocalBenchmarkAdapter(default_fixture_file().parent),
        lambda phase, _identity, _request: tested_calls.append(phase),
        artifact_sink=lab,
    )

    class FailedVerifier:
        execution_mode = "local"
        verifier = VerifierIdentity("independent-model", "local:verifier", digest("verifier"))

        def __init__(self) -> None:
            self.calls = 0

        def verify(
            self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, result: TrialResult
        ) -> VerifierVerdict:
            del plan, fixture, result
            self.calls += 1
            raise ValidationFailed("malformed verifier output")

    verifier = FailedVerifier()
    service = BenchmarkExecutionService(lab, registry)
    for _ in range(5):
        with pytest.raises(PolicyViolation, match="failure retained"):
            service.execute(
                suite=suite,
                plan=plan,
                adapter=adapter,
                verifier_adapter=verifier,
                claims=lab,
            )
    calls = tuple(tested_calls)
    assert calls == ("tested",) * 5
    assert verifier.calls == 5
    _, replay = service.execute(
        suite=suite,
        plan=plan,
        adapter=adapter,
        verifier_adapter=verifier,
        claims=lab,
    )
    assert len(replay) == 5
    assert tuple(tested_calls) == calls
    assert verifier.calls == 5
    assert lab.counts()["benchmark_failure"] == 5


def test_wrong_fixture_result_terminalizes_first_claim_and_trial(tmp_path: Path) -> None:
    registry, suite, plan = _contracts()
    lab = _lab(tmp_path)
    _bind(lab, registry, suite, plan)
    process = Path(__file__).parents[1] / "fixtures" / "local_benchmark_process.py"
    base = LocalProcessBenchmarkAdapter(
        "tested-model",
        (sys.executable, str(process)),
        DeterministicLocalBenchmarkAdapter(default_fixture_file().parent),
        lambda *_: None,
        artifact_sink=lab,
    )

    class WrongFixtureAdapter:
        execution_mode = "local"
        model_id = "tested-model"

        def invoke(
            self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, repetition: int
        ) -> TrialResult:
            return replace(
                base.invoke(plan=plan, fixture=fixture, repetition=repetition),
                fixture_digest=digest("wrong-fixture"),
            )

    class UnusedVerifier:
        execution_mode = "local"
        verifier = VerifierIdentity("independent-model", "local:verifier", digest("verifier"))

        def verify(
            self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, result: TrialResult
        ) -> VerifierVerdict:
            raise AssertionError((plan, fixture, result))

    with pytest.raises(PolicyViolation, match="failure retained"):
        BenchmarkExecutionService(lab, registry).execute(
            suite=suite,
            plan=plan,
            adapter=WrongFixtureAdapter(),
            verifier_adapter=UnusedVerifier(),
            claims=lab,
        )
    plan_id = lab.ensure_plan(registry=registry, suite=suite, plan=plan)[0]
    assert len(lab.list_trials(plan_id)) == 1
    assert lab.counts()["call_claim"] == 1
    assert lab.counts()["call_receipt"] == 1
    assert lab.counts()["benchmark_failure"] == 1
    with sqlite3.connect(lab.path) as db:
        assert db.execute("select status from call_receipt").fetchone() == ("failed",)


def test_blind_pair_hides_model_ids_and_scores_require_real_float() -> None:
    packet = blind_pair(
        digest("plan"),
        "model-one",
        digest("left"),
        "model-two",
        digest("right"),
    )
    assert {alias for alias, _ in packet.aliases} == {"A", "B"}
    assert "model-one" not in repr(packet.aliases)
    assert packet.mapping_digest.startswith("sha256:")
    assert parse_score(0.5) == 0.5
    for invalid in ("A", 1, True, -0.1, 1.1, float("inf"), float("nan")):
        with pytest.raises(ValidationFailed):
            parse_score(invalid)


def test_artifacts_are_immutable_bounded_and_secret_free(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    payload = canonical_bytes({"answer": "safe"})
    value = lab.store_artifact("raw", payload)
    assert lab.store_artifact("raw", payload) == value
    assert lab.store_artifact("normalized", payload) == value
    assert lab.store_artifact("fixed", canonical_bytes({"answer": "fixed"})).startswith("sha256:")
    artifact = lab.artifact_root / value.removeprefix("sha256:")
    assert artifact.read_bytes() == payload
    with pytest.raises(PolicyViolation, match="sensitive"):
        lab.store_artifact("raw", b'{"answer":"Bearer abcdefgh"}')
    with pytest.raises(ValidationFailed):
        lab.store_artifact("raw", b"x" * (1_048_576 + 1))


@pytest.mark.parametrize(
    "source,error",
    [
        ('print(\'{"a":1,"a":2}\')', ValidationFailed),
        ("print('{\"score\":NaN}')", ValidationFailed),
        ('print("x" * 1000001)', PolicyViolation),
        ('import sys; sys.stderr.write("x" * 16385); print("{}")', PolicyViolation),
    ],
)
def test_local_process_rejects_duplicate_nonfinite_or_oversize_output(
    tmp_path: Path, source: str, error: type[Exception]
) -> None:
    script = tmp_path / "fake.py"
    script.write_text(source, encoding="utf-8")
    with pytest.raises(error):
        _run_json_process((sys.executable, str(script)), {"request": "safe"}, 2)


def test_local_process_timeout_is_bounded_and_sanitized(tmp_path: Path) -> None:
    script = tmp_path / "slow.py"
    script.write_text("import time; time.sleep(3)", encoding="utf-8")
    with pytest.raises(PolicyViolation, match=r"could not|calistirilamadi"):
        _run_json_process((sys.executable, str(script)), {"request": "safe"}, 1)


def test_concurrent_plan_prepare_is_one_graph_and_append_only(tmp_path: Path) -> None:
    registry, suite, plan = _contracts()
    lab = _lab(tmp_path)
    _bind(lab, registry, suite, plan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda _: lab.ensure_plan(registry=registry, suite=suite, plan=plan),
                range(2),
            )
        )
    assert {created for _, created in results} == {False, True}
    with sqlite3.connect(lab.path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("delete from benchmark_plan")
    with sqlite3.connect(lab.path) as db:
        db.execute("drop trigger benchmark_plan_no_delete")
    with pytest.raises(PolicyViolation, match="schema drift"):
        lab.counts()
