from __future__ import annotations

import os
from pathlib import Path

import pytest

from zekam.application.client_instruction_bootstrap import (
    apply_client_instruction_bootstrap,
    plan_client_instruction_bootstrap,
)
from zekam.domain.client_integration import ClientIntegrationPolicy
from zekam.domain.errors import ConfigurationError


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "user"
    home.mkdir()
    return home


def _all_enabled() -> ClientIntegrationPolicy:
    return ClientIntegrationPolicy(opencode=True, codex=True, claude_code=True)


def test_missing_client_instruction_files_are_created_and_idempotent(tmp_path: Path) -> None:
    home = _home(tmp_path)
    plan = plan_client_instruction_bootstrap(user_home=home, integration_policy=_all_enabled())

    assert {item.client_id for item in plan.files} == {"codex", "claude-code", "opencode"}
    assert all(item.action == "create" for item in plan.files)
    apply_client_instruction_bootstrap(plan)

    repeat = plan_client_instruction_bootstrap(user_home=home, integration_policy=_all_enabled())
    assert all(item.action == "unchanged" for item in repeat.files)
    for item in repeat.files:
        body = item.path.read_text(encoding="utf-8")
        assert body.count("zekam-managed-client-instructions/v1:start") == 1
        assert "zekam doctor --hazirla --json" in body
        assert "zekam loop status" in body
        assert "Obsidian projection salt okunur" in body


def test_modified_managed_section_is_conflict_and_user_content_is_preserved(tmp_path: Path) -> None:
    home = _home(tmp_path)
    target = home / ".codex" / "AGENTS.md"
    target.parent.mkdir()
    target.write_text(
        "# Benim kurallarim\n\n"
        "<!-- zekam-managed-client-instructions/v1:start -->\nold\n"
        "<!-- zekam-managed-client-instructions/v1:end -->\n\nson\n",
        encoding="utf-8",
    )

    before = target.read_bytes()
    with pytest.raises(ConfigurationError, match="ownership drift"):
        plan_client_instruction_bootstrap(user_home=home, integration_policy=_all_enabled())
    assert target.read_bytes() == before


def test_unmanaged_existing_content_gets_one_managed_section(tmp_path: Path) -> None:
    home = _home(tmp_path)
    target = home / ".claude" / "CLAUDE.md"
    target.parent.mkdir()
    target.write_text("kullanici icerigi", encoding="utf-8")

    apply_client_instruction_bootstrap(
        plan_client_instruction_bootstrap(user_home=home, integration_policy=_all_enabled())
    )

    body = target.read_text(encoding="utf-8")
    assert body.startswith("kullanici icerigi\n\n")
    assert body.count("zekam-managed-client-instructions/v1:start") == 1


@pytest.mark.parametrize(
    "body",
    (
        "<!-- zekam-managed-client-instructions/v1:start -->",
        "<!-- zekam-managed-client-instructions/v1:end -->",
    ),
)
def test_broken_managed_section_fails_closed(tmp_path: Path, body: str) -> None:
    home = _home(tmp_path)
    target = home / ".config" / "opencode" / "AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text(body, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="bozuk"):
        plan_client_instruction_bootstrap(user_home=home, integration_policy=_all_enabled())


def test_symlink_target_fails_closed(tmp_path: Path) -> None:
    home = _home(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    target = home / ".codex" / "AGENTS.md"
    target.parent.mkdir()
    try:
        os.symlink(outside, target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink olusturulamadi: {exc}")

    with pytest.raises(ConfigurationError, match="regular file"):
        plan_client_instruction_bootstrap(user_home=home, integration_policy=_all_enabled())


def test_apply_rejects_stale_plan_and_preserves_new_user_content(tmp_path: Path) -> None:
    home = _home(tmp_path)
    plan = plan_client_instruction_bootstrap(user_home=home, integration_policy=_all_enabled())
    target = home / ".codex" / "AGENTS.md"
    target.parent.mkdir()
    target.write_text("sonradan eklendi", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="stale"):
        apply_client_instruction_bootstrap(plan)
    assert target.read_text(encoding="utf-8") == "sonradan eklendi"


def test_default_policy_only_plans_opencode_instruction_file(tmp_path: Path) -> None:
    home = _home(tmp_path)

    plan = plan_client_instruction_bootstrap(user_home=home)

    assert [(item.client_id, item.path) for item in plan.files] == [
        ("opencode", home / ".config" / "opencode" / "AGENTS.md")
    ]
