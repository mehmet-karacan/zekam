"""Read-only CLI vertical flow for WP-10.04 idea proposals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.idea_proposals import build_work_transition_plan, idea_proposal_from_dict
from zekam.domain.canonical import digest
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.e2e


def test_idea_cli_propose_review_and_work_plan_are_provider_free() -> None:
    runner = CliRunner()
    proposed = runner.invoke(
        app,
        [
            "idea",
            "propose",
            "Arama",
            "Benzer kanitlari sirala",
            "--impact",
            "Daha iyi citation",
            "--risk",
            "medium",
            "--project",
            "demo",
            "--json",
        ],
    )
    assert proposed.exit_code == 0, proposed.output
    proposal = json.loads(proposed.output)

    reviewed = runner.invoke(app, ["idea", "review", "--proposal", proposed.output, "--json"])
    assert reviewed.exit_code == 0, reviewed.output
    review = json.loads(reviewed.output)
    assert review["read_only"] is True
    assert review["items"][0]["decision"] == "unique"

    planned = runner.invoke(
        app,
        ["idea", "work-plan", proposed.output, "--project", "demo", "--json"],
    )
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["state"] == "approval-required"
    assert plan["creates_work"] is False
    assert plan["grants_authority"] is False

    approved = build_work_transition_plan(
        # CLI payload is verified before it reaches this application helper.
        idea_proposal_from_dict(proposal),
        project_ref="demo",
        approved=True,
        approval_scope_digest=plan["scope_digest"],
    )
    assert approved["ready_for_work"] is True
    assert approved["creates_work"] is False
    assert proposal["proposal_digest"] == digest(
        {key: value for key, value in proposal.items() if key != "proposal_digest"}
    )


def test_idea_work_create_requires_exact_approval_scope_and_writes_canonical_work(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    source = tmp_path / "demo"
    source.mkdir()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    added = runner.invoke(
        app,
        [
            "project",
            "add",
            str(source),
            "--slug",
            "demo",
            "--uygula",
            "--home",
            str(home),
        ],
    )
    assert added.exit_code == 0, added.output
    proposed = runner.invoke(
        app,
        [
            "idea",
            "propose",
            "Arama",
            "Benzer kanitlari sirala",
            "--impact",
            "Daha iyi citation",
            "--risk",
            "medium",
            "--project",
            "demo",
            "--json",
        ],
    )
    assert proposed.exit_code == 0, proposed.output
    plan = runner.invoke(
        app,
        ["idea", "work-plan", proposed.output, "--project", "demo", "--json"],
    )
    assert plan.exit_code == 0, plan.output
    scope_digest = json.loads(plan.output)["scope_digest"]

    rejected = runner.invoke(
        app,
        [
            "idea",
            "work-create",
            proposed.output,
            "--project",
            "demo",
            "--approval-scope-digest",
            digest("wrong-scope"),
            "--uygula",
            "--home",
            str(home),
        ],
    )
    assert rejected.exit_code != 0
    assert "eslesmiyor" in rejected.output

    created = runner.invoke(
        app,
        [
            "idea",
            "work-create",
            proposed.output,
            "--project",
            "demo",
            "--approval-scope-digest",
            scope_digest,
            "--uygula",
            "--json",
            "--home",
            str(home),
        ],
    )
    assert created.exit_code == 0, created.output
    document = json.loads(created.output)
    assert document["state"] == "work-created"
    assert document["creates_work"] is True
    assert document["work"]["kind"] == "idea"

    listed = runner.invoke(
        app,
        ["work", "list", "--proje", "demo", "--json", "--home", str(home)],
    )
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)[0]["type"] == "idea"
