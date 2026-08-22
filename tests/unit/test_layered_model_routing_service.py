from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from zekam.application.layered_model_routing import build_role_policy
from zekam.domain.model_routing import LAYER_ORDER, AgentRole, RoutingLayer
from zekam.interfaces.cli import model_routing as routing_cli


def test_role_policy_requires_exact_layer_prefix_and_role_independence() -> None:
    reviewer = build_role_policy(AgentRole.REVIEWER, RoutingLayer.PROJECT)
    verifier = build_role_policy(AgentRole.VERIFIER, RoutingLayer.PROJECT)

    assert reviewer.required_layers == LAYER_ORDER
    assert reviewer.independent_from_roles == (AgentRole.IMPLEMENTER,)
    assert verifier.independent_from_roles == (AgentRole.IMPLEMENTER, AgentRole.REVIEWER)
    assert reviewer.fallback_model_ids == ()


def test_opencode_execution_target_binds_parallel_model_selection(tmp_path, monkeypatch) -> None:
    executable = tmp_path / "opencode.exe"
    executable.write_bytes(b"reviewed-opencode")
    context = SimpleNamespace(
        settings=SimpleNamespace(clients=(SimpleNamespace(name="opencode", executable=executable),))
    )
    monkeypatch.setattr(routing_cli, "build_context", lambda **_: context)

    target = routing_cli._execution_target(None, now=dt.datetime.now(dt.UTC))

    assert target.execution_mode == "native-parallel"
    assert target.model_selectable is True
    assert target.max_concurrency == 3
