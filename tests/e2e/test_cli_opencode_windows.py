from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from zekam.application.opencode_lifecycle import recent_events
from zekam.interfaces.cli import opencode as opencode_commands
from zekam.interfaces.cli.main import app


def test_opencode_plugin_event_and_precompact_have_durable_local_ack(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / ".zekam"

    event = runner.invoke(
        app,
        [
            "opencode",
            "event",
            "--type",
            "session.created",
            "--session",
            "ses_windows_acceptance",
            "--delivery-id",
            "delivery-windows-1",
            "--home",
            str(home),
        ],
    )
    assert event.exit_code == 0, event.output
    event_ack = json.loads(event.output)
    assert event_ack["status"] == "durable-local-ack"
    assert event_ack["contains_prompt"] is False
    assert event_ack["contains_response"] is False

    replay = runner.invoke(
        app,
        [
            "opencode",
            "event",
            "--type",
            "session.created",
            "--session",
            "ses_windows_acceptance",
            "--delivery-id",
            "delivery-windows-1",
            "--home",
            str(home),
        ],
    )
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output)["event_digest"] == event_ack["event_digest"]

    compact = runner.invoke(
        app,
        [
            "opencode",
            "pre-compact",
            "--session",
            "ses_windows_acceptance",
            "--delivery-id",
            "delivery-windows-compact",
            "--home",
            str(home),
        ],
    )
    assert compact.exit_code == 0, compact.output
    compact_ack = json.loads(compact.output)
    assert compact_ack["status"] == "checkpoint-acknowledged"
    assert compact_ack["durability"] == "local-ledger"
    assert compact_ack["grants_authority"] is False

    events = recent_events(home, limit=10)
    assert [item["event_type"] for item in reversed(events)] == [
        "session.created",
        "session.compacting",
    ]


def test_opencode_spool_status_is_read_only_and_content_free(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["opencode", "spool-status", "--home", str(tmp_path / ".zekam")],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["schema"] == "zekam-opencode-spool-status/v1"
    assert document["queued"] == 0
    assert document["grants_authority"] is False


def test_opencode_spool_drain_replays_only_typed_event(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / ".zekam"
    spool = home / "global" / "runtime" / "opencode-plugin-spool"
    spool.mkdir(parents=True)
    identifier = str(uuid4())
    item = {
        "schema": "zekam-opencode-plugin-spool/v2",
        "id": identifier,
        "args": [
            "opencode",
            "event",
            "--type",
            "session.idle",
            "--session",
            "ses_spool_replay",
            "--delivery-id",
            identifier,
        ],
        "attempts": 0,
    }
    (spool / f"1-{identifier}.json").write_text(json.dumps(item), encoding="utf-8")

    result = runner.invoke(
        app,
        ["opencode", "spool-drain", "--home", str(home)],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["acknowledged"] == 1
    assert document["remaining"] == 0
    assert document["contains_prompt"] is False
    assert document["contains_response"] is False
    assert recent_events(home, limit=10)[0]["event_type"] == "session.idle"


def test_opencode_install_supports_read_only_plan_and_explicit_apply(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    user_home = tmp_path / "user"
    executable = tmp_path / "opencode.exe"
    executable.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(opencode_commands.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(opencode_commands.Path, "home", lambda: user_home)

    planned = runner.invoke(app, ["opencode", "install"])
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["available"] is True
    assert plan["apply"] is False
    assert not (user_home / ".config" / "opencode" / "opencode.json").exists()

    applied = runner.invoke(app, ["opencode", "install", "--uygula"])
    assert applied.exit_code == 0, applied.output
    receipt = json.loads(applied.output)
    assert receipt["apply"] is True
    assert receipt["grants_authority"] is False
    assert (user_home / ".config" / "opencode" / "opencode.json").is_file()
    assert (user_home / ".config" / "opencode" / "plugins" / "zekam-lifecycle.js").is_file()
