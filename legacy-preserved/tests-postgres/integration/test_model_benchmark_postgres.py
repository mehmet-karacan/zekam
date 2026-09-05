"""P07 model ledger PostgreSQL kabul testleri."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.config import DatabaseSettings
from zekam.application.execution import ExecutionHost
from zekam.application.model_benchmark_service import (
    BenchmarkExecutionService,
    load_fixture_registry,
)
from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_benchmark import (
    BenchmarkPlan,
    BenchmarkSuite,
    DecisionRequirements,
    SuiteKind,
    TrialResult,
    TrialStatus,
    VerifierIdentity,
    VerifierVerdict,
    benchmark_effect_digest,
    benchmark_verifier_effect_digest,
)
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import FailureCategory, Job, JobKind
from zekam.infrastructure.postgres.connection import connect
from zekam.infrastructure.postgres.model_benchmark_repository import BenchmarkRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_model_benchmark_tables_and_rls_exist(migrated_database: DatabaseSettings) -> None:
    expected = {
        "benchmark_suite",
        "benchmark_plan",
        "benchmark_trial",
        "benchmark_verifier_result",
        "benchmark_aggregate",
        "quota_observation",
        "model_quota_pool_binding",
        "model_decision",
        "runtime_observation",
        "deliberation_result",
    }
    with connect(migrated_database) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select tablename, rowsecurity from pg_tables where schemaname = 'models'"
            " and tablename = any(%s)",
            (list(expected),),
        )
        rows = cursor.fetchall()
    assert {row[0] for row in rows} == expected
    assert all(row[1] for row in rows)


def test_model_benchmark_constraints_are_database_enforced(
    migrated_database: DatabaseSettings,
) -> None:
    with connect(migrated_database) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select conname from pg_constraint where conname = any(%s)",
            (
                [
                    "benchmark_repetitions_minimum",
                    "benchmark_verifier_independent",
                    "quota_unknown_no_guess",
                    "model_decision_no_authority",
                    "deliberation_round_limit",
                    "deliberation_no_authority",
                    "benchmark_plan_suite_same_realm",
                    "benchmark_trial_plan_same_realm",
                    "benchmark_aggregate_plan_same_realm",
                ],
            ),
        )
        names = {row[0] for row in cursor.fetchall()}
    assert names == {
        "benchmark_repetitions_minimum",
        "benchmark_verifier_independent",
        "quota_unknown_no_guess",
        "model_decision_no_authority",
        "deliberation_round_limit",
        "deliberation_no_authority",
        "benchmark_plan_suite_same_realm",
        "benchmark_trial_plan_same_realm",
        "benchmark_aggregate_plan_same_realm",
    }


def test_trial_claim_trigger_checks_realm_effect_status_and_result(
    migrated_database: DatabaseSettings,
) -> None:
    with connect(migrated_database) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select pg_get_functiondef('models.enforce_benchmark_claim_realm()'::regprocedure)"
        )
        definition = str(cursor.fetchone()[0])
    for required in (
        "c.realm_id = new.realm_id",
        "c.operation = 'model-benchmark-tested'",
        "c.operation = 'model-benchmark-verifier'",
        "c.effect_digest",
        "r.status = 'completed'",
        "r.result_digest = new.response_digest",
    ):
        assert required in definition


def test_duplicate_benchmark_plan_reuses_durable_record_without_new_cost(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    registry = load_fixture_registry()
    suite = BenchmarkSuite(
        suite_id="general",
        version=1,
        kind=SuiteKind.GENERAL,
        fixture_digests=tuple(item.fixture_digest for item in registry.fixtures),
    )
    plan = BenchmarkPlan(
        model_id="model-idempotent",
        suite_digest=suite.suite_digest,
        inventory_digest=digest({"inventory": 1}),
        policy_digest=digest({"policy": 1}),
        fixture_registry_digest=registry.registry_digest,
    )
    repository = BenchmarkRepository(connection, realm.id)
    first_id, first_created = repository.ensure_plan(registry=registry, suite=suite, plan=plan)
    second_id, second_created = repository.ensure_plan(registry=registry, suite=suite, plan=plan)
    assert first_id == second_id
    assert first_created
    assert not second_created
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from models.benchmark_plan where plan_digest = %s",
            (plan.plan_digest,),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "select count(*) from models.benchmark_trial where plan_id = %s", (first_id,)
        )
        assert cursor.fetchone()[0] == 0


def _trial(fixture_digest: str, *, response_digest: str) -> TrialResult:
    return TrialResult(
        fixture_digest=fixture_digest,
        repetition=1,
        status=TrialStatus.PASSED,
        parse_ok=True,
        format_ok=True,
        evidence_ok=True,
        verifier_approved=True,
        quality=0.9,
        reliability=0.9,
        latency_ms=10,
        input_tokens=10,
        output_tokens=5,
        retry_count=0,
        human_corrections=0,
        estimated_cost=0.01,
        actual_cost=0.01,
        response_digest=response_digest,
        evidence_digest=digest({"evidence": response_digest}),
    )


@pytest.mark.parametrize("receipt_case", ("failed", "mismatch", "valid"))
def test_trial_rejects_failed_or_result_mismatched_receipt(
    realm_session: tuple[Any, Any], tmp_path: Path, receipt_case: str
) -> None:
    realm, connection = realm_session
    source = tmp_path / "source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    host = ExecutionHost(connection, realm.id, worker_label="benchmark-worker")
    job = Job.create(
        realm_id=realm.id,
        project_id=project.id,
        kind=JobKind.MUTATION,
        idempotency_key=f"benchmark-{uuid4()}",
        resources=parse_requests(write=("model-benchmark:model:general",)),
        required_capabilities=("provider.call",),
    )
    host.jobs.enqueue(job)
    work = host.acquire_work(capabilities=("provider.call",))
    assert work is not None
    registry = load_fixture_registry()
    fixture = registry.fixtures[0]
    suite = BenchmarkSuite("general-one", 1, SuiteKind.GENERAL, (fixture.fixture_digest,))
    plan = BenchmarkPlan(
        "model",
        suite.suite_digest,
        digest("inventory"),
        digest("policy"),
        registry.registry_digest,
    )
    repository = BenchmarkRepository(connection, realm.id)
    plan_id, _ = repository.ensure_plan(registry=registry, suite=suite, plan=plan)
    response_digest = digest({"response": 1})
    tested_claim = host.claim_effect(
        work,
        operation="model-benchmark-tested",
        effect_digest=benchmark_effect_digest(plan.plan_digest, fixture.fixture_digest, 1),
        authorization_digest=digest("authorization"),
        resources=parse_requests(write=("model-benchmark:model:general",)),
        adapter_digest=digest("adapter"),
    )
    if receipt_case == "failed":
        host.record_failure(tested_claim, category=FailureCategory.ADAPTER)
    elif receipt_case == "mismatch":
        host.record_success(tested_claim, result_digest=digest({"different": True}))
    else:
        host.record_success(tested_claim, result_digest=response_digest)
    verifier = VerifierIdentity("verifier", "worker:verifier", digest("verifier"))
    verifier_evidence = digest("verifier-evidence")
    verifier_claim = host.claim_effect(
        work,
        operation="model-benchmark-verifier",
        effect_digest=benchmark_verifier_effect_digest(
            plan.plan_digest,
            fixture.fixture_digest,
            1,
            verifier.model_id,
            response_digest,
        ),
        authorization_digest=digest("authorization"),
        resources=parse_requests(write=("model-benchmark:model:general",)),
        adapter_digest=verifier.provenance_digest,
    )
    host.record_success(verifier_claim, result_digest=verifier_evidence)

    def invocation() -> tuple[Any, bool]:
        return BenchmarkExecutionService(repository, registry).record_trial(
            plan_id,
            tested_claim_id=tested_claim.id,
            verifier_claim_id=verifier_claim.id,
            verdict=VerifierVerdict(
                "model",
                verifier.model_id,
                verifier.execution_identity,
                response_digest,
                True,
                verifier_evidence,
            ),
            result=_trial(fixture.fixture_digest, response_digest=response_digest),
        )

    if receipt_case == "valid":
        _, created = invocation()
        assert created
    else:
        with pytest.raises(PolicyViolation, match="exact plan/effect/receipt"):
            invocation()


def test_benchmark_ledger_is_immutable(realm_session: tuple[Any, Any]) -> None:
    _, connection = realm_session
    for table in ("benchmark_suite", "benchmark_plan", "quota_observation", "model_decision"):
        with (
            pytest.raises(Exception, match=r"append-only|permission denied"),
            connection.cursor() as cursor,
        ):
            cursor.execute(f"delete from models.{table}")


def test_canonical_decision_candidate_query_executes_with_freshness_gates(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    candidates = BenchmarkRepository(connection, realm.id).load_decision_candidates(
        DecisionRequirements(
            workload="code",
            client="codex",
            modality="chat",
            project_id=str(uuid4()),
            required_capabilities=(),
            verifier_model_id="independent-verifier",
            local_data_required=True,
            max_latency_ms=1000,
            max_cost=1,
            max_tokens=1000,
            evidence_digest=digest("requirements"),
        )
    )
    assert isinstance(candidates, tuple)
