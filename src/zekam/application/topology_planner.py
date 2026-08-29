"""TaskPlan oncesi olcumlu execution topology suitability karari."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.execution_topology import (
    ExecutionTopologyDecision,
    ExecutionTopologyPattern,
    GraphNodeMode,
    LoopSuitabilityAssessment,
    MeasurementSourceTier,
)
from zekam.domain.work import EffectKind, TaskPlan

IRREVERSIBLE_EFFECT_LABELS = frozenset(
    {
        "deploy",
        "release",
        "migration-apply",
        "git-push",
        "git-merge",
        "external-message",
        "email-send",
        "form-submit",
        "payment",
        "crypto-transfer",
        "signature",
        "captcha",
        "mfa",
        "otp",
        "destructive-delete",
        "policy-relaxation",
        "authorization-relaxation",
    }
)


@dataclass(frozen=True, slots=True)
class TopologySuitabilityRequest:
    plan: TaskPlan
    objective_digest: str
    measurement_available: bool | None
    measurement_source_tier: MeasurementSourceTier
    measurement_estimated_cost_micros: int | None
    action_estimated_cost_micros: int | None
    reversible: bool | None
    idempotent_or_receipt_bound: bool | None
    creative_diversity_goal: bool = False
    human_judgment_required: bool = False
    distinct_deliverable_count: int | None = 1
    expected_coordination_cost_micros: int | None = 0
    high_risk_effects: tuple[str, ...] = ()
    parallel_ready_count: int = 1
    fan_in_required: bool = False
    requested_pattern: ExecutionTopologyPattern | None = None
    node_modes: tuple[tuple[str, GraphNodeMode], ...] = ()
    parallelism_ceiling: int = 1
    estimated_calls: int = 1
    estimated_tokens: int = 0
    estimated_cost_micros: int = 0

    def __post_init__(self) -> None:
        if self.parallel_ready_count < 0:
            raise ValidationFailed("Parallel ready count negatif olamaz")
        if self.parallelism_ceiling < 1:
            raise ValidationFailed("Parallelism ceiling pozitif olmali")
        if min(self.estimated_calls, self.estimated_tokens, self.estimated_cost_micros) < 0:
            raise ValidationFailed("Topology tahminleri negatif olamaz")
        normalized = tuple(item.strip().lower() for item in self.high_risk_effects)
        if any(not item for item in normalized):
            raise ValidationFailed("High-risk effect etiketi bos olamaz")
        step_ids = {item.step_id for item in self.plan.steps}
        mode_ids = tuple(item[0] for item in self.node_modes)
        if len(mode_ids) != len(set(mode_ids)) or not set(mode_ids) <= step_ids:
            raise ValidationFailed("Node mode exact TaskPlan step'ine baglanmali")

    @property
    def dependency_edge_count(self) -> int:
        return sum(len(step.depends_on) for step in self.plan.steps)

    @property
    def normalized_high_risk_effects(self) -> tuple[str, ...]:
        return tuple(sorted({item.strip().lower() for item in self.high_risk_effects}))


@dataclass(frozen=True, slots=True)
class TopologyPlanner:
    """Suitability sonucunu TaskPlan digest'ine bagli authority-free karara cevirir."""

    maximum_measurement_to_action_ratio: Decimal = Decimal("0.5")

    def __post_init__(self) -> None:
        if not Decimal("0") <= self.maximum_measurement_to_action_ratio <= Decimal("1"):
            raise ValidationFailed("Measurement/action ratio threshold 0..1 olmali")

    def assess(self, request: TopologySuitabilityRequest) -> LoopSuitabilityAssessment:
        pattern, reasons = self._recommend(request)
        return LoopSuitabilityAssessment.create(
            measurement_available=request.measurement_available,
            measurement_source_tier=request.measurement_source_tier,
            measurement_estimated_cost_micros=request.measurement_estimated_cost_micros,
            action_estimated_cost_micros=request.action_estimated_cost_micros,
            reversible=request.reversible,
            idempotent_or_receipt_bound=request.idempotent_or_receipt_bound,
            creative_diversity_goal=request.creative_diversity_goal,
            human_judgment_required=request.human_judgment_required,
            distinct_deliverable_count=request.distinct_deliverable_count,
            dependency_edge_count=request.dependency_edge_count,
            expected_coordination_cost_micros=request.expected_coordination_cost_micros,
            recommended_pattern=pattern,
            reason_codes=reasons,
        )

    def decide(self, request: TopologySuitabilityRequest) -> ExecutionTopologyDecision:
        assessment = self.assess(request)
        pattern = assessment.recommended_pattern
        modes = request.node_modes or self._default_modes(request, pattern)
        human_gates = (
            tuple(f"effect:{label}" for label in request.normalized_high_risk_effects)
            or ("exact-human-review",)
            if pattern is ExecutionTopologyPattern.QUEUE_HUMAN_REVIEW
            else ()
        )
        ceiling = request.parallelism_ceiling
        if pattern not in {
            ExecutionTopologyPattern.GRAPH,
            ExecutionTopologyPattern.TOURNAMENT,
        }:
            ceiling = 1
        return ExecutionTopologyDecision.create(
            pattern=pattern,
            objective_digest=request.objective_digest,
            plan_digest=request.plan.plan_digest,
            node_modes=modes,
            parallelism_ceiling=ceiling,
            estimated_calls=request.estimated_calls,
            estimated_tokens=request.estimated_tokens,
            estimated_cost_micros=request.estimated_cost_micros,
            estimated_coordination_overhead_micros=(request.expected_coordination_cost_micros or 0),
            required_human_gates=human_gates,
            reason_codes=assessment.reason_codes,
        )

    def _recommend(
        self, request: TopologySuitabilityRequest
    ) -> tuple[ExecutionTopologyPattern, tuple[str, ...]]:
        high_risk = set(request.normalized_high_risk_effects)
        unknown_risk = high_risk - IRREVERSIBLE_EFFECT_LABELS
        plan_high_risk = any(
            step.risk.lower() in {"high", "critical"} for step in request.plan.steps
        )
        git_push = any(step.effect is EffectKind.GIT_PUSH for step in request.plan.steps)
        effectful = any(step.effect is not EffectKind.NONE for step in request.plan.steps)
        if high_risk or plan_high_risk or git_push or (effectful and request.reversible is None):
            codes = ["irreversible-or-high-risk-human-gate"]
            if unknown_risk:
                codes.append("unknown-critical-effect-fail-closed")
            if effectful and request.reversible is None:
                codes.append("reversibility-unknown")
            return ExecutionTopologyPattern.QUEUE_HUMAN_REVIEW, tuple(codes)
        if request.reversible is False:
            return (
                ExecutionTopologyPattern.QUEUE_HUMAN_REVIEW,
                ("irreversible-effect-human-gate",),
            )
        if request.creative_diversity_goal:
            return ExecutionTopologyPattern.TOURNAMENT, ("creative-diversity-tournament",)

        distinct = request.distinct_deliverable_count
        graph_evidence = (
            distinct is not None
            and distinct >= 2
            and (
                request.dependency_edge_count > 0
                or request.parallel_ready_count >= 2
                or request.fan_in_required
            )
        )
        if request.requested_pattern is ExecutionTopologyPattern.GRAPH and not graph_evidence:
            if distinct is None:
                return ExecutionTopologyPattern.BLOCKED, ("graph-critical-field-unknown",)
            return (
                ExecutionTopologyPattern.SINGLE_PASS,
                ("graph-rejected-single-artifact",),
            )
        if graph_evidence:
            if request.expected_coordination_cost_micros is None:
                return ExecutionTopologyPattern.BLOCKED, ("coordination-cost-unknown",)
            if request.action_estimated_cost_micros is not None and (
                request.expected_coordination_cost_micros > request.action_estimated_cost_micros
            ):
                return (
                    ExecutionTopologyPattern.SINGLE_PASS,
                    ("graph-coordination-cost-too-high",),
                )
            return ExecutionTopologyPattern.GRAPH, ("distinct-deliverable-graph",)

        if request.measurement_available is None:
            return ExecutionTopologyPattern.BLOCKED, ("measurement-availability-unknown",)
        measured = (
            request.measurement_available
            and request.measurement_source_tier.supports_measured_progress
        )
        if request.measurement_available and not measured:
            return ExecutionTopologyPattern.SINGLE_PASS, ("measurement-source-not-independent",)
        if not measured:
            return ExecutionTopologyPattern.SINGLE_PASS, ("measurement-unavailable-no-loop",)
        if request.idempotent_or_receipt_bound is None:
            return ExecutionTopologyPattern.SINGLE_PASS, ("retry-safety-unknown",)
        if not request.idempotent_or_receipt_bound:
            return ExecutionTopologyPattern.SINGLE_PASS, ("retry-not-safe",)
        ratio = self._measurement_ratio(request)
        if ratio is None:
            return ExecutionTopologyPattern.SINGLE_PASS, ("measurement-cost-unknown",)
        if ratio > self.maximum_measurement_to_action_ratio:
            return ExecutionTopologyPattern.SINGLE_PASS, ("measurement-too-expensive",)
        if len(request.plan.steps) == 1 and request.plan.steps[0].effect is EffectKind.NONE:
            return ExecutionTopologyPattern.DIRECT, ("deterministic-single-operation",)
        return ExecutionTopologyPattern.BOUNDED_LOOP, ("cheap-measurement-reversible-loop",)

    @staticmethod
    def _measurement_ratio(request: TopologySuitabilityRequest) -> Decimal | None:
        measurement = request.measurement_estimated_cost_micros
        action = request.action_estimated_cost_micros
        if measurement is None or action is None or action <= 0:
            return None
        return Decimal(measurement) / Decimal(action)

    @staticmethod
    def _default_modes(
        request: TopologySuitabilityRequest, pattern: ExecutionTopologyPattern
    ) -> tuple[tuple[str, GraphNodeMode], ...]:
        if pattern is ExecutionTopologyPattern.BOUNDED_LOOP:
            mode = GraphNodeMode.BOUNDED_LOOP
        elif pattern is ExecutionTopologyPattern.TOURNAMENT:
            mode = GraphNodeMode.TOURNAMENT
        elif pattern is ExecutionTopologyPattern.QUEUE_HUMAN_REVIEW:
            mode = GraphNodeMode.HUMAN_GATE
        else:
            mode = GraphNodeMode.DIRECT
        return tuple((step.step_id, mode) for step in request.plan.steps)


def assert_topology_matches_plan(decision: ExecutionTopologyDecision, plan: TaskPlan) -> None:
    """Admission yardimcisi: stale veya farkli plan kararini fail-closed reddeder."""
    if decision.plan_digest != plan.plan_digest:
        raise PolicyViolation("Topology decision current TaskPlan'a bagli degil")
    if {item[0] for item in decision.node_modes} != {step.step_id for step in plan.steps}:
        raise PolicyViolation("Topology node modes exact TaskPlan step setini kapsamiyor")
