"""Bounded candidate-isolated tournament planning and independent selection."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.execution_topology import (
    TournamentBudget,
    TournamentCandidateAssignment,
    TournamentPlan,
)


@dataclass(frozen=True, slots=True)
class CandidateSubmission:
    assignment_id: UUID
    result_digest: str
    used_tokens: int
    used_cost_micros: int
    completed_at: dt.datetime
    visible_candidate_output_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        parse_digest(self.result_digest)
        if min(self.used_tokens, self.used_cost_micros) < 0:
            raise ValidationFailed("Candidate kullanim degerleri negatif olamaz")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValidationFailed("Candidate completed_at timezone-aware olmali")
        if self.visible_candidate_output_digests:
            raise PolicyViolation("Candidate baska candidate output'unu goremez")


@dataclass(frozen=True, slots=True)
class SelectorScore:
    candidate_assignment_id: UUID
    score: float
    evidence_digest: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.score):
            raise ValidationFailed("Selector score sonlu olmali")
        parse_digest(self.evidence_digest)


@dataclass(frozen=True, slots=True)
class TournamentResult:
    plan_digest: str
    selected_candidate_assignment_id: UUID
    selected_result_digest: str
    selector_assignment_id: UUID
    selector_model_id: str
    selector_execution_identity: str
    score_evidence_digest: str
    status: str
    result_digest: str
    grants_promotion: bool = False

    def __post_init__(self) -> None:
        if self.grants_promotion:
            raise PolicyViolation("Tournament result otomatik promotion yetkisi veremez")
        if self.status not in {"selected-candidate", "awaiting-human-review"}:
            raise ValidationFailed("Tournament result status gecersiz")
        for value in (self.plan_digest, self.selected_result_digest, self.score_evidence_digest):
            parse_digest(value)
        if self.result_digest:
            parse_digest(self.result_digest)
            if self.result_digest != self.computed_digest:
                raise PolicyViolation("Tournament result digest mismatch")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-tournament-result/v1",
            "plan_digest": self.plan_digest,
            "selected_candidate_assignment_id": str(self.selected_candidate_assignment_id),
            "selected_result_digest": self.selected_result_digest,
            "selector_assignment_id": str(self.selector_assignment_id),
            "selector_model_id": self.selector_model_id,
            "selector_execution_identity": self.selector_execution_identity,
            "score_evidence_digest": self.score_evidence_digest,
            "status": self.status,
            "grants_promotion": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> TournamentResult:
        values["result_digest"] = ""
        draft = cls(**values)
        return cls(**{**values, "result_digest": draft.computed_digest})

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"result_digest": self.result_digest}


@dataclass(frozen=True, slots=True)
class TournamentPlanner:
    def create_plan(
        self,
        *,
        candidates: tuple[TournamentCandidateAssignment, ...],
        shared_objective_digest: str,
        candidate_context_digest: str,
        selector_assignment_id: UUID,
        selector_model_id: str,
        selector_execution_identity: str,
        selector_spec_digest: str,
        human_final_gate: bool,
        budget: TournamentBudget,
        now: dt.datetime,
    ) -> TournamentPlan:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValidationFailed("Tournament plan zamani timezone-aware olmali")
        if budget.deadline <= now:
            raise PolicyViolation("Tournament deadline gecmis olamaz")
        return TournamentPlan.create(
            candidate_assignments=candidates,
            shared_objective_digest=shared_objective_digest,
            candidate_context_digest=candidate_context_digest,
            selector_assignment_id=selector_assignment_id,
            selector_model_id=selector_model_id,
            selector_execution_identity=selector_execution_identity,
            selector_spec_digest=selector_spec_digest,
            human_final_gate=human_final_gate,
            budget=budget,
        )


@dataclass(frozen=True, slots=True)
class IndependentTournamentSelector:
    """Tum candidate receipt'lerini exact-once puanlayip promotion vermeden secer."""

    def select(
        self,
        *,
        plan: TournamentPlan,
        submissions: tuple[CandidateSubmission, ...],
        scores: tuple[SelectorScore, ...],
        selector_assignment_id: UUID,
        selector_model_id: str,
        selector_execution_identity: str,
        now: dt.datetime,
    ) -> TournamentResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValidationFailed("Tournament selection zamani timezone-aware olmali")
        if now > plan.budget.deadline:
            raise PolicyViolation("Tournament deadline asildi")
        if (
            selector_assignment_id != plan.selector_assignment_id
            or selector_model_id != plan.selector_model_id
            or selector_execution_identity != plan.selector_execution_identity
        ):
            raise PolicyViolation("Tournament selector identity plan ile eslesmiyor")

        candidate_ids = {item.assignment_id for item in plan.candidate_assignments}
        submission_by_id = {item.assignment_id: item for item in submissions}
        score_by_id = {item.candidate_assignment_id: item for item in scores}
        if (
            len(submission_by_id) != len(submissions)
            or set(submission_by_id) != candidate_ids
            or len(score_by_id) != len(scores)
            or set(score_by_id) != candidate_ids
        ):
            raise PolicyViolation("Tournament candidate submission/score cardinality exact olmali")
        assignment_by_id = {item.assignment_id: item for item in plan.candidate_assignments}
        for assignment_id, submission in submission_by_id.items():
            assignment = assignment_by_id[assignment_id]
            if submission.used_tokens > assignment.token_budget:
                raise PolicyViolation("Candidate token budget'i asildi")
            if submission.used_cost_micros > assignment.cost_budget_micros:
                raise PolicyViolation("Candidate cost budget'i asildi")
            if submission.completed_at > plan.budget.deadline:
                raise PolicyViolation("Candidate deadline'i asti")
        if sum(item.used_tokens for item in submissions) > plan.budget.max_tokens:
            raise PolicyViolation("Tournament total token budget'i asildi")
        if sum(item.used_cost_micros for item in submissions) > plan.budget.max_cost_micros:
            raise PolicyViolation("Tournament total cost budget'i asildi")

        winner_id = max(candidate_ids, key=lambda item: (score_by_id[item].score, str(item)))
        winner = submission_by_id[winner_id]
        score_evidence = digest(
            {
                "selector_spec_digest": plan.selector_spec_digest,
                "scores": [
                    {
                        "candidate_assignment_id": str(item.candidate_assignment_id),
                        "score": item.score,
                        "evidence_digest": item.evidence_digest,
                    }
                    for item in sorted(scores, key=lambda item: str(item.candidate_assignment_id))
                ],
            }
        )
        return TournamentResult.create(
            plan_digest=plan.plan_digest,
            selected_candidate_assignment_id=winner_id,
            selected_result_digest=winner.result_digest,
            selector_assignment_id=selector_assignment_id,
            selector_model_id=selector_model_id,
            selector_execution_identity=selector_execution_identity,
            score_evidence_digest=score_evidence,
            status=("awaiting-human-review" if plan.human_final_gate else "selected-candidate"),
            grants_promotion=False,
        )
