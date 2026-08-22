"""Claim-before-adapter ve duplicate-cost benchmark execution host testleri."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from zekam.application.model_benchmark_service import (
    BenchmarkExecutionService,
    DeterministicLocalBenchmarkAdapter,
    LocalProcessBenchmarkAdapter,
    LocalProcessBenchmarkVerifier,
    default_fixture_file,
    load_fixture_registry,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_benchmark import (
    BenchmarkAggregate,
    BenchmarkFixture,
    BenchmarkPlan,
    BenchmarkSuite,
    SuiteKind,
    TrialResult,
    TrialStatus,
    VerifierIdentity,
    VerifierVerdict,
)

pytestmark = pytest.mark.integration


class FakeGateway:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.completed: set[UUID] = set()

    def claim_tested(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, repetition: int
    ) -> UUID:
        claim_id = uuid4()
        self.events.append(f"claim:{repetition}")
        return claim_id

    def complete_tested(self, *, claim_id: UUID, result: TrialResult) -> None:
        self.events.append(f"receipt:{result.repetition}")
        self.completed.add(claim_id)

    def claim_verifier(
        self,
        *,
        plan: BenchmarkPlan,
        fixture: BenchmarkFixture,
        result: TrialResult,
        verifier: VerifierIdentity,
    ) -> UUID:
        claim_id = uuid4()
        self.events.append(f"verifier-claim:{result.repetition}")
        return claim_id

    def complete_verifier(self, *, claim_id: UUID, verdict: VerifierVerdict) -> None:
        self.events.append("verifier-receipt")
        self.completed.add(claim_id)


class FakeStore:
    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway
        self.plan_id = uuid4()
        self.trials: dict[tuple[str, int], TrialResult] = {}

    def ensure_plan(
        self, *, registry: object, suite: BenchmarkSuite, plan: BenchmarkPlan
    ) -> tuple[UUID, bool]:
        return self.plan_id, not self.trials

    def list_trials(self, plan_id: UUID) -> tuple[TrialResult, ...]:
        return tuple(self.trials.values())

    def trial_receipt_matches(
        self,
        *,
        plan_id: UUID,
        tested_claim_id: UUID,
        verifier_claim_id: UUID,
        verdict: VerifierVerdict,
        result: TrialResult,
    ) -> bool:
        return (
            plan_id == self.plan_id
            and tested_claim_id in self.gateway.completed
            and verifier_claim_id in self.gateway.completed
        )

    def record_trial(
        self,
        *,
        plan_id: UUID,
        tested_claim_id: UUID,
        verifier_claim_id: UUID,
        verdict: VerifierVerdict,
        result: TrialResult,
        observed_at: dt.datetime | None = None,
    ) -> tuple[UUID, bool]:
        key = (result.fixture_digest, result.repetition)
        created = key not in self.trials
        self.trials.setdefault(key, result)
        return uuid4(), created

    def store_aggregate(self, *, plan_id: UUID, aggregate: BenchmarkAggregate) -> UUID:
        return uuid4()


class FakeAdapter:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    @property
    def execution_mode(self) -> str:
        return "local"

    @property
    def model_id(self) -> str:
        return "model"

    def invoke(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, repetition: int
    ) -> TrialResult:
        fixture_digest = fixture.fixture_digest
        assert self.events[-1] == f"claim:{repetition}"
        self.events.append(f"adapter:{repetition}")
        self.calls += 1
        return TrialResult(
            fixture_digest=fixture_digest,
            repetition=repetition,
            status=TrialStatus.PASSED,
            parse_ok=True,
            format_ok=True,
            evidence_ok=True,
            verifier_approved=False,
            quality=0.9,
            reliability=0.9,
            latency_ms=10,
            input_tokens=10,
            output_tokens=5,
            retry_count=0,
            human_corrections=0,
            estimated_cost=0.01,
            actual_cost=0.01,
            response_digest=digest({"response": repetition}),
            evidence_digest=digest({"evidence": repetition}),
        )


class FakeVerifier:
    verifier = VerifierIdentity("verifier", "worker:verifier", digest("verifier"))
    execution_mode = "local"

    def verify(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, result: TrialResult
    ) -> VerifierVerdict:
        return VerifierVerdict(
            plan.model_id,
            self.verifier.model_id,
            self.verifier.execution_identity,
            result.response_digest,
            True,
            digest({"verified": result.response_digest}),
        )


def test_claim_precedes_adapter_and_duplicate_plan_has_no_second_provider_cost() -> None:
    registry = load_fixture_registry()
    fixture = registry.eligible(remote=True)[0]
    suite = BenchmarkSuite("general-one", 1, SuiteKind.GENERAL, (fixture.fixture_digest,))
    plan = BenchmarkPlan(
        "model",
        suite.suite_digest,
        digest("inventory"),
        digest("policy"),
        registry.registry_digest,
    )
    gateway = FakeGateway()
    store = FakeStore(gateway)
    adapter = FakeAdapter(gateway.events)
    service = BenchmarkExecutionService(store, registry)

    service.execute(
        suite=suite,
        plan=plan,
        adapter=adapter,
        verifier_adapter=FakeVerifier(),
        claims=gateway,
    )
    assert adapter.calls == 5
    service.execute(
        suite=suite,
        plan=plan,
        adapter=adapter,
        verifier_adapter=FakeVerifier(),
        claims=gateway,
    )
    assert adapter.calls == 5


def test_remote_execution_rejects_local_only_fixture_before_claim() -> None:
    registry = load_fixture_registry()
    fixture = next(item for item in registry.fixtures if item not in registry.eligible(remote=True))
    suite = BenchmarkSuite("local-only", 1, SuiteKind.GENERAL, (fixture.fixture_digest,))
    plan = BenchmarkPlan(
        "model",
        suite.suite_digest,
        digest("inventory"),
        digest("policy"),
        registry.registry_digest,
        remote_execution=True,
    )
    gateway = FakeGateway()
    service = BenchmarkExecutionService(FakeStore(gateway), registry)
    with pytest.raises(PolicyViolation, match="Local-only"):
        service.execute(
            suite=suite,
            plan=plan,
            adapter=FakeAdapter(gateway.events),
            verifier_adapter=FakeVerifier(),
            claims=gateway,
        )
    assert gateway.events == []


def test_production_local_adapter_executes_shipped_secret_free_artifact() -> None:
    registry = load_fixture_registry()
    fixture = registry.fixtures[0]
    suite = BenchmarkSuite("local", 1, SuiteKind.GENERAL, (fixture.fixture_digest,))
    plan = BenchmarkPlan(
        "model",
        suite.suite_digest,
        digest("inventory"),
        digest("policy"),
        registry.registry_digest,
    )
    process = Path(__file__).parents[1] / "fixtures" / "local_benchmark_process.py"
    oracle = DeterministicLocalBenchmarkAdapter(default_fixture_file().parent)
    adapter = LocalProcessBenchmarkAdapter("model", (sys.executable, str(process)), oracle)
    verifier = LocalProcessBenchmarkVerifier(
        VerifierIdentity("verifier", "process:verifier", digest("verifier-process")),
        (sys.executable, str(process)),
    )
    result = adapter.invoke(plan=plan, fixture=fixture, repetition=1)
    verdict = verifier.verify(plan=plan, fixture=fixture, result=result)
    assert not result.verifier_approved
    assert verdict.approved
    assert result.actual_cost == 0


def test_verifier_rejects_self_approval_and_canned_response_binding() -> None:
    registry = load_fixture_registry()
    fixture = registry.fixtures[0]
    suite = BenchmarkSuite("local", 1, SuiteKind.GENERAL, (fixture.fixture_digest,))
    plan = BenchmarkPlan(
        "model",
        suite.suite_digest,
        digest("inventory"),
        digest("policy"),
        registry.registry_digest,
    )
    process = Path(__file__).parents[1] / "fixtures" / "local_benchmark_process.py"
    oracle = DeterministicLocalBenchmarkAdapter(default_fixture_file().parent)
    result = LocalProcessBenchmarkAdapter("model", (sys.executable, str(process)), oracle).invoke(
        plan=plan, fixture=fixture, repetition=1
    )
    self_verifier = LocalProcessBenchmarkVerifier(
        VerifierIdentity("model", "process:self", digest("self")),
        (sys.executable, str(process)),
    )
    with pytest.raises(PolicyViolation, match="kendi verifier"):
        self_verifier.verify(plan=plan, fixture=fixture, result=result)
    stale_verifier = LocalProcessBenchmarkVerifier(
        VerifierIdentity("verifier", "process:verifier", digest("verifier")),
        (sys.executable, str(process), "--stale"),
    )
    with pytest.raises(PolicyViolation, match="binding drift"):
        stale_verifier.verify(plan=plan, fixture=fixture, result=result)


def test_aggregate_requires_exact_repetitions_for_every_suite_fixture() -> None:
    registry = load_fixture_registry()
    fixtures = registry.fixtures[:2]
    suite = BenchmarkSuite(
        "two-fixtures", 1, SuiteKind.GENERAL, tuple(item.fixture_digest for item in fixtures)
    )
    plan = BenchmarkPlan(
        "model",
        suite.suite_digest,
        digest("inventory"),
        digest("policy"),
        registry.registry_digest,
    )
    gateway = FakeGateway()
    store = FakeStore(gateway)
    service = BenchmarkExecutionService(store, registry)
    service.execute(
        suite=suite,
        plan=plan,
        adapter=FakeAdapter(gateway.events),
        verifier_adapter=FakeVerifier(),
        claims=gateway,
    )
    store.trials = {
        key: value for key, value in store.trials.items() if key[0] == fixtures[0].fixture_digest
    }
    with pytest.raises(PolicyViolation, match="exact repetition"):
        service.aggregate(
            store.plan_id,
            plan=plan,
            suite=suite,
            tested_model_id="model",
            verifier=VerifierIdentity("verifier", "worker:verifier", digest("provenance")),
        )
