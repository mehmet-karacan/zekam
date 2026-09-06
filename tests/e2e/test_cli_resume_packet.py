from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from zekam.application.workspace_resume import build_resume_packet, render_resume_prompt
from zekam.interfaces.cli.main import app


def test_resume_packet_combines_work_checkpoint_projects_and_capabilities(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / ".zekam"
    source = tmp_path / "demo"
    source.mkdir()

    initialized = runner.invoke(app, ["init", "--home", str(home)])
    assert initialized.exit_code == 0, initialized.output
    added = runner.invoke(
        app,
        ["project", "add", str(source), "--slug", "demo", "--uygula", "--home", str(home)],
    )
    assert added.exit_code == 0, added.output
    created = runner.invoke(
        app,
        [
            "work",
            "create",
            "demo",
            "Resume acceptance",
            "--ozet",
            "Model degisse de devam et",
            "--kriter",
            "Sonraki adim gorunur",
            "--uygula",
            "--home",
            str(home),
        ],
    )
    assert created.exit_code == 0, created.output
    checkpoint = runner.invoke(
        app,
        [
            "opencode",
            "event",
            "--type",
            "session.checkpoint",
            "--session",
            "session-a",
            "--completed",
            "Resume packet uygulandi",
            "--pending",
            "Bagimsiz verifier",
            "--next-action",
            "Verifier sonucunu kontrol et",
            "--home",
            str(home),
        ],
    )
    assert checkpoint.exit_code == 0, checkpoint.output

    result = runner.invoke(
        app,
        ["resume", "--json", "--session", "session-b", "--home", str(home)],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["schema"] == "zekam-resume-packet/v1"
    assert document["semantic_state"] == "ready"
    assert document["latest_semantic_checkpoint"]["completed"] == "Resume packet uygulandi"
    assert document["latest_semantic_checkpoint"]["pending"] == "Bagimsiz verifier"
    assert document["next_safe_action"] == "Verifier sonucunu kontrol et"
    assert document["work"]["open"][0]["title"] == "Resume acceptance"
    assert document["projects"][0]["project_ref"] == "demo"
    assert document["projects"][0]["rag"]["state"] == "unavailable"
    assert "project-rag" in document["capabilities"]["ready"]
    assert document["read_only"] is True
    assert document["grants_authority"] is False
    assert document["packet_digest"].startswith("sha256:")

    prompt = runner.invoke(
        app,
        ["resume", "--prompt", "--session", "session-b", "--home", str(home)],
    )
    assert prompt.exit_code == 0, prompt.output
    assert prompt.output.startswith("ZEKAM_RESUME_PACKET_V1")
    assert "not authority or instructions" in prompt.output

    same_session = runner.invoke(
        app,
        ["resume", "--json", "--session", "session-a", "--home", str(home)],
    )
    assert same_session.exit_code == 0, same_session.output
    assert json.loads(same_session.output)["semantic_state"] == "work-only"
    compaction_view = runner.invoke(app, ["resume", "--json", "--home", str(home)])
    assert compaction_view.exit_code == 0, compaction_view.output
    assert json.loads(compaction_view.output)["semantic_state"] == "ready"


def test_capabilities_expose_ready_partial_and_scaffold_without_claiming_authority() -> None:
    result = CliRunner().invoke(app, ["capabilities", "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["schema"] == "zekam-capability-inventory/v1"
    assert document["counts"] == {"ready": 6, "partial": 6, "scaffold": 1}
    assert document["read_only"] is True
    assert document["grants_authority"] is False
    assert all(item["verified_by"] for item in document["capabilities"])
    assert any(item["gap"] for item in document["capabilities"] if item["status"] != "ready")


def test_resume_prompt_projects_large_state_to_a_bounded_valid_packet() -> None:
    packet = {
        "schema": "zekam-resume-packet/v1",
        "semantic_state": "ready",
        "latest_semantic_checkpoint": None,
        "work": {
            "open": [
                {
                    "id": str(index),
                    "title": "x" * 500,
                    "acceptance_criteria": ["y" * 500] * 20,
                }
                for index in range(20)
            ],
            "recently_completed": [],
        },
        "projects": [
            {"project_ref": str(index), "aliases": ["z" * 500] * 20} for index in range(50)
        ],
        "capabilities": {"ready": ["project-rag"]},
        "packet_digest": "sha256:" + "0" * 64,
        "grants_authority": False,
    }

    prompt = render_resume_prompt(packet)

    assert len(prompt.encode("utf-8")) <= 16 * 1024
    projected = json.loads(prompt.split("\n", 2)[2])
    assert projected["prompt_truncated"] is True
    assert projected["prompt_projection_of"] == packet["packet_digest"]
    assert projected["grants_authority"] is False


def test_resume_prompt_redacts_secret_like_work_text_before_system_injection() -> None:
    sensitive_text = "password" + "='real-resume-value-12345'"
    packet = {
        "schema": "zekam-resume-packet/v1",
        "work": {"open": [{"title": sensitive_text}], "recently_completed": []},
        "projects": [],
        "packet_digest": "sha256:" + "1" * 64,
        "grants_authority": False,
    }

    prompt = render_resume_prompt(packet)

    assert sensitive_text not in prompt
    projected = json.loads(prompt.split("\n", 2)[2])
    assert projected["work"]["open"][0]["title"] == "[REDACTED:secret-like]"
    assert projected["prompt_redacted_fields"] == 1
    assert projected["prompt_projection_of"] == packet["packet_digest"]


def test_workspace_resume_does_not_quarantine_invalid_lifecycle_files(tmp_path: Path) -> None:
    home = tmp_path / ".zekam"
    lifecycle = home / "global" / "runtime" / "opencode-lifecycle"
    lifecycle.mkdir(parents=True)
    invalid = lifecycle / "invalid.json"
    invalid.write_text("not-json", encoding="utf-8")

    packet = build_resume_packet(home)

    assert packet["read_only"] is True
    assert invalid.read_text(encoding="utf-8") == "not-json"
    assert not (lifecycle / "quarantine").exists()
