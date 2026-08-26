from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from zekam.application.memory_routing import (
    default_memory_routing_policy_file,
    load_memory_routing_policy,
    sanitized_memory_routing_policy,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.memory_routing import (
    MemoryCapabilityClass,
    MemoryRouteEvidence,
    MemoryWorkload,
    build_memory_route_request,
    capability_requirements,
)
from zekam.domain.model_routing import AgentRole, RouteCapabilityBinding, RoutingLayer


def _evidence() -> MemoryRouteEvidence:
    return MemoryRouteEvidence(
        inventory_digest=digest("inventory"),
        catalog_snapshot_digest=digest("catalog"),
        benchmark_digest=digest("benchmark"),
        health_digest=digest("health"),
        cost_latency_digest=digest("cost-latency"),
        policy_digest=digest("policy"),
        execution_target_digest=digest("target"),
    )


def _binding(role: AgentRole) -> RouteCapabilityBinding:
    return RouteCapabilityBinding(
        evidence_role=role,
        source_revision="revision-1",
        suite_digest=digest("suite"),
        registry_digest=digest("registry"),
        execution_profile_digest=digest("execution-profile"),
        evaluator_provenance_digest=digest("evaluator"),
    )


def test_default_policy_is_exact_provider_neutral_and_authority_free() -> None:
    policy = load_memory_routing_policy()
    report = sanitized_memory_routing_policy(policy)
    assert report["workload_count"] == 6
    assert report["provider_calls_default"] is False
    assert report["grants_authority"] is False
    serialized = default_memory_routing_policy_file().read_text(encoding="utf-8").casefold()
    assert "provider:" not in serialized
    assert "model_id" not in serialized


def test_high_risk_route_excludes_requester_model_and_execution_identity() -> None:
    policy = load_memory_routing_policy()
    route = policy.route_for(MemoryWorkload.CONFLICT_TRIAGE)
    request = build_memory_route_request(
        policy=policy,
        workload=route.workload,
        evidence=_evidence(),
        capability_binding=_binding(route.role),
        requesting_model_id="model-under-review",
        requesting_execution_identity="execution-under-review",
    )
    assert request.target_layer is RoutingLayer.WORKLOAD
    assert request.workload == "memory-conflict-triage"
    assert request.technology == "memory-continuity"
    assert request.excluded_model_ids == ("model-under-review",)
    assert request.excluded_execution_identities == ("execution-under-review",)


def test_independent_route_fails_closed_without_both_identities() -> None:
    policy = load_memory_routing_policy()
    route = policy.route_for(MemoryWorkload.CRITICAL_REVIEW)
    with pytest.raises(ValidationFailed, match="model ve execution identity"):
        build_memory_route_request(
            policy=policy,
            workload=route.workload,
            evidence=_evidence(),
            capability_binding=_binding(route.role),
        )


def test_deterministic_capability_never_requires_model_binding() -> None:
    requirements = capability_requirements(MemoryCapabilityClass.DETERMINISTIC)
    assert requirements.required_dimensions == ()


def test_unknown_field_or_missing_workload_is_rejected(tmp_path: Path) -> None:
    document = yaml.safe_load(default_memory_routing_policy_file().read_text(encoding="utf-8"))
    document["workloads"].pop("memory-session-summary")
    document["unexpected"] = True
    target = tmp_path / "memory-routing.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValidationFailed, match="exact shape"):
        load_memory_routing_policy(target)
