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
    ModelFamilyPolicy,
    ProjectRoutingContext,
    RoleRoutingPolicy,
    RouteCapabilityBinding,
    RouteCapabilityDimension,
    RouteCapabilityEvidence,
    RouteCapabilityRequirements,
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
    requirements = values.get("capability_requirements")
    if (
        isinstance(requirements, RouteCapabilityRequirements)
        and requirements.required_dimensions
        and "capability_binding" not in values
    ):
        values["capability_binding"] = _binding()
    return LayeredRouteRequest(**values)  # type: ignore[arg-type]


def _binding(*, role: AgentRole = AgentRole.IMPLEMENTER) -> RouteCapabilityBinding:
    return RouteCapabilityBinding(
        evidence_role=role,
        source_revision="revision-1",
        suite_digest=digest("capability-suite"),
        registry_digest=digest("capability-registry"),
        execution_profile_digest=digest("capability-profile"),
        evaluator_provenance_digest=digest("capability-evaluator"),
    )


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


def _capability(
    model: str,
    dimension: RouteCapabilityDimension,
    *,
    score: float = 0.9,
    quantity: int = 4096,
    receipts: int = 2,
) -> RouteCapabilityEvidence:
    return RouteCapabilityEvidence(
        model_id=model,
        role=AgentRole.IMPLEMENTER,
        dimension=dimension,
        score=score,
        observed_quantity=quantity,
        receipt_count=receipts,
        inventory_digest=INVENTORY,
        policy_digest=POLICY,
        source_revision="revision-1",
        suite_digest=digest("capability-suite"),
        registry_digest=digest("capability-registry"),
        execution_profile_digest=digest("capability-profile"),
        evaluator_provenance_digest=digest("capability-evaluator"),
        source_scorecard_digest=digest(f"scorecard:{model}:{dimension.value}"),
        episode_evidence_digests=(digest(f"episode:{model}:{dimension.value}"),),
        observed_at=NOW - dt.timedelta(minutes=5),
        expires_at=NOW + dt.timedelta(days=1),
    )


def _requirements() -> RouteCapabilityRequirements:
    return RouteCapabilityRequirements(
        minimum_context_tokens=1024,
        minimum_tool_score=0.7,
        minimum_structured_output_score=0.8,
        minimum_long_session_seconds=30,
        minimum_long_session_score=0.75,
    )


def _family_policy() -> ModelFamilyPolicy:
    return ModelFamilyPolicy(
        model_families=(("model-a", "qwen"), ("model-b", "deepseek")),
        same_family_allowed_risks=("low", "medium"),
    )


def _all_capabilities(model: str) -> tuple[RouteCapabilityEvidence, ...]:
    return tuple(_capability(model, dimension) for dimension in RouteCapabilityDimension)


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


def test_all_required_capability_dimensions_select_and_bind_evidence() -> None:
    request = _request(capability_requirements=_requirements())
    evidence = _all_capabilities("model-a")
    decision = decide_layered_model(
        request,
        _policy(),
        _all_layers("model-a", 0.9),
        evidence,
        now=NOW,
    )
    assert decision.status is RouteStatus.SELECTED
    assert decision.primary_model_id == "model-a"
    assert set(decision.candidates[0].evidence_digests) >= {
        item.evidence_digest for item in evidence
    }


@pytest.mark.parametrize("missing", tuple(RouteCapabilityDimension))
def test_missing_capability_dimension_fails_closed(
    missing: RouteCapabilityDimension,
) -> None:
    decision = decide_layered_model(
        _request(capability_requirements=_requirements()),
        _policy(),
        _all_layers("model-a", 0.9),
        tuple(item for item in _all_capabilities("model-a") if item.dimension is not missing),
        now=NOW,
    )
    assert decision.status is RouteStatus.PENDING
    assert f"capability-missing:{missing.value}" in decision.candidates[0].rejection_reasons


def test_capability_threshold_receipt_staleness_and_drift_fail_closed() -> None:
    evidence = list(_all_capabilities("model-a"))
    evidence[0] = replace(evidence[0], observed_quantity=100)
    evidence[1] = replace(evidence[1], score=0.1, receipt_count=0)
    evidence[2] = replace(evidence[2], score=0.1, expires_at=NOW - dt.timedelta(seconds=1))
    evidence[3] = replace(
        evidence[3],
        score=0.1,
        observed_quantity=1,
        receipt_count=0,
        inventory_digest=digest("old-inventory"),
        policy_digest=digest("old-policy"),
    )
    decision = decide_layered_model(
        _request(capability_requirements=_requirements()),
        _policy(),
        _all_layers("model-a", 0.9),
        tuple(evidence),
        now=NOW,
    )
    assert decision.status is RouteStatus.PENDING
    reasons = set(decision.candidates[0].rejection_reasons)
    assert {
        "context-capacity",
        "tool-score",
        "tool-receipt-missing",
        "structured-output-score",
        "capability-stale:structured-output",
        "capability-inventory-drift:long-session",
        "capability-policy-drift:long-session",
        "long-session-duration",
        "long-session-score",
        "long-session-checkpoint-missing",
    } <= reasons


def test_capability_requirements_change_decision_digest() -> None:
    base = decide_layered_model(_request(), _policy(), _all_layers("model-a", 0.9), now=NOW)
    required = decide_layered_model(
        _request(capability_requirements=_requirements()),
        _policy(),
        _all_layers("model-a", 0.9),
        _all_capabilities("model-a"),
        now=NOW,
    )
    assert base.evidence_digest != required.evidence_digest


def test_capability_evidence_from_another_role_cannot_route() -> None:
    decision = decide_layered_model(
        _request(
            capability_requirements=_requirements(),
            capability_binding=_binding(role=AgentRole.REVIEWER),
        ),
        _policy(),
        _all_layers("model-a", 0.9),
        _all_capabilities("model-a"),
        now=NOW,
    )
    assert decision.status is RouteStatus.PENDING
    assert set(decision.candidates[0].rejection_reasons) == {
        f"capability-missing:{dimension.value}" for dimension in RouteCapabilityDimension
    }


def test_capability_suite_registry_profile_evaluator_and_source_drift_fail_closed() -> None:
    evidence = tuple(
        replace(
            item,
            source_revision="old-revision",
            suite_digest=digest("old-suite"),
            registry_digest=digest("old-registry"),
            execution_profile_digest=digest("old-profile"),
            evaluator_provenance_digest=digest("old-evaluator"),
        )
        for item in _all_capabilities("model-a")
    )
    decision = decide_layered_model(
        _request(capability_requirements=_requirements()),
        _policy(),
        _all_layers("model-a", 0.9),
        evidence,
        now=NOW,
    )
    assert decision.status is RouteStatus.PENDING
    reasons = set(decision.candidates[0].rejection_reasons)
    for dimension in RouteCapabilityDimension:
        assert {
            f"capability-source-drift:{dimension.value}",
            f"capability-suite-drift:{dimension.value}",
            f"capability-registry-drift:{dimension.value}",
            f"capability-profile-drift:{dimension.value}",
            f"capability-evaluator-drift:{dimension.value}",
        } <= reasons


@pytest.mark.parametrize(
    "changes",
    (
        {"minimum_context_tokens": 1.5},
        {"minimum_long_session_seconds": True},
        {"minimum_tool_score": "0.8"},
        {"minimum_structured_output_score": False},
    ),
)
def test_capability_requirement_types_fail_closed(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationFailed):
        RouteCapabilityRequirements(**changes)  # type: ignore[arg-type]


def test_high_risk_verifier_rejects_same_family_and_selects_distinct_family() -> None:
    family_policy = _family_policy()
    request = _request(
        role=AgentRole.VERIFIER,
        risk="high",
        family_policy_digest=family_policy.policy_digest,
        excluded_model_families=("qwen",),
        excluded_model_ids=("builder-model",),
        excluded_execution_identities=("builder-execution",),
    )
    policy = replace(
        _policy(),
        role=AgentRole.VERIFIER,
        independent_from_roles=(AgentRole.IMPLEMENTER,),
    )
    qualifications = tuple(
        replace(item, role=AgentRole.VERIFIER)
        for item in _all_layers("model-a", 0.9) + _all_layers("model-b", 0.8)
    )
    decision = decide_layered_model(
        request,
        policy,
        qualifications,
        family_policy=family_policy,
        now=NOW,
    )
    assert decision.status is RouteStatus.SELECTED
    assert decision.primary_model_id == "model-b"
    same_family = next(item for item in decision.candidates if item.model_id == "model-a")
    assert "same-family-verifier" in same_family.rejection_reasons


def test_low_risk_policy_explicitly_allows_same_family_verifier() -> None:
    family_policy = _family_policy()
    request = _request(
        role=AgentRole.VERIFIER,
        risk="low",
        family_policy_digest=family_policy.policy_digest,
        excluded_model_families=("qwen",),
        excluded_model_ids=("builder-model",),
        excluded_execution_identities=("builder-execution",),
    )
    policy = replace(
        _policy(),
        role=AgentRole.VERIFIER,
        independent_from_roles=(AgentRole.IMPLEMENTER,),
    )
    qualifications = tuple(
        replace(item, role=AgentRole.VERIFIER) for item in _all_layers("model-a", 0.9)
    )
    decision = decide_layered_model(
        request,
        policy,
        qualifications,
        family_policy=family_policy,
        now=NOW,
    )
    assert decision.status is RouteStatus.SELECTED
    assert decision.primary_model_id == "model-a"


def test_family_policy_digest_drift_is_rejected() -> None:
    with pytest.raises(PolicyViolation, match="family policy drift"):
        decide_layered_model(
            _request(family_policy_digest=digest("old-family-policy")),
            _policy(),
            _all_layers("model-a", 0.9),
            family_policy=_family_policy(),
            now=NOW,
        )


def test_high_risk_verifier_without_family_evidence_is_pending() -> None:
    request = _request(
        role=AgentRole.VERIFIER,
        risk="critical",
        excluded_model_ids=("builder-model",),
        excluded_execution_identities=("builder-execution",),
    )
    policy = replace(
        _policy(),
        role=AgentRole.VERIFIER,
        independent_from_roles=(AgentRole.IMPLEMENTER,),
    )
    qualifications = tuple(
        replace(item, role=AgentRole.VERIFIER) for item in _all_layers("model-a", 0.9)
    )
    decision = decide_layered_model(request, policy, qualifications, now=NOW)
    assert decision.status is RouteStatus.PENDING
    assert "family-independence-evidence-missing" in decision.candidates[0].rejection_reasons


def test_high_risk_same_family_requires_explicit_policy_allowance() -> None:
    family_policy = ModelFamilyPolicy(
        model_families=(("model-a", "qwen"),),
        same_family_allowed_risks=("high",),
    )
    request = _request(
        role=AgentRole.VERIFIER,
        risk="high",
        family_policy_digest=family_policy.policy_digest,
        excluded_model_families=("qwen",),
        excluded_model_ids=("builder-model",),
        excluded_execution_identities=("builder-execution",),
    )
    policy = replace(
        _policy(),
        role=AgentRole.VERIFIER,
        independent_from_roles=(AgentRole.IMPLEMENTER,),
    )
    qualifications = tuple(
        replace(item, role=AgentRole.VERIFIER) for item in _all_layers("model-a", 0.9)
    )
    decision = decide_layered_model(
        request,
        policy,
        qualifications,
        family_policy=family_policy,
        now=NOW,
    )
    assert decision.status is RouteStatus.SELECTED


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
