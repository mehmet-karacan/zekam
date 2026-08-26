"""Authority-free, vendor-neutral routing contracts for memory workloads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import NotFound, ValidationFailed
from zekam.domain.model_routing import (
    AgentRole,
    LayeredRouteRequest,
    RouteCapabilityBinding,
    RouteCapabilityRequirements,
    RoutingLayer,
)

MEMORY_ROUTING_SCHEMA = "zekam-memory-routing-policy/v1"
MEMORY_TECHNOLOGY = "memory-continuity"


class MemoryWorkload(StrEnum):
    """Reviewed workload labels; none of them names a provider or model."""

    SESSION_SUMMARY = "memory-session-summary"
    CANDIDATE_EXTRACTION = "memory-candidate-extraction"
    CONFLICT_TRIAGE = "memory-conflict-triage"
    SKILL_DRAFT = "memory-skill-draft"
    FAILURE_ROOT_CAUSE = "memory-failure-root-cause"
    CRITICAL_REVIEW = "memory-critical-review"


class MemoryCapabilityClass(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    FAST_CHEAP = "FAST_CHEAP"
    BALANCED = "BALANCED"
    STRONG_REASONING = "STRONG_REASONING"
    CRITICAL_REVIEW = "CRITICAL_REVIEW"


_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})


@dataclass(frozen=True, slots=True)
class MemoryWorkloadRoute:
    workload: MemoryWorkload
    capability_class: MemoryCapabilityClass
    role: AgentRole
    risk: str
    deterministic_prepass: bool
    model_optional: bool
    independent_review: bool

    def __post_init__(self) -> None:
        if self.risk not in _RISK_LEVELS:
            raise ValidationFailed("Memory routing risk gecersiz")
        if not self.deterministic_prepass:
            raise ValidationFailed("Memory workload deterministic prepass ister")
        if self.capability_class is MemoryCapabilityClass.DETERMINISTIC and not self.model_optional:
            raise ValidationFailed("Deterministic workload model kullanamaz")
        if self.risk in {"high", "critical"} and not self.independent_review:
            raise ValidationFailed("High/critical memory route bagimsiz review ister")
        if self.capability_class is MemoryCapabilityClass.CRITICAL_REVIEW and (
            self.role is not AgentRole.VERIFIER or self.risk != "critical"
        ):
            raise ValidationFailed("Critical review verifier ve critical risk ister")

    def body(self) -> dict[str, Any]:
        return {
            "workload": self.workload.value,
            "capability_class": self.capability_class.value,
            "role": self.role.value,
            "risk": self.risk,
            "deterministic_prepass": self.deterministic_prepass,
            "model_optional": self.model_optional,
            "independent_review": self.independent_review,
        }


@dataclass(frozen=True, slots=True)
class MemoryRoutingPolicy:
    routes: tuple[MemoryWorkloadRoute, ...]
    provider_calls_default: bool = False

    def __post_init__(self) -> None:
        workloads = tuple(item.workload for item in self.routes)
        if len(workloads) != len(set(workloads)) or frozenset(workloads) != frozenset(
            MemoryWorkload
        ):
            raise ValidationFailed("Memory routing policy exact workload setini ister")
        if self.provider_calls_default:
            raise ValidationFailed("Memory routing provider call default-deny olmali")

    def route_for(self, workload: MemoryWorkload) -> MemoryWorkloadRoute:
        for route in self.routes:
            if route.workload is workload:
                return route
        raise NotFound(f"Memory workload route bulunamadi: {workload.value}")

    @property
    def policy_digest(self) -> str:
        return digest(
            {
                "schema": MEMORY_ROUTING_SCHEMA,
                "provider_calls_default": self.provider_calls_default,
                "routes": [item.body() for item in sorted(self.routes, key=lambda x: x.workload)],
            }
        )


@dataclass(frozen=True, slots=True)
class MemoryRouteEvidence:
    """Current evidence bindings required before the existing router may select a model."""

    inventory_digest: str
    catalog_snapshot_digest: str
    benchmark_digest: str
    health_digest: str
    cost_latency_digest: str
    policy_digest: str
    execution_target_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.inventory_digest,
            self.catalog_snapshot_digest,
            self.benchmark_digest,
            self.health_digest,
            self.cost_latency_digest,
            self.policy_digest,
            self.execution_target_digest,
        ):
            parse_digest(value)

    @property
    def evidence_digest(self) -> str:
        return digest(
            {
                "inventory_digest": self.inventory_digest,
                "catalog_snapshot_digest": self.catalog_snapshot_digest,
                "benchmark_digest": self.benchmark_digest,
                "health_digest": self.health_digest,
                "cost_latency_digest": self.cost_latency_digest,
                "policy_digest": self.policy_digest,
                "execution_target_digest": self.execution_target_digest,
            }
        )


def capability_requirements(
    capability_class: MemoryCapabilityClass,
) -> RouteCapabilityRequirements:
    """Translate a vendor-neutral capability class to existing measured dimensions."""

    if capability_class is MemoryCapabilityClass.DETERMINISTIC:
        return RouteCapabilityRequirements()
    if capability_class is MemoryCapabilityClass.FAST_CHEAP:
        return RouteCapabilityRequirements(minimum_structured_output_score=0.75)
    if capability_class is MemoryCapabilityClass.BALANCED:
        return RouteCapabilityRequirements(
            minimum_context_tokens=1024,
            minimum_structured_output_score=0.80,
        )
    if capability_class is MemoryCapabilityClass.STRONG_REASONING:
        return RouteCapabilityRequirements(
            minimum_context_tokens=2048,
            minimum_structured_output_score=0.85,
            minimum_long_session_seconds=60,
            minimum_long_session_score=0.75,
        )
    return RouteCapabilityRequirements(
        minimum_context_tokens=2048,
        minimum_structured_output_score=0.90,
        minimum_long_session_seconds=60,
        minimum_long_session_score=0.85,
    )


def build_memory_route_request(
    *,
    policy: MemoryRoutingPolicy,
    workload: MemoryWorkload,
    evidence: MemoryRouteEvidence,
    capability_binding: RouteCapabilityBinding | None,
    requesting_model_id: str | None = None,
    requesting_execution_identity: str | None = None,
) -> LayeredRouteRequest:
    """Build an authority-free request for the existing evidence-bound router."""

    route = policy.route_for(workload)
    requirements = capability_requirements(route.capability_class)
    if bool(requirements.required_dimensions) != (capability_binding is not None):
        raise ValidationFailed("Memory route measured capability binding mismatch")
    if route.independent_review and (
        not requesting_model_id
        or not requesting_execution_identity
        or not requesting_model_id.strip()
        or not requesting_execution_identity.strip()
    ):
        raise ValidationFailed("Independent memory review model ve execution identity ister")
    excluded_model_ids: tuple[str, ...] = ()
    excluded_execution_identities: tuple[str, ...] = ()
    if route.independent_review:
        assert requesting_model_id is not None
        assert requesting_execution_identity is not None
        excluded_model_ids = (requesting_model_id,)
        excluded_execution_identities = (requesting_execution_identity,)
    return LayeredRouteRequest(
        role=route.role,
        target_layer=RoutingLayer.WORKLOAD,
        workload=route.workload.value,
        technology=MEMORY_TECHNOLOGY,
        project_id=None,
        project_context_digest=None,
        inventory_digest=evidence.inventory_digest,
        routing_policy_digest=policy.policy_digest,
        policy_digest=evidence.policy_digest,
        execution_target_digest=evidence.execution_target_digest,
        capability_requirements=requirements,
        capability_binding=capability_binding,
        risk=route.risk,
        excluded_model_ids=excluded_model_ids,
        excluded_execution_identities=excluded_execution_identities,
    )
