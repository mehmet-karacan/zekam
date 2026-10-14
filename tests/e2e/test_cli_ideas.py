"""Read-only CLI vertical flow for WP-10.04 idea proposals."""

from __future__ import annotations

import json

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
