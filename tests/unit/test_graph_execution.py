from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import uuid4

import pytest

from zekam.application.graph_execution import GraphExecutionRecorder, GraphNodeObservation
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_topology import (
    GraphNodeMode,
    GraphNodeTerminalState,
    GraphTerminalState,
)
from zekam.domain.work import EffectKind, PlanStep, TaskPlan, WorkItem, WorkType

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 8, 29, 9, 0, tzinfo=dt.UTC)
ITEM = WorkItem.create(
    realm_id=uuid4(), project_id=uuid4(), type=WorkType.TASK, title="Graph", now=T0
)


def _at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


def _plan(*steps: PlanStep) -> TaskPlan:
    return TaskPlan.create(
        work_item=ITEM,
        revision=1,
        source_revision="main:abc",
        policy_digest=digest({"policy": 1}),
        steps=tuple(steps),
        now=T0,
    )


def _observation(step: str, start: float, end: float, **changes: object) -> GraphNodeObservation:
    values: dict[str, object] = {
        "step_id": step,
        "mode": GraphNodeMode.DIRECT,
        "queued_at": T0,
        "started_at": _at(start),
        "ended_at": _at(end),
        "result_digest": digest({"step": step}),
        "terminal_state": GraphNodeTerminalState.COMPLETED,
    }
    values.update(changes)
    return GraphNodeObservation(**values)  # type: ignore[arg-type]


def test_graph_receipt_measures_overlap_critical_path_wait_and_coordination() -> None:
    plan = _plan(
        PlanStep("a", "A", EffectKind.FILE_WRITE, logical_resources=("path:x:a",)),
        PlanStep("b", "B", EffectKind.FILE_WRITE, logical_resources=("path:x:b",)),
        PlanStep("c", "C", EffectKind.NONE, depends_on=("a", "b")),
    )
    receipt = GraphExecutionRecorder().build_receipt(
        graph_root_id=uuid4(),
        plan=plan,
        observations=(
            _observation("a", 0, 2, coordination_input_tokens=3),
            _observation("b", 0.5, 1.5, coordination_output_tokens=4),
            _observation("c", 2.5, 3, coordination_cost_micros=7),
        ),
        claimed_parallel=True,
        expected_coordination_cost_micros=10,
    )
    assert receipt.max_observed_concurrency == 2
    assert receipt.parallel_overlap_duration_millis == 1000
    assert receipt.critical_path == ("a", "c")
    assert receipt.coordination_input_tokens == 3
    assert receipt.coordination_output_tokens == 4
    assert receipt.coordination_cost_micros == 7
    assert receipt.terminal_state is GraphTerminalState.COMPLETED
    assert receipt.topology_feedback == ()


def test_sequential_intervals_cannot_claim_parallelism() -> None:
    plan = _plan(
        PlanStep("a", "A", EffectKind.NONE),
        PlanStep("b", "B", EffectKind.NONE, depends_on=("a",)),
    )
    with pytest.raises(PolicyViolation, match="Fake parallelism"):
        GraphExecutionRecorder().build_receipt(
            graph_root_id=uuid4(),
            plan=plan,
            observations=(_observation("a", 0, 1), _observation("b", 1, 2)),
            claimed_parallel=True,
        )


def test_dependency_cannot_start_before_predecessor_finishes() -> None:
    plan = _plan(
        PlanStep("a", "A", EffectKind.NONE),
        PlanStep("b", "B", EffectKind.NONE, depends_on=("a",)),
    )
    with pytest.raises(PolicyViolation, match="predecessor"):
        GraphExecutionRecorder().build_receipt(
            graph_root_id=uuid4(),
            plan=plan,
            observations=(_observation("a", 0, 2), _observation("b", 1, 3)),
        )


def test_overlapping_writes_to_conflicting_resource_are_rejected() -> None:
    plan = _plan(
        PlanStep("a", "A", EffectKind.FILE_WRITE, logical_resources=("path:x:src",)),
        PlanStep("b", "B", EffectKind.FILE_WRITE, logical_resources=("path:x:src/y",)),
    )
    with pytest.raises(PolicyViolation, match="Resource-conflicting"):
        GraphExecutionRecorder().build_receipt(
            graph_root_id=uuid4(),
            plan=plan,
            observations=(_observation("a", 0, 2), _observation("b", 1, 3)),
        )


def test_child_failure_is_not_swallowed_by_fan_in() -> None:
    plan = _plan(PlanStep("a", "A", EffectKind.NONE), PlanStep("b", "B", EffectKind.NONE))
    failed = replace(_observation("b", 1, 2), terminal_state=GraphNodeTerminalState.FAILED)
    receipt = GraphExecutionRecorder().build_receipt(
        graph_root_id=uuid4(),
        plan=plan,
        observations=(_observation("a", 0, 1), failed),
    )
    assert receipt.terminal_state is GraphTerminalState.FAILED
    assert {item.terminal_state for item in receipt.node_receipts} == {
        GraphNodeTerminalState.COMPLETED,
        GraphNodeTerminalState.FAILED,
    }


def test_coordination_overrun_feeds_back_simpler_topology() -> None:
    plan = _plan(PlanStep("a", "A", EffectKind.NONE), PlanStep("b", "B", EffectKind.NONE))
    receipt = GraphExecutionRecorder().build_receipt(
        graph_root_id=uuid4(),
        plan=plan,
        observations=(
            _observation("a", 0, 1, coordination_cost_micros=6),
            _observation("b", 1, 2, coordination_cost_micros=6),
        ),
        expected_coordination_cost_micros=10,
    )
    assert "simpler-topology-recommended" in receipt.topology_feedback
    assert "sequential-graph-observed" in receipt.topology_feedback
