from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from zekam.application.client_hook_bootstrap import (
    apply_client_hook_bootstrap,
    plan_client_hook_bootstrap,
)
from zekam.domain.errors import ConfigurationError


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "user"
    home.mkdir()
    return home


def _plan(home: Path):  # type: ignore[no-untyped-def]
    return plan_client_hook_bootstrap(user_home=home, python_executable=Path(sys.executable))


def test_create_real_client_hook_files_and_repeat_is_idempotent(tmp_path: Path) -> None:
    home = _home(tmp_path)
    plan = _plan(home)
    assert all(item.action == "create" for item in plan.files)
    apply_client_hook_bootstrap(plan)

    repeat = _plan(home)
    assert all(item.action == "unchanged" for item in repeat.files)
    codex = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    claude = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    for event in ("SessionStart", "PreCompact", "PostCompact", "Stop", "SessionEnd"):
        codex_hook = codex["hooks"][event][0]["hooks"][0]
        claude_hook = claude["hooks"][event][0]["hooks"][0]
        assert codex_hook["commandWindows"]
        assert "--client codex --client-version 0.150.1" in codex_hook["command"]
        assert "commandWindows" not in claude_hook
        assert "--client claude-code --client-version 2.1.224" in claude_hook["command"]


def test_preserves_user_settings_and_hooks_while_updating_managed_entry(tmp_path: Path) -> None:
    home = _home(tmp_path)
    target = home / ".claude" / "settings.json"
    target.parent.mkdir()
    target.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "Stop": [
                        {"matcher": "user", "hooks": [{"type": "command", "command": "mine"}]},
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python -m zekam.interfaces.cli.client hook "
                                    "--client claude-code --client-version 1.0.0",
                                }
                            ]
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    apply_client_hook_bootstrap(_plan(home))

    stored = json.loads(target.read_text(encoding="utf-8"))
    assert stored["theme"] == "dark"
    assert stored["hooks"]["Stop"][0]["hooks"][0]["command"] == "mine"
    assert len(stored["hooks"]["Stop"]) == 2
    assert "2.1.224" in stored["hooks"]["Stop"][1]["hooks"][0]["command"]


def test_duplicate_or_broken_managed_entries_fail_closed(tmp_path: Path) -> None:
    home = _home(tmp_path)
    target = home / ".codex" / "hooks.json"
    target.parent.mkdir()
    command = "python -m zekam.interfaces.cli.client hook --client codex --client-version 0.1"
    group = {"hooks": [{"type": "command", "command": command}]}
    target.write_text(json.dumps({"hooks": {"Stop": [group, group]}}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="duplicate"):
        _plan(home)

    target.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"command": command}, {"command": command}]}]}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="bozuk"):
        _plan(home)


def test_stale_plan_preserves_new_user_config(tmp_path: Path) -> None:
    home = _home(tmp_path)
    plan = _plan(home)
    target = home / ".claude" / "settings.json"
    target.parent.mkdir()
    target.write_text('{"new": true}\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="stale"):
        apply_client_hook_bootstrap(plan)
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}


def test_symlink_config_fails_closed_when_supported(tmp_path: Path) -> None:
    home = _home(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    target = home / ".claude" / "settings.json"
    target.parent.mkdir()
    try:
        os.symlink(outside, target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink olusturulamadi: {exc}")
    with pytest.raises(ConfigurationError, match="regular file"):
        _plan(home)
