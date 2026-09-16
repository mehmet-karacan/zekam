"""MCP registry dry-run, claimed apply, native preservation and status."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

import zekam.application.mcp_integrations as mcp_integrations
import zekam.interfaces.cli.mcp as mcp_commands
from zekam.interfaces.cli.main import app


def test_mcp_add_projects_one_registration_to_all_installed_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "zekam-home"
    native = tmp_path / "native-home"
    native.mkdir()
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\n"
        "cli:\n"
        "  integrations:\n"
        "    opencode: true\n"
        "    codex: true\n"
        "    claude-code: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_commands.Path, "home", lambda: native)
    monkeypatch.setattr(mcp_integrations, "_executable_present", lambda _client: True)

    opencode = native / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True)
    opencode.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "unrelated": {"keep": True},
                "mcp": {
                    "innova-atlassian": {
                        "type": "local",
                        "command": ["python", "old.py"],
                        "enabled": True,
                        "environment": {"JIRA_API_TOKEN": "old-secret"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    codex = native / ".codex" / "config.toml"
    codex.parent.mkdir(parents=True)
    codex.write_text('model = "test"\n', encoding="utf-8")
    claude = native / ".claude.json"
    claude.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

    args = [
        "mcp",
        "add",
        "innova-atlassian",
        "--command",
        "zekam",
        "--arg",
        "mcp",
        "--arg",
        "serve",
        "--arg",
        "innova-atlassian",
        "--env-var",
        "JIRA_API_TOKEN",
        "--env-var",
        "CONFLUENCE_API_TOKEN",
        "--adopt",
        "--home",
        str(home),
        "--json",
    ]
    planned = runner.invoke(app, args)
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["apply"] is False
    assert plan["provider_calls"] == 0
    assert plan["network_calls"] == 0
    assert "old-secret" not in planned.output

    applied = runner.invoke(
        app,
        [*args[:-1], "--plan-digest", plan["plan_digest"], "--uygula", "--json"],
    )
    assert applied.exit_code == 0, applied.output
    receipt = json.loads(applied.output)
    assert receipt["operational_terminal_evidence_digest"] == receipt["receipt_digest"]

    opencode_document = json.loads(opencode.read_text(encoding="utf-8"))
    assert opencode_document["unrelated"] == {"keep": True}
    entry = opencode_document["mcp"]["innova-atlassian"]
    assert entry["command"] == ["zekam", "mcp", "serve", "innova-atlassian"]
    assert entry["environment"]["JIRA_API_TOKEN"] == "{env:JIRA_API_TOKEN}"
    assert "old-secret" not in opencode.read_text(encoding="utf-8")
    assert all(b"old-secret" not in path.read_bytes() for path in home.rglob("*") if path.is_file())

    codex_document = tomllib.loads(codex.read_text(encoding="utf-8"))
    assert codex_document["model"] == "test"
    assert codex_document["mcp_servers"]["innova-atlassian"]["env_vars"] == [
        "JIRA_API_TOKEN",
        "CONFLUENCE_API_TOKEN",
    ]
    claude_document = json.loads(claude.read_text(encoding="utf-8"))
    assert claude_document["theme"] == "dark"
    assert claude_document["mcpServers"]["innova-atlassian"]["command"] == "zekam"

    status = runner.invoke(app, ["mcp", "status", "--home", str(home), "--json"])
    assert status.exit_code == 0, status.output
    status_document = json.loads(status.output)
    assert [item["name"] for item in status_document["servers"]] == ["innova-atlassian"]
    assert status_document["last_receipt_digest"] == receipt["receipt_digest"]

    second_args = [
        "mcp",
        "add",
        "sample-tools",
        "--command",
        "sample-mcp",
        "--home",
        str(home),
        "--json",
    ]
    second_plan_result = runner.invoke(app, second_args)
    assert second_plan_result.exit_code == 0, second_plan_result.output
    second_plan = json.loads(second_plan_result.output)
    second_apply = runner.invoke(
        app,
        [
            *second_args[:-1],
            "--plan-digest",
            second_plan["plan_digest"],
            "--uygula",
            "--json",
        ],
    )
    assert second_apply.exit_code == 0, second_apply.output

    sync_plan_result = runner.invoke(app, ["mcp", "sync", "--home", str(home), "--json"])
    assert sync_plan_result.exit_code == 0, sync_plan_result.output
    sync_plan = json.loads(sync_plan_result.output)
    sync_apply = runner.invoke(
        app,
        [
            "mcp",
            "sync",
            "--plan-digest",
            sync_plan["plan_digest"],
            "--uygula",
            "--home",
            str(home),
            "--json",
        ],
    )
    assert sync_apply.exit_code == 0, sync_apply.output
    final_open = json.loads(opencode.read_text(encoding="utf-8"))["mcp"]
    assert set(final_open) >= {"innova-atlassian", "sample-tools"}


def test_mcp_rollback_removes_adopted_entry_without_persisting_old_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "zekam-home"
    native = tmp_path / "native-home"
    native.mkdir()
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\n"
        "cli:\n"
        "  integrations:\n"
        "    opencode: true\n"
        "    codex: true\n"
        "    claude-code: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_commands.Path, "home", lambda: native)
    monkeypatch.setattr(
        mcp_integrations,
        "_executable_present",
        lambda client: client.value == "opencode",
    )
    opencode = native / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True)
    original = (
        b'{"unrelated":{"keep":true},"mcp":{"sample":{"type":"local","command":["old-secret"]}}}\n'
    )
    opencode.write_bytes(original)
    add_args = [
        "mcp",
        "add",
        "sample",
        "--command",
        "new-server",
        "--adopt",
        "--home",
        str(home),
        "--json",
    ]
    planned = json.loads(runner.invoke(app, add_args).output)
    applied_result = runner.invoke(
        app,
        [
            *add_args[:-1],
            "--plan-digest",
            planned["plan_digest"],
            "--uygula",
            "--json",
        ],
    )
    assert applied_result.exit_code == 0, applied_result.output
    receipt = json.loads(applied_result.output)
    assert opencode.read_bytes() != original

    rollback_args = [
        "mcp",
        "rollback",
        "--receipt",
        receipt["receipt_digest"],
        "--home",
        str(home),
        "--json",
    ]
    rollback_plan_result = runner.invoke(app, rollback_args)
    assert rollback_plan_result.exit_code == 0, rollback_plan_result.output
    rollback_plan = json.loads(rollback_plan_result.output)
    rollback_result = runner.invoke(
        app,
        [
            *rollback_args[:-1],
            "--plan-digest",
            rollback_plan["plan_digest"],
            "--uygula",
            "--json",
        ],
    )
    assert rollback_result.exit_code == 0, rollback_result.output
    rolled_back = json.loads(opencode.read_text(encoding="utf-8"))
    assert rolled_back["unrelated"] == {"keep": True}
    assert rolled_back["mcp"] == {}
    assert all(b"old-secret" not in path.read_bytes() for path in home.rglob("*") if path.is_file())
    status = json.loads(runner.invoke(app, ["mcp", "status", "--home", str(home), "--json"]).output)
    assert status["servers"] == []


def test_mcp_remove_preserves_unrelated_config_and_can_be_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "zekam-home"
    native = tmp_path / "native-home"
    native.mkdir()
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    monkeypatch.setattr(mcp_commands.Path, "home", lambda: native)
    monkeypatch.setattr(
        mcp_integrations,
        "_executable_present",
        lambda client: client.value == "opencode",
    )
    opencode = native / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True)
    opencode.write_text(
        json.dumps(
            {
                "unrelated": {"keep": True},
                "mcp": {
                    "user-owned": {
                        "type": "local",
                        "command": ["user-server"],
                        "enabled": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    add_args = [
        "mcp",
        "add",
        "managed-server",
        "--command",
        "managed-server",
        "--home",
        str(home),
        "--json",
    ]
    add_plan = json.loads(runner.invoke(app, add_args).output)
    add_result = runner.invoke(
        app,
        [
            *add_args[:-1],
            "--plan-digest",
            add_plan["plan_digest"],
            "--uygula",
            "--json",
        ],
    )
    assert add_result.exit_code == 0, add_result.output

    remove_args = [
        "mcp",
        "remove",
        "managed-server",
        "--home",
        str(home),
        "--json",
    ]
    remove_plan_result = runner.invoke(app, remove_args)
    assert remove_plan_result.exit_code == 0, remove_plan_result.output
    remove_plan = json.loads(remove_plan_result.output)
    remove_result = runner.invoke(
        app,
        [
            *remove_args[:-1],
            "--plan-digest",
            remove_plan["plan_digest"],
            "--uygula",
            "--json",
        ],
    )
    assert remove_result.exit_code == 0, remove_result.output
    remove_receipt = json.loads(remove_result.output)

    removed_document = json.loads(opencode.read_text(encoding="utf-8"))
    assert removed_document["unrelated"] == {"keep": True}
    assert set(removed_document["mcp"]) == {"user-owned"}
    status = json.loads(runner.invoke(app, ["mcp", "status", "--home", str(home), "--json"]).output)
    assert status["servers"] == []

    rollback_args = [
        "mcp",
        "rollback",
        "--receipt",
        remove_receipt["receipt_digest"],
        "--home",
        str(home),
        "--json",
    ]
    rollback_plan = json.loads(runner.invoke(app, rollback_args).output)
    rollback_result = runner.invoke(
        app,
        [
            *rollback_args[:-1],
            "--plan-digest",
            rollback_plan["plan_digest"],
            "--uygula",
            "--json",
        ],
    )
    assert rollback_result.exit_code == 0, rollback_result.output
    restored_document = json.loads(opencode.read_text(encoding="utf-8"))
    assert set(restored_document["mcp"]) == {"managed-server", "user-owned"}
    restored_status = json.loads(
        runner.invoke(app, ["mcp", "status", "--home", str(home), "--json"]).output
    )
    assert [item["name"] for item in restored_status["servers"]] == ["managed-server"]
    sync_after_rollback = runner.invoke(app, ["mcp", "sync", "--home", str(home), "--json"])
    assert sync_after_rollback.exit_code == 0, sync_after_rollback.output


def test_mcp_update_narrows_clients_and_removes_old_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "zekam-home"
    native = tmp_path / "native-home"
    native.mkdir()
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\n"
        "cli:\n"
        "  integrations:\n"
        "    opencode: true\n"
        "    codex: true\n"
        "    claude-code: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_commands.Path, "home", lambda: native)
    monkeypatch.setattr(mcp_integrations, "_executable_present", lambda _client: True)

    add_args = [
        "mcp",
        "add",
        "sample",
        "--command",
        "server-v1",
        "--client",
        "opencode",
        "--client",
        "codex",
        "--home",
        str(home),
        "--json",
    ]
    add_plan = json.loads(runner.invoke(app, add_args).output)
    add_result = runner.invoke(
        app,
        [*add_args[:-1], "--plan-digest", add_plan["plan_digest"], "--uygula", "--json"],
    )
    assert add_result.exit_code == 0, add_result.output

    update_args = [
        "mcp",
        "add",
        "sample",
        "--command",
        "server-v2",
        "--client",
        "opencode",
        "--home",
        str(home),
        "--json",
    ]
    update_plan_result = runner.invoke(app, update_args)
    assert update_plan_result.exit_code == 0, update_plan_result.output
    update_plan = json.loads(update_plan_result.output)
    update_result = runner.invoke(
        app,
        [
            *update_args[:-1],
            "--plan-digest",
            update_plan["plan_digest"],
            "--uygula",
            "--json",
        ],
    )
    assert update_result.exit_code == 0, update_result.output

    opencode = json.loads(
        (native / ".config" / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert opencode["mcp"]["sample"]["command"] == ["server-v2"]
    codex = (native / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "zekam-managed-mcp:start sample" not in codex
    status = json.loads(runner.invoke(app, ["mcp", "status", "--home", str(home), "--json"]).output)
    assert status["servers"][0]["clients"] == ["opencode"]


def test_mcp_codex_drift_fails_closed_and_clean_v2_uses_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "zekam-home"
    native = tmp_path / "native-home"
    native.mkdir()
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\n"
        "cli:\n"
        "  integrations:\n"
        "    opencode: true\n"
        "    codex: true\n"
        "    claude-code: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_commands.Path, "home", lambda: native)
    monkeypatch.setattr(mcp_integrations, "_executable_present", lambda _client: True)
    monkeypatch.setattr(mcp_integrations, "_opencode_uses_v2", lambda: True)
    opencode_path = native / ".config" / "opencode" / "opencode.json"
    opencode_path.parent.mkdir(parents=True)
    opencode_path.write_text(
        json.dumps({"$schema": "https://opencode.ai/config.json"}),
        encoding="utf-8",
    )

    args = [
        "mcp",
        "add",
        "sample",
        "--command",
        "server",
        "--client",
        "opencode",
        "--client",
        "codex",
        "--home",
        str(home),
        "--json",
    ]
    plan = json.loads(runner.invoke(app, args).output)
    applied = runner.invoke(
        app,
        [*args[:-1], "--plan-digest", plan["plan_digest"], "--uygula", "--json"],
    )
    assert applied.exit_code == 0, applied.output
    opencode = json.loads(opencode_path.read_text(encoding="utf-8"))
    assert opencode["mcp"]["servers"]["sample"]["command"] == ["server"]

    codex_path = native / ".codex" / "config.toml"
    codex_path.write_text(
        codex_path.read_text(encoding="utf-8").replace('command = "server"', 'command = "drift"'),
        encoding="utf-8",
    )
    sync = runner.invoke(app, ["mcp", "sync", "--home", str(home), "--json"])
    assert sync.exit_code == 70
    assert "Codex MCP entry user drift" in sync.output
