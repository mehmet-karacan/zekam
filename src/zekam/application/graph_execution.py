"""Mevcut TaskPlan DAG icin gercek interval tabanli graph execution evidence."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from itertools import combinations
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.execution_topology import (
    GraphExecutionReceipt,
    GraphNodeMode,
    GraphNodeReceipt,
    GraphNodeTerminalState,
    GraphTerminalState,
)
from zekam.domain.resources import conflicts, parse_requests
from zekam.domain.work import EffectKind, PlanStep, TaskPlan


@dataclass(frozen=True, slots=True)
class GraphNodeObservation:
    step_id: str
    mode: GraphNodeMode
    queued_at: dt.datetime
    started_at: dt.datetime
    ended_at: dt.datetime
    result_digest: str
    terminal_state: GraphNodeTerminalState
    coordination_input_tokens: int = 0
    coordination_output_tokens: int = 0
    coordination_cost_micros: int = 0
    coordination_message_count: int = 0


def _step_requests(step: PlanStep):  # type: ignore[no-untyped-def]
    if step.effect is EffectKind.NONE:
        return parse_requests(read=step.logical_resources)
    return parse_requests(write=step.logical_resources)


def _overlap(left: GraphNodeObservation, right: GraphNodeObservation) -> bool:
    return max(left.started_at, right.started_at) < min(left.ended_at, right.ended_at)


def _millis(delta: dt.timedelta) -> int:
    return round(delta.total_seconds() * 1000)


@dataclass(frozen=True, slots=True)
class GraphExecutionRecorder:
    """TaskPlan'i kopyalamadan node observations'i immutable receipt'e kapatir."""

    def build_receipt(
        self,
        *,
        graph_root_id: UUID,
        plan: TaskPlan,
        observations: tuple[GraphNodeObservation, ...],
        claimed_parallel: bool = False,
        expected_coordination_cost_micros: int | None = None,
    ) -> GraphExecutionReceipt:
        if expected_coordination_cost_micros is not None and expected_coordination_cost_micros < 0:
            raise ValidationFailed("Expected coordination cost negatif olamaz")
        by_step = {item.step_id: item for item in observations}
        plan_ids = {step.step_id for step in plan.steps}
        if len(by_step) != len(observations) or set(by_step) != plan_ids:
            raise PolicyViolation("Graph observations exact TaskPlan step setini kapsamali")

        step_by_id = {step.step_id: step for step in plan.steps}
        receipts: list[GraphNodeReceipt] = []
        for step_id in plan.execution_order:
            observation = by_step[step_id]
            predecessor_ends = [by_step[item].ended_at for item in step_by_id[step_id].depends_on]
            dependency_ready = max([observation.queued_at, *predecessor_ends])
            if observation.started_at < dependency_ready:
                raise PolicyViolation("Graph node predecessor tamamlanmadan baslamis")
            dependency_wait = max(
                (
                    max(predecessor_ends) - observation.queued_at
                    if predecessor_ends
                    else dt.timedelta()
                ),
                dt.timedelta(),
            )
            resource_wait = observation.started_at - dependency_ready
            receipts.append(
                GraphNodeReceipt(
                    step_id=step_id,
                    mode=observation.mode,
                    queued_at=observation.queued_at,
                    started_at=observation.started_at,
                    ended_at=observation.ended_at,
                    dependency_wait_millis=_millis(dependency_wait),
                    resource_wait_millis=_millis(resource_wait),
                    coordination_input_tokens=observation.coordination_input_tokens,
                    coordination_output_tokens=observation.coordination_output_tokens,
                    coordination_cost_micros=observation.coordination_cost_micros,
                    coordination_message_count=observation.coordination_message_count,
                    result_digest=observation.result_digest,
                    terminal_state=observation.terminal_state,
                )
            )

        self._assert_overlaps_are_real(plan, observations)
        maximum, overlap_millis, wall_millis = self._parallel_metrics(observations)
        if claimed_parallel and maximum < 2:
            raise PolicyViolation("Fake parallelism: gercek interval overlap yok")
        runtime_total = sum(_millis(item.ended_at - item.started_at) for item in observations)
        efficiency = 1_000_000
        if maximum > 0 and wall_millis > 0:
            efficiency = min(1_000_000, round(runtime_total * 1_000_000 / (wall_millis * maximum)))

        critical_path = self._critical_path(plan, tuple(receipts))
        terminal = self._terminal_state(observations)
        fan_in = digest(
            {
                "plan_digest": plan.plan_digest,
                "nodes": [
                    {
                        "step_id": item.step_id,
                        "result_digest": item.result_digest,
                        "terminal_state": item.terminal_state.value,
                    }
                    for item in sorted(observations, key=lambda item: item.step_id)
                ],
                "terminal_state": terminal.value,
            }
        )
        coordination_cost = sum(item.coordination_cost_micros for item in observations)
        feedback: tuple[str, ...] = ()
        if (
            expected_coordination_cost_micros is not None
            and coordination_cost > expected_coordination_cost_micros
        ):
            feedback = ("simpler-topology-recommended",)
        if maximum < 2 and len(observations) > 1:
            feedback = tuple(sorted({*feedback, "sequential-graph-observed"}))

        return GraphExecutionReceipt.create(
            graph_root_id=graph_root_id,
            plan_digest=plan.plan_digest,
            node_receipts=tuple(receipts),
            critical_path=critical_path,
            max_observed_concurrency=maximum,
            parallel_overlap_duration_millis=overlap_millis,
            parallel_efficiency_ppm=efficiency,
            coordination_input_tokens=sum(item.coordination_input_tokens for item in observations),
            coordination_output_tokens=sum(
                item.coordination_output_tokens for item in observations
            ),
            coordination_cost_micros=coordination_cost,
            coordination_message_count=sum(
                item.coordination_message_count for item in observations
            ),
            fan_in_result_digest=fan_in,
            terminal_state=terminal,
            topology_feedback=feedback,
        )

    @staticmethod
    def _assert_overlaps_are_real(
        plan: TaskPlan, observations: tuple[GraphNodeObservation, ...]
    ) -> None:
        step_by_id = {step.step_id: step for step in plan.steps}
        for left, right in combinations(observations, 2):
            if not _overlap(left, right):
                continue
            if (
                left.step_id in step_by_id[right.step_id].depends_on
                or right.step_id in step_by_id[left.step_id].depends_on
            ):
                raise PolicyViolation("Dependent graph node intervals overlap edemez")
            for left_request in _step_requests(step_by_id[left.step_id]):
                for right_request in _step_requests(step_by_id[right.step_id]):
                    if conflicts(left_request, right_request):
                        raise PolicyViolation(
                            "Resource-conflicting graph node intervals overlap edemez"
                        )

    @staticmethod
    def _parallel_metrics(
        observations: tuple[GraphNodeObservation, ...],
    ) -> tuple[int, int, int]:
        events = sorted(
            [
                (moment, delta)
                for item in observations
                for moment, delta in ((item.started_at, 1), (item.ended_at, -1))
            ],
            key=lambda item: (item[0], item[1]),
        )
        active = 0
        maximum = 0
        overlap = dt.timedelta()
        previous: dt.datetime | None = None
        for moment, delta in events:
            if previous is not None and active >= 2:
                overlap += moment - previous
            active += delta
            maximum = max(maximum, active)
            previous = moment
        wall = max(item.ended_at for item in observations) - min(
            item.started_at for item in observations
        )
        return maximum, _millis(overlap), _millis(wall)

    @staticmethod
    def _critical_path(plan: TaskPlan, receipts: tuple[GraphNodeReceipt, ...]) -> tuple[str, ...]:
        by_id = {item.step_id: item for item in receipts}
        steps = {item.step_id: item for item in plan.steps}
        scores: dict[str, int] = {}
        paths: dict[str, tuple[str, ...]] = {}
        for step_id in plan.execution_order:
            receipt = by_id[step_id]
            weight = (
                receipt.duration_millis
                + receipt.dependency_wait_millis
                + receipt.resource_wait_millis
            )
            predecessors = steps[step_id].depends_on
            if not predecessors:
                scores[step_id] = weight
                paths[step_id] = (step_id,)
                continue
            best = max(predecessors, key=lambda item: (scores[item], item))
            scores[step_id] = scores[best] + weight
            paths[step_id] = (*paths[best], step_id)
        winner = max(scores, key=lambda item: (scores[item], item))
        return paths[winner]

    @staticmethod
    def _terminal_state(
        observations: tuple[GraphNodeObservation, ...],
    ) -> GraphTerminalState:
        states = {item.terminal_state for item in observations}
        if GraphNodeTerminalState.RECOVERY_REQUIRED in states:
            return GraphTerminalState.RECOVERY_REQUIRED
        if states != {GraphNodeTerminalState.COMPLETED}:
            return GraphTerminalState.FAILED
        return GraphTerminalState.COMPLETED
