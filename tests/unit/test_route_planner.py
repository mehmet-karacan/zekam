"""Route planner: direct, single, sequential, parallel, blocked, recovery."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.application.route_planner import (
    ExecutionBudget,
    RoutePlanner,
    StepState,
    declared_resources,
)
from zekam.domain.errors import ValidationFailed
from zekam.domain.runtime import RouteKind
from zekam.domain.work import EffectKind, PlanStep, TaskPlan, WorkItem, WorkType

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)
POLICY_DIGEST = "sha256:" + "a" * 64
SOURCE_REVISION = "sha256:" + "b" * 64

_ITEM = WorkItem.create(
    realm_id=uuid4(), project_id=uuid4(), type=WorkType.TASK, title="Plan sahibi", now=NOW
)


def _plan(steps: tuple[PlanStep, ...]) -> TaskPlan:
    return TaskPlan.create(
        work_item=_ITEM,
        revision=1,
        source_revision=SOURCE_REVISION,
        policy_digest=POLICY_DIGEST,
        steps=steps,
        now=NOW,
    )


def _generous() -> ExecutionBudget:
    return ExecutionBudget(
        worker_slots=8,
        quota_safe_slots=8,
        token_budget_slots=8,
        cost_budget_slots=8,
        provider_rate_slots=8,
        policy_concurrency_limit=8,
    )


# -- butce -----------------------------------------------------------------------------


def test_budget_ceiling_is_the_minimum() -> None:
    budget = ExecutionBudget(
        worker_slots=8,
        quota_safe_slots=3,
        token_budget_slots=8,
        cost_budget_slots=8,
        provider_rate_slots=8,
        policy_concurrency_limit=8,
    )
    assert budget.ceiling == 3
    assert budget.limiting_factor() == "quota_safe_slots"


def test_negative_budget_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        ExecutionBudget(worker_slots=-1)


def test_default_budget_is_serial() -> None:
    assert ExecutionBudget().ceiling == 1


# -- karar turleri -------------------------------------------------------------------------


def test_single_ready_step_is_single() -> None:
    plan = _plan((PlanStep(step_id="a", title="A", effect=EffectKind.FILE_WRITE),))
    decision = RoutePlanner(_generous()).decide(plan)
    assert decision.kind is RouteKind.SINGLE
    assert decision.steps == ("a",)
    assert decision.parallelism == 1


def test_single_read_only_step_in_deterministic_work_is_direct() -> None:
    plan = _plan((PlanStep(step_id="a", title="A", effect=EffectKind.NONE),))
    decision = RoutePlanner(_generous()).decide(plan, agentic=False)
    assert decision.kind is RouteKind.DIRECT


def test_independent_steps_run_in_parallel() -> None:
    plan = _plan(
        (
            PlanStep(
                step_id="a",
                title="A",
                effect=EffectKind.FILE_WRITE,
                logical_resources=("path:zekam:a.py",),
            ),
            PlanStep(
                step_id="b",
                title="B",
                effect=EffectKind.FILE_WRITE,
                logical_resources=("path:zekam:b.py",),
            ),
        )
    )
    decision = RoutePlanner(_generous()).decide(plan)
    assert decision.kind is RouteKind.PARALLEL
    assert set(decision.steps) == {"a", "b"}
    assert decision.parallelism == 2


def test_conflicting_writes_force_sequential() -> None:
    plan = _plan(
        (
            PlanStep(
                step_id="a",
                title="A",
                effect=EffectKind.FILE_WRITE,
                logical_resources=("path:zekam:src",),
            ),
            PlanStep(
                step_id="b",
                title="B",
                effect=EffectKind.FILE_WRITE,
                logical_resources=("path:zekam:src/inner.py",),
            ),
        )
    )
    decision = RoutePlanner(_generous()).decide(plan)
    assert decision.kind is RouteKind.SEQUENTIAL
    assert decision.reason == "kaynak-catismasi"
    assert decision.conflicts


def test_budget_of_one_forces_sequential() -> None:
    plan = _plan(
        (
            PlanStep(
                step_id="a",
                title="A",
                effect=EffectKind.FILE_WRITE,
                logical_resources=("path:zekam:a.py",),
            ),
            PlanStep(
                step_id="b",
                title="B",
                effect=EffectKind.FILE_WRITE,
                logical_resources=("path:zekam:b.py",),
            ),
        )
    )
    decision = RoutePlanner(ExecutionBudget()).decide(plan)
    assert decision.kind is RouteKind.SEQUENTIAL
    assert decision.parallelism == 1
    assert decision.limiting_factor is not None


def test_parallelism_is_capped_by_the_smallest_budget() -> None:
    steps = tuple(
        PlanStep(
            step_id=f"s{index}",
            title=f"S{index}",
            effect=EffectKind.FILE_WRITE,
            logical_resources=(f"path:zekam:{index}.py",),
        )
        for index in range(6)
    )
    budget = ExecutionBudget(
        worker_slots=6,
        quota_safe_slots=6,
        token_budget_slots=2,
        cost_budget_slots=6,
        provider_rate_slots=6,
        policy_concurrency_limit=6,
    )
    decision = RoutePlanner(budget).decide(_plan(steps))
    assert decision.kind is RouteKind.PARALLEL
    assert decision.parallelism == 2
    assert decision.limiting_factor == "token_budget_slots"


def test_dependent_steps_are_not_ready_together() -> None:
    plan = _plan(
        (
            PlanStep(step_id="a", title="A", effect=EffectKind.NONE),
            PlanStep(step_id="b", title="B", effect=EffectKind.NONE, depends_on=("a",)),
        )
    )
    planner = RoutePlanner(_generous())
    first = planner.decide(plan)
    assert first.steps == ("a",)

    second = planner.decide(plan, states=[StepState("a", completed=True)])
    assert second.steps == ("b",)


def test_all_steps_completed_is_direct_and_empty() -> None:
    plan = _plan((PlanStep(step_id="a", title="A", effect=EffectKind.NONE),))
    decision = RoutePlanner(_generous()).decide(plan, states=[StepState("a", completed=True)])
    assert decision.kind is RouteKind.DIRECT
    assert decision.steps == ()


def test_failed_dependency_blocks_the_rest() -> None:
    plan = _plan(
        (
            PlanStep(step_id="a", title="A", effect=EffectKind.NONE),
            PlanStep(step_id="b", title="B", effect=EffectKind.NONE, depends_on=("a",)),
        )
    )
    decision = RoutePlanner(_generous()).decide(plan, states=[StepState("a", failed=True)])
    assert decision.kind is RouteKind.BLOCKED
    assert "b" in decision.blocked_steps


def test_recovery_required_step_wins_over_everything() -> None:
    plan = _plan(
        (
            PlanStep(step_id="a", title="A", effect=EffectKind.FILE_WRITE),
            PlanStep(step_id="b", title="B", effect=EffectKind.FILE_WRITE),
        )
    )
    decision = RoutePlanner(_generous()).decide(
        plan, states=[StepState("a", recovery_required=True)]
    )
    assert decision.kind is RouteKind.RECOVERY
    assert decision.steps == ("a",)
    assert decision.parallelism == 0


def test_read_only_steps_on_the_same_path_can_run_in_parallel() -> None:
    plan = _plan(
        (
            PlanStep(
                step_id="a",
                title="A",
                effect=EffectKind.NONE,
                logical_resources=("path:zekam:src",),
            ),
            PlanStep(
                step_id="b",
                title="B",
                effect=EffectKind.NONE,
                logical_resources=("path:zekam:src/inner.py",),
            ),
        )
    )
    decision = RoutePlanner(_generous()).decide(plan)
    assert decision.kind is RouteKind.PARALLEL
    assert decision.conflicts == ()


def test_decision_serializes_cleanly() -> None:
    plan = _plan((PlanStep(step_id="a", title="A", effect=EffectKind.NONE),))
    document = RoutePlanner(_generous()).decide(plan).as_dict()
    assert document["kind"] == "single"
    assert document["steps"] == ["a"]


def test_declared_resources_cover_every_step() -> None:
    plan = _plan(
        (
            PlanStep(
                step_id="a",
                title="A",
                effect=EffectKind.NONE,
                logical_resources=("path:zekam:r.py",),
            ),
            PlanStep(
                step_id="b",
                title="B",
                effect=EffectKind.FILE_WRITE,
                logical_resources=("path:zekam:w.py",),
            ),
        )
    )
    resources = declared_resources(plan)
    modes = {item.resource.text: item.mode.value for item in resources}
    assert modes == {"path:zekam:r.py": "read", "path:zekam:w.py": "write"}
