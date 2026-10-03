"""Model- ve dil-bagimsiz unit-test mühendisligi kontratlari.

Bu modul yalniz saf domain verisi ve deterministik karar matematigi tasir:
istek/scope, coverage gozlemi (tamsayi sayaclar), tam hassasiyetli oran
karsilastirmasi, attempt kaydi ve terminal status/reason. Maven/JaCoCo, model veya
dosya sistemi bilgisi icermez; adaptorler bu kontrata normalize edilmis veri verir.

Temel kurallar:

- ``covered``/``missed``/``total`` tamsayidir; oran gosterim yuvarlamasiyla degil
  ``covered * paydasi >= payi * total`` capraz carpimiyla karsilastirilir.
- ``NOT_APPLICABLE``, ``NOT_MEASURED``, ``MISSING_REPORT``, ``MAPPING_ERROR`` ve
  gercek yuzde 0 (``MEASURED`` + ``covered=0``) ayri durumlardir.
- Dosya oranlarinin ortalamasi scope coverage degildir; sayaclar toplanir.
- Terminal sonuc ``success=true`` icine gomulmez: mevcut ``LoopTerminalState`` +
  ayri ``UnitTestStopReason`` makine-okunur sekilde birlikte tasinir.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from fractions import Fraction
from typing import Any, Final

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.loop_policy import LoopTerminalState
from zekam.domain.loop_progress import LoopStopReason

CONTRACT_VERSION: Final = "zekam-unit-test-engineering/v1"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class CoverageMetric(StrEnum):
    LINE = "line"
    BRANCH = "branch"


class CoveragePolicy(StrEnum):
    PER_FILE = "per-file"
    AGGREGATE = "aggregate"


class CoverageState(StrEnum):
    MEASURED = "measured"
    NOT_APPLICABLE = "not-applicable"
    NOT_MEASURED = "not-measured"
    MISSING_REPORT = "missing-report"
    MAPPING_ERROR = "mapping-error"


class EvaluationVerdict(StrEnum):
    MET = "met"
    NOT_MET = "not-met"
    INCONCLUSIVE = "inconclusive"


class AttemptState(StrEnum):
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class UnitTestStopReason(StrEnum):
    TARGET_REACHED = "target-reached"
    ALREADY_AT_TARGET = "already-at-target"
    BUDGET_EXHAUSTED = "budget-exhausted"
    STAGNATION_REVIEW = "stagnation-review"
    ENVIRONMENT_MISSING = "environment-missing"
    TECHNOLOGY_UNSUPPORTED = "technology-unsupported"
    SPEC_AMBIGUOUS = "spec-ambiguous"
    MEASUREMENT_INCOMPLETE = "measurement-incomplete"
    PRODUCTION_DEFECT = "production-defect"
    REFACTOR_APPROVAL_REQUIRED = "refactor-approval-required"
    USER_PAUSED = "user-paused"
    USER_CANCELLED = "user-cancelled"
    RECOVERY_REQUIRED = "recovery-required"


#: Her reason mevcut loop terminal enum'una tek anlamli eslenir; reason ayrica tasinir.
STOP_REASON_TERMINAL_STATE: Final[Mapping[UnitTestStopReason, LoopTerminalState]] = {
    UnitTestStopReason.TARGET_REACHED: LoopTerminalState.PASSED,
    UnitTestStopReason.ALREADY_AT_TARGET: LoopTerminalState.PASSED,
    UnitTestStopReason.BUDGET_EXHAUSTED: LoopTerminalState.BUDGET_EXHAUSTED,
    UnitTestStopReason.STAGNATION_REVIEW: LoopTerminalState.MANUAL_REVIEW,
    UnitTestStopReason.ENVIRONMENT_MISSING: LoopTerminalState.BLOCKED,
    UnitTestStopReason.TECHNOLOGY_UNSUPPORTED: LoopTerminalState.BLOCKED,
    UnitTestStopReason.SPEC_AMBIGUOUS: LoopTerminalState.BLOCKED,
    UnitTestStopReason.MEASUREMENT_INCOMPLETE: LoopTerminalState.BLOCKED,
    UnitTestStopReason.PRODUCTION_DEFECT: LoopTerminalState.MANUAL_REVIEW,
    UnitTestStopReason.REFACTOR_APPROVAL_REQUIRED: LoopTerminalState.MANUAL_REVIEW,
    UnitTestStopReason.USER_PAUSED: LoopTerminalState.BLOCKED,
    UnitTestStopReason.USER_CANCELLED: LoopTerminalState.BLOCKED,
    UnitTestStopReason.RECOVERY_REQUIRED: LoopTerminalState.BLOCKED,
}

#: Varsa mevcut ``LoopStopReason`` karsiligi (okuyucu uyumu icin); yoksa None.
STOP_REASON_LOOP_REASON: Final[Mapping[UnitTestStopReason, LoopStopReason | None]] = {
    UnitTestStopReason.TARGET_REACHED: LoopStopReason.TARGET_REACHED,
    UnitTestStopReason.ALREADY_AT_TARGET: None,
    UnitTestStopReason.BUDGET_EXHAUSTED: None,
    UnitTestStopReason.STAGNATION_REVIEW: LoopStopReason.NO_PROGRESS,
    UnitTestStopReason.ENVIRONMENT_MISSING: None,
    UnitTestStopReason.TECHNOLOGY_UNSUPPORTED: None,
    UnitTestStopReason.SPEC_AMBIGUOUS: None,
    UnitTestStopReason.MEASUREMENT_INCOMPLETE: LoopStopReason.INVALID_MEASUREMENT,
    UnitTestStopReason.PRODUCTION_DEFECT: LoopStopReason.RISK_ESCALATION,
    UnitTestStopReason.REFACTOR_APPROVAL_REQUIRED: LoopStopReason.RISK_ESCALATION,
    UnitTestStopReason.USER_PAUSED: None,
    UnitTestStopReason.USER_CANCELLED: None,
    UnitTestStopReason.RECOVERY_REQUIRED: None,
}

#: Terminal receipt (kanit) gerektiren basari anlamlari.
RECEIPT_REQUIRED_REASONS: Final = frozenset(
    {UnitTestStopReason.TARGET_REACHED, UnitTestStopReason.ALREADY_AT_TARGET}
)
#: Devam edilebilir (final olmayan) duraklar.
RESUMABLE_REASONS: Final = frozenset(
    {UnitTestStopReason.USER_PAUSED, UnitTestStopReason.RECOVERY_REQUIRED}
)


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        raise ValidationFailed(f"{label} gecersiz kimlik")
    return value


def _digest_value(value: object, label: str) -> str:
    if type(value) is not str:
        raise ValidationFailed(f"{label} digest metin olmali")
    parse_digest(value)
    return value


def _non_negative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValidationFailed(f"{label} negatif olmayan tamsayi olmali")
    return value


def normalize_relative_source(path: str) -> str:
    """Tasinabilir relative POSIX yolu dogrular; traversal/absolute/ters egik cizgi reddedilir."""

    if type(path) is not str or not path or path != path.strip():
        raise ValidationFailed("Kaynak yolu bos/bosluklu olamaz")
    if "\\" in path or "\x00" in path or path.startswith("/") or _WINDOWS_DRIVE_RE.match(path):
        raise ValidationFailed("Kaynak yolu portable relative POSIX olmali")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationFailed("Kaynak yolu traversal/bos bilesen tasiyamaz")
    return path


@dataclass(frozen=True, slots=True)
class RatioThreshold:
    """Tam hassasiyetli esik: ``numerator / denominator`` (ornek 90/100)."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        _non_negative_int(self.numerator, "Esik payi")
        if type(self.denominator) is not int or self.denominator <= 0:
            raise ValidationFailed("Esik paydasi pozitif tamsayi olmali")
        if self.numerator > self.denominator:
            raise ValidationFailed("Esik 1'i asamaz")

    @classmethod
    def from_percent(cls, percent: str | int) -> RatioThreshold:
        """``"90"``, ``"89.5"`` veya ``90`` -> tam oran; float kabul edilmez."""

        if type(percent) is bool or type(percent) not in (str, int):
            raise ValidationFailed("Esik yuzdesi metin veya tamsayi olmali (float yok)")
        try:
            value = Decimal(str(percent))
        except InvalidOperation as exc:
            raise ValidationFailed("Esik yuzdesi sayi degil") from exc
        if not value.is_finite() or value < 0 or value > 100:
            raise ValidationFailed("Esik yuzdesi 0..100 araliginda olmali")
        fraction = Fraction(value) / 100
        return cls(fraction.numerator, fraction.denominator)

    def as_fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def met_by(self, covered: int, total: int) -> bool:
        """Tam hassasiyet: ``covered/total >= numerator/denominator`` (yuvarlama yok)."""

        _non_negative_int(covered, "covered")
        if type(total) is not int or total <= 0 or covered > total:
            raise ValidationFailed("Oran icin 0 <= covered <= total ve total > 0 gerekir")
        return covered * self.denominator >= self.numerator * total


@dataclass(frozen=True, slots=True)
class UnitTestBudget:
    max_attempts: int
    process_timeout_seconds: int
    total_elapsed_seconds: int

    def __post_init__(self) -> None:
        for label, value in (
            ("max_attempts", self.max_attempts),
            ("process_timeout_seconds", self.process_timeout_seconds),
            ("total_elapsed_seconds", self.total_elapsed_seconds),
        ):
            if type(value) is not int or value <= 0:
                raise ValidationFailed(f"Butce {label} pozitif tamsayi olmali")
        if self.process_timeout_seconds > self.total_elapsed_seconds:
            raise ValidationFailed("Process timeout toplam sureyi asamaz")


@dataclass(frozen=True, slots=True)
class UnitTestRequest:
    """Cozulmus unit-test istegi; her varsayilan acikca isaretlenir."""

    project_id: str
    source_binding_id: str
    source_revision: str
    source_files: tuple[str, ...]
    metric: CoverageMetric
    threshold: RatioThreshold
    policy: CoveragePolicy
    budget: UnitTestBudget
    allowed_test_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    regression_scope: tuple[str, ...] = ()
    defaults_applied: tuple[str, ...] = field(default_factory=tuple)
    work_item_id: str | None = None
    plan_id: str | None = None
    run_id: str | None = None
    source_snapshot_id: str | None = None
    graph_generation_digest: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project_id")
        _identifier(self.source_binding_id, "source_binding_id")
        for label, value in (
            ("work_item_id", self.work_item_id),
            ("plan_id", self.plan_id),
            ("run_id", self.run_id),
            ("source_snapshot_id", self.source_snapshot_id),
        ):
            if value is not None:
                _identifier(value, label)
        if self.graph_generation_digest is not None:
            _digest_value(self.graph_generation_digest, "graph_generation_digest")
        if type(self.source_revision) is not str or not self.source_revision.strip():
            raise ValidationFailed("source_revision bos olamaz")
        if not isinstance(self.metric, CoverageMetric) or not isinstance(
            self.policy, CoveragePolicy
        ):
            raise ValidationFailed("metric/policy enum olmali")
        if not self.source_files:
            raise ValidationFailed("En az bir hedef kaynak dosyasi gerekir")
        object.__setattr__(
            self, "source_files", tuple(normalize_relative_source(p) for p in self.source_files)
        )
        if len(set(self.source_files)) != len(self.source_files):
            raise ValidationFailed("Hedef kaynak dosyalari tekrar edemez")
        for name in ("allowed_test_paths", "forbidden_paths", "regression_scope"):
            values = tuple(normalize_relative_source(p) for p in getattr(self, name))
            object.__setattr__(self, name, values)
        if set(self.allowed_test_paths) & set(self.forbidden_paths):
            raise ValidationFailed("Izinli ve yasakli yol cakismasi")
        if set(self.source_files) & set(self.allowed_test_paths):
            raise ValidationFailed("Hedef production dosyasi test yazma alani olamaz")

    @classmethod
    def with_defaults(
        cls,
        *,
        project_id: str,
        source_binding_id: str,
        source_revision: str,
        source_files: Iterable[str],
        percent: str | int,
        budget: UnitTestBudget,
        metric: CoverageMetric | None = None,
        policy: CoveragePolicy | None = None,
        allowed_test_paths: Iterable[str] = (),
        forbidden_paths: Iterable[str] = (),
        regression_scope: Iterable[str] = (),
        work_item_id: str | None = None,
        plan_id: str | None = None,
        run_id: str | None = None,
        source_snapshot_id: str | None = None,
        graph_generation_digest: str | None = None,
    ) -> UnitTestRequest:
        """Kullanici yalniz "bu dosyalarda %N" derse: LINE + PER_FILE; planda gorunur."""

        applied: list[str] = []
        if metric is None:
            metric = CoverageMetric.LINE
            applied.append("metric=line")
        if policy is None:
            policy = CoveragePolicy.PER_FILE
            applied.append("policy=per-file")
        return cls(
            project_id=project_id,
            source_binding_id=source_binding_id,
            source_revision=source_revision,
            source_files=tuple(source_files),
            metric=metric,
            threshold=RatioThreshold.from_percent(percent),
            policy=policy,
            budget=budget,
            allowed_test_paths=tuple(allowed_test_paths),
            forbidden_paths=tuple(forbidden_paths),
            regression_scope=tuple(regression_scope),
            defaults_applied=tuple(applied),
            work_item_id=work_item_id,
            plan_id=plan_id,
            run_id=run_id,
            source_snapshot_id=source_snapshot_id,
            graph_generation_digest=graph_generation_digest,
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract": CONTRACT_VERSION,
            "project_id": self.project_id,
            "source_binding_id": self.source_binding_id,
            "source_revision": self.source_revision,
            "source_files": list(self.source_files),
            "metric": self.metric.value,
            "threshold": [self.threshold.numerator, self.threshold.denominator],
            "policy": self.policy.value,
            "budget": [
                self.budget.max_attempts,
                self.budget.process_timeout_seconds,
                self.budget.total_elapsed_seconds,
            ],
            "allowed_test_paths": list(self.allowed_test_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "regression_scope": list(self.regression_scope),
            "defaults_applied": list(self.defaults_applied),
        }
        for name in (
            "work_item_id",
            "plan_id",
            "run_id",
            "source_snapshot_id",
            "graph_generation_digest",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload

    @property
    def request_digest(self) -> str:
        return digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class CoverageObservation:
    """Tek dosya/metrik gozlemi. Sayaclar yalniz ``MEASURED`` iken bulunur."""

    source_file: str
    metric: CoverageMetric
    state: CoverageState
    covered: int | None = None
    missed: int | None = None
    total: int | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        normalize_relative_source(self.source_file)
        if not isinstance(self.metric, CoverageMetric) or not isinstance(self.state, CoverageState):
            raise ValidationFailed("metric/state enum olmali")
        counters = (self.covered, self.missed, self.total)
        if self.state is CoverageState.MEASURED:
            if any(type(c) is not int or c < 0 for c in counters):
                raise ValidationFailed("MEASURED icin tamsayi sayaclar gerekir")
            assert self.covered is not None and self.missed is not None
            assert self.total is not None
            if self.total <= 0:
                raise ValidationFailed("MEASURED total > 0 olmali; total=0 N/A durumudur")
            if self.covered + self.missed != self.total:
                raise ValidationFailed("covered + missed == total olmali")
        elif any(c is not None for c in counters):
            raise ValidationFailed("Olculmemis durum sayac tasiyamaz (sahte 0/100 yok)")
        if self.state is not CoverageState.MEASURED and not self.reason.strip():
            raise ValidationFailed("MEASURED olmayan gozlem gerekce tasimali")

    @classmethod
    def measured(
        cls, source_file: str, metric: CoverageMetric, *, covered: int, missed: int
    ) -> CoverageObservation:
        return cls(
            source_file,
            metric,
            CoverageState.MEASURED,
            covered,
            missed,
            covered + missed if type(covered) is int and type(missed) is int else None,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "metric": self.metric.value,
            "state": self.state.value,
            "covered": self.covered,
            "missed": self.missed,
            "total": self.total,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class FileEvaluation:
    source_file: str
    state: CoverageState
    verdict: EvaluationVerdict | None
    covered: int | None
    total: int | None


@dataclass(frozen=True, slots=True)
class ScopeEvaluation:
    verdict: EvaluationVerdict
    policy: CoveragePolicy
    metric: CoverageMetric
    files: tuple[FileEvaluation, ...]
    aggregate_covered: int
    aggregate_total: int
    aggregate_met: bool | None
    blockers: tuple[str, ...]

    @property
    def aggregate_ratio(self) -> Fraction | None:
        if self.aggregate_total == 0:
            return None
        return Fraction(self.aggregate_covered, self.aggregate_total)


def evaluate_scope(
    request: UnitTestRequest, observations: Iterable[CoverageObservation]
) -> ScopeEvaluation:
    """Hedef metrik icin scope degerlendirmesi.

    - Rapora hic girmeyen hedef dosya ``MISSING_REPORT`` sayilir (N/A degil).
    - Olculememis/eslesmemis dosya varsa verdict ``INCONCLUSIVE`` (hedef gecildi denmez).
    - ``NOT_APPLICABLE`` dosya karsilastirmaya girmez ama gorunur kalir; hepsi N/A ise
      verdict ``INCONCLUSIVE``.
    - Aggregate sayaclari toplar; dosya oranlarinin ortalamasi alinmaz. Aggregate her
      iki politikada da raporlanir, yalniz AGGREGATE politikasinda karar verir.
    """

    wanted = set(request.source_files)
    by_file: dict[str, CoverageObservation] = {}
    for seen_item in observations:
        if seen_item.metric is not request.metric:
            continue
        if seen_item.source_file not in wanted:
            raise ValidationFailed("Gozlem istek scope'u disinda bir dosyaya ait")
        if seen_item.source_file in by_file:
            raise ValidationFailed("Ayni dosya/metrik icin birden fazla gozlem")
        by_file[seen_item.source_file] = seen_item

    files: list[FileEvaluation] = []
    blockers: list[str] = []
    covered_sum = 0
    total_sum = 0
    per_file_ok = True
    applicable = 0
    for source in request.source_files:
        item = by_file.get(source)
        if item is None:
            files.append(FileEvaluation(source, CoverageState.MISSING_REPORT, None, None, None))
            blockers.append(f"{source}: missing-report")
            continue
        if item.state is CoverageState.NOT_APPLICABLE:
            files.append(FileEvaluation(source, item.state, None, None, None))
            continue
        if item.state is not CoverageState.MEASURED:
            files.append(FileEvaluation(source, item.state, None, None, None))
            blockers.append(f"{source}: {item.state.value}")
            continue
        assert item.covered is not None and item.total is not None
        applicable += 1
        covered_sum += item.covered
        total_sum += item.total
        met = request.threshold.met_by(item.covered, item.total)
        per_file_ok = per_file_ok and met
        files.append(
            FileEvaluation(
                source,
                item.state,
                EvaluationVerdict.MET if met else EvaluationVerdict.NOT_MET,
                item.covered,
                item.total,
            )
        )

    aggregate_met = request.threshold.met_by(covered_sum, total_sum) if total_sum > 0 else None
    if blockers:
        verdict = EvaluationVerdict.INCONCLUSIVE
    elif applicable == 0:
        blockers.append("hicbir hedef dosyada uygulanabilir olcu yok")
        verdict = EvaluationVerdict.INCONCLUSIVE
    elif request.policy is CoveragePolicy.PER_FILE:
        verdict = EvaluationVerdict.MET if per_file_ok else EvaluationVerdict.NOT_MET
    else:
        verdict = EvaluationVerdict.MET if aggregate_met else EvaluationVerdict.NOT_MET
    return ScopeEvaluation(
        verdict,
        request.policy,
        request.metric,
        tuple(files),
        covered_sum,
        total_sum,
        aggregate_met,
        tuple(blockers),
    )


@dataclass(frozen=True, slots=True)
class UnitTestAttempt:
    """Kanonik attempt kaydi (claim yuzu). Execution/olcum kanitlari receipt'e baglanir."""

    attempt_id: str
    request_digest: str
    ordinal: int
    parent_attempt_id: str | None
    plan_digest: str
    candidate_digest: str
    idempotency_key: str
    changed_files: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.attempt_id, "attempt_id")
        _digest_value(self.request_digest, "request_digest")
        _digest_value(self.plan_digest, "plan_digest")
        _digest_value(self.candidate_digest, "candidate_digest")
        _identifier(self.idempotency_key, "idempotency_key")
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValidationFailed("ordinal 1 veya buyuk olmali")
        if (self.ordinal == 1) != (self.parent_attempt_id is None):
            raise ValidationFailed("Yalniz ilk attempt parent tasimaz")
        if self.parent_attempt_id is not None:
            _identifier(self.parent_attempt_id, "parent_attempt_id")
        seen: set[str] = set()
        for path, file_digest in self.changed_files:
            normalize_relative_source(path)
            _digest_value(file_digest, "changed file digest")
            if path in seen:
                raise ValidationFailed("changed_files tekrar eden yol")
            seen.add(path)

    def to_payload(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "request_digest": self.request_digest,
            "ordinal": self.ordinal,
            "parent_attempt_id": self.parent_attempt_id,
            "plan_digest": self.plan_digest,
            "candidate_digest": self.candidate_digest,
            "changed_files": [list(item) for item in self.changed_files],
        }

    @property
    def effect_digest(self) -> str:
        return digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class UnitTestTerminal:
    """Terminal status + reason; ``success`` boolean'i yoktur."""

    request_digest: str
    stop_reason: UnitTestStopReason
    receipt_digest: str | None
    evidence_digest: str | None = None

    def __post_init__(self) -> None:
        _digest_value(self.request_digest, "request_digest")
        if not isinstance(self.stop_reason, UnitTestStopReason):
            raise ValidationFailed("stop_reason enum olmali")
        if self.stop_reason in RECEIPT_REQUIRED_REASONS and self.receipt_digest is None:
            raise PolicyViolation("Terminal receipt yoksa basari yoktur")
        if self.receipt_digest is not None:
            _digest_value(self.receipt_digest, "receipt_digest")
        if self.evidence_digest is not None:
            _digest_value(self.evidence_digest, "evidence_digest")

    @property
    def status(self) -> LoopTerminalState:
        return STOP_REASON_TERMINAL_STATE[self.stop_reason]

    @property
    def loop_stop_reason(self) -> LoopStopReason | None:
        return STOP_REASON_LOOP_REASON[self.stop_reason]

    @property
    def final(self) -> bool:
        return self.stop_reason not in RESUMABLE_REASONS

    def machine_status(self) -> dict[str, str | bool]:
        return {
            "status": self.status.value,
            "reason": self.stop_reason.value,
            "final": self.final,
        }
