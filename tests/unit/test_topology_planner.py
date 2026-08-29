from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.application.topology_planner import (
    TopologyPlanner,
    TopologySuitabilityRequest,
    assert_topology_matches_plan,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_topology import (
    ExecutionTopologyPattern,
    MeasurementSourceTier,
)
from zekam.domain.work import EffectKind, PlanStep, TaskPlan, WorkItem, WorkType

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 29, 9, 0, tzinfo=dt.UTC)
OBJECTIVE = digest({"objective": "quality"})
ITEM = WorkItem.create(
    realm_id=uuid4(), project_id=uuid4(), type=WorkType.TASK, title="Topology", now=NOW
)


def _plan(*steps: PlanStep) -> TaskPlan:
    return TaskPlan.create(
        work_item=ITEM,
        revision=1,
        source_revision="main:abc",
        policy_digest=digest({"policy": 1}),
        steps=tuple(steps),
        now=NOW,
    )


def _request(plan: TaskPlan, **changes: object) -> TopologySuitabilityRequest:
    values: dict[str, object] = {
        "plan": plan,
        "objective_digest": OBJECTIVE,
        "measurement_available": True,
        "measurement_source_tier": MeasurementSourceTier.DETERMINISTIC_EXTERNAL,
        "measurement_estimated_cost_micros": 10,
        "action_estimated_cost_micros": 100,
        "reversible": True,
        "idempotent_or_receipt_bound": True,
    }
    values.update(changes)
    return TopologySuitabilityRequest(**values)  # type: ignore[arg-type]


def test_cheap_external_measurement_and_reversible_effect_selects_bounded_loop() -> None:
    plan = _plan(PlanStep("edit", "Edit", EffectKind.FILE_WRITE))
    assessment = TopologyPlanner().assess(_request(plan))
    assert assessment.recommended_pattern is ExecutionTopologyPattern.BOUNDED_LOOP
    assert str(assessment.measurement_to_action_ratio) == "0.1"


def test_measurement_unavailable_never_selects_loop() -> None:
    plan = _plan(PlanStep("edit", "Edit", EffectKind.FILE_WRITE))
    decision = TopologyPlanner().decide(_request(plan, measurement_available=False))
    assert decision.pattern is ExecutionTopologyPattern.SINGLE_PASS
    assert "measurement-unavailable-no-loop" in decision.reason_codes


def test_model_self_report_is_not_measured_progress() -> None:
    plan = _plan(PlanStep("edit", "Edit", EffectKind.FILE_WRITE))
    decision = TopologyPlanner().decide(
        _request(plan, measurement_source_tier=MeasurementSourceTier.MODEL_SELF_REPORT)
    )
    assert decision.pattern is ExecutionTopologyPattern.SINGLE_PASS


def test_creative_diversity_selects_tournament() -> None:
    plan = _plan(PlanStep("draft", "Draft", EffectKind.FILE_WRITE))
    decision = TopologyPlanner().decide(_request(plan, creative_diversity_goal=True))
    assert decision.pattern is ExecutionTopologyPattern.TOURNAMENT


def test_real_distinct_deliverables_and_dependency_select_graph() -> None:
    plan = _plan(
        PlanStep("a", "A", EffectKind.FILE_WRITE),
        PlanStep("b", "B", EffectKind.FILE_WRITE, depends_on=("a",)),
    )
    decision = TopologyPlanner().decide(
        _request(plan, distinct_deliverable_count=2, parallelism_ceiling=3)
    )
    assert decision.pattern is ExecutionTopologyPattern.GRAPH
    assert decision.plan_digest == plan.plan_digest
    assert decision.parallelism_ceiling == 3
    assert decision.grants_authority is False


def test_single_artifact_graph_request_is_rejected() -> None:
    plan = _plan(PlanStep("a", "A", EffectKind.FILE_WRITE))
    decision = TopologyPlanner().decide(
        _request(plan, requested_pattern=ExecutionTopologyPattern.GRAPH)
    )
    assert decision.pattern is ExecutionTopologyPattern.SINGLE_PASS
    assert "graph-rejected-single-artifact" in decision.reason_codes


@pytest.mark.parametrize("reversible", [False, None])
def test_irreversible_or_unknown_effect_goes_to_human_review(reversible: bool | None) -> None:
    plan = _plan(PlanStep("push", "Push", EffectKind.GIT_PUSH, risk="high"))
    decision = TopologyPlanner().decide(_request(plan, reversible=reversible))
    assert decision.pattern is ExecutionTopologyPattern.QUEUE_HUMAN_REVIEW
    assert decision.required_human_gates


def test_unknown_measurement_is_fail_closed() -> None:
    plan = _plan(PlanStep("read", "Read", EffectKind.NONE))
    decision = TopologyPlanner().decide(_request(plan, measurement_available=None))
    assert decision.pattern is ExecutionTopologyPattern.BLOCKED


def test_topology_admission_rejects_stale_plan() -> None:
    first = _plan(PlanStep("a", "A", EffectKind.NONE))
    second = TaskPlan.create(
        work_item=ITEM,
        revision=2,
        source_revision="main:def",
        policy_digest=digest({"policy": 1}),
        steps=(PlanStep("a", "A", EffectKind.NONE),),
        now=NOW,
    )
    decision = TopologyPlanner().decide(_request(first))
    with pytest.raises(PolicyViolation, match="current TaskPlan"):
        assert_topology_matches_plan(decision, second)
