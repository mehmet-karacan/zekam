from __future__ import annotations

from zekam.application.layered_model_routing import build_role_policy
from zekam.domain.model_routing import LAYER_ORDER, AgentRole, RoutingLayer


def test_role_policy_requires_exact_layer_prefix_and_role_independence() -> None:
    reviewer = build_role_policy(AgentRole.REVIEWER, RoutingLayer.PROJECT)
    verifier = build_role_policy(AgentRole.VERIFIER, RoutingLayer.PROJECT)

    assert reviewer.required_layers == LAYER_ORDER
    assert reviewer.independent_from_roles == (AgentRole.IMPLEMENTER,)
    assert verifier.independent_from_roles == (AgentRole.IMPLEMENTER, AgentRole.REVIEWER)
    assert reviewer.fallback_model_ids == ()
