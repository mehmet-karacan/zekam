"""Current-source branch closure for the bounded research domain."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.research import (
    Citation,
    CitationVerification,
    Conflict,
    ConflictKind,
    Finding,
    PlanCandidate,
    ReportStatus,
    ResearchBudget,
    ResearchDag,
    ResearchNode,
    ResearchQuestion,
    ResearchReport,
    ResearchRole,
    RoleOutcome,
    RoleResult,
    SourceKind,
    SourcePolicy,
    SourceSnapshot,
    synthesize,
)

NOW = dt.datetime(2026, 9, 5, tzinfo=dt.UTC)
DIGEST = digest("wp16-research")


def _question(**changes: object) -> ResearchQuestion:
    values: dict[str, object] = {
        "question_id": "q",
        "question": "Is recovery evidence complete?",
        "project_ref": "akilli-kasa",
        "work_ref": "WP-16",
        "intent_digest": DIGEST,
        "source_revision": "HEAD",
        "policy": SourcePolicy(frozenset({SourceKind.FILE}), project_scope="akilli-kasa"),
        "budget": ResearchBudget(10, 1, 10),
        "created_at": NOW,
    }
    values.update(changes)
    return ResearchQuestion(**values)  # type: ignore[arg-type]


def _evidence() -> tuple[SourceSnapshot, Finding, CitationVerification]:
    snapshot = SourceSnapshot("s", SourceKind.FILE, "docs/a.md", DIGEST, NOW)
    finding = Finding("f", "Recovery is evidenced", (Citation("s", "L1", DIGEST),), "high")
    return snapshot, finding, CitationVerification("verifier", ("f",))


def _report(**changes: object) -> ResearchReport:
    snapshot, finding, verification = _evidence()
    values: dict[str, object] = {
        "report_id": "r",
        "question_id": "q",
        "question_digest": DIGEST,
        "findings": (finding,),
        "unresolved_conflicts": (),
        "non_success_results": (),
        "verification": verification,
        "snapshots": (snapshot,),
        "status": ReportStatus.ANSWERED,
    }
    values.update(changes)
    return ResearchReport(**values)  # type: ignore[arg-type]


def test_question_remaining_bounds_empty_fields_and_current_path() -> None:
    with pytest.raises(ValidationFailed, match="bounded"):
        _question(question="x" * 1001)
    for field in ("project_ref", "work_ref", "source_revision"):
        with pytest.raises(ValidationFailed, match=f"{field} bos"):
            _question(**{field: " "})

    question = _question()
    question.assert_current(current_source_revision="HEAD", current_intent_digest=DIGEST)
    assert not question.is_stale(current_source_revision="HEAD", current_intent_digest=DIGEST)


def test_snapshot_remaining_locator_time_and_https_host_fail_closed() -> None:
    with pytest.raises(ValidationFailed, match="locator bos"):
        SourceSnapshot("s", SourceKind.FILE, " ", DIGEST, NOW)
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        SourceSnapshot("s", SourceKind.FILE, "a.md", DIGEST, NOW.replace(tzinfo=None))
    with pytest.raises(ValidationFailed, match="host alani"):
        SourceSnapshot(
            "s", SourceKind.HTTPS, "https://example.com/a", DIGEST, NOW, host="other.com"
        )


def test_dag_conflict_verification_and_empty_synthesis_reject_invalid_state() -> None:
    with pytest.raises(ValidationFailed, match="DAG bos"):
        ResearchDag("q", ())
    with pytest.raises(ValidationFailed, match="tekrar"):
        ResearchDag(
            "q",
            (
                ResearchNode("same", ResearchRole.RESEARCHER),
                ResearchNode("same", ResearchRole.CRITIC),
            ),
        )

    # Exercise the method-level cycle guard even when deserializing an object that
    # has not passed the constructor's identical guard.
    dag = object.__new__(ResearchDag)
    object.__setattr__(dag, "question_id", "q")
    object.__setattr__(
        dag,
        "nodes",
        (
            ResearchNode("a", ResearchRole.RESEARCHER, ("b",)),
            ResearchNode("b", ResearchRole.CRITIC, ("a",)),
        ),
    )
    with pytest.raises(ValidationFailed, match="dongu"):
        dag.parallel_groups()

    with pytest.raises(ValidationFailed, match="farkli"):
        Conflict("c", ConflictKind.COMPATIBLE, "f", "f", "detail")
    with pytest.raises(ValidationFailed, match="aciklamasi bos"):
        Conflict("c", ConflictKind.COMPATIBLE, "f1", "f2", " ")
    with pytest.raises(ValidationFailed, match="her red"):
        CitationVerification("v", (), ("f",), ())
    with pytest.raises(ValidationFailed, match="en az bir"):
        synthesize((), conflicts=(), verification=CitationVerification("v", ()))


def test_report_and_candidate_remaining_invariants_fail_closed() -> None:
    _, finding, verification = _evidence()
    blocked = RoleResult(
        ResearchRole.CRITIC,
        "critic",
        RoleOutcome.BLOCKED,
        blocker="missing evidence",
    )
    conflict = Conflict("c", ConflictKind.DIRECT_CONTRADICTION, "f", "other", "conflict")

    invalid_reports: tuple[dict[str, object], ...] = (
        {"grants_authority": True},
        {"findings": ()},
        {"unresolved_conflicts": (conflict,)},
        {"non_success_results": (blocked,)},
        {"status": ReportStatus.ABSTAINED},
    )
    for changes in invalid_reports:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            _report(**changes)

    unknown = Finding("f", "Unknown source", (Citation("unknown", "L1", DIGEST),), "high")
    with pytest.raises(ValidationFailed, match="bilinmeyen snapshot"):
        _report(findings=(unknown,))

    base: dict[str, object] = {
        "candidate_id": "pc",
        "report_id": "r",
        "report_digest": DIGEST,
        "work_ref": "WP-16",
        "source_revision": "HEAD",
        "proposed_steps": ("verify",),
        "writable_resources": ("docs/a.md",),
        "acceptance": ("passes",),
        "rollback": "revert",
        "risk": "low",
    }
    invalid_candidates: tuple[dict[str, object], ...] = (
        {"proposed_steps": ()},
        {"acceptance": ()},
        {"rollback": " "},
        {"risk": "unknown"},
    )
    for changes in invalid_candidates:
        values = dict(base)
        values.update(changes)
        with pytest.raises(ValidationFailed):
            PlanCandidate(**values)  # type: ignore[arg-type]

    assert _report(findings=(finding,), verification=verification).is_actionable
