"""WP-10.04 authority-free idea proposal/review contracts."""

from __future__ import annotations

import pytest

from zekam.application.idea_proposals import (
    CostEstimate,
    IdeaEvidence,
    build_idea_proposal,
    build_work_transition_plan,
    review_idea_proposals,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


def _evidence(ref: str = "knowledge/demo/lesson") -> IdeaEvidence:
    return IdeaEvidence(kind="knowledge", ref=ref, digest=digest(ref))


def _proposal(*, project_ref: str | None = "demo"):
    return build_idea_proposal(
        title="Arama sonucunu iyilestir",
        summary="Benzer kanitlari daha iyi sirala",
        project_ref=project_ref,
        impact="Daha az tekrar ve daha iyi citation kapsami",
        cost=CostEstimate.unknown("Olcum yapilmadi"),
        risk="medium",
        evidence=(_evidence(),),
    )


def test_idea_proposal_is_secret_free_bounded_and_authority_free() -> None:
    proposal = _proposal()
    payload = proposal.as_dict()

    assert payload["schema"] == "zekam-idea-proposal/v1"
    assert payload["cost"] == {
        "status": "unknown",
        "duration_ms": None,
        "tokens": None,
        "cost_micros": None,
        "reason": "Olcum yapilmadi",
    }
    assert payload["evidence"] == [
        {
            "kind": "knowledge",
            "ref": "knowledge/demo/lesson",
            "digest": digest("knowledge/demo/lesson"),
        }
    ]
    assert payload["candidate_only"] is True
    assert payload["activatable"] is False
    assert payload["read_only"] is True
    assert payload["grants_authority"] is False
    assert "work_id" not in payload


def test_secret_like_idea_text_is_rejected_without_persisting_content() -> None:
    with pytest.raises(PolicyViolation):
        build_idea_proposal(
            title="API key icin fikir",
            summary="secret: do-not-store",
            project_ref="demo",
            impact="Dusuk",
            cost=CostEstimate.unknown("Olcum yok"),
            risk="low",
            evidence=(_evidence(),),
        )


def test_public_pii_like_idea_text_is_rejected() -> None:
    with pytest.raises(PolicyViolation):
        build_idea_proposal(
            title="Kullanici geri bildirimi",
            summary="mehmet@example.com icin iyilestirme",
            project_ref="demo",
            impact="Ornek",
            cost=CostEstimate.unknown("Olcum yok"),
            risk="low",
            evidence=(_evidence(),),
        )


def test_review_deduplicates_by_project_scope_and_keeps_evidence_bounded() -> None:
    first = _proposal()
    second = build_idea_proposal(
        title="Arama sonucunu iyilestir",
        summary="Benzer kanitlari daha iyi sirala",
        project_ref="demo",
        impact="Daha az tekrar ve daha iyi citation kapsami",
        cost=CostEstimate.unknown("Olcum yapilmadi"),
        risk="medium",
        evidence=(_evidence("research/run-1"),),
    )
    other_project = _proposal(project_ref="other")

    review = review_idea_proposals((first, second, other_project))

    assert review["read_only"] is True
    assert review["grants_authority"] is False
    decisions = {item["proposal_digest"]: item for item in review["items"]}
    assert (
        decisions[first.proposal_digest]["dedup_key"]
        == decisions[second.proposal_digest]["dedup_key"]
    )
    assert {
        decisions[first.proposal_digest]["decision"],
        decisions[second.proposal_digest]["decision"],
    } == {
        "unique",
        "duplicate",
    }
    assert decisions[other_project.proposal_digest]["decision"] == "unique"


def test_work_transition_requires_exact_scope_and_explicit_approval() -> None:
    proposal = _proposal(project_ref=None)
    with pytest.raises(ValidationFailed):
        build_work_transition_plan(proposal, project_ref=None)

    pending = build_work_transition_plan(proposal, project_ref="demo")
    assert pending["state"] == "approval-required"
    assert pending["ready_for_work"] is False
    assert pending["approval_required"] is True
    assert pending["grants_authority"] is False
    assert "work_id" not in pending

    approved = build_work_transition_plan(
        proposal,
        project_ref="demo",
        approved=True,
        approval_scope_digest=pending["scope_digest"],
    )
    assert approved["state"] == "approval-recorded"
    assert approved["ready_for_work"] is True
    assert approved["creates_work"] is False

    with pytest.raises(PolicyViolation):
        build_work_transition_plan(
            proposal,
            project_ref="demo",
            approved=True,
            approval_scope_digest=digest("wrong-scope"),
        )
