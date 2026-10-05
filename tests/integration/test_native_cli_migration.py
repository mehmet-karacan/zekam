"""Native-first gecis: legacy global temizlik, no-op tekrar, yeniden globallesme yok."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from zekam.application.client_hook_bootstrap import (
    apply_client_hook_bootstrap,
    plan_client_hook_bootstrap,
)
from zekam.application.client_instruction_bootstrap import (
    apply_client_instruction_bootstrap,
    plan_client_instruction_bootstrap,
)
from zekam.application.client_integrations import (
    apply_rollback_plan,
    apply_sync_plan,
    build_rollback_plan,
    build_sync_plan,
    integration_status,
)
from zekam.application.composition import ApplicationContext, build_context
from zekam.application.opencode_agent_bootstrap import (
    LEGACY_LIFECYCLE_PLUGIN_DIGESTS,
    apply_opencode_agent_bootstrap,
    is_known_lifecycle_plugin,
    opencode_template_bundle,
    plan_opencode_agent_bootstrap,
)
from zekam.domain.client_integration import ClientIntegrationPolicy

pytestmark = pytest.mark.integration

_LEGACY_CONFIG = (
    "schema: zekam-config/v1\n"
    "cli:\n"
    "  integrations:\n"
    "    opencode: true\n"
    "    codex: true\n"
    "    claude-code: true\n"
)
_ALL = ClientIntegrationPolicy(opencode=True, codex=True, claude_code=True)


def _legacy_context(tmp_path: Path) -> ApplicationContext:
    home = tmp_path / "zekam-home"
    home.mkdir()
    (home / "config.yaml").write_text(_LEGACY_CONFIG, encoding="utf-8", newline="\n")
    return build_context(home=home, environ={})


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    }


def _seed_legacy_global_distribution(tmp_path: Path) -> Path:
    native = tmp_path / "native-user"
    native.mkdir()
    for relative, text in (
        (Path(".codex") / "AGENTS.md", "# Codex kullanici kurali\n"),
        (Path(".claude") / "CLAUDE.md", "# Claude kullanici kurali\n"),
        (Path(".config") / "opencode" / "AGENTS.md", "# OpenCode kullanici kurali\n"),
    ):
        target = native / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    instructions = plan_client_instruction_bootstrap(user_home=native, integration_policy=_ALL)
    apply_client_instruction_bootstrap(instructions)
    hooks = plan_client_hook_bootstrap(
        user_home=native,
        python_executable=Path(sys.executable).resolve(strict=True),
        policy=_ALL,
    )
    apply_client_hook_bootstrap(hooks)
    stub = tmp_path / "opencode.exe"
    stub.write_bytes(b"stub")
    bootstrap = plan_opencode_agent_bootstrap(executable=stub, user_home=native, enabled=True)
    apply_opencode_agent_bootstrap(bootstrap, authorized_plan_digest=bootstrap.plan_digest)
    return native


def test_legacy_all_true_config_is_reconciled_without_enabling_global_install(
    tmp_path: Path,
) -> None:
    context = _legacy_context(tmp_path)
    integrations = context.settings.cli.integrations

    assert integrations.legacy
    assert integrations.legacy_selection == ("opencode", "codex", "claude-code")
    assert not any(integrations.clients().values())


def test_dry_run_plans_exact_cleanup_apply_preserves_user_text_and_second_plan_is_noop(
    tmp_path: Path,
) -> None:
    context = _legacy_context(tmp_path)
    native = _seed_legacy_global_distribution(tmp_path)
    before_native = _snapshot(native)
    before_home = _snapshot(context.home)

    plan = build_sync_plan(context, scope="user", native_user_root=native)

    # A12: dry-run dosya/DB/lock/spool/dizin yan etkisi uretmez.
    assert _snapshot(native) == before_native
    assert _snapshot(context.home) == before_home
    assert plan.conflicts == ()
    operations = {(row["operation"], row.get("client")) for row in plan.operations}
    assert ("update-user-policy", None) in operations
    assert ("detach-client-instruction-config", "codex") in operations
    assert ("detach-client-instruction-config", "claude-code") in operations
    assert ("detach-client-hook-config", "codex") in operations
    assert ("detach-client-hook-config", "claude-code") in operations
    assert ("detach-opencode-config", "opencode") in operations
    assert any(
        row["operation"] == "quarantine-managed-native-artifact"
        and row["artifact_type"] == "plugin"
        for row in plan.operations
    )

    # A13/A15: yalniz sahipligi kanitli bolumler cikar, kullanici metni korunur.
    apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    assert (native / ".codex" / "AGENTS.md").read_text(encoding="utf-8") == (
        "# Codex kullanici kurali\n"
    )
    assert (native / ".claude" / "CLAUDE.md").read_text(encoding="utf-8") == (
        "# Claude kullanici kurali\n"
    )
    assert not (native / ".config" / "opencode" / "plugins" / "zekam-lifecycle.js").exists()
    assert not list((native / ".config" / "opencode" / "agents").glob("zekam-*.md"))

    # A11/A18: yeniden yuklenen politika v2 ve hepsi kapali; ikinci plan no-op.
    reloaded = build_context(home=context.home, environ={})
    assert not reloaded.settings.cli.integrations.legacy
    assert not any(reloaded.settings.cli.integrations.clients().values())
    second = build_sync_plan(reloaded, scope="user", native_user_root=native)
    assert second.operations == ()
    assert second.conflicts == ()
    states = {
        item["client"]: item["effective_state"]
        for item in integration_status(reloaded, native_user_root=native)["clients"]
    }
    assert set(states) == {"opencode", "codex", "claude-code", "gemini"}
    assert set(states.values()) == {"disabled-clean"}


def test_producers_do_not_regrow_global_artifacts_after_cleanup(tmp_path: Path) -> None:
    context = _legacy_context(tmp_path)
    native = _seed_legacy_global_distribution(tmp_path)
    plan = build_sync_plan(context, scope="user", native_user_root=native)
    apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    reloaded = build_context(home=context.home, environ={})
    policy = reloaded.settings.cli.integrations
    stub = tmp_path / "opencode.exe"

    instructions = plan_client_instruction_bootstrap(user_home=native, integration_policy=policy)
    hooks = plan_client_hook_bootstrap(
        user_home=native,
        python_executable=Path(sys.executable).resolve(strict=True),
        policy=policy,
    )
    opencode = plan_opencode_agent_bootstrap(
        executable=stub if stub.exists() else None,
        user_home=native,
        enabled=policy.opencode,
    )

    assert instructions.files == ()
    assert hooks.files == ()
    assert opencode.agents_to_create == ()
    assert not opencode.lifecycle_plugin_to_create
    assert not opencode.config_update_required


def test_cleanup_rolls_back_exactly_but_reports_global_restore_honestly(tmp_path: Path) -> None:
    context = _legacy_context(tmp_path)
    native = _seed_legacy_global_distribution(tmp_path)
    before_native = _snapshot(native)
    plan = build_sync_plan(context, scope="user", native_user_root=native)
    receipt = apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)

    rollback = build_rollback_plan(
        build_context(home=context.home, environ={}),
        receipt_id=str(receipt["receipt_id"]),
        native_user_root=native,
    )
    apply_rollback_plan(rollback, authorized_plan_digest=rollback.plan_digest)

    assert _snapshot(native) == before_native


def test_plugin_ownership_accepts_only_current_or_reviewed_legacy_bytes() -> None:
    current = opencode_template_bundle()["plugins/zekam-lifecycle.js"].encode()

    assert is_known_lifecycle_plugin(current)
    assert len(LEGACY_LIFECYCLE_PLUGIN_DIGESTS) >= 7
    assert not is_known_lifecycle_plugin(b"// zekam-managed-plugin/v2\nuser edited\n")
    assert not is_known_lifecycle_plugin(current + b"\n// user edit\n")


def test_global_plugin_has_workspace_guard_before_any_side_effect() -> None:
    current = opencode_template_bundle()["plugins/zekam-lifecycle.js"]

    assert current.index("isZekamWorkspace(directory)") < current.index("mkdir(quarantine")
    assert "repository-context.json" in current


_LEGACY_PERMISSION = {
    "*": "allow",
    "edit": "allow",
    "bash": "allow",
    "todowrite": "allow",
    "webfetch": "allow",
    "external_directory": {"*": "allow"},
    "task": "allow",
}


def test_exact_legacy_permission_override_is_removed_and_user_fields_survive(
    tmp_path: Path,
) -> None:
    context = _legacy_context(tmp_path)
    native = _seed_legacy_global_distribution(tmp_path)
    config = native / ".config" / "opencode" / "opencode.json"
    document = json.loads(config.read_text(encoding="utf-8"))
    document["permission"] = _LEGACY_PERMISSION
    document["provider"] = {"safe": {"timeout": 30}}
    config.write_text(json.dumps(document), encoding="utf-8")

    plan = build_sync_plan(context, scope="user", native_user_root=native)
    assert plan.conflicts == ()
    apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)

    stored = json.loads(config.read_text(encoding="utf-8"))
    assert "permission" not in stored
    assert stored["provider"] == {"safe": {"timeout": 30}}


def test_customized_permission_block_is_never_touched(tmp_path: Path) -> None:
    context = _legacy_context(tmp_path)
    native = _seed_legacy_global_distribution(tmp_path)
    config = native / ".config" / "opencode" / "opencode.json"
    document = json.loads(config.read_text(encoding="utf-8"))
    document["permission"] = {**_LEGACY_PERMISSION, "bash": "ask"}
    config.write_text(json.dumps(document), encoding="utf-8")

    plan = build_sync_plan(context, scope="user", native_user_root=native)
    apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)

    stored = json.loads(config.read_text(encoding="utf-8"))
    assert stored["permission"]["bash"] == "ask"
