from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from zekam.application.tournament import (
    CandidateSubmission,
    IndependentTournamentSelector,
    SelectorScore,
    TournamentPlanner,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_topology import (
    TournamentBudget,
    TournamentCandidateAssignment,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 29, 10, 0, tzinfo=dt.UTC)


def _candidate(model: str, execution: str) -> TournamentCandidateAssignment:
    return TournamentCandidateAssignment(uuid4(), model, execution, 100, 50)


def _plan(*, human_final_gate: bool = False):  # type: ignore[no-untyped-def]
    candidates = (_candidate("builder-a", "exec-a"), _candidate("builder-b", "exec-b"))
    return TournamentPlanner().create_plan(
        candidates=candidates,
        shared_objective_digest=digest({"objective": "thumbnail"}),
        candidate_context_digest=digest({"context": "shared"}),
        selector_assignment_id=uuid4(),
        selector_model_id="independent-selector",
        selector_execution_identity="selector-exec",
        selector_spec_digest=digest({"selector": 1}),
        human_final_gate=human_final_gate,
        budget=TournamentBudget(2, 200, 100, NOW + dt.timedelta(minutes=5)),
        now=NOW,
    )


def _submission(assignment_id: UUID, suffix: str) -> CandidateSubmission:
    return CandidateSubmission(
        assignment_id,
        digest({"result": suffix}),
        50,
        20,
        NOW + dt.timedelta(minutes=1),
    )


def test_selector_model_and_execution_are_independent_from_candidates() -> None:
    plan = _plan()
    with pytest.raises(PolicyViolation, match="Selector candidate model"):
        replace(plan, selector_model_id=plan.candidate_assignments[0].model_id, plan_digest="")
    with pytest.raises(PolicyViolation, match="Selector candidate execution"):
        replace(
            plan,
            selector_execution_identity=plan.candidate_assignments[0].execution_identity,
            plan_digest="",
        )


def test_candidate_cannot_see_other_candidate_output() -> None:
    with pytest.raises(PolicyViolation, match="baska candidate"):
        CandidateSubmission(
            uuid4(),
            digest({"result": 1}),
            1,
            1,
            NOW,
            (digest({"other": 1}),),
        )


def test_independent_selector_selects_without_granting_promotion() -> None:
    plan = _plan()
    first, second = plan.candidate_assignments
    submissions = (_submission(first.assignment_id, "a"), _submission(second.assignment_id, "b"))
    result = IndependentTournamentSelector().select(
        plan=plan,
        submissions=submissions,
        scores=(
            SelectorScore(first.assignment_id, 0.4, digest({"score": "a"})),
            SelectorScore(second.assignment_id, 0.9, digest({"score": "b"})),
        ),
        selector_assignment_id=plan.selector_assignment_id,
        selector_model_id=plan.selector_model_id,
        selector_execution_identity=plan.selector_execution_identity,
        now=NOW + dt.timedelta(minutes=2),
    )
    assert result.selected_candidate_assignment_id == second.assignment_id
    assert result.status == "selected-candidate"
    assert result.grants_promotion is False


def test_qualitative_tournament_waits_for_human_final_gate() -> None:
    plan = _plan(human_final_gate=True)
    first, second = plan.candidate_assignments
    result = IndependentTournamentSelector().select(
        plan=plan,
        submissions=(_submission(first.assignment_id, "a"), _submission(second.assignment_id, "b")),
        scores=(
            SelectorScore(first.assignment_id, 1.0, digest({"score": "a"})),
            SelectorScore(second.assignment_id, 0.0, digest({"score": "b"})),
        ),
        selector_assignment_id=plan.selector_assignment_id,
        selector_model_id=plan.selector_model_id,
        selector_execution_identity=plan.selector_execution_identity,
        now=NOW + dt.timedelta(minutes=2),
    )
    assert result.status == "awaiting-human-review"


def test_missing_candidate_submission_is_fail_closed() -> None:
    plan = _plan()
    first, second = plan.candidate_assignments
    with pytest.raises(PolicyViolation, match="cardinality exact"):
        IndependentTournamentSelector().select(
            plan=plan,
            submissions=(_submission(first.assignment_id, "a"),),
            scores=(
                SelectorScore(first.assignment_id, 1.0, digest({"score": "a"})),
                SelectorScore(second.assignment_id, 0.0, digest({"score": "b"})),
            ),
            selector_assignment_id=plan.selector_assignment_id,
            selector_model_id=plan.selector_model_id,
            selector_execution_identity=plan.selector_execution_identity,
            now=NOW + dt.timedelta(minutes=2),
        )


def test_candidate_actual_usage_cannot_exceed_assignment_budget() -> None:
    plan = _plan()
    first, second = plan.candidate_assignments
    over = replace(_submission(first.assignment_id, "a"), used_tokens=101)
    with pytest.raises(PolicyViolation, match="token budget"):
        IndependentTournamentSelector().select(
            plan=plan,
            submissions=(over, _submission(second.assignment_id, "b")),
            scores=(
                SelectorScore(first.assignment_id, 1.0, digest({"score": "a"})),
                SelectorScore(second.assignment_id, 0.0, digest({"score": "b"})),
            ),
            selector_assignment_id=plan.selector_assignment_id,
            selector_model_id=plan.selector_model_id,
            selector_execution_identity=plan.selector_execution_identity,
            now=NOW + dt.timedelta(minutes=2),
        )
