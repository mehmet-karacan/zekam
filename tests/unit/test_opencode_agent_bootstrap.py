from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.application.opencode_agent_bootstrap import (
    DEFAULT_AGENT,
    apply_opencode_agent_bootstrap,
    plan_opencode_agent_bootstrap,
)
from zekam.domain.errors import ConfigurationError


def _executable(tmp_path: Path) -> Path:
    value = tmp_path / "opencode.exe"
    value.write_text("stub", encoding="utf-8")
    return value


def test_apply_installs_global_agents_and_preserves_provider_configuration(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    config = user_home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"provider": {"litellm": {"options": {"timeout": 60}}}}))

    plan = plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)
    apply_opencode_agent_bootstrap(plan)

    stored = json.loads(config.read_text(encoding="utf-8"))
    assert stored["default_agent"] == DEFAULT_AGENT
    assert stored["provider"]["litellm"]["options"]["timeout"] == 60
    agents = user_home / ".config" / "opencode" / "agents"
    installed = {item.name for item in agents.iterdir()}
    assert {
        "zekam-builder.md",
        "zekam-coordinator.md",
        "zekam-memory-curator.md",
        "zekam-researcher.md",
        "zekam-router.md",
        "zekam-verifier.md",
    } <= installed
    assert "Cikti disiplini" in (agents / "zekam-coordinator.md").read_text(encoding="utf-8")
    coordinator = (agents / "zekam-coordinator.md").read_text(encoding="utf-8")
    assert "webfetch: allow" in coordinator
    assert '"*": deny' in coordinator
    assert '"zekam-builder": allow' in coordinator
    assert "Kendin terminal" in coordinator
    assert "Dispatch protokolu" in coordinator
    assert "Eszamanli child sayisi ucu gecemez" in coordinator
    assert '"zekam-router": allow' in coordinator
    assert '"zekam-implementer-*": allow' in coordinator
    model_agents = [name for name in installed if name.startswith("zekam-implementer-")]
    assert model_agents
    model_agent = (agents / model_agents[0]).read_text(encoding="utf-8")
    assert "model: litellm/" in model_agent
    assert "hidden: true" in model_agent
    assert "canonical_model_id=" in model_agent
    plugin = user_home / ".config" / "opencode" / "plugins" / "zekam-lifecycle.js"
    assert plugin.is_file()
    assert "tool.execute.before" in plugin.read_text(encoding="utf-8")
    assert "session.error" in plugin.read_text(encoding="utf-8")
    assert '"--task-label"' in plugin.read_text(encoding="utf-8")
    verifier = (agents / "zekam-verifier.md").read_text(encoding="utf-8")
    assert '"zekam doctor *": allow' in verifier
    assert '"zekam work list *": allow' in verifier
    assert '"*": ask' in verifier
    repeat = plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)
    assert repeat.agents_to_create == ()
    assert not repeat.config_update_required


def test_missing_opencode_has_no_global_side_effect_plan(tmp_path: Path) -> None:
    plan = plan_opencode_agent_bootstrap(executable=None, user_home=tmp_path / "user")
    apply_opencode_agent_bootstrap(plan)
    assert not plan.available
    assert not plan.config_path.exists()
    assert not plan.lifecycle_plugin_to_create


def test_conflicting_owned_agent_fails_closed(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    agents = user_home / ".config" / "opencode" / "agents"
    agents.mkdir(parents=True)
    (agents / "zekam-coordinator.md").write_text("custom", encoding="utf-8")

    plan = plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)
    assert plan.conflicting_agents == ("zekam-coordinator.md",)
    with pytest.raises(ConfigurationError, match="cakisiyor"):
        apply_opencode_agent_bootstrap(plan)
