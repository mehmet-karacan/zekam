"""Versioned personal-skill lifecycle contracts without authority-bearing content."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class SkillOriginKind(StrEnum):
    FAILURE_LESSON = "failure_lesson"
    VERIFIED_SUCCESS = "verified_success"
    USER_CORRECTION = "user_correction"
    USER_REQUEST = "user_request"


class SkillScopeKind(StrEnum):
    REALM = "realm"
    PROJECT = "project"
    USER = "user"


class SkillInvocationState(StrEnum):
    DISCOVERED = "discovered"
    SELECTED = "selected"
    LOADED = "loaded"
    INVOKED = "invoked"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    UNVERIFIED = "unverified"


_INVOCATION_NEXT = {
    SkillInvocationState.DISCOVERED: frozenset(
        {
            SkillInvocationState.SELECTED,
            SkillInvocationState.BLOCKED,
            SkillInvocationState.CANCELLED,
        }
    ),
    SkillInvocationState.SELECTED: frozenset(
        {SkillInvocationState.LOADED, SkillInvocationState.BLOCKED, SkillInvocationState.CANCELLED}
    ),
    SkillInvocationState.LOADED: frozenset(
        {SkillInvocationState.INVOKED, SkillInvocationState.BLOCKED, SkillInvocationState.CANCELLED}
    ),
    SkillInvocationState.INVOKED: frozenset(
        {
            SkillInvocationState.COMPLETED,
            SkillInvocationState.BLOCKED,
            SkillInvocationState.CANCELLED,
        }
    ),
    SkillInvocationState.COMPLETED: frozenset({SkillInvocationState.UNVERIFIED}),
    SkillInvocationState.UNVERIFIED: frozenset(),
    SkillInvocationState.BLOCKED: frozenset(),
    SkillInvocationState.CANCELLED: frozenset(),
}


def assert_invocation_transition(
    previous: SkillInvocationState | None, current: SkillInvocationState
) -> None:
    if previous is None:
        if current is not SkillInvocationState.DISCOVERED:
            raise PolicyViolation("Skill invocation must begin as discovered")
        return
    if current not in _INVOCATION_NEXT[previous]:
        raise PolicyViolation("Skill invocation state transition invalid")


def _bounded(value: str, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise ValidationFailed(f"Personal skill {label} invalid")
    return value


@dataclass(frozen=True, slots=True)
class SkillOriginEvidence:
    kind: SkillOriginKind
    evidence_digest: str
    work_ref: str
    run_ref: str
    artifact_revision_digest: str | None = None
    user_ref: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not SkillOriginKind:
            raise ValidationFailed("Personal skill exact origin kind required")
        parse_digest(self.evidence_digest)
        _bounded(self.work_ref, "work ref")
        _bounded(self.run_ref, "run ref")
        if self.kind is SkillOriginKind.USER_CORRECTION:
            if self.artifact_revision_digest is None or self.user_ref is None:
                raise ValidationFailed("User correction needs accepted artifact and user source")
            parse_digest(self.artifact_revision_digest)
            _bounded(self.user_ref, "user ref")
        elif self.kind is SkillOriginKind.USER_REQUEST:
            if self.user_ref is None or self.artifact_revision_digest is not None:
                raise ValidationFailed("User request needs user source without fabricated artifact")
            _bounded(self.user_ref, "user ref")
        elif self.user_ref is not None:
            raise ValidationFailed("Only real user correction/request may carry user_ref")
        if self.artifact_revision_digest is not None:
            parse_digest(self.artifact_revision_digest)

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": str(self.kind),
            "evidence_digest": self.evidence_digest,
            "work_ref": self.work_ref,
            "run_ref": self.run_ref,
            "artifact_revision_digest": self.artifact_revision_digest,
            "user_ref": self.user_ref,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class PersonalSkillRevision:
    skill_id: str
    name: str
    description: str
    version: int
    scope_kind: SkillScopeKind
    scope_ref: str
    package_digest: str
    author_ref: str
    trigger_terms: tuple[str, ...]
    non_trigger_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("skill_id", "name", "description", "scope_ref", "author_ref"):
            value = getattr(self, field)
            if not isinstance(value, str):
                raise ValidationFailed(f"Personal skill {field} must be text")
            _bounded(value, field, 1024)
        if type(self.version) is not int or not 1 <= self.version <= 100_000:
            raise ValidationFailed("Personal skill version invalid")
        if type(self.scope_kind) is not SkillScopeKind:
            raise ValidationFailed("Personal skill exact scope kind required")
        parse_digest(self.package_digest)
        for label, values in (
            ("trigger terms", self.trigger_terms),
            ("non-trigger terms", self.non_trigger_terms),
        ):
            if not isinstance(values, tuple) or len(values) > 64 or len(set(values)) != len(values):
                raise ValidationFailed(f"Personal skill {label} invalid")
            for value in values:
                _bounded(value, label, 128)
        if not self.trigger_terms:
            raise ValidationFailed("Personal skill at least one trigger term required")

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-personal-skill-revision/v2",
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "scope": {"kind": str(self.scope_kind), "ref": self.scope_ref},
            "package_digest": self.package_digest,
            "author_ref": self.author_ref,
            "trigger_terms": list(self.trigger_terms),
            "non_trigger_terms": list(self.non_trigger_terms),
            "grants_authority": False,
        }

    @property
    def revision_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class SkillEvaluationV2:
    revision_digest: str
    plan_digest: str
    state: str
    trials: int
    evaluator_ref: str
    verifier_ref: str
    evaluator_evidence_digest: str
    verifier_evidence_digest: str
    evaluator_execution_identity: str
    verifier_execution_identity: str
    metrics: dict[str, dict[str, float | str | int | bool]]
    uncertainty: dict[str, float | int | str]

    def __post_init__(self) -> None:
        parse_digest(self.revision_digest)
        parse_digest(self.plan_digest)
        if self.state not in {
            "improved",
            "equal",
            "regressed",
            "blocked",
            "failed",
            "insufficient-evidence",
        }:
            raise ValidationFailed("Skill evaluation v2 state invalid")
        if type(self.trials) is not int or not 0 <= self.trials <= 10_000:
            raise ValidationFailed("Skill evaluation v2 trials invalid")
        _bounded(self.evaluator_ref, "evaluator")
        _bounded(self.verifier_ref, "verifier")
        parse_digest(self.evaluator_evidence_digest)
        parse_digest(self.verifier_evidence_digest)
        _bounded(self.evaluator_execution_identity, "evaluator execution identity")
        _bounded(self.verifier_execution_identity, "verifier execution identity")
        if (
            self.evaluator_ref == self.verifier_ref
            or self.evaluator_evidence_digest == self.verifier_evidence_digest
            or self.evaluator_execution_identity == self.verifier_execution_identity
        ):
            raise PolicyViolation("Skill evaluation v2 independent verifier evidence required")
        if not isinstance(self.metrics, dict) or not isinstance(self.uncertainty, dict):
            raise ValidationFailed("Skill evaluation v2 metrics invalid")
        if self.state in {"improved", "equal", "regressed"} and not self.metrics:
            raise ValidationFailed("Comparable skill evaluation v2 metrics required")
        comparisons: list[int] = []
        for metric in self.metrics.values():
            if not isinstance(metric, dict):
                raise ValidationFailed("Skill evaluation v2 metric body invalid")
            baseline = metric.get("baseline")
            candidate = metric.get("candidate")
            direction = metric.get("direction", "maximize")
            if (
                isinstance(baseline, bool)
                or not isinstance(baseline, (int, float))
                or isinstance(candidate, bool)
                or not isinstance(candidate, (int, float))
                or not math.isfinite(float(baseline))
                or not math.isfinite(float(candidate))
                or direction not in {"maximize", "minimize"}
            ):
                raise ValidationFailed("Skill evaluation v2 metric comparison invalid")
            delta = float(candidate) - float(baseline)
            if direction == "minimize":
                delta = -delta
            comparisons.append(1 if delta > 0 else -1 if delta < 0 else 0)
        if self.state == "improved" and (1 not in comparisons or -1 in comparisons):
            raise ValidationFailed("Improved evaluation metrics contradict state")
        if self.state == "equal" and any(comparison != 0 for comparison in comparisons):
            raise ValidationFailed("Equal evaluation metrics contradict state")
        if self.state == "regressed" and -1 not in comparisons:
            raise ValidationFailed("Regressed evaluation metrics contradict state")
        if self.uncertainty.get("sample_size") != self.trials:
            raise ValidationFailed("Skill evaluation uncertainty sample size drift")

    @property
    def activatable(self) -> bool:
        return self.state == "improved" and self.trials >= 5

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-personal-skill-evaluation/v2",
            "revision_digest": self.revision_digest,
            "plan_digest": self.plan_digest,
            "state": self.state,
            "trials": self.trials,
            "evaluator_ref": self.evaluator_ref,
            "verifier_ref": self.verifier_ref,
            "evaluator_evidence_digest": self.evaluator_evidence_digest,
            "verifier_evidence_digest": self.verifier_evidence_digest,
            "evaluator_execution_identity": self.evaluator_execution_identity,
            "verifier_execution_identity": self.verifier_execution_identity,
            "metrics": self.metrics,
            "uncertainty": self.uncertainty,
            "activatable": self.activatable,
            "grants_authority": False,
        }
