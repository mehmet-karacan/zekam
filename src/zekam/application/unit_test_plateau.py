"""Olculmus ilerleme, plateau tespiti, butce ve onay zarfi (W05, D11, D18, D19).

Plateau yuvarlanmis yuzdeye bakmaz: AYNI gap/risk kumesi + TEKRARLANAN basarisiz yaklasim
+ dogrulanmis kazanimin durmasi BIRLIKTE gerekir. Tespit sonrasi sirayla:

1. stale rapor / yanlis scope / eksik context ihtimali (her biri gap basina bir kez),
2. kalan butcede gercekten farkli bir strateji (denenmeyen),
3. aksi halde ``STAGNATION_REVIEW``: "bu scope, yontem ve butceyle ek ilerleme bulunamadi".

Plateau "production'a dokunmadan imkansiz" kaniti degildir; butce bitisi
``BUDGET_EXHAUSTED``'dir ve refactor zorunlulugu diye adlandirilmaz. Somut testability
bulgusu yalniz kanit referanslariyla ayri raporlanir.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from zekam.application.unit_test_plan import BehaviorPlan, RiskStatus, ScenarioBoard, risk_status
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.unit_test_engineering import ScopeEvaluation, UnitTestBudget

STAGNATION_MESSAGE: Final = "bu scope, yontem ve butceyle ek ilerleme bulunamadi"


class ProgressKind(StrEnum):
    NEW_COVERED_COUNT = "new-covered-count"
    VERIFIED_SCENARIO = "verified-scenario"
    ORACLE_RESOLVED = "oracle-resolved"
    VERIFIED_FIXTURE_READINESS = "verified-fixture-readiness"
    VERIFIED_MUTATION = "verified-mutation"


@dataclass(frozen=True, slots=True)
class ProgressGain:
    """Her kazanimin harness/verifier kaniti vardir; modelin yeni cumlesi novelty degildir."""

    kind: ProgressKind
    evidence_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.evidence_digest)


class StrategyKind(StrEnum):
    ALTERNATE_FIXTURE = "alternate-fixture"
    INPUT_PARTITION = "input-partition"
    OBSERVABLE_BOUNDARY = "observable-boundary"
    MOCK_BOUNDARY = "mock-boundary"
    SETUP_DISCOVERY = "setup-discovery"


STRATEGY_ORDER: Final = tuple(StrategyKind)


class AttemptOutcomeKind(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"
    DEFECT = "defect"
    INTERRUPTED = "interrupted"


def approach_digest(
    strategy: StrategyKind, scenario_ids: Iterable[str], test_paths: Iterable[str]
) -> str:
    """Yaklasim kimligi: strateji + senaryo kumesi + hedef test yollari (icerik degil)."""

    return digest(
        {
            "strategy": strategy.value,
            "scenarios": sorted(set(scenario_ids)),
            "paths": sorted(set(test_paths)),
        }
    )


def gap_digest(
    evaluation: ScopeEvaluation | None, plan: BehaviorPlan | None, board: ScenarioBoard | None
) -> str:
    """Kalan gap/risk kumesi: dosya basina missed sayaci + cozulmemis senaryo kimlikleri."""

    files: list[list[Any]] = []
    if evaluation is not None:
        for item in evaluation.files:
            files.append([item.source_file, item.state.value, item.covered, item.total])
    unresolved: list[str] = []
    if plan is not None and board is not None:
        unresolved = sorted(
            s.scenario_id
            for s in plan.scenarios
            if risk_status(board.state_of(s.scenario_id)) is RiskStatus.UNRESOLVED
        )
    return digest({"files": sorted(files), "unresolved": unresolved})


def state_gap_digest(
    observations: Mapping[str, tuple[int, int]],
    plan: BehaviorPlan | None,
    board: ScenarioBoard | None,
) -> str:
    """Dongu durumundan ayni gap kimligi: kabul edilmis sayaclar + cozulmemis senaryolar."""

    unresolved: list[str] = []
    if plan is not None and board is not None:
        unresolved = sorted(
            s.scenario_id
            for s in plan.scenarios
            if risk_status(board.state_of(s.scenario_id)) is RiskStatus.UNRESOLVED
        )
    return digest(
        {
            "counters": sorted([f, c, t] for f, (c, t) in observations.items()),
            "unresolved": unresolved,
        }
    )


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    ordinal: int
    outcome: AttemptOutcomeKind
    approach_digest: str
    gap_digest: str
    gains: tuple[ProgressGain, ...] = ()
    stale_measurement: bool = False
    scope_suspect: bool = False
    context_incomplete: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "outcome": self.outcome.value,
            "approach_digest": self.approach_digest,
            "gap_digest": self.gap_digest,
            "gains": [[g.kind.value, g.evidence_digest] for g in self.gains],
            "stale_measurement": self.stale_measurement,
            "scope_suspect": self.scope_suspect,
            "context_incomplete": self.context_incomplete,
        }

    @classmethod
    def from_payload(cls, p: Mapping[str, Any]) -> AttemptRecord:
        return cls(
            ordinal=int(p["ordinal"]),
            outcome=AttemptOutcomeKind(p["outcome"]),
            approach_digest=str(p["approach_digest"]),
            gap_digest=str(p["gap_digest"]),
            gains=tuple(ProgressGain(ProgressKind(k), str(d)) for k, d in p["gains"]),
            stale_measurement=bool(p["stale_measurement"]),
            scope_suspect=bool(p["scope_suspect"]),
            context_incomplete=bool(p["context_incomplete"]),
        )


@dataclass(frozen=True, slots=True)
class PlateauPolicy:
    window: int = 3
    min_repeated_failed_approaches: int = 1
    #: setup-discovery girisimi icin sinirli kesif butcesi (anlik coverage getirmeyebilir).
    discovery_attempts: int = 1

    def __post_init__(self) -> None:
        if (
            self.window < 2
            or self.min_repeated_failed_approaches < 1
            or self.discovery_attempts < 0
        ):
            raise ValidationFailed("Plateau politikasi gecersiz")


_FAILED: Final = frozenset(
    {AttemptOutcomeKind.REJECTED, AttemptOutcomeKind.FAILED, AttemptOutcomeKind.DEFECT}
)


@dataclass(frozen=True, slots=True)
class PlateauSignal:
    gap_digest: str
    window: int
    repeated_failed_approaches: int


def detect_plateau(
    history: tuple[AttemptRecord, ...], policy: PlateauPolicy
) -> PlateauSignal | None:
    """Uc kosul birlikte: gap degismedi + dogrulanmis kazanim yok + basarisiz yaklasim tekrari."""

    if len(history) < policy.window:
        return None
    recent = history[-policy.window :]
    earlier = history[: -policy.window]
    current_gap = recent[-1].gap_digest
    if any(r.gap_digest != current_gap for r in recent):
        return None
    if any(r.gains for r in recent):
        return None
    failed_before = {r.approach_digest for r in earlier if r.outcome in _FAILED}
    seen: set[str] = set()
    repeated = 0
    for record in recent:
        if record.outcome not in _FAILED:
            continue
        if record.approach_digest in seen or record.approach_digest in failed_before:
            repeated += 1
        seen.add(record.approach_digest)
    if repeated < policy.min_repeated_failed_approaches:
        return None
    return PlateauSignal(current_gap, policy.window, repeated)


class PlateauActionKind(StrEnum):
    REMEASURE_FRESH = "remeasure-fresh"
    RECHECK_SCOPE = "recheck-scope"
    REFRESH_CONTEXT = "refresh-context"
    CHANGE_STRATEGY = "change-strategy"
    STOP_STAGNATION = "stop-stagnation"


@dataclass(frozen=True, slots=True)
class PlateauAction:
    kind: PlateauActionKind
    strategy: StrategyKind | None = None
    message: str = ""
    #: Plateau hicbir zaman "production'a dokunmadan imkansiz" kaniti degildir.
    production_impossibility_claim: bool = False
    remedy_key: str = ""


def plan_plateau_response(
    signal: PlateauSignal,
    history: tuple[AttemptRecord, ...],
    *,
    applied_remedies: frozenset[str],
    tried_strategies: frozenset[StrategyKind],
    attempts_remaining: int,
    discovery_used: int,
    policy: PlateauPolicy,
) -> PlateauAction:
    recent = history[-signal.window :]

    def remedy(kind: PlateauActionKind) -> str:
        return f"{kind.value}:{signal.gap_digest}"

    for kind, flagged in (
        (PlateauActionKind.REMEASURE_FRESH, any(r.stale_measurement for r in recent)),
        (PlateauActionKind.RECHECK_SCOPE, any(r.scope_suspect for r in recent)),
        (PlateauActionKind.REFRESH_CONTEXT, any(r.context_incomplete for r in recent)),
    ):
        if flagged and remedy(kind) not in applied_remedies:
            return PlateauAction(
                kind, message=f"{kind.value} once giderilir", remedy_key=remedy(kind)
            )
    if attempts_remaining >= 1:
        for strategy in STRATEGY_ORDER:
            if strategy in tried_strategies:
                continue
            if (
                strategy is StrategyKind.SETUP_DISCOVERY
                and discovery_used >= policy.discovery_attempts
            ):
                continue
            return PlateauAction(
                PlateauActionKind.CHANGE_STRATEGY,
                strategy=strategy,
                message=f"farkli strateji: {strategy.value}",
            )
    return PlateauAction(PlateauActionKind.STOP_STAGNATION, message=STAGNATION_MESSAGE)


# -- butce ve onay zarfi ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoopLimits:
    """Ilk kalibrasyon degerleri; bilimsel optimum veya ust sinir degildir (konfigure edilir)."""

    max_output_bytes: int = 2_000_000
    max_context_bytes: int = 200_000
    max_remote_calls: int = 0
    max_diagnostic_runs: int = 4
    flaky_repeats: int = 2
    batch_size: int = 3
    max_repair_per_scenario: int = 3
    max_proposal_rejections: int = 3
    max_dispatch_failures: int = 2
    max_replans: int = 3

    def __post_init__(self) -> None:
        for name in self.__slots__:
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValidationFailed(f"Limit {name} negatif olmayan tamsayi olmali")
        if self.batch_size < 1 or self.flaky_repeats < 1:
            raise ValidationFailed("batch_size ve flaky_repeats pozitif olmali")

    def to_payload(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}

    def within(self, approved: LoopLimits) -> bool:
        return all(getattr(self, n) <= getattr(approved, n) for n in self.__slots__)


@dataclass(frozen=True, slots=True)
class UnitTestApprovals:
    """D18: test yazma, build, bagimlilik, ag/model ve production ayri izinlerdir."""

    write_tests: bool = False
    build: bool = False
    dependency_change: bool = False
    network: bool = False
    remote_model: bool = False
    production_change: bool = False
    approved_budget: UnitTestBudget | None = None
    approved_limits: LoopLimits | None = None

    def missing_for_run(self, *, remote_agents: bool) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.write_tests:
            missing.append("write_tests")
        if not self.build:
            missing.append("build")
        if remote_agents and not self.remote_model:
            missing.append("remote_model")
        return tuple(missing)

    def budget_increase_needs_approval(self, budget: UnitTestBudget, limits: LoopLimits) -> bool:
        """Onayli aralik icindeyse False: aralik icinde onay tekrari istenmez."""

        approved_budget = self.approved_budget
        if approved_budget is not None and (
            budget.max_attempts > approved_budget.max_attempts
            or budget.process_timeout_seconds > approved_budget.process_timeout_seconds
            or budget.total_elapsed_seconds > approved_budget.total_elapsed_seconds
        ):
            return True
        if approved_budget is None:
            return True
        return self.approved_limits is not None and not limits.within(self.approved_limits)


@dataclass(slots=True)
class BudgetMeter:
    """Kalici sayaclar (snapshot'ta tasinir); attempt sayisi ledger'dan gelir."""

    elapsed_seconds: float = 0.0
    output_bytes: int = 0
    context_bytes: int = 0
    remote_calls: int = 0
    process_runs: int = 0
    diagnostic_runs: int = 0
    dispatch_failures: int = 0
    proposal_rejections: int = 0
    replans: int = 0
    discovery_attempts: int = 0
    extra: dict[str, int] = field(default_factory=dict)

    def exceeded(
        self, budget: UnitTestBudget, limits: LoopLimits, *, attempts_used: int
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if attempts_used >= budget.max_attempts:
            reasons.append("attempts")
        if self.elapsed_seconds >= budget.total_elapsed_seconds:
            reasons.append("elapsed")
        if self.output_bytes >= limits.max_output_bytes:
            reasons.append("output")
        if self.context_bytes >= limits.max_context_bytes * max(1, budget.max_attempts):
            reasons.append("context")
        if limits.max_remote_calls and self.remote_calls >= limits.max_remote_calls:
            reasons.append("remote-calls")
        return tuple(reasons)

    def to_payload(self) -> dict[str, Any]:
        return {
            "elapsed_seconds": int(self.elapsed_seconds),
            "output_bytes": self.output_bytes,
            "context_bytes": self.context_bytes,
            "remote_calls": self.remote_calls,
            "process_runs": self.process_runs,
            "diagnostic_runs": self.diagnostic_runs,
            "dispatch_failures": self.dispatch_failures,
            "proposal_rejections": self.proposal_rejections,
            "replans": self.replans,
            "discovery_attempts": self.discovery_attempts,
        }

    @classmethod
    def from_payload(cls, p: Mapping[str, Any]) -> BudgetMeter:
        return cls(
            elapsed_seconds=float(p["elapsed_seconds"]),
            output_bytes=int(p["output_bytes"]),
            context_bytes=int(p["context_bytes"]),
            remote_calls=int(p["remote_calls"]),
            process_runs=int(p["process_runs"]),
            diagnostic_runs=int(p["diagnostic_runs"]),
            dispatch_failures=int(p["dispatch_failures"]),
            proposal_rejections=int(p["proposal_rejections"]),
            replans=int(p["replans"]),
            discovery_attempts=int(p["discovery_attempts"]),
        )
