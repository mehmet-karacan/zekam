from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

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
    for agent_path in agents.glob("*.md"):
        body = agent_path.read_text(encoding="utf-8")
        frontmatter = body.split("---", 2)[1]
        assert isinstance(yaml.safe_load(frontmatter), dict), agent_path.name
    assert "Cikti disiplini" in (agents / "zekam-coordinator.md").read_text(encoding="utf-8")
    coordinator = (agents / "zekam-coordinator.md").read_text(encoding="utf-8")
    builder = (agents / "zekam-builder.md").read_text(encoding="utf-8")
    researcher = (agents / "zekam-researcher.md").read_text(encoding="utf-8")
    verifier = (agents / "zekam-verifier.md").read_text(encoding="utf-8")
    assert "webfetch: allow" in coordinator
    assert '"*": allow' in coordinator
    assert "edit: allow" in coordinator
    assert '"C:/innova/projeler/**": allow' in coordinator
    assert '"zekam-builder": allow' in coordinator
    assert "tekrar onay istemeden" in coordinator
    assert '"*git commit*": deny' in coordinator
    assert '"*git push*": deny' in coordinator
    assert '"*git clone*": deny' in coordinator
    assert '"*git worktree add*": deny' in coordinator
    assert '"*Copy-Item*": deny' in coordinator
    assert '"git commit *": deny' in coordinator
    assert '"git push *": deny' in coordinator
    assert "detached worktree veya gecici proje klonu olusturma" in coordinator
    assert "Zekam source rootuna geçici rapor, memo" in coordinator
    assert "Zekam source rootuna geçici rapor, memo" in builder
    assert "Zekam source rootuna memo, rapor" in researcher
    assert "Zekam source rootuna memo, rapor" in verifier
    assert "Dispatch protokolu" in coordinator
    assert "Eszamanli child sayisi ucu gecemez" in coordinator
    assert '"zekam-router": allow' in coordinator
    assert '"zekam-implementer-*": allow' in coordinator
    assert "zekam project source-root" in coordinator
    assert "Tum inceleme, Git kaniti, test ve kod degisikliklerini" in coordinator
    model_agents = [name for name in installed if name.startswith("zekam-implementer-")]
    assert model_agents
    model_agent = (agents / model_agents[0]).read_text(encoding="utf-8")
    assert "model: litellm/" in model_agent
    assert "hidden: true" in model_agent
    assert "canonical_model_id=" in model_agent
    assert "edit: allow" in model_agent
    assert '"C:/innova/projeler/**": allow' in model_agent
    assert '"*git commit*": deny' in model_agent
    assert '"*git clone*": deny' in model_agent
    assert '"*git worktree add*": deny' in model_agent
    plugin = user_home / ".config" / "opencode" / "plugins" / "zekam-lifecycle.js"
    assert plugin.is_file()
    assert "tool.execute.before" in plugin.read_text(encoding="utf-8")
    assert "session.error" in plugin.read_text(encoding="utf-8")
    assert '"--task-label"' in plugin.read_text(encoding="utf-8")
    assert '"zekam doctor *": allow' in verifier
    assert '"zekam work list *": allow' in verifier
    assert '"*": ask' in verifier
    assert '"C:/innova/projeler/**": allow' in verifier
    assert '"zekam project source-root *": allow' in verifier
    assert '"C:/innova/projeler/**": allow' in researcher
    assert '"zekam project source-root *": allow' in researcher
    assert '"git -C * log*": allow' in researcher
    assert "kopya, mirror, clone" in researcher
    repeat = plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)
    assert repeat.agents_to_create == ()
    assert repeat.agents_to_update == ()
    assert not repeat.config_update_required


def test_managed_agent_policy_is_upgraded_without_conflict(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    agents = user_home / ".config" / "opencode" / "agents"
    agents.mkdir(parents=True)
    coordinator = agents / "zekam-coordinator.md"
    coordinator.write_text(
        "---\n# zekam-managed-agent/v1\ndescription: Zekam old\n---\n",
        encoding="utf-8",
    )

    plan = plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)
    assert "zekam-coordinator.md" in plan.agents_to_update
    assert plan.conflicting_agents == ()
    apply_opencode_agent_bootstrap(plan)

    upgraded = coordinator.read_text(encoding="utf-8")
    assert '"*": allow' in upgraded
    assert '"*git commit*": deny' in upgraded


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


def test_repository_policy_allows_tools_but_denies_commit_and_push() -> None:
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    permission = config["permission"]
    assert permission["edit"] == "allow"
    assert permission["external_directory"]["*"] == "deny"
    assert permission["external_directory"]["C:/innova/projeler/**"] == "allow"
    assert permission["bash"]["*"] == "ask"
    assert permission["bash"]["*git commit*"] == "deny"
    assert permission["bash"]["*git push*"] == "deny"
    assert permission["bash"]["*git clone*"] == "deny"
    assert permission["bash"]["*git worktree add*"] == "deny"
    assert permission["bash"]["*Copy-Item*"] == "deny"

    manifest = (root / "PROJE_MANIFESTI.yaml").read_text(encoding="utf-8")
    assert "mutation_workspace: exact-bound-real-source-root" in manifest
    assert "project_copy_or_mirror: deny" in manifest
    assert "detached_worktree_for_mutation: deny" in manifest
