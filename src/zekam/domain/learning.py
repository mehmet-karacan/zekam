"""Ogrenme dongusu, skill yasam dongusu ve olculu iterasyon sozlesmesi.

Ders **kanittan** turer: dogrulanmis kok neden olmadan ders uretilmez, ayni kanit
iki kez sayilmaz. Skill kendi kendini aktif registry'ye yazamaz; aktivasyon exact
approval ve rollback plani ister. Dongu sinirsiz donmez: iterasyon, maliyet ve
ilerleme durgunlugu durdurucudur.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.optimization import (
    MeasurementEvidence,
    MetricDirection,
    MetricRole,
    MetricSpec,
    ProgressState,
    evaluate_progress,
)

#: Bir dersin uretilebilmesi icin gereken bagimsiz gozlem sayisi.
MINIMUM_OBSERVATIONS = 2

#: Skill degerlendirmesi icin gereken en az deneme sayisi.
MINIMUM_SKILL_TRIALS = 5

MAX_ITERATIONS = 10


class LearningTarget(StrEnum):
    TEST = "test"
    EVAL = "eval"
    GUIDANCE = "guidance"
    SKILL = "skill"


class SkillState(StrEnum):
    CANDIDATE = "candidate"
    EVALUATED = "evaluated"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


class StopReason(StrEnum):
    GOAL_REACHED = "goal-reached"
    ITERATION_BUDGET = "iteration-budget"
    COST_BUDGET = "cost-budget"
    NO_PROGRESS = "no-progress"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class FailureOccurrence:
    """Tek bir basarisizlik gozlemi.

    `evidence_digest` ayni kanitin iki kez sayilmasini engeller: farkli run'lar
    ayni digest'i uretiyorsa bu tek gozlemdir.
    """

    occurrence_key: str
    evidence_digest: str
    run_ref: str
    observed_at: dt.datetime
    failure_category: str

    def __post_init__(self) -> None:
        parse_digest(self.evidence_digest)
        for label, value in (
            ("occurrence_key", self.occurrence_key),
            ("run_ref", self.run_ref),
            ("failure_category", self.failure_category),
        ):
            if not value.strip():
                raise ValidationFailed(f"{label} bos olamaz")
        if self.observed_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "occurrence_key": self.occurrence_key,
            "evidence_digest": self.evidence_digest,
            "run_ref": self.run_ref,
            "failure_category": self.failure_category,
        }


def distinct_observations(occurrences: tuple[FailureOccurrence, ...]) -> int:
    """Ayni kanit digest'i tek gozlem sayilir."""

    return len({item.evidence_digest for item in occurrences})


@dataclass(frozen=True, slots=True)
class RootCause:
    """Dogrulanmis kok neden. Dogrulanmadan ders uretilemez."""

    statement: str
    verified_by: str
    evidence_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.evidence_digest)
        if not self.statement.strip():
            raise ValidationFailed("kok neden ifadesi bos olamaz")
        if not self.verified_by.strip():
            raise ValidationFailed("kok nedeni dogrulayan kimlik bos olamaz")

    def as_dict(self) -> dict[str, str]:
        return {
            "statement": self.statement,
            "verified_by": self.verified_by,
            "evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True, slots=True)
class LearningCandidate:
    """Ders adayi. Kanit ve kok neden olmadan hedefe donusemez."""

    candidate_id: str
    occurrence_key: str
    occurrences: tuple[FailureOccurrence, ...]
    target: LearningTarget
    proposal: str
    author_ref: str
    root_cause: RootCause | None = None
    critical: bool = False

    def __post_init__(self) -> None:
        if not self.occurrences:
            raise ValidationFailed("ders adayi en az bir gozlem ister")
        if any(item.occurrence_key != self.occurrence_key for item in self.occurrences):
            raise ValidationFailed("gozlemler ayni occurrence key'e ait olmali")
        if not self.proposal.strip():
            raise ValidationFailed("oneri bos olamaz")

    @property
    def observation_count(self) -> int:
        return distinct_observations(self.occurrences)

    def readiness(self) -> tuple[bool, str]:
        """Ders uretilebilir mi? (karar, gerekce)"""

        if self.root_cause is None:
            return False, "dogrulanmis kok neden olmadan ders uretilmez"
        if self.observation_count < MINIMUM_OBSERVATIONS and not self.critical:
            return False, (
                f"en az {MINIMUM_OBSERVATIONS} bagimsiz gozlem veya kritik olay isareti gerekir"
            )
        return True, "kok neden dogrulandi ve gozlem esigi karsilandi"

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "occurrence_key": self.occurrence_key,
            "observation_count": self.observation_count,
            "target": str(self.target),
            "proposal": self.proposal,
            "author_ref": self.author_ref,
            "root_cause": self.root_cause.as_dict() if self.root_cause else None,
            "critical": self.critical,
        }


@dataclass(frozen=True, slots=True)
class LearningPromotion:
    """Ders adayinin hedefe donusme karari."""

    candidate_id: str
    target: LearningTarget
    approved: bool
    verifier_ref: str
    reason: str

    def __post_init__(self) -> None:
        if not self.verifier_ref.strip():
            raise ValidationFailed("verifier referansi bos olamaz")
        if not self.reason.strip():
            raise ValidationFailed("karar gerekce ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "target": str(self.target),
            "approved": self.approved,
            "verifier_ref": self.verifier_ref,
            "reason": self.reason,
        }


def promote_learning(candidate: LearningCandidate, *, verifier_ref: str) -> LearningPromotion:
    """Bagimsiz verifier ile ders kararini uretir."""

    if verifier_ref == candidate.author_ref:
        raise PolicyViolation("ders verifier'i yazarla ayni kimlik olamaz")
    ready, reason = candidate.readiness()
    return LearningPromotion(
        candidate_id=candidate.candidate_id,
        target=candidate.target,
        approved=ready,
        verifier_ref=verifier_ref,
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class SkillFixture:
    """Skill degerlendirme fixture'i. Surumlu ve secret-free."""

    fixture_id: str
    version: str
    content_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.content_digest)
        if not self.version.strip():
            raise ValidationFailed("fixture surumu bos olamaz")

    def as_dict(self) -> dict[str, str]:
        return {
            "fixture_id": self.fixture_id,
            "version": self.version,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class SkillEvaluation:
    """Skill olcumu. Degerlendiren ve dogrulayan kimlikler ayridir."""

    skill_id: str
    fixtures: tuple[SkillFixture, ...]
    trials: int
    successes: int
    evaluator_ref: str
    verifier_ref: str
    baseline_success_rate: float

    def __post_init__(self) -> None:
        if not self.fixtures:
            raise ValidationFailed("degerlendirme fixture ister")
        if self.trials < MINIMUM_SKILL_TRIALS:
            raise ValidationFailed(f"en az {MINIMUM_SKILL_TRIALS} deneme gerekir")
        if not 0 <= self.successes <= self.trials:
            raise ValidationFailed("basari sayisi deneme sayisini asamaz")
        if self.evaluator_ref == self.verifier_ref:
            raise PolicyViolation("degerlendiren ve dogrulayan ayni kimlik olamaz")
        if not 0.0 <= self.baseline_success_rate <= 1.0:
            raise ValidationFailed("baseline orani 0..1 araliginda olmali")

    @property
    def success_rate(self) -> float:
        return self.successes / self.trials

    @property
    def improves(self) -> bool:
        return self.success_rate > self.baseline_success_rate

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "fixtures": [item.as_dict() for item in self.fixtures],
            "trials": self.trials,
            "successes": self.successes,
            "success_rate": round(self.success_rate, 6),
            "baseline_success_rate": self.baseline_success_rate,
            "improves": self.improves,
            "evaluator_ref": self.evaluator_ref,
            "verifier_ref": self.verifier_ref,
        }

    @property
    def evaluation_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class Skill:
    """Kayitli skill. Kendi kendini aktive edemez."""

    skill_id: str
    name: str
    body_digest: str
    state: SkillState
    revision: int
    author_ref: str
    evaluation_digest: str | None = None
    approved_by: str | None = None
    rollback_plan: str | None = None
    self_promoted: bool = False

    def __post_init__(self) -> None:
        if self.self_promoted:
            raise PolicyViolation("skill kendi kendini aktif registry'ye yazamaz")
        parse_digest(self.body_digest)
        if self.revision < 1:
            raise ValidationFailed("revision 1'den kucuk olamaz")
        if self.state is SkillState.ACTIVE:
            self._assert_activatable()

    def _assert_activatable(self) -> None:
        if self.evaluation_digest is None:
            raise PolicyViolation("olculmemis skill aktif olamaz")
        if self.approved_by is None:
            raise AuthorizationRequired("skill aktivasyonu exact approval ister")
        if self.approved_by == self.author_ref:
            raise PolicyViolation("approval yazarla ayni kimlik olamaz")
        if not (self.rollback_plan or "").strip():
            raise PolicyViolation("skill aktivasyonu rollback plani ister")

    def activate(
        self, evaluation: SkillEvaluation, *, approved_by: str, rollback_plan: str
    ) -> Skill:
        """Aktivasyon: olcum, bagimsiz onay ve rollback plani zorunlu."""

        if self.state not in {SkillState.CANDIDATE, SkillState.EVALUATED}:
            raise PolicyViolation("yalniz aday veya olculmus skill aktive edilir")
        if evaluation.skill_id != self.skill_id:
            raise ValidationFailed("degerlendirme baska bir skill'e ait")
        if not evaluation.improves:
            raise PolicyViolation("baseline'i gecmeyen skill aktive edilemez")
        return Skill(
            skill_id=self.skill_id,
            name=self.name,
            body_digest=self.body_digest,
            state=SkillState.ACTIVE,
            revision=self.revision,
            author_ref=self.author_ref,
            evaluation_digest=evaluation.evaluation_digest,
            approved_by=approved_by,
            rollback_plan=rollback_plan,
        )

    def deprecate(self, reason: str) -> Skill:
        if self.state is not SkillState.ACTIVE:
            raise PolicyViolation("yalniz aktif skill deprecate edilir")
        if not reason.strip():
            raise ValidationFailed("deprecate gerekce ister")
        return Skill(
            skill_id=self.skill_id,
            name=self.name,
            body_digest=self.body_digest,
            state=SkillState.DEPRECATED,
            revision=self.revision,
            author_ref=self.author_ref,
            evaluation_digest=self.evaluation_digest,
            approved_by=self.approved_by,
            rollback_plan=self.rollback_plan,
        )

    def retire(self) -> Skill:
        if self.state not in {SkillState.DEPRECATED, SkillState.ACTIVE}:
            raise PolicyViolation("yalniz aktif veya deprecated skill emekliye ayrilir")
        return Skill(
            skill_id=self.skill_id,
            name=self.name,
            body_digest=self.body_digest,
            state=SkillState.RETIRED,
            revision=self.revision,
            author_ref=self.author_ref,
            evaluation_digest=self.evaluation_digest,
            approved_by=self.approved_by,
            rollback_plan=self.rollback_plan,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "body_digest": self.body_digest,
            "state": str(self.state),
            "revision": self.revision,
            "author_ref": self.author_ref,
            "evaluation_digest": self.evaluation_digest,
            "approved_by": self.approved_by,
            "rollback_plan": self.rollback_plan,
            "self_promoted": False,
        }


def deduplicate_skills(candidates: tuple[Skill, ...]) -> tuple[Skill, ...]:
    """Ayni govdeye sahip skill adaylari tekrar kaydedilmez."""

    seen: set[str] = set()
    unique: list[Skill] = []
    for candidate in candidates:
        if candidate.body_digest in seen:
            continue
        seen.add(candidate.body_digest)
        unique.append(candidate)
    return tuple(unique)


@dataclass(frozen=True, slots=True)
class IterationBudget:
    """Olculu dongu butcesi. Sinirsiz iterasyon yoktur."""

    max_iterations: int = 3
    max_cost_units: int = 100
    stall_limit: int = 2

    def __post_init__(self) -> None:
        if not 1 <= self.max_iterations <= MAX_ITERATIONS:
            raise ValidationFailed(f"iterasyon siniri 1..{MAX_ITERATIONS} olmali")
        if self.max_cost_units <= 0:
            raise ValidationFailed("maliyet butcesi pozitif olmali")
        if self.stall_limit < 1:
            raise ValidationFailed("durgunluk siniri pozitif olmali")

    def as_dict(self) -> dict[str, int]:
        return {
            "max_iterations": self.max_iterations,
            "max_cost_units": self.max_cost_units,
            "stall_limit": self.stall_limit,
        }


@dataclass(frozen=True, slots=True)
class IterationOutcome:
    """Tek iterasyonun olculebilir sonucu."""

    iteration: int
    score: float
    cost_units: int
    verified: bool

    def __post_init__(self) -> None:
        if self.iteration < 1:
            raise ValidationFailed("iterasyon 1'den kucuk olamaz")
        if self.cost_units < 0:
            raise ValidationFailed("maliyet negatif olamaz")
        if not math.isfinite(self.score):
            raise ValidationFailed("iterasyon skoru sonlu olmali")


@dataclass(frozen=True, slots=True)
class LoopDecision:
    """Donguye devam edilir mi ve neden?"""

    should_continue: bool
    reason: StopReason | None
    detail: str
    spent_cost: int
    best_score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "should_continue": self.should_continue,
            "reason": str(self.reason) if self.reason else None,
            "detail": self.detail,
            "spent_cost": self.spent_cost,
            "best_score": round(self.best_score, 6),
        }


def evaluate_loop(
    outcomes: tuple[IterationOutcome, ...],
    budget: IterationBudget,
    *,
    goal_score: float,
    blocked: bool = False,
) -> LoopDecision:
    """Legacy scalar API'yi shared directional optimization cekirdegine uyarlar."""

    if not math.isfinite(goal_score):
        raise ValidationFailed("dongu hedef skoru sonlu olmali")
    if outcomes:
        ordinals = tuple(item.iteration for item in outcomes)
        if ordinals != tuple(sorted(set(ordinals))):
            raise ValidationFailed("dongu iteration sirasi tekil ve artan olmali")

    spent = sum(item.cost_units for item in outcomes)
    best = max((item.score for item in outcomes), default=0.0)

    if blocked:
        return LoopDecision(False, StopReason.BLOCKED, "harici blocker", spent, best)
    if any(item.verified and item.score >= goal_score for item in outcomes):
        return LoopDecision(False, StopReason.GOAL_REACHED, "hedef dogrulandi", spent, best)
    if len(outcomes) >= budget.max_iterations:
        return LoopDecision(
            False, StopReason.ITERATION_BUDGET, "iterasyon butcesi doldu", spent, best
        )
    if spent >= budget.max_cost_units:
        return LoopDecision(False, StopReason.COST_BUDGET, "maliyet butcesi doldu", spent, best)
    if _stalled(outcomes, budget.stall_limit):
        return LoopDecision(
            False, StopReason.NO_PROGRESS, "ardisik iterasyonlarda ilerleme yok", spent, best
        )
    return LoopDecision(True, None, "olculebilir ilerleme var", spent, best)


def _stalled(outcomes: tuple[IterationOutcome, ...], limit: int) -> bool:
    """Son turlari shared directional progress semantigiyle degerlendirir."""

    if len(outcomes) <= limit:
        return False
    historical = outcomes[: len(outcomes) - limit]
    baseline_outcome = max(historical, key=lambda item: item.score)
    recent = outcomes[len(outcomes) - limit :]
    return all(
        _scalar_progress(baseline_outcome, item).progress_state
        in {ProgressState.PLATEAU, ProgressState.REGRESSED, ProgressState.INVALID}
        for item in recent
    )


def _scalar_progress(previous: IterationOutcome, current: IterationOutcome):  # type: ignore[no-untyped-def]
    """Legacy score'u tek primary maximize metric olarak shared evaluator'a baglar."""

    spec = MetricSpec(
        metric_id="legacy-score",
        name="legacy score",
        unit="point",
        direction=MetricDirection.MAXIMIZE,
        role=MetricRole.PRIMARY,
        source_kind="legacy-independent-verifier",
        target_value=max(previous.score, current.score) + 1.0,
        minimum_meaningful_delta=1e-9,
        regression_tolerance=1e-9,
    )

    def evidence(item: IterationOutcome, label: str) -> MeasurementEvidence:
        return MeasurementEvidence(
            metric_id="legacy-score",
            value=item.score,
            evidence_ref=f"learning:iteration:{item.iteration}:{label}",
            evidence_digest=digest(
                {
                    "iteration": item.iteration,
                    "score": item.score,
                    "verified": item.verified,
                    "label": label,
                }
            ),
            source_revision="learning-evaluate-loop/v2-adapter",
            measured_at=dt.datetime(1970, 1, 1, tzinfo=dt.UTC),
            measurement_identity=(
                "legacy-external-measurer" if item.verified else "legacy-producer"
            ),
            verifier_identity=(
                "legacy-independent-verifier" if item.verified else "legacy-producer"
            ),
            producer_self_report=not item.verified,
        )

    baseline = (evidence(previous, "baseline"),)
    current_evidence = (evidence(current, "current"),)
    return evaluate_progress((spec,), baseline, baseline, current_evidence)


@dataclass(frozen=True, slots=True)
class ContextEffectiveness:
    """Baglam etkinligi. Route kararina girer ama authority uretmez."""

    manifest_digest: str
    token_cost: int
    evidence_used: int
    verified_success: bool
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("etkinlik olcumu authority veremez")
        parse_digest(self.manifest_digest)
        if self.token_cost <= 0:
            raise ValidationFailed("token maliyeti pozitif olmali")
        if self.evidence_used < 0:
            raise ValidationFailed("kanit sayisi negatif olamaz")

    @property
    def evidence_per_kilotoken(self) -> float:
        return self.evidence_used / (self.token_cost / 1000)

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_digest": self.manifest_digest,
            "token_cost": self.token_cost,
            "evidence_used": self.evidence_used,
            "verified_success": self.verified_success,
            "evidence_per_kilotoken": round(self.evidence_per_kilotoken, 6),
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class RouteFeedback:
    """Olculen etkinligin route girdisi haline gelmis ozeti."""

    samples: tuple[ContextEffectiveness, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.samples:
            raise ValidationFailed("geri bildirim en az bir olcum ister")

    @property
    def verified_success_rate(self) -> float:
        return sum(1 for item in self.samples if item.verified_success) / len(self.samples)

    @property
    def mean_token_cost(self) -> float:
        return sum(item.token_cost for item in self.samples) / len(self.samples)

    @property
    def mean_evidence_density(self) -> float:
        return sum(item.evidence_per_kilotoken for item in self.samples) / len(self.samples)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_count": len(self.samples),
            "verified_success_rate": round(self.verified_success_rate, 6),
            "mean_token_cost": round(self.mean_token_cost, 6),
            "mean_evidence_density": round(self.mean_evidence_density, 6),
            "grants_authority": False,
        }
