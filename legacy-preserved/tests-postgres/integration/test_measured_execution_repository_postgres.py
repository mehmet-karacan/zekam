from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.scaffolding_ablation import ScaffoldingAblationService
from zekam.application.topology_planner import TopologyPlanner, TopologySuitabilityRequest
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.execution_topology import MeasurementSourceTier
from zekam.domain.model_context_experiment import ContextAblationProfile
from zekam.domain.scaffolding_ablation import (
    ScaffoldingAblationPair,
    ScaffoldingAblationPolicy,
    ScaffoldingArmEvidence,
    ScaffoldingDeprecationRollbackPlan,
    ScaffoldingMetrics,
)
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.measured_execution_repository import (
    PostgresMeasuredExecutionRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def _arm(arm_id: str, *, candidate: bool, source_revision: str) -> ScaffoldingArmEvidence:
    profile = (
        ContextAblationProfile(("core",), ("critic",))
        if candidate
        else ContextAblationProfile(("core", "critic"), ())
    )
    return ScaffoldingArmEvidence(
        arm_id,
        profile,
        "local-model",
        digest("execution-profile"),
        digest("objective"),
        digest("metric-vector"),
        digest("validator-assets"),
        digest("fixtures"),
        digest("paired-trials"),
        source_revision,
        5,
        ScaffoldingMetrics(0.9, 0.95, 90 if candidate else 100, 90, 90),
        digest(("ablation-evidence", arm_id)),
    )


def test_topology_and_scaffolding_records_are_idempotent_in_postgres(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "measured-execution-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title="Measured execution")
    plan = graph.create_plan(
        work.id,
        source_revision="git:measured-execution",
        policy_digest=digest("policy"),
        steps=(PlanStep("build", "Build", EffectKind.FILE_WRITE),),
    )
    request = TopologySuitabilityRequest(
        plan=plan,
        objective_digest=digest("objective"),
        measurement_available=True,
        measurement_source_tier=MeasurementSourceTier.DETERMINISTIC_EXTERNAL,
        measurement_estimated_cost_micros=1,
        action_estimated_cost_micros=10,
        reversible=True,
        idempotent_or_receipt_bound=True,
    )
    planner = TopologyPlanner()
    assessment = planner.assess(request)
    decision = planner.decide(request)
    repository = PostgresMeasuredExecutionRepository(connection, realm.id)

    topology = repository.store_topology_decision(
        plan=plan, assessment=assessment, decision=decision
    )
    topology_replay = repository.store_topology_decision(
        plan=plan, assessment=assessment, decision=decision
    )
    assert topology.created is True
    assert topology_replay.created is False
    assert topology.record_id == topology_replay.record_id

    pair = ScaffoldingAblationPair(
        _arm("baseline", candidate=False, source_revision=plan.source_revision),
        _arm("candidate", candidate=True, source_revision=plan.source_revision),
        "critic",
    )
    policy = ScaffoldingAblationPolicy()
    rollback = ScaffoldingDeprecationRollbackPlan(
        "critic",
        digest("restore-critic"),
        plan.source_revision,
        "review:critic-ablation",
    )
    ablation_decision = ScaffoldingAblationService.evaluate(
        pair=pair, policy=policy, rollback_plan=rollback
    )
    ablation = repository.store_scaffolding_ablation(
        plan=plan,
        pair=pair,
        policy=policy,
        rollback_plan=rollback,
        decision=ablation_decision,
    )
    ablation_replay = repository.store_scaffolding_ablation(
        plan=plan,
        pair=pair,
        policy=policy,
        rollback_plan=rollback,
        decision=ablation_decision,
    )
    assert ablation.created is True
    assert ablation_replay.created is False
    assert ablation.record_id == ablation_replay.record_id

    with connection.cursor() as cursor:
        cursor.execute(
            "select "
            "(select count(*) from runtime.execution_topology_decision where id=%s),"
            "(select count(*) from runtime.scaffolding_ablation where id=%s),"
            "(select ablation_body->>'status' from runtime.scaffolding_ablation where id=%s),"
            "(select (ablation_body->>'auto_delete')::boolean "
            " from runtime.scaffolding_ablation where id=%s)",
            (
                topology.record_id,
                ablation.record_id,
                ablation.record_id,
                ablation.record_id,
            ),
        )
        assert cursor.fetchone() == (1, 1, "review-required", False)
