from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from zekam.application.opencode_agent_bootstrap import (
    DEFAULT_AGENT,
    apply_opencode_agent_bootstrap,
    opencode_template_bundle,
    plan_opencode_agent_bootstrap,
)
from zekam.domain.errors import ConfigurationError, PolicyViolation


def _executable(tmp_path: Path) -> Path:
    value = tmp_path / "opencode.exe"
    value.write_text("stub", encoding="utf-8")
    return value


def test_disabled_policy_makes_bootstrap_side_effect_free(tmp_path: Path) -> None:
    user_home = tmp_path / "never-created"
    plan = plan_opencode_agent_bootstrap(
        executable=_executable(tmp_path),
        user_home=user_home,
        enabled=False,
    )

    assert plan.integration_enabled is False
    assert plan.catalog_scope_state == "disabled-by-policy"
    apply_opencode_agent_bootstrap(plan, authorized_plan_digest=plan.plan_digest)
    assert not user_home.exists()


def test_apply_rejects_plan_digest_and_native_input_drift(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    executable = _executable(tmp_path)
    plan = plan_opencode_agent_bootstrap(executable=executable, user_home=user_home)

    with pytest.raises(PolicyViolation, match="exact plan digest"):
        apply_opencode_agent_bootstrap(plan, authorized_plan_digest="sha256:" + "0" * 64)

    config = user_home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"user_setting": true}\n', encoding="utf-8")
    with pytest.raises(PolicyViolation, match="input changed"):
        apply_opencode_agent_bootstrap(plan, authorized_plan_digest=plan.plan_digest)
    assert json.loads(config.read_text(encoding="utf-8")) == {"user_setting": True}


def test_apply_installs_global_agents_and_preserves_provider_configuration(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    config = user_home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "provider": {"litellm": {"options": {"timeout": 60}}},
                "permission": {"edit": "ask"},
            }
        )
    )

    plan = plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)
    apply_opencode_agent_bootstrap(plan, authorized_plan_digest=plan.plan_digest)

    stored = json.loads(config.read_text(encoding="utf-8"))
    assert stored["default_agent"] == DEFAULT_AGENT
    assert stored["plugin"] == ["./plugins/zekam-lifecycle.js"]
    assert stored["provider"]["litellm"]["options"]["timeout"] == 60
    assert stored["permission"] == {
        "*": "allow",
        "edit": "allow",
        "bash": "allow",
        "todowrite": "allow",
        "webfetch": "allow",
        "external_directory": {"*": "allow"},
        "task": "allow",
    }
    agents = user_home / ".config" / "opencode" / "agents"
    installed = {item.name for item in agents.iterdir()}
    assert {
        "zekam-builder.md",
        "zekam-coordinator.md",
        "zekam-memory-curator.md",
        "zekam-researcher.md",
        "zekam-research-runner.md",
        "zekam-router.md",
        "zekam-verifier.md",
    } <= installed
    for agent_path in agents.glob("*.md"):
        body = agent_path.read_text(encoding="utf-8")
        frontmatter = body.split("---", 2)[1]
        parsed = yaml.safe_load(frontmatter)
        assert isinstance(parsed, dict), agent_path.name
        assert parsed["permission"]["*"] == "allow", agent_path.name
    assert "Cikti disiplini" in (agents / "zekam-coordinator.md").read_text(encoding="utf-8")
    coordinator = (agents / "zekam-coordinator.md").read_text(encoding="utf-8")
    builder = (agents / "zekam-builder.md").read_text(encoding="utf-8")
    researcher = (agents / "zekam-researcher.md").read_text(encoding="utf-8")
    verifier = (agents / "zekam-verifier.md").read_text(encoding="utf-8")
    runner = (agents / "zekam-research-runner.md").read_text(encoding="utf-8")
    assert '"*": allow' in coordinator
    assert "Bash, PowerShell ve CMD komutlarinda kullanici onayi istemez" in coordinator
    assert "detached worktree veya gecici proje klonu olusturma" in coordinator
    assert "Zekam source rootuna geçici rapor, memo" in coordinator
    assert "Zekam source rootuna geçici rapor, memo" in builder
    assert "Zekam source rootuna memo, rapor" in researcher
    assert "Zekam source rootuna memo, rapor" in verifier
    assert "Dispatch protokolu" in coordinator
    assert "RAG-first bilgi protokolu" in coordinator
    assert '"zekam model campaign run *": ask' not in coordinator
    assert "`zekam project resume`" not in coordinator
    assert "ZEKAM_RESUME_PACKET_V1" in coordinator
    assert "parallel-project-rag" in coordinator
    assert "`general` route'u" in coordinator
    assert "Route `general` ise source/RAG komutu cagirmadan" in coordinator
    assert "yalniz temel `zekam-researcher` agent'ini cagir" in coordinator
    assert "--authorize-remote-query" in coordinator
    assert "subagent zorunlu degildir" in coordinator
    assert "en fazla ilk uc" in coordinator
    assert "`used_chunk_ids` degerini" in coordinator
    assert "`lexical-only-degraded`" in coordinator
    assert "`snapshot_only=true`" in coordinator
    assert "son indekslenmis" in coordinator
    assert "capabilities, help veya ikinci query cagirma" in coordinator
    assert "resolve/source-root zinciri calistirma" in coordinator
    assert "`zekam-router` bir shell/CLI komutu" in coordinator
    assert "Git worktree dirty" in coordinator
    assert "dispatch hazirligi icin butun RAG indeksini" in coordinator
    assert "top-level `project_ref`" in coordinator
    assert "locator_type=database-object" in coordinator
    assert "locator_type=database-object" in researcher
    assert "fiziksel dosya" in researcher
    assert "knowledge explain/show" in researcher
    assert "`ask` komutunu tekrar cagirma" in researcher
    assert "retrieval_digest" in coordinator
    assert "recursive shell ile tarayamaz" in coordinator
    assert "Eszamanli child sayisi ucu gecemez" in coordinator
    assert "zekam project source-root" in coordinator
    assert "Tum inceleme, Git kaniti, test ve kod degisikliklerini" in coordinator
    model_agents = [name for name in installed if name.startswith("zekam-implementer-")]
    assert model_agents == []
    assert plan.catalog_scope_state == "not-configured"
    plugin = user_home / ".config" / "opencode" / "plugins" / "zekam-lifecycle.js"
    assert plugin.is_file()
    assert "tool.execute.before" in plugin.read_text(encoding="utf-8")
    assert "session.error" in plugin.read_text(encoding="utf-8")
    assert '"--task-label"' not in plugin.read_text(encoding="utf-8")
    plugin_body = plugin.read_text(encoding="utf-8")
    assert plugin_body.startswith("// zekam-managed-plugin/v2")
    assert "opencode-plugin-spool" in plugin_body
    assert '"experimental.session.compacting"' in plugin_body
    assert '"experimental.chat.system.transform"' in plugin_body
    assert '"resume", "--prompt"' in plugin_body
    assert "resumePacket(input.sessionID, false)" in plugin_body
    assert 'if (excludeCurrent) cmd.push("--session", session)' in plugin_body
    assert "ZEKAM_RESUME_PACKET_V1" in plugin_body
    assert "output.context.push(packet)" in plugin_body
    assert '"pre-compact"' in plugin_body
    assert "canonical pre-compact checkpoint ACK failed" in plugin_body
    assert "attempts >= 5" in plugin_body
    assert "ownerToken" in plugin_body
    assert "process.kill(pid, 0)" in plugin_body
    assert "await rename(lockPath, abandoned)" in plugin_body
    assert "current.owner.ownerToken === lock.ownerToken" in plugin_body
    assert "drainInFlight" in plugin_body
    assert "drainRequested" in plugin_body
    assert "for (let pass = 0; pass < 8" in plugin_body
    assert 'error?.code === "EPERM"' in plugin_body
    assert '"--delivery-id"' in plugin_body
    assert "zekam-opencode-plugin-spool/v2" in plugin_body
    assert "quarantine" in plugin_body
    assert "exitCode === 0" in plugin_body
    assert "yerel dayanikli kuyruga alindi" in plugin_body
    assert "continuity checkpoint kaydedildi" not in plugin_body
    assert "`zekam project resume`" not in verifier
    assert "mode: primary" in runner
    assert "kopya, mirror, clone" in researcher
    assert "bounded source fallback" in researcher
    assert all('"*": allow' in path.read_text(encoding="utf-8") for path in agents.glob("*.md"))
    router = (agents / "zekam-router.md").read_text(encoding="utf-8")
    assert "model secimi degildir" in router
    repeat = plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)
    assert repeat.agents_to_create == ()
    assert repeat.agents_to_update == ()
    assert not repeat.config_update_required


def test_repository_managed_agents_never_prompt_for_shell() -> None:
    agents = Path(__file__).parents[2] / ".opencode" / "agents"
    paths = sorted(agents.glob("zekam-*.md"))

    assert len(paths) == 7
    for path in paths:
        frontmatter = path.read_text(encoding="utf-8").split("---", 2)[1]
        parsed = yaml.safe_load(frontmatter)
        assert isinstance(parsed, dict), path.name
        assert parsed["permission"]["*"] == "allow", path.name


def test_every_generated_agent_template_allows_shell_without_prompt() -> None:
    agent_templates = {
        name: body
        for name, body in opencode_template_bundle().items()
        if name.startswith("agents/")
    }

    assert len(agent_templates) == 51
    for name, body in agent_templates.items():
        parsed = yaml.safe_load(body.split("---", 2)[1])
        assert isinstance(parsed, dict), name
        assert parsed["permission"]["*"] == "allow", name


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
    apply_opencode_agent_bootstrap(plan, authorized_plan_digest=plan.plan_digest)

    upgraded = coordinator.read_text(encoding="utf-8")
    assert '"*": allow' in upgraded


def test_model_agent_retirement_moves_outside_recursive_scan_tree(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    agents = user_home / ".config" / "opencode" / "agents"
    agents.mkdir(parents=True)
    stale = agents / "zekam-implementer-stale.md"
    stale.write_text(
        "---\n# zekam-managed-agent/v1\ndescription: stale\n---\n",
        encoding="utf-8",
    )

    plan = plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)
    assert plan.agents_to_retire == (stale.name,)
    apply_opencode_agent_bootstrap(plan, authorized_plan_digest=plan.plan_digest)

    retired = agents.parent / ".zekam-retired-agents" / stale.name
    assert retired.is_file()
    assert not stale.exists()
    assert not (agents / ".zekam-retired").exists()
    repeat = plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)
    assert repeat.agents_to_retire == ()
    assert repeat.legacy_retired_agents_to_migrate == ()


def test_legacy_recursive_retirement_is_migrated_outside_agent_tree(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    agents = user_home / ".config" / "opencode" / "agents"
    legacy = agents / ".zekam-retired"
    legacy.mkdir(parents=True)
    stale = legacy / "zekam-reviewer-stale.md"
    stale.write_text(
        "---\n# zekam-managed-agent/v1\ndescription: stale\n---\n",
        encoding="utf-8",
    )

    plan = plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)
    assert plan.legacy_retired_agents_to_migrate == (stale.name,)
    apply_opencode_agent_bootstrap(plan, authorized_plan_digest=plan.plan_digest)

    assert not legacy.exists()
    assert (agents.parent / ".zekam-retired-agents" / stale.name).is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_retirement_rejects_existing_windows_junction_destination(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    agents = user_home / ".config" / "opencode" / "agents"
    agents.mkdir(parents=True)
    stale = agents / "zekam-implementer-stale.md"
    stale.write_text(
        "---\n# zekam-managed-agent/v1\ndescription: stale\n---\n",
        encoding="utf-8",
    )
    external = tmp_path / "external"
    external.mkdir()
    retired = agents.parent / ".zekam-retired-agents"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(retired), str(external)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert created.returncode == 0, created.stdout + created.stderr

    with pytest.raises(ConfigurationError, match="regular directory"):
        plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)

    assert stale.is_file()
    assert not (external / stale.name).exists()


def test_legacy_managed_lifecycle_plugin_is_updated(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    plugin = user_home / ".config" / "opencode" / "plugins" / "zekam-lifecycle.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text(
        "// zekam-managed-plugin/v1\n"
        'import { tool } from "@opencode-ai/plugin"\n'
        "export const ZekamLifecycle = async () => ({ "
        "tool: { zekam_checkpoint: tool({}) } })\n",
        encoding="utf-8",
    )

    plan = plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)
    assert plan.lifecycle_plugin_to_create
    assert not plan.lifecycle_plugin_conflict
    apply_opencode_agent_bootstrap(plan, authorized_plan_digest=plan.plan_digest)

    body = plugin.read_text(encoding="utf-8")
    assert body.startswith("// zekam-managed-plugin/v2")
    assert "opencode-plugin-spool" in body


def test_unmanaged_plugin_that_looks_similar_is_preserved(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    plugin = user_home / ".config" / "opencode" / "plugins" / "zekam-lifecycle.js"
    plugin.parent.mkdir(parents=True)
    custom = (
        'import { tool } from "@opencode-ai/plugin"\n'
        "export const ZekamLifecycle = async () => ({ "
        "tool: { zekam_checkpoint: tool({}) } })\n"
    )
    plugin.write_text(custom, encoding="utf-8")

    plan = plan_opencode_agent_bootstrap(executable=_executable(tmp_path), user_home=user_home)

    assert plan.lifecycle_plugin_conflict
    assert not plan.lifecycle_plugin_to_create
    with pytest.raises(ConfigurationError, match="cakisiyor"):
        apply_opencode_agent_bootstrap(plan, authorized_plan_digest=plan.plan_digest)
    assert plugin.read_text(encoding="utf-8") == custom


def test_missing_opencode_has_no_global_side_effect_plan(tmp_path: Path) -> None:
    plan = plan_opencode_agent_bootstrap(executable=None, user_home=tmp_path / "user")
    apply_opencode_agent_bootstrap(plan, authorized_plan_digest=plan.plan_digest)
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
        apply_opencode_agent_bootstrap(plan, authorized_plan_digest=plan.plan_digest)


def test_repository_policy_allows_all_opencode_effects() -> None:
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    permission = config["permission"]
    assert permission["*"] == "allow"
    assert permission["edit"] == "allow"
    assert permission["external_directory"]["*"] == "allow"
    assert permission["bash"] == "allow"
    assert permission["todowrite"] == "allow"
    assert permission["webfetch"] == "allow"
    assert permission["task"] == "allow"

    manifest = (root / "PROJE_MANIFESTI.yaml").read_text(encoding="utf-8")
    assert "mutation_workspace: exact-bound-real-source-root" in manifest
    assert "project_copy_or_mirror: deny" in manifest
    assert "detached_worktree_for_mutation: deny" in manifest
