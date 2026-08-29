"""Olcumlu topology, graph evidence ve tournament domain sozlesmeleri.

Bu modeller TaskPlan veya AgentGraph icin ikinci bir authority/store kurmaz. Her
karar ve receipt mevcut plan/objective digest'lerine baglidir ve authority vermez.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class ExecutionTopologyPattern(StrEnum):
    DIRECT = "direct"
    SINGLE_PASS = "single-pass"
    TOURNAMENT = "tournament"
    BOUNDED_LOOP = "bounded-loop"
    GRAPH = "graph"
    QUEUE_HUMAN_REVIEW = "queue-human-review"
    BLOCKED = "blocked"


class MeasurementSourceTier(StrEnum):
    DETERMINISTIC_EXTERNAL = "deterministic-external"
    INDEPENDENT_VERIFIER = "independent-verifier"
    HUMAN_REVIEW = "human-review"
    MODEL_SELF_REPORT = "model-self-report"
    UNKNOWN = "unknown"

    @property
    def supports_measured_progress(self) -> bool:
        return self in {
            MeasurementSourceTier.DETERMINISTIC_EXTERNAL,
            MeasurementSourceTier.INDEPENDENT_VERIFIER,
        }


class GraphNodeMode(StrEnum):
    DIRECT = "direct"
    BOUNDED_LOOP = "bounded-loop"
    TOURNAMENT = "tournament"
    HUMAN_GATE = "human-gate"


class GraphNodeTerminalState(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery-required"


class GraphTerminalState(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery-required"


def _non_negative(value: int, label: str) -> None:
    if value < 0:
        raise ValidationFailed(f"{label} negatif olamaz")


def _nonblank(value: str, label: str) -> None:
    if not value.strip():
        raise ValidationFailed(f"{label} bos olamaz")


def _aware(value: dt.datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailed(f"{label} timezone-aware olmali")


@dataclass(frozen=True, slots=True)
class LoopSuitabilityAssessment:
    measurement_available: bool | None
    measurement_source_tier: MeasurementSourceTier
    measurement_estimated_cost_micros: int | None
    action_estimated_cost_micros: int | None
    reversible: bool | None
    idempotent_or_receipt_bound: bool | None
    creative_diversity_goal: bool
    human_judgment_required: bool
    distinct_deliverable_count: int | None
    dependency_edge_count: int | None
    expected_coordination_cost_micros: int | None
    recommended_pattern: ExecutionTopologyPattern
    reason_codes: tuple[str, ...]
    assessment_digest: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.measurement_estimated_cost_micros, "Measurement cost"),
            (self.action_estimated_cost_micros, "Action cost"),
            (self.expected_coordination_cost_micros, "Coordination cost"),
            (self.distinct_deliverable_count, "Distinct deliverable count"),
            (self.dependency_edge_count, "Dependency edge count"),
        ):
            if value is not None:
                _non_negative(value, label)
        if not self.reason_codes or any(not item.strip() for item in self.reason_codes):
            raise ValidationFailed("Suitability reason code zorunlu")
        if self.assessment_digest:
            parse_digest(self.assessment_digest)
            if self.assessment_digest != self.computed_digest:
                raise PolicyViolation("Suitability assessment digest mismatch")

    @property
    def measurement_to_action_ratio(self) -> Decimal | None:
        measurement = self.measurement_estimated_cost_micros
        action = self.action_estimated_cost_micros
        if measurement is None or action is None or action == 0:
            return None
        return Decimal(measurement) / Decimal(action)

    def body(self) -> dict[str, Any]:
        ratio = self.measurement_to_action_ratio
        return {
            "schema": "zekam-loop-suitability-assessment/v1",
            "measurement_available": self.measurement_available,
            "measurement_source_tier": self.measurement_source_tier.value,
            "measurement_estimated_cost_micros": self.measurement_estimated_cost_micros,
            "action_estimated_cost_micros": self.action_estimated_cost_micros,
            "measurement_to_action_ratio": ratio,
            "reversible": self.reversible,
            "idempotent_or_receipt_bound": self.idempotent_or_receipt_bound,
            "creative_diversity_goal": self.creative_diversity_goal,
            "human_judgment_required": self.human_judgment_required,
            "distinct_deliverable_count": self.distinct_deliverable_count,
            "dependency_edge_count": self.dependency_edge_count,
            "expected_coordination_cost_micros": self.expected_coordination_cost_micros,
            "recommended_pattern": self.recommended_pattern.value,
            "reason_codes": list(self.reason_codes),
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> LoopSuitabilityAssessment:
        values["reason_codes"] = tuple(sorted(set(values["reason_codes"])))
        values["assessment_digest"] = ""
        draft = cls(**values)
        return cls(**{**values, "assessment_digest": draft.computed_digest})

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"assessment_digest": self.assessment_digest}


@dataclass(frozen=True, slots=True)
class ExecutionTopologyDecision:
    pattern: ExecutionTopologyPattern
    objective_digest: str
    plan_digest: str
    node_modes: tuple[tuple[str, GraphNodeMode], ...]
    parallelism_ceiling: int
    estimated_calls: int
    estimated_tokens: int
    estimated_cost_micros: int
    estimated_coordination_overhead_micros: int
    required_human_gates: tuple[str, ...]
    reason_codes: tuple[str, ...]
    decision_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Topology decision authority uretemez")
        parse_digest(self.objective_digest)
        parse_digest(self.plan_digest)
        for value, label in (
            (self.parallelism_ceiling, "Parallelism ceiling"),
            (self.estimated_calls, "Estimated calls"),
            (self.estimated_tokens, "Estimated tokens"),
            (self.estimated_cost_micros, "Estimated cost"),
            (self.estimated_coordination_overhead_micros, "Coordination overhead"),
        ):
            _non_negative(value, label)
        step_ids = tuple(item[0] for item in self.node_modes)
        if len(step_ids) != len(set(step_ids)) or any(not item.strip() for item in step_ids):
            raise ValidationFailed("Topology node mode step kimlikleri tekil olmali")
        if not self.reason_codes:
            raise ValidationFailed("Topology decision reason code ister")
        if self.pattern is ExecutionTopologyPattern.QUEUE_HUMAN_REVIEW:
            if not self.required_human_gates:
                raise PolicyViolation("Human review topology exact gate ister")
        elif self.required_human_gates:
            raise PolicyViolation("Human gate yalniz queue-human-review topology'de olur")
        if self.decision_digest:
            parse_digest(self.decision_digest)
            if self.decision_digest != self.computed_digest:
                raise PolicyViolation("Topology decision digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-execution-topology-decision/v1",
            "pattern": self.pattern.value,
            "objective_digest": self.objective_digest,
            "plan_digest": self.plan_digest,
            "node_modes": [
                {"step_id": step_id, "mode": mode.value} for step_id, mode in self.node_modes
            ],
            "parallelism_ceiling": self.parallelism_ceiling,
            "estimated_calls": self.estimated_calls,
            "estimated_tokens": self.estimated_tokens,
            "estimated_cost_micros": self.estimated_cost_micros,
            "estimated_coordination_overhead_micros": self.estimated_coordination_overhead_micros,
            "required_human_gates": list(self.required_human_gates),
            "reason_codes": list(self.reason_codes),
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> ExecutionTopologyDecision:
        values["node_modes"] = tuple(sorted(values.get("node_modes", ())))
        values["required_human_gates"] = tuple(sorted(set(values["required_human_gates"])))
        values["reason_codes"] = tuple(sorted(set(values["reason_codes"])))
        values["decision_digest"] = ""
        draft = cls(**values)
        return cls(**{**values, "decision_digest": draft.computed_digest})

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"decision_digest": self.decision_digest}


@dataclass(frozen=True, slots=True)
class GraphNodeReceipt:
    step_id: str
    mode: GraphNodeMode
    queued_at: dt.datetime
    started_at: dt.datetime
    ended_at: dt.datetime
    dependency_wait_millis: int
    resource_wait_millis: int
    coordination_input_tokens: int
    coordination_output_tokens: int
    coordination_cost_micros: int
    coordination_message_count: int
    result_digest: str
    terminal_state: GraphNodeTerminalState

    def __post_init__(self) -> None:
        _nonblank(self.step_id, "Graph node step")
        for moment, label in (
            (self.queued_at, "Graph node queued_at"),
            (self.started_at, "Graph node started_at"),
            (self.ended_at, "Graph node ended_at"),
        ):
            _aware(moment, label)
        if not self.queued_at <= self.started_at < self.ended_at:
            raise ValidationFailed("Graph node interval sirasi gecersiz")
        for amount, label in (
            (self.dependency_wait_millis, "Dependency wait"),
            (self.resource_wait_millis, "Resource wait"),
            (self.coordination_input_tokens, "Coordination input tokens"),
            (self.coordination_output_tokens, "Coordination output tokens"),
            (self.coordination_cost_micros, "Coordination cost"),
            (self.coordination_message_count, "Coordination message count"),
        ):
            _non_negative(amount, label)
        parse_digest(self.result_digest)

    @property
    def duration_millis(self) -> int:
        return round((self.ended_at - self.started_at).total_seconds() * 1000)

    def body(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "mode": self.mode.value,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "dependency_wait_millis": self.dependency_wait_millis,
            "resource_wait_millis": self.resource_wait_millis,
            "coordination_input_tokens": self.coordination_input_tokens,
            "coordination_output_tokens": self.coordination_output_tokens,
            "coordination_cost_micros": self.coordination_cost_micros,
            "coordination_message_count": self.coordination_message_count,
            "result_digest": self.result_digest,
            "terminal_state": self.terminal_state.value,
        }


@dataclass(frozen=True, slots=True)
class GraphExecutionReceipt:
    graph_root_id: UUID
    plan_digest: str
    node_receipts: tuple[GraphNodeReceipt, ...]
    critical_path: tuple[str, ...]
    max_observed_concurrency: int
    parallel_overlap_duration_millis: int
    parallel_efficiency_ppm: int
    coordination_input_tokens: int
    coordination_output_tokens: int
    coordination_cost_micros: int
    coordination_message_count: int
    fan_in_result_digest: str
    terminal_state: GraphTerminalState
    topology_feedback: tuple[str, ...]
    receipt_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.plan_digest)
        parse_digest(self.fan_in_result_digest)
        if not self.node_receipts:
            raise ValidationFailed("Graph receipt node receipt ister")
        node_ids = tuple(item.step_id for item in self.node_receipts)
        if len(node_ids) != len(set(node_ids)):
            raise ValidationFailed("Graph node receipt step kimligi tekil olmali")
        if not self.critical_path or not set(self.critical_path) <= set(node_ids):
            raise ValidationFailed("Graph critical path node receipt'lere baglanmali")
        for value, label in (
            (self.max_observed_concurrency, "Max observed concurrency"),
            (self.parallel_overlap_duration_millis, "Parallel overlap"),
            (self.coordination_input_tokens, "Coordination input tokens"),
            (self.coordination_output_tokens, "Coordination output tokens"),
            (self.coordination_cost_micros, "Coordination cost"),
            (self.coordination_message_count, "Coordination message count"),
        ):
            _non_negative(value, label)
        if not 0 <= self.parallel_efficiency_ppm <= 1_000_000:
            raise ValidationFailed("Parallel efficiency 0..1000000 ppm olmali")
        if self.receipt_digest:
            parse_digest(self.receipt_digest)
            if self.receipt_digest != self.computed_digest:
                raise PolicyViolation("Graph execution receipt digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-graph-execution-receipt/v1",
            "graph_root_id": str(self.graph_root_id),
            "plan_digest": self.plan_digest,
            "node_receipts": [item.body() for item in self.node_receipts],
            "critical_path": list(self.critical_path),
            "max_observed_concurrency": self.max_observed_concurrency,
            "parallel_overlap_duration_millis": self.parallel_overlap_duration_millis,
            "parallel_efficiency_ppm": self.parallel_efficiency_ppm,
            "coordination_input_tokens": self.coordination_input_tokens,
            "coordination_output_tokens": self.coordination_output_tokens,
            "coordination_cost_micros": self.coordination_cost_micros,
            "coordination_message_count": self.coordination_message_count,
            "fan_in_result_digest": self.fan_in_result_digest,
            "terminal_state": self.terminal_state.value,
            "topology_feedback": list(self.topology_feedback),
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> GraphExecutionReceipt:
        values["node_receipts"] = tuple(sorted(values["node_receipts"], key=lambda x: x.step_id))
        values["topology_feedback"] = tuple(sorted(set(values["topology_feedback"])))
        values["receipt_digest"] = ""
        draft = cls(**values)
        return cls(**{**values, "receipt_digest": draft.computed_digest})

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"receipt_digest": self.receipt_digest}


@dataclass(frozen=True, slots=True)
class TournamentBudget:
    max_candidates: int
    max_tokens: int
    max_cost_micros: int
    deadline: dt.datetime

    def __post_init__(self) -> None:
        _aware(self.deadline, "Tournament deadline")
        if min(self.max_candidates, self.max_tokens, self.max_cost_micros) <= 0:
            raise ValidationFailed("Tournament budget degerleri pozitif olmali")

    def body(self) -> dict[str, Any]:
        return {
            "max_candidates": self.max_candidates,
            "max_tokens": self.max_tokens,
            "max_cost_micros": self.max_cost_micros,
            "deadline": self.deadline,
        }


@dataclass(frozen=True, slots=True)
class TournamentCandidateAssignment:
    assignment_id: UUID
    model_id: str
    execution_identity: str
    token_budget: int
    cost_budget_micros: int

    def __post_init__(self) -> None:
        _nonblank(self.model_id, "Candidate model")
        _nonblank(self.execution_identity, "Candidate execution identity")
        if min(self.token_budget, self.cost_budget_micros) <= 0:
            raise ValidationFailed("Candidate token/cost budget pozitif olmali")

    def body(self) -> dict[str, Any]:
        return {
            "assignment_id": str(self.assignment_id),
            "model_id": self.model_id,
            "execution_identity": self.execution_identity,
            "token_budget": self.token_budget,
            "cost_budget_micros": self.cost_budget_micros,
        }


@dataclass(frozen=True, slots=True)
class TournamentPlan:
    candidate_assignments: tuple[TournamentCandidateAssignment, ...]
    shared_objective_digest: str
    candidate_context_digest: str
    selector_assignment_id: UUID
    selector_model_id: str
    selector_execution_identity: str
    selector_spec_digest: str
    human_final_gate: bool
    budget: TournamentBudget
    plan_digest: str
    candidate_isolation: bool = True
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Tournament plan authority uretemez")
        if not self.candidate_isolation:
            raise PolicyViolation("Tournament candidate isolation zorunlu")
        if len(self.candidate_assignments) < 2:
            raise ValidationFailed("Tournament en az iki candidate ister")
        parse_digest(self.shared_objective_digest)
        parse_digest(self.candidate_context_digest)
        parse_digest(self.selector_spec_digest)
        _nonblank(self.selector_model_id, "Selector model")
        _nonblank(self.selector_execution_identity, "Selector execution identity")
        ids = tuple(item.assignment_id for item in self.candidate_assignments)
        executions = tuple(item.execution_identity for item in self.candidate_assignments)
        if len(ids) != len(set(ids)) or len(executions) != len(set(executions)):
            raise PolicyViolation("Tournament candidate assignment ve execution tekil olmali")
        if self.selector_assignment_id in ids:
            raise PolicyViolation("Selector candidate assignment olamaz")
        if self.selector_execution_identity in executions:
            raise PolicyViolation("Selector candidate execution identity olamaz")
        if self.selector_model_id in {item.model_id for item in self.candidate_assignments}:
            raise PolicyViolation("Selector candidate model ile ayni olamaz")
        if len(ids) > self.budget.max_candidates:
            raise PolicyViolation("Tournament candidate count budget'i asiyor")
        if sum(item.token_budget for item in self.candidate_assignments) > self.budget.max_tokens:
            raise PolicyViolation("Tournament token budget'i asiyor")
        if (
            sum(item.cost_budget_micros for item in self.candidate_assignments)
            > self.budget.max_cost_micros
        ):
            raise PolicyViolation("Tournament cost budget'i asiyor")
        if self.plan_digest:
            parse_digest(self.plan_digest)
            if self.plan_digest != self.computed_digest:
                raise PolicyViolation("Tournament plan digest mismatch")

    @property
    def candidate_count(self) -> int:
        return len(self.candidate_assignments)

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-tournament-plan/v1",
            "candidate_count": self.candidate_count,
            "candidate_assignments": [item.body() for item in self.candidate_assignments],
            "shared_objective_digest": self.shared_objective_digest,
            "candidate_context_digest": self.candidate_context_digest,
            "candidate_isolation": True,
            "selector_assignment_id": str(self.selector_assignment_id),
            "selector_model_id": self.selector_model_id,
            "selector_execution_identity": self.selector_execution_identity,
            "selector_spec_digest": self.selector_spec_digest,
            "human_final_gate": self.human_final_gate,
            "budget": self.budget.body(),
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> TournamentPlan:
        values["candidate_assignments"] = tuple(
            sorted(values["candidate_assignments"], key=lambda x: str(x.assignment_id))
        )
        values["plan_digest"] = ""
        draft = cls(**values)
        return cls(**{**values, "plan_digest": draft.computed_digest})

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"plan_digest": self.plan_digest}
