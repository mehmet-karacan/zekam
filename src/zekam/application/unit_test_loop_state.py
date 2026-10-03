"""Unit-test dongusu portlari, kalici durum ve sonuc tipleri (W05).

Dongu durumu (plan, senaryo tahtasi, kabul edilmis dosyalar, tarihce, butce sayaclari)
her attempt'in kanit nesnesinde (nesne deposu, icerik adresli) tasinir; ledger'daki
receipt'in ``evidence_digest``'i bu nesnenin digest'idir. Resume bu zincirden okur; ayri
bir JSON state dosyasi veya yeni authority yoktur.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from zekam.application.unit_test_ledger import UnitTestLedgerReader
from zekam.application.unit_test_measurement import MeasurementRecord
from zekam.application.unit_test_plan import BehaviorPlan, ScenarioBoard
from zekam.application.unit_test_plateau import (
    AttemptRecord,
    BudgetMeter,
    StrategyKind,
)
from zekam.domain.unit_test_engineering import (
    UnitTestRequest,
    UnitTestStopReason,
    UnitTestTerminal,
)


class ControlSignal(StrEnum):
    RUN = "run"
    PAUSE = "pause"
    CANCEL = "cancel"


class LoopControl(Protocol):
    @property
    def cancel_event(self) -> threading.Event: ...

    def signal(self) -> ControlSignal: ...


class InMemoryLoopControl:
    """Kullanici pause/cancel istegi; cancel olayi calisan olcume iletilir."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._pause = False

    @property
    def cancel_event(self) -> threading.Event:
        return self._event

    def request_cancel(self) -> None:
        self._event.set()

    def request_pause(self) -> None:
        self._pause = True

    def clear_pause(self) -> None:
        self._pause = False

    def signal(self) -> ControlSignal:
        if self._event.is_set():
            return ControlSignal.CANCEL
        return ControlSignal.PAUSE if self._pause else ControlSignal.RUN


@dataclass(slots=True)
class LedgerLoopControl:
    """Operational ledger terminal'lerinden okunan durable pause/cancel kapisi."""

    ledger: UnitTestLedgerReader
    request_digest: str
    allow_resume: bool = False
    _event: threading.Event = field(default_factory=threading.Event)

    @property
    def cancel_event(self) -> threading.Event:
        return self._event

    def signal(self) -> ControlSignal:
        if self._event.is_set():
            return ControlSignal.CANCEL
        terminals = self.ledger.list_terminals(self.request_digest)
        if not terminals:
            return ControlSignal.RUN
        reason = terminals[-1].stop_reason
        if reason is UnitTestStopReason.USER_CANCELLED:
            return ControlSignal.CANCEL
        if reason is UnitTestStopReason.USER_PAUSED and not self.allow_resume:
            return ControlSignal.PAUSE
        return ControlSignal.RUN


@dataclass(frozen=True, slots=True)
class MeasuredRun:
    """Olcum portu sonucu. ``record`` yoksa plan hazir degildi (``blocked_*``)."""

    record: MeasurementRecord | None
    blocked_reason: UnitTestStopReason | None = None
    blocked_details: tuple[str, ...] = ()
    failed_cases: tuple[str, ...] = ()
    output_tail: str = ""
    output_bytes: int = 0


class UnitTestMeasurer(Protocol):
    def measure(
        self,
        request: UnitTestRequest,
        *,
        attempt_id: str,
        run_id: str,
        candidate_digest: str,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> MeasuredRun: ...


@dataclass(frozen=True, slots=True)
class EnvironmentObservation:
    source_revision: str
    production_digests: Mapping[str, str]
    build_config_digest: str
    launcher_digest: str
    protected_dirty_paths: frozenset[str] = frozenset()
    #: Test agacindaki dosya digest'leri; gorevin kendi dosyalari karsilastirmada disarida kalir.
    test_tree_digests: Mapping[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_revision": self.source_revision,
            "production_digests": sorted(self.production_digests.items()),
            "build_config_digest": self.build_config_digest,
            "launcher_digest": self.launcher_digest,
            "test_tree_digests": sorted(self.test_tree_digests.items()),
        }

    def drifted_from(
        self, other: EnvironmentObservation, *, own_test_paths: frozenset[str] = frozenset()
    ) -> tuple[str, ...]:
        """Harici drift (M12). ``own_test_paths`` gorevin KENDI yollaridir (M13): drift degil."""

        reasons: list[str] = []
        if self.source_revision != other.source_revision:
            reasons.append("source-revision")
        if self.launcher_digest != other.launcher_digest:
            reasons.append("toolchain")
        if self.build_config_digest != other.build_config_digest:
            reasons.append("config")
        if dict(self.production_digests) != dict(other.production_digests):
            reasons.append("production")
        mine = {p: d for p, d in self.test_tree_digests.items() if p not in own_test_paths}
        theirs = {p: d for p, d in other.test_tree_digests.items() if p not in own_test_paths}
        if mine != theirs:
            reasons.append("test-tree")
        return tuple(reasons)


class EnvironmentObserver(Protocol):
    def observe(self) -> EnvironmentObservation: ...


@dataclass(slots=True)
class LoopState:
    """Resume icin gereken tum durum; yalniz kanit nesnesinde kalicilasir."""

    plan: BehaviorPlan | None = None
    board: ScenarioBoard = field(default_factory=ScenarioBoard)
    accepted_files: dict[str, str] = field(default_factory=dict)
    frozen_paths: set[str] = field(default_factory=set)
    defects: list[dict[str, Any]] = field(default_factory=list)
    history: list[AttemptRecord] = field(default_factory=list)
    meter: BudgetMeter = field(default_factory=BudgetMeter)
    remedies: set[str] = field(default_factory=set)
    tried_strategies: set[StrategyKind] = field(default_factory=set)
    forced_strategy: StrategyKind | None = None
    baseline_obs: EnvironmentObservation | None = None
    accepted_observations: dict[str, tuple[int, int]] = field(default_factory=dict)
    accepted_measurement_digest: str | None = None
    needs_rebaseline: bool = False
    needs_replan: bool = False
    repair_counts: dict[str, int] = field(default_factory=dict)
    failure_notes: dict[str, list[str]] = field(default_factory=dict)
    testability_findings: list[dict[str, Any]] = field(default_factory=list)
    context_gaps: list[str] = field(default_factory=list)
    flaky_diagnoses: list[dict[str, Any]] = field(default_factory=list)
    last_builder_agent_id: str | None = None
    stale_flag: bool = False
    scope_flag: bool = False
    context_flag: bool = False
    accepted_candidate_digest: str | None = None
    accepted_plan_digest: str | None = None
    char_gate: set[str] = field(default_factory=set)
    owned_scenarios: dict[str, list[str]] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan": None if self.plan is None else self.plan.to_payload(),
            "board": self.board.to_payload(),
            "accepted_files": sorted(self.accepted_files.items()),
            "frozen_paths": sorted(self.frozen_paths),
            "defects": self.defects,
            "history": [r.to_payload() for r in self.history],
            "meter": self.meter.to_payload(),
            "remedies": sorted(self.remedies),
            "tried_strategies": sorted(s.value for s in self.tried_strategies),
            "forced_strategy": None if self.forced_strategy is None else self.forced_strategy.value,
            "baseline_obs": None if self.baseline_obs is None else self.baseline_obs.to_payload(),
            "accepted_observations": sorted(
                [f, c, t] for f, (c, t) in self.accepted_observations.items()
            ),
            "accepted_measurement_digest": self.accepted_measurement_digest,
            "needs_rebaseline": self.needs_rebaseline,
            "needs_replan": self.needs_replan,
            "repair_counts": sorted(self.repair_counts.items()),
            "failure_notes": sorted([k, v] for k, v in self.failure_notes.items()),
            "testability_findings": self.testability_findings,
            "context_gaps": self.context_gaps,
            "flaky_diagnoses": self.flaky_diagnoses,
            "last_builder_agent_id": self.last_builder_agent_id,
            "stale_flag": self.stale_flag,
            "scope_flag": self.scope_flag,
            "context_flag": self.context_flag,
            "accepted_candidate_digest": self.accepted_candidate_digest,
            "accepted_plan_digest": self.accepted_plan_digest,
            "char_gate": sorted(self.char_gate),
            "owned_scenarios": sorted([k, v] for k, v in self.owned_scenarios.items()),
        }

    @classmethod
    def from_payload(cls, p: Mapping[str, Any], request: UnitTestRequest) -> LoopState:
        obs = p["baseline_obs"]
        return cls(
            plan=None if p["plan"] is None else BehaviorPlan.from_payload(p["plan"], request),
            board=ScenarioBoard.from_payload(p["board"]),
            accepted_files={str(a): str(b) for a, b in p["accepted_files"]},
            frozen_paths=set(p["frozen_paths"]),
            defects=list(p["defects"]),
            history=[AttemptRecord.from_payload(r) for r in p["history"]],
            meter=BudgetMeter.from_payload(p["meter"]),
            remedies=set(p["remedies"]),
            tried_strategies={StrategyKind(s) for s in p["tried_strategies"]},
            forced_strategy=(
                None if p["forced_strategy"] is None else StrategyKind(p["forced_strategy"])
            ),
            baseline_obs=(
                None
                if obs is None
                else EnvironmentObservation(
                    str(obs["source_revision"]),
                    {str(a): str(b) for a, b in obs["production_digests"]},
                    str(obs["build_config_digest"]),
                    str(obs["launcher_digest"]),
                    test_tree_digests={str(a): str(b) for a, b in obs.get("test_tree_digests", [])},
                )
            ),
            accepted_observations={
                str(f): (int(c), int(t)) for f, c, t in p["accepted_observations"]
            },
            accepted_measurement_digest=p["accepted_measurement_digest"],
            needs_rebaseline=bool(p["needs_rebaseline"]),
            needs_replan=bool(p["needs_replan"]),
            repair_counts={str(k): int(v) for k, v in p["repair_counts"]},
            failure_notes={str(k): list(v) for k, v in p["failure_notes"]},
            testability_findings=list(p["testability_findings"]),
            context_gaps=list(p["context_gaps"]),
            flaky_diagnoses=list(p["flaky_diagnoses"]),
            last_builder_agent_id=p["last_builder_agent_id"],
            stale_flag=bool(p["stale_flag"]),
            scope_flag=bool(p["scope_flag"]),
            context_flag=bool(p["context_flag"]),
            accepted_candidate_digest=p.get("accepted_candidate_digest"),
            accepted_plan_digest=p.get("accepted_plan_digest"),
            char_gate=set(p.get("char_gate", [])),
            owned_scenarios={str(k): list(v) for k, v in p.get("owned_scenarios", [])},
        )


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """Makine-okunur sonuc. ``success`` boolean'i yoktur: status + reason + final."""

    request_digest: str
    terminal: UnitTestTerminal
    attempts: int
    executed_attempts: int
    detail: tuple[str, ...] = ()
    defect_proposals: tuple[Mapping[str, Any], ...] = ()
    needs_specification: tuple[str, ...] = ()
    testability_findings: tuple[Mapping[str, Any], ...] = ()

    def machine_status(self) -> dict[str, str | bool]:
        return self.terminal.machine_status()
