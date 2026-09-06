"""Provider-free native benchmark campaign composition.

This module exercises Zekam's real append-only benchmark ledger on every
supported desktop platform.  It is deliberately a pipeline acceptance
campaign: it validates plan/claim/receipt/artifact/trial/aggregate wiring and
must never be presented as production model qualification evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.application.model_benchmark_service import (
    BenchmarkExecutionService,
    DeterministicLocalBenchmarkAdapter,
    default_fixture_file,
    load_fixture_registry,
)
from zekam.application.portable_benchmark import inspect_portable_benchmark
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import (
    BenchmarkFixture,
    BenchmarkPlan,
    BenchmarkSuite,
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
    dry_run,
)

NATIVE_MODEL_ID = "zekam-deterministic-mock-v1"
NATIVE_VERIFIER_ID = "zekam-independent-contract-verifier-v1"
NATIVE_FIXTURE_ID = "general-chat-json-tr"
NATIVE_SUITE_ID = "native-pipeline-acceptance"


def _implementation_bundle_digest() -> str:
    """Bind the plan to the complete in-process execution/ledger closure."""

    package_root = Path(__file__).resolve().parents[1]
    sources = (
        Path(__file__).resolve(),
        package_root / "application" / "model_benchmark_service.py",
        package_root / "application" / "portable_benchmark.py",
        package_root / "domain" / "model_benchmark.py",
        package_root / "infrastructure" / "sqlite" / "local_model_benchmark.py",
    )
    if any(not source.is_file() or source.is_symlink() for source in sources):
        raise PolicyViolation("Native benchmark implementation bundle identity invalid")
    return digest(
        {
            "schema": "zekam-native-benchmark-implementation-bundle/v1",
            "sources": [
                {
                    "logical_name": source.relative_to(package_root).as_posix(),
                    "digest": digest_of_bytes(source.read_bytes()),
                }
                for source in sources
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class NativeCampaignContracts:
    registry: FixtureRegistry
    fixture: BenchmarkFixture
    suite: BenchmarkSuite
    plan: BenchmarkPlan
    task: LocalBenchmarkTask
    grader: LocalGraderContract
    verifier: VerifierIdentity
    implementation_source_digest: str
    portable_inspection: dict[str, Any] | None = None

    def plan_document(self) -> dict[str, Any]:
        budget = dry_run(self.plan, self.suite, max_calls=self.plan.repetitions * 2)
        portable = self.portable_inspection
        return {
            "schema": "zekam-native-benchmark-campaign-plan/v1",
            "campaign_kind": "pipeline-acceptance",
            "execution_mode": "provider-free-local-mock",
            "plan_digest": self.plan.plan_digest,
            "suite_digest": self.suite.suite_digest,
            "fixture_registry_digest": self.registry.registry_digest,
            "implementation_source_digest": self.implementation_source_digest,
            "implementation_source_count": 5,
            "model_id": self.plan.model_id,
            "verifier_model_id": self.verifier.model_id,
            "model_count": 1,
            "fixture_count": 1,
            "repetitions": self.plan.repetitions,
            "trial_count": budget.trial_count,
            "exact_call_budget": budget.call_count,
            "tested_calls": budget.trial_count,
            "verifier_calls": budget.trial_count,
            "provider_calls": 0,
            "audio_models_excluded": (
                0
                if portable is None
                else portable["models"]["endpoint_type_counts"].get("audio", 0)
            ),
            "portable_source": (
                None
                if portable is None
                else {
                    "inspection_digest": portable["inspection_digest"],
                    "source_digest": portable["source_digest"],
                    "model_count": portable["models"]["total"],
                    "real_model_count": portable["models"]["real"],
                    "mock_model_count": portable["models"]["mock"],
                    "task_count": portable["tasks"]["total"],
                    "suite_count": portable["suite_count"],
                    "release_gate_count": portable["release_gate_count"],
                    "design_input_only": True,
                    "executed": False,
                }
            ),
            "foreign_code_execution": False,
            "qualifies_production_models": False,
            "requires_apply": True,
            "requires_exact_plan_digest": True,
            "grants_authority": False,
        }


def build_native_campaign(
    *, repetitions: int = 5, portable_root: Path | None = None
) -> NativeCampaignContracts:
    """Build the stable, secret-free native pipeline acceptance campaign."""

    registry = load_fixture_registry()
    implementation_source_digest = _implementation_bundle_digest()
    portable = None if portable_root is None else inspect_portable_benchmark(portable_root)
    try:
        fixture = next(item for item in registry.fixtures if item.case_id == NATIVE_FIXTURE_ID)
    except StopIteration as exc:
        raise ValidationFailed("Native benchmark fixture registry entry missing") from exc
    suite = BenchmarkSuite(
        suite_id=NATIVE_SUITE_ID,
        version=1,
        kind=SuiteKind.GENERAL,
        fixture_digests=(fixture.fixture_digest,),
    )
    grader = LocalGraderContract(
        grader_id="native-contract-verifier",
        version=1,
        implementation_digest=implementation_source_digest,
        dimensions=("correctness",),
    )
    task = LocalBenchmarkTask(
        task_id="native-pipeline-acceptance",
        version=1,
        fixture_digest=fixture.fixture_digest,
        prompt_digest=digest(
            {"contract": "zekam-native-mock-response/v1", "fixture": fixture.fixture_digest}
        ),
        hidden_key_digest=digest(
            {"contract": "digest-only-no-hidden-material", "fixture": fixture.fixture_digest}
        ),
        grader_digest=grader.grader_digest,
        repetitions=repetitions,
        timeout_seconds=10,
        scoring_dimensions=grader.dimensions,
    )
    plan = BenchmarkPlan(
        model_id=NATIVE_MODEL_ID,
        suite_digest=suite.suite_digest,
        inventory_digest=digest(
            {
                "schema": "zekam-native-benchmark-inventory/v1",
                "tested": NATIVE_MODEL_ID,
                "verifier": NATIVE_VERIFIER_ID,
                "implementation_source_digest": implementation_source_digest,
                "portable_source_digest": (None if portable is None else portable["source_digest"]),
                "portable_inspection_digest": (
                    None if portable is None else portable["inspection_digest"]
                ),
            }
        ),
        policy_digest=digest(
            {
                "schema": "zekam-native-benchmark-policy/v1",
                "provider_calls": 0,
                "foreign_code_execution": False,
                "qualification": False,
                "implementation_source_digest": implementation_source_digest,
                "portable_policy": None if portable is None else portable["policy"],
            }
        ),
        fixture_registry_digest=registry.registry_digest,
        repetitions=repetitions,
        remote_execution=False,
    )
    verifier = VerifierIdentity(
        model_id=NATIVE_VERIFIER_ID,
        execution_identity="local:in-process-contract-v1",
        provenance_digest=grader.implementation_digest,
    )
    return NativeCampaignContracts(
        registry,
        fixture,
        suite,
        plan,
        task,
        grader,
        verifier,
        implementation_source_digest,
        portable,
    )


@dataclass(frozen=True, slots=True)
class NativeMockBenchmarkAdapter:
    """Deterministic in-process tested adapter; never invokes a model/provider."""

    oracle: DeterministicLocalBenchmarkAdapter
    artifacts: SQLiteLocalBenchmarkLab

    @property
    def execution_mode(self) -> str:
        return "local"

    @property
    def model_id(self) -> str:
        return NATIVE_MODEL_ID

    def invoke(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, repetition: int
    ) -> TrialResult:
        if plan.model_id != self.model_id or plan.remote_execution:
            raise PolicyViolation("Native mock adapter exact local plan ister")
        source = self.oracle.load(fixture)
        metrics = source.get("metrics")
        if not isinstance(metrics, dict):
            raise ValidationFailed("Native mock fixture metrics missing")
        response = {
            "schema": "zekam-native-mock-response/v1",
            "plan_digest": plan.plan_digest,
            "case_id": fixture.case_id,
            "repetition": repetition,
            "result": "contract-ok",
        }
        normalized = canonical_bytes(response)
        raw = canonical_bytes(
            {
                "schema": "zekam-benchmark-tested-result/v1",
                "model_id": self.model_id,
                "response": response,
                "pipeline_acceptance_only": True,
            }
        )
        raw_digest = self.artifacts.store_artifact("raw", raw)
        response_digest = self.artifacts.store_artifact("normalized", normalized)
        self.artifacts.bind_artifacts(
            response_digest=response_digest,
            raw_digest=raw_digest,
            normalized_digest=response_digest,
        )
        return TrialResult(
            fixture_digest=fixture.fixture_digest,
            repetition=repetition,
            status=TrialStatus.PASSED,
            parse_ok=True,
            format_ok=True,
            evidence_ok=True,
            verifier_approved=False,
            quality=float(metrics["quality"]),
            reliability=float(metrics["reliability"]),
            latency_ms=int(metrics["latency_ms"]),
            input_tokens=int(metrics["input_tokens"]),
            output_tokens=int(metrics["output_tokens"]),
            retry_count=0,
            human_corrections=0,
            estimated_cost=0.0,
            actual_cost=0.0,
            response_digest=response_digest,
            evidence_digest=digest(
                {
                    "adapter": "native-mock-tested/v1",
                    "fixture": fixture.fixture_digest,
                    "repetition": repetition,
                    "response": response_digest,
                }
            ),
            tool_correctness=1.0,
            recovery=1.0,
        )


@dataclass(frozen=True, slots=True)
class NativeContractVerifier:
    """Independent deterministic contract verifier for the mock pipeline."""

    identity: VerifierIdentity
    artifacts: SQLiteLocalBenchmarkLab

    @property
    def execution_mode(self) -> str:
        return "local"

    @property
    def verifier(self) -> VerifierIdentity:
        return self.identity

    def verify(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, result: TrialResult
    ) -> VerifierVerdict:
        approved = (
            plan.model_id != self.verifier.model_id
            and result.fixture_digest == fixture.fixture_digest
            and result.status is TrialStatus.PASSED
            and result.parse_ok
            and result.format_ok
            and result.evidence_ok
        )
        evidence_body = {
            "schema": "zekam-native-contract-verdict/v1",
            "plan_digest": plan.plan_digest,
            "tested_model_id": plan.model_id,
            "verifier_model_id": self.verifier.model_id,
            "fixture_digest": fixture.fixture_digest,
            "response_digest": result.response_digest,
            "approved": approved,
        }
        evidence_digest = self.artifacts.store_artifact("verifier", canonical_bytes(evidence_body))
        return VerifierVerdict(
            tested_model_id=plan.model_id,
            verifier_model_id=self.verifier.model_id,
            execution_identity=self.verifier.execution_identity,
            tested_response_digest=result.response_digest,
            approved=approved,
            evidence_digest=evidence_digest,
        )


def run_native_campaign(
    lab: SQLiteLocalBenchmarkLab, contracts: NativeCampaignContracts
) -> dict[str, Any]:
    """Execute or replay the exact native campaign and return durable evidence."""

    lab.register_contracts(
        contracts.task,
        contracts.grader,
        plan=contracts.plan,
        suite=contracts.suite,
    )
    before = lab.counts()
    oracle_root = default_fixture_file().parent
    adapter = NativeMockBenchmarkAdapter(DeterministicLocalBenchmarkAdapter(oracle_root), lab)
    verifier = NativeContractVerifier(contracts.verifier, lab)
    service = BenchmarkExecutionService(lab, contracts.registry)
    plan_id, trials = service.execute(
        suite=contracts.suite,
        plan=contracts.plan,
        adapter=adapter,
        verifier_adapter=verifier,
        claims=lab,
    )
    aggregate = service.aggregate(
        plan_id,
        plan=contracts.plan,
        suite=contracts.suite,
        tested_model_id=contracts.plan.model_id,
        verifier=contracts.verifier,
    )
    after = lab.counts()
    snapshot = lab.campaign_snapshot(contracts.plan.plan_digest)
    if snapshot is None or snapshot["aggregate_digest"] is None:
        raise PolicyViolation("Native campaign aggregate ledger evidence missing")
    return {
        "schema": "zekam-native-benchmark-campaign-run/v1",
        "campaign_kind": "pipeline-acceptance",
        "plan_id": str(plan_id),
        "plan_digest": contracts.plan.plan_digest,
        "state": "completed",
        "approved": aggregate.approved,
        "trial_count": len(trials),
        "new_claims": after["call_claim"] - before["call_claim"],
        "new_receipts": after["call_receipt"] - before["call_receipt"],
        "provider_calls": 0,
        "foreign_code_execution": False,
        "qualifies_production_models": False,
        "aggregate_digest": snapshot["aggregate_digest"],
        "aggregate_evidence_digest": aggregate.evidence_digest,
        "ledger_counts": after,
    }
