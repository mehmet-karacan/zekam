from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import Error as PsycopgError

from zekam.application.model_benchmark_service import load_fixture_registry
from zekam.application.model_registry import load_inventory
from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.model_benchmark import BenchmarkPlan, BenchmarkSuite, SuiteKind
from zekam.domain.model_routing import (
    AgentRole,
    ExecutionTargetSnapshot,
    LayeredRouteRequest,
    ProjectRoutingContext,
    RoleRoutingPolicy,
    RoutingLayer,
    decide_layered_model,
)
from zekam.infrastructure.postgres import migrations
from zekam.infrastructure.postgres.model_benchmark_repository import BenchmarkRepository
from zekam.infrastructure.postgres.model_repository import ModelInventoryRepository
from zekam.infrastructure.postgres.model_routing_repository import ModelRoutingRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

NOW = dt.datetime(2026, 8, 22, 12, tzinfo=dt.UTC)


def _context(realm: Any, connection: Any, tmp_path: Path) -> ProjectRoutingContext:
    source = tmp_path / f"routing-project-{uuid4()}"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname="routing-fixture"\nversion="1"\ndependencies=["pytest"]\n',
        encoding="utf-8",
    )
    service = ProjectIntegrationService(connection, realm)
    project = service.register(source_path=source)
    scan = service.scan(project.id, now=NOW)
    return ProjectRoutingContext(
        project_id=project.id,
        source_revision_id=scan.revision.id,
        source_revision=scan.revision.revision,
        tree_digest=scan.revision.tree_digest,
        capability_profile_digest=scan.profile.digest,
        dependency_digest=digest("dependencies"),
        framework_digest=digest("frameworks"),
        technology_digest=digest("technology"),
        architecture_digest=digest("architecture"),
        rules_digest=digest("rules"),
        suite_digest=digest("project-suite"),
        inventory_digest=digest("inventory"),
        policy_digest=digest("model-security-policy"),
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(days=7),
    )


def _policy(context: ProjectRoutingContext) -> RoleRoutingPolicy:
    return RoleRoutingPolicy(
        role=AgentRole.IMPLEMENTER,
        target_layer=RoutingLayer.PROJECT,
        required_layers=(
            RoutingLayer.GENERAL,
            RoutingLayer.WORKLOAD,
            RoutingLayer.PROJECT,
        ),
        top_k=3,
        fallback_model_ids=(),
        max_cost=10,
        max_latency_ms=30_000,
        independent_from_roles=(),
        policy_digest=digest("routing-role-policy"),
    )


def _benchmark_evidence(
    connection: Any,
    realm_id: UUID,
    *,
    model_id: str,
    inventory_digest: str,
    policy_digest: str,
) -> tuple[UUID, UUID, str, str]:
    record = next(item for item in load_inventory().records if item.model_id == model_id)
    ModelInventoryRepository(connection, realm_id).upsert(record)
    model_id = record.model_id
    registry = load_fixture_registry()
    fixture = registry.fixtures[0]
    suite = BenchmarkSuite(
        suite_id=f"routing:{uuid4()}",
        version=1,
        kind=SuiteKind.GENERAL,
        fixture_digests=(fixture.fixture_digest,),
    )
    plan = BenchmarkPlan(
        model_id=model_id,
        suite_digest=suite.suite_digest,
        inventory_digest=inventory_digest,
        policy_digest=policy_digest,
        fixture_registry_digest=registry.registry_digest,
        repetitions=5,
    )
    benchmark = BenchmarkRepository(connection, realm_id)
    plan_id, _ = benchmark.ensure_plan(registry=registry, suite=suite, plan=plan)
    with connection.cursor() as cursor:
        cursor.execute(
            "select suite_id from models.benchmark_plan where realm_id = %s and id = %s",
            (realm_id, plan_id),
        )
        suite_record_id = UUID(str(cursor.fetchone()[0]))
        aggregate_id = uuid4()
        aggregate_evidence = digest("aggregate-evidence")
        metric = {"mean": 0.9, "median": 0.9, "p95": 0.9, "variance": 0.0}
        cursor.execute(
            "insert into models.benchmark_aggregate"
            " (id, realm_id, plan_id, tested_model_id, verifier_model_id,"
            " verifier_execution_identity, verifier_provenance_digest, approved, unsafe,"
            " metrics, evidence_digest) values (%s, %s, %s, %s, %s, %s, %s, true, false,"
            " %s::jsonb, %s)",
            (
                aggregate_id,
                realm_id,
                plan_id,
                model_id,
                "independent-verifier",
                "verifier:execution",
                digest("verifier-provenance"),
                canonical_json({"quality": metric}),
                aggregate_evidence,
            ),
        )
    return suite_record_id, aggregate_id, suite.suite_digest, aggregate_evidence


def test_migrations_23_through_current_can_down_and_reapply(
    isolated_migrated_database: Any,
) -> None:
    from zekam.infrastructure.postgres.connection import connect

    with connect(isolated_migrated_database) as connection:
        latest = migrations.discover_migrations()[-1].version
        for head in range(latest, 22, -1):
            assert migrations.status(connection).head == head
            migrations.downgrade(connection, target=head)
        migrations.upgrade(connection)
        assert migrations.status(connection).head == latest


def test_context_policy_decision_roundtrip_is_append_only(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    context = _context(realm, connection, tmp_path)
    repository = ModelRoutingRepository(connection, realm.id)
    context_id, inserted = repository.store_project_context(context)
    replay_id, replay_inserted = repository.store_project_context(context)
    assert inserted is True and replay_inserted is False and replay_id == context_id
    assert repository.latest_context(context.project_id) == (context_id, context)

    policy = _policy(context)
    policy_id = repository.store_role_policy(policy, effective_from=NOW)
    assert repository.latest_policy(AgentRole.IMPLEMENTER, RoutingLayer.PROJECT, at=NOW) == (
        policy_id,
        policy,
    )
    execution_target = ExecutionTargetSnapshot(
        client_id="opencode",
        slot="default",
        execution_mode="native-sequential",
        model_selectable=True,
        structured_result=False,
        cancellation=False,
        max_concurrency=1,
        cost_evidence_digest=digest("unknown-cost"),
        capability_digest=digest("opencode-capability"),
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(days=7),
    )
    execution_target_id, _ = repository.store_execution_target(execution_target)
    request = LayeredRouteRequest(
        role=AgentRole.IMPLEMENTER,
        target_layer=RoutingLayer.PROJECT,
        workload="code",
        technology="java",
        project_id=context.project_id,
        project_context_digest=context.context_digest,
        inventory_digest=context.inventory_digest,
        routing_policy_digest=policy.policy_digest,
        policy_digest=context.policy_digest,
        execution_target_digest=execution_target.snapshot_digest,
    )
    decision = decide_layered_model(request, policy, (), now=NOW)
    decision_id, decision_inserted = repository.record_decision(
        decision,
        role_policy_id=policy_id,
        project_context_id=context_id,
        execution_target_id=execution_target_id,
        decided_at=NOW,
    )
    assert decision_inserted is True
    assert repository.decision(decision_id).decision == decision
    replay, replayed = repository.record_decision(
        decision,
        role_policy_id=policy_id,
        project_context_id=context_id,
        execution_target_id=execution_target_id,
        decided_at=NOW,
    )
    assert replay == decision_id and replayed is False
    with connection.cursor() as cursor:
        with pytest.raises(PsycopgError):
            cursor.execute(
                "update models.model_route_decision set status = 'selected' where id = %s",
                (decision_id,),
            )
        connection.rollback()


def test_execution_target_expiry_and_capability_change_are_not_current(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    repository = ModelRoutingRepository(connection, realm.id)
    first = ExecutionTargetSnapshot(
        client_id="opencode",
        slot="default",
        execution_mode="native-sequential",
        model_selectable=True,
        structured_result=False,
        cancellation=False,
        max_concurrency=1,
        cost_evidence_digest=digest("unknown-cost"),
        capability_digest=digest("opencode-capability-v1"),
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(minutes=1),
    )
    first_id, _ = repository.store_execution_target(first)
    assert repository.latest_execution_target("opencode", at=NOW) == (first_id, first)
    assert repository.latest_execution_target("opencode", at=NOW + dt.timedelta(minutes=2)) is None
    changed = ExecutionTargetSnapshot(
        client_id="opencode",
        slot="default",
        execution_mode="native-sequential",
        model_selectable=True,
        structured_result=False,
        cancellation=False,
        max_concurrency=1,
        cost_evidence_digest=digest("unknown-cost"),
        capability_digest=digest("opencode-capability-v2"),
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(days=1),
    )
    changed_id, inserted = repository.store_execution_target(changed)
    assert inserted is True
    assert changed_id != first_id
    assert changed.snapshot_digest != first.snapshot_digest
    assert repository.execution_target_by_digest(changed.snapshot_digest, at=NOW) == (
        changed_id,
        changed,
    )
    assert repository.execution_target_by_digest(first.snapshot_digest, at=NOW) == (
        first_id,
        first,
    )


def test_project_qualification_rejects_general_suite_substitution(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    context = _context(realm, connection, tmp_path)
    repository = ModelRoutingRepository(connection, realm.id)
    context_id, _ = repository.store_project_context(context)
    model_record = next(item for item in load_inventory().records if item.enabled)
    suite_id, aggregate_id, suite_digest, aggregate_evidence = _benchmark_evidence(
        connection,
        realm.id,
        model_id=model_record.model_id,
        inventory_digest=context.inventory_digest,
        policy_digest=context.policy_digest,
    )
    del aggregate_id, aggregate_evidence
    with pytest.raises(PsycopgError):
        repository.store_suite_binding(
            benchmark_suite_id=suite_id,
            suite_digest=suite_digest,
            layer=RoutingLayer.PROJECT,
            role=AgentRole.IMPLEMENTER,
            workload="code",
            technology="java",
            project_context_id=context_id,
            binding_digest=digest("suite-binding"),
        )
    connection.rollback()


def test_context_trigger_rejects_cross_project_revision(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    first = _context(realm, connection, tmp_path)
    second = _context(realm, connection, tmp_path)
    forged = ProjectRoutingContext(
        project_id=first.project_id,
        source_revision_id=second.source_revision_id,
        source_revision=second.source_revision,
        tree_digest=second.tree_digest,
        capability_profile_digest=second.capability_profile_digest,
        dependency_digest=first.dependency_digest,
        framework_digest=first.framework_digest,
        technology_digest=first.technology_digest,
        architecture_digest=first.architecture_digest,
        rules_digest=first.rules_digest,
        suite_digest=first.suite_digest,
        inventory_digest=first.inventory_digest,
        policy_digest=first.policy_digest,
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(days=1),
    )
    with pytest.raises(PsycopgError):
        ModelRoutingRepository(connection, realm.id).store_project_context(forged)
    connection.rollback()
    with pytest.raises(PsycopgError):
        ModelRoutingRepository(connection, uuid4()).store_project_context(first)
    connection.rollback()


def test_context_status_view_marks_new_source_and_profile_stale(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    context = _context(realm, connection, tmp_path)
    repository = ModelRoutingRepository(connection, realm.id)
    context_id, _ = repository.store_project_context(context)
    with connection.cursor() as cursor:
        cursor.execute(
            "select l.absolute_path from projects.source_binding_local l"
            " join projects.source_binding b on b.realm_id = l.realm_id and b.id = l.binding_id"
            " where b.realm_id = %s and b.project_id = %s",
            (realm.id, context.project_id),
        )
        source = Path(str(cursor.fetchone()[0]))
    (source / "pyproject.toml").write_text(
        '[project]\nname="routing-fixture"\nversion="2"\ndependencies=["pytest","fastapi"]\n',
        encoding="utf-8",
    )
    ProjectIntegrationService(connection, realm).scan(
        context.project_id, now=NOW + dt.timedelta(minutes=1)
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "select stale_reasons from projects.routing_context_current_status"
            " where realm_id = %s and id = %s",
            (realm.id, context_id),
        )
        reasons = set(cursor.fetchone()[0])
    assert {"source-revision", "tree", "capability-profile"} <= reasons
