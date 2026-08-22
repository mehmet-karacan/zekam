from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_routing import (
    AgentRole,
    CandidateDisposition,
    LayeredRouteRequest,
    ProjectRoutingContext,
    RoleRoutingPolicy,
    RouteStatus,
    RoutingLayer,
    RoutingQualification,
    StaleReason,
    decide_layered_model,
)

NOW = dt.datetime(2026, 8, 22, 12, tzinfo=dt.UTC)
PROJECT_ID = uuid4()
CONTEXT_DIGEST = digest("project-context")
INVENTORY = digest("inventory")
POLICY = digest("policy")
ROUTING_POLICY = digest("routing-policy")


def _policy(*, fallback: tuple[str, ...] = ("model-b",)) -> RoleRoutingPolicy:
    return RoleRoutingPolicy(
        role=AgentRole.IMPLEMENTER,
        target_layer=RoutingLayer.PROJECT,
        required_layers=(
            RoutingLayer.GENERAL,
            RoutingLayer.WORKLOAD,
            RoutingLayer.PROJECT,
        ),
        top_k=3,
        fallback_model_ids=fallback,
        max_cost=2.0,
        max_latency_ms=30_000,
        independent_from_roles=(),
        policy_digest=ROUTING_POLICY,
    )


def _request(**changes: object) -> LayeredRouteRequest:
    values: dict[str, object] = {
        "role": AgentRole.IMPLEMENTER,
        "target_layer": RoutingLayer.PROJECT,
        "workload": "code",
        "technology": "java",
        "project_id": PROJECT_ID,
        "project_context_digest": CONTEXT_DIGEST,
        "inventory_digest": INVENTORY,
        "routing_policy_digest": ROUTING_POLICY,
        "policy_digest": POLICY,
        "execution_target_digest": digest("execution-target"),
    }
    values.update(changes)
    return LayeredRouteRequest(**values)  # type: ignore[arg-type]


def _qualification(model: str, layer: RoutingLayer, score: float) -> RoutingQualification:
    return RoutingQualification(
        model_id=model,
        layer=layer,
        role=AgentRole.IMPLEMENTER,
        suite_digest=digest(f"suite:{model}:{layer.value}"),
        aggregate_id=uuid4(),
        aggregate_evidence_digest=digest(f"aggregate:{model}:{layer.value}"),
        health_result_id=uuid4(),
        health_evidence_digest=digest(f"health:{model}"),
        inventory_digest=INVENTORY,
        policy_digest=POLICY,
        verifier_model_id="independent-verifier",
        verifier_execution_identity=f"verify:{model}:{layer.value}",
        tested_execution_identity=f"tested:{model}:{layer.value}",
        score=score,
        mean_latency_ms=100,
        mean_cost=0.1,
        workload=None if layer is RoutingLayer.GENERAL else "code",
        technology=None if layer is RoutingLayer.GENERAL else "java",
        project_context_digest=CONTEXT_DIGEST if layer is RoutingLayer.PROJECT else None,
        qualified=True,
        unsafe=False,
        valid_from=NOW - dt.timedelta(hours=1),
        expires_at=NOW + dt.timedelta(days=1),
    )


def _all_layers(model: str, score: float) -> tuple[RoutingQualification, ...]:
    return tuple(_qualification(model, layer, score) for layer in RoutingLayer)


def test_three_layer_intersection_selects_primary_and_explicit_fallback() -> None:
    decision = decide_layered_model(
        _request(),
        _policy(),
        _all_layers("model-a", 0.9) + _all_layers("model-b", 0.8),
        now=NOW,
    )
    assert decision.status is RouteStatus.SELECTED
    assert decision.primary_model_id == "model-a"
    assert decision.fallback_model_id == "model-b"
    dispositions = {item.model_id: item.disposition for item in decision.candidates}
    assert dispositions == {
        "model-a": CandidateDisposition.PRIMARY,
        "model-b": CandidateDisposition.FALLBACK,
    }
    assert decision.authority_granted is False


def test_missing_project_layer_is_pending_and_never_guesses() -> None:
    partial = tuple(
        _qualification("model-a", layer, 0.9)
        for layer in (RoutingLayer.GENERAL, RoutingLayer.WORKLOAD)
    )
    decision = decide_layered_model(_request(), _policy(), partial, now=NOW)
    assert decision.status is RouteStatus.PENDING
    assert decision.primary_model_id is None
    assert decision.candidates[0].rejection_reasons == ("missing:project",)


def test_stale_health_qualification_policy_and_inventory_drift_fail_closed() -> None:
    stale = replace(
        _qualification("model-a", RoutingLayer.PROJECT, 1.0),
        expires_at=NOW - dt.timedelta(seconds=1),
        inventory_digest=digest("old-inventory"),
        policy_digest=digest("old-policy"),
    )
    qualifications = (
        _qualification("model-a", RoutingLayer.GENERAL, 1.0),
        _qualification("model-a", RoutingLayer.WORKLOAD, 1.0),
        stale,
    )
    decision = decide_layered_model(_request(), _policy(), qualifications, now=NOW)
    assert decision.status is RouteStatus.PENDING
    assert set(decision.candidates[0].rejection_reasons) == {
        "stale:project",
        "inventory-drift:project",
        "policy-drift:project",
    }


def test_fallback_scope_cannot_silently_expand() -> None:
    decision = decide_layered_model(
        _request(),
        _policy(fallback=("not-present",)),
        _all_layers("model-a", 0.9) + _all_layers("model-b", 0.8),
        now=NOW,
    )
    assert decision.primary_model_id == "model-a"
    assert decision.fallback_model_id is None


def test_budget_filter_precedes_score_and_rejects_expensive_best_model() -> None:
    expensive = tuple(
        replace(item, score=1.0, mean_cost=3.0, mean_latency_ms=40_000)
        for item in _all_layers("model-a", 1.0)
    )
    decision = decide_layered_model(
        _request(), _policy(), expensive + _all_layers("model-b", 0.5), now=NOW
    )
    assert decision.primary_model_id == "model-b"
    rejected = next(item for item in decision.candidates if item.model_id == "model-a")
    assert "cost-budget:general" in rejected.rejection_reasons
    assert "latency-budget:project" in rejected.rejection_reasons


def test_model_and_execution_independence_exclusions_reject_candidate() -> None:
    decision = decide_layered_model(
        _request(
            excluded_model_ids=("model-a",),
            excluded_execution_identities=("tested:model-b:project",),
        ),
        _policy(),
        _all_layers("model-a", 0.9) + _all_layers("model-b", 0.8),
        now=NOW,
    )
    assert decision.status is RouteStatus.PENDING
    reasons = {item.model_id: item.rejection_reasons for item in decision.candidates}
    assert "model-independence" in reasons["model-a"]
    assert "execution-independence" in reasons["model-b"]


def test_verifier_must_be_independent_by_model_and_execution() -> None:
    base = _qualification("model-a", RoutingLayer.GENERAL, 1.0)
    with pytest.raises(PolicyViolation, match="kendi verifier"):
        replace(base, verifier_model_id="model-a")
    with pytest.raises(PolicyViolation, match="execution identity"):
        replace(base, verifier_execution_identity=base.tested_execution_identity)


def test_project_context_reports_each_exact_drift_reason() -> None:
    context = ProjectRoutingContext(
        project_id=PROJECT_ID,
        source_revision_id=uuid4(),
        source_revision="abc123",
        tree_digest=digest("tree"),
        capability_profile_digest=digest("capability"),
        dependency_digest=digest("dependency"),
        framework_digest=digest("framework"),
        technology_digest=digest("technology"),
        architecture_digest=digest("architecture"),
        rules_digest=digest("rules"),
        suite_digest=digest("suite"),
        inventory_digest=INVENTORY,
        policy_digest=POLICY,
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(hours=1),
    )
    current = replace(
        context,
        source_revision_id=uuid4(),
        source_revision="def456",
        tree_digest=digest("tree-2"),
        framework_digest=digest("framework-2"),
        rules_digest=digest("rules-2"),
    )
    assert set(context.stale_reasons(current, now=NOW + dt.timedelta(hours=2))) == {
        StaleReason.SOURCE_REVISION,
        StaleReason.TREE,
        StaleReason.FRAMEWORK,
        StaleReason.RULES,
        StaleReason.EXPIRED,
    }


def test_policy_requires_exact_layer_prefix() -> None:
    with pytest.raises(ValidationFailed, match="exact prefix"):
        replace(_policy(), required_layers=(RoutingLayer.PROJECT,))


def test_independent_role_policy_requires_model_and_execution_exclusions() -> None:
    policy = replace(_policy(), independent_from_roles=(AgentRole.REVIEWER,))
    decision = decide_layered_model(_request(), policy, _all_layers("model-a", 0.9), now=NOW)
    assert decision.status is RouteStatus.PENDING
    assert decision.candidates[0].rejection_reasons == ("independence-evidence-missing",)
