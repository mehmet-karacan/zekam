"""Opsiyonel PIT mutation kanitinin dogru raporlanmasi ve kabul karari (W06 / D10).

Saf ve I/O'suz. Mutation her gorevde zorunlu degildir; PIT kullanilmadiginda temel akis
calisir ve rapor ``mutation_not_run`` der. Kullanici mutation'i kabul kosulu secerse
calistirilmamis, basarisiz, gecersiz ya da eksik PIT bu kosulu GECIRMIS SAYILMAZ.

Sonuc siniflari ayri ham sayaclardir: KILLED / SURVIVED / NO_COVERAGE / NON_VIABLE /
TIMED_OUT / MEMORY_ERROR / RUN_ERROR. ``STARTED``/``NOT_STARTED`` (bitmemis) ve taninmayan
durumlar ayri sayilir ve raporu ``complete=False`` yapar.

Skor tanimlari (tam sayi orani; yuvarlama yok, esik capraz carpimla karsilastirilir):

- ``detected`` = KILLED + TIMED_OUT + MEMORY_ERROR (testin yakaladigi; timeout/memory
  sonsuz dongu/kaynak patlamasi mutantini yakalanmis sayma PIT'in yaygin kullanimidir ve
  burada acik bir tanim olarak sabitlenir).
- ``mutation-score`` = detected / (detected + SURVIVED + NO_COVERAGE). NO_COVERAGE payda
  icindedir: hic test tarafindan calistirilmayan mutant skoru dusurur.
- ``test-strength`` = detected / (detected + SURVIVED). NO_COVERAGE payda DISINDADIR:
  yalniz test tarafindan calistirilan mutantlar uzerinde assertion gucunu olcer.
- NON_VIABLE (gecersiz bytecode) ve RUN_ERROR (sonucu bilinmeyen) iki paydadan da
  disarida tutulur ve ham sayac olarak gorunur. RUN_ERROR > 0 raporu eksik sayar.

PIT'in kendi hesapladigi skor ``mutations.xml`` icinde yoktur ve payda tanimi arac
surumune gore farkli olabilir. Verilirse ``tool_reported_percent`` ayri bir bilgi alanidir;
bizim skorumuzla birlestirilmez ve kabul karari YALNIZ ham sayaclardan ve yukaridaki acik
tanimlardan cikar.

SURVIVED/NO_COVERAGE otomatik test acigi DEGILDIR: equivalent mutant veya scope disi etki
olasiligi vardir; her biri ``review_required`` isaretlenir. Mutation expected oracle'i
icat etmez: hicbir beklenen deger/test govdesi bu rapordan uretilmez.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed

MUTATION_CONTRACT: Final = "zekam-unit-test-mutation/v1"
MAX_REVIEW_ITEMS: Final = 500
REVIEW_REASON: Final = "equivalent-or-out-of-scope-possible"


class PitStatus(StrEnum):
    KILLED = "KILLED"
    SURVIVED = "SURVIVED"
    NO_COVERAGE = "NO_COVERAGE"
    NON_VIABLE = "NON_VIABLE"
    TIMED_OUT = "TIMED_OUT"
    MEMORY_ERROR = "MEMORY_ERROR"
    RUN_ERROR = "RUN_ERROR"


#: Bitmemis mutant durumlari: sonuc degil; rapor eksik sayilir.
UNFINISHED_STATUSES: Final = frozenset({"STARTED", "NOT_STARTED"})
REVIEW_STATUSES: Final = frozenset({PitStatus.SURVIVED, PitStatus.NO_COVERAGE})


@dataclass(frozen=True, slots=True)
class MutantKey:
    """Iki kosu arasinda ayni mutanti eslemek icin kimlik (``ordinal`` tekrar edenleri ayirir)."""

    source_file: str
    mutated_class: str
    method: str
    method_description: str
    line: int
    mutator: str
    index: str
    ordinal: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "mutated_class": self.mutated_class,
            "method": self.method,
            "method_description": self.method_description,
            "line": self.line,
            "mutator": self.mutator,
            "index": self.index,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True, slots=True)
class PitCounters:
    """Ayri ham sayaclar; hicbiri digerine katilmaz."""

    killed: int = 0
    survived: int = 0
    no_coverage: int = 0
    non_viable: int = 0
    timed_out: int = 0
    memory_error: int = 0
    run_error: int = 0
    unfinished: int = 0
    unknown: int = 0

    def __post_init__(self) -> None:
        for name in self.__slots__:
            if getattr(self, name) < 0:
                raise ValidationFailed("PIT sayaci negatif olamaz")

    @property
    def total_listed(self) -> int:
        return (
            self.killed + self.survived + self.no_coverage + self.non_viable
            + self.timed_out + self.memory_error + self.run_error
            + self.unfinished + self.unknown
        )  # fmt: skip

    @property
    def detected(self) -> int:
        return self.killed + self.timed_out + self.memory_error

    @property
    def complete(self) -> bool:
        """RUN_ERROR, bitmemis ya da taninmayan durum yoksa True."""

        return self.run_error == 0 and self.unfinished == 0 and self.unknown == 0

    def to_payload(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}


@dataclass(frozen=True, slots=True)
class ScoreRatio:
    name: str
    numerator: int
    denominator: int
    definition: str

    def meets(self, threshold_numerator: int, threshold_denominator: int) -> bool:
        return self.numerator * threshold_denominator >= threshold_numerator * self.denominator

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "definition": self.definition,
        }


class MutationScoreKind(StrEnum):
    MUTATION_SCORE = "mutation-score"
    TEST_STRENGTH = "test-strength"


_DEFINITIONS: Final = {
    MutationScoreKind.MUTATION_SCORE: (
        "(KILLED+TIMED_OUT+MEMORY_ERROR) / (KILLED+TIMED_OUT+MEMORY_ERROR+SURVIVED+NO_COVERAGE); "
        "NON_VIABLE ve RUN_ERROR payda disi"
    ),
    MutationScoreKind.TEST_STRENGTH: (
        "(KILLED+TIMED_OUT+MEMORY_ERROR) / (KILLED+TIMED_OUT+MEMORY_ERROR+SURVIVED); "
        "NO_COVERAGE, NON_VIABLE ve RUN_ERROR payda disi"
    ),
}


def compute_score(counters: PitCounters, kind: MutationScoreKind) -> ScoreRatio | None:
    """Payda 0 ise None doner (sahte 0 ya da 100 yok)."""

    denominator = counters.detected + counters.survived
    if kind is MutationScoreKind.MUTATION_SCORE:
        denominator += counters.no_coverage
    if denominator == 0:
        return None
    return ScoreRatio(kind.value, counters.detected, denominator, _DEFINITIONS[kind])


@dataclass(frozen=True, slots=True)
class ReviewItem:
    key: MutantKey
    status: PitStatus
    review_required: bool = True
    reason: str = REVIEW_REASON

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key.to_payload(),
            "status": self.status.value,
            "review_required": self.review_required,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PitReport:
    counters: PitCounters
    statuses: tuple[tuple[MutantKey, PitStatus], ...]
    review_items: tuple[ReviewItem, ...]
    review_items_truncated: bool = False

    @property
    def review_required_count(self) -> int:
        return self.counters.survived + self.counters.no_coverage

    def status_map(self) -> dict[MutantKey, PitStatus]:
        return dict(self.statuses)


class MutationRunState(StrEnum):
    NOT_RUN = "not-run"
    TOOL_MISSING = "tool-missing"
    SETUP_REQUIRED = "setup-required"
    NOT_SUPPORTED = "not-supported"
    RUN_FAILED = "run-failed"
    REPORT_INVALID = "report-invalid"
    COMPLETED = "completed"


_NOT_RUN_STATES: Final = frozenset(
    {
        MutationRunState.NOT_RUN,
        MutationRunState.TOOL_MISSING,
        MutationRunState.SETUP_REQUIRED,
        MutationRunState.NOT_SUPPORTED,
    }
)


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    state: MutationRunState
    plan_digest: str | None = None
    report: PitReport | None = None
    report_digest: str | None = None
    tool_reported_percent: str | None = None
    reasons: tuple[str, ...] = ()
    #: PIT'in kosuldugu test aday agacinin digest'i; Maven plani degil, test adayina baglar.
    candidate_digest: str | None = None

    def __post_init__(self) -> None:
        if (self.state is MutationRunState.COMPLETED) != (self.report is not None):
            raise ValidationFailed("Rapor yalniz COMPLETED durumunda bulunur")
        if self.state is MutationRunState.COMPLETED and (
            self.plan_digest is None or self.report_digest is None or not self.candidate_digest
        ):
            raise ValidationFailed(
                "COMPLETED mutation plan, rapor ve test aday digest'ine baglanmali"
            )

    @property
    def mutation_status(self) -> str:
        """Kullaniciya gorunen kisa durum."""

        if self.state in _NOT_RUN_STATES:
            return "mutation_not_run"
        if self.state is MutationRunState.COMPLETED:
            return "mutation_completed"
        return "mutation_run_failed"


NOT_RUN_OUTCOME: Final = MutationOutcome(MutationRunState.NOT_RUN)


class MutationVerdict(StrEnum):
    NOT_REQUIRED = "not-required"
    MET = "met"
    NOT_MET = "not-met"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class MutationAcceptancePolicy:
    """Kullanicinin mutation'i kabul kosulu secip secmedigi; esik YALNIZ kullanicidan gelir."""

    required: bool = False
    score_kind: MutationScoreKind = MutationScoreKind.MUTATION_SCORE
    threshold_numerator: int | None = None
    threshold_denominator: int | None = None

    def __post_init__(self) -> None:
        for value in (self.threshold_numerator, self.threshold_denominator):
            if value is not None and type(value) is not int:  # bool/float/str reddedilir
                raise ValidationFailed("Mutation esigi yalniz duz int (bool/float degil) olabilir")
        has_num = self.threshold_numerator is not None
        if has_num != (self.threshold_denominator is not None):
            raise ValidationFailed("Mutation esigi pay ve payda ile birlikte verilmeli")
        if has_num:
            assert self.threshold_numerator is not None
            assert self.threshold_denominator is not None
            if self.threshold_denominator <= 0 or not (
                0 <= self.threshold_numerator <= self.threshold_denominator
            ):
                raise ValidationFailed("Mutation esigi 0 <= pay <= payda ve payda > 0 olmali")
        if self.required and not has_num:
            raise ValidationFailed("Zorunlu mutation kabulu icin kullanici esigi gerekir")
        if not self.required and has_num:
            raise ValidationFailed("Opsiyonel mutation'a esik atanamaz (sessiz zorunluluk yok)")


@dataclass(frozen=True, slots=True)
class MutationDecision:
    verdict: MutationVerdict
    mutation_status: str
    state: MutationRunState
    score: ScoreRatio | None
    review_required_count: int
    reasons: tuple[str, ...] = field(default=())

    @property
    def satisfied(self) -> bool:
        """Mutation kosulu gecti mi. Calistirilmamis/basarisiz/eksik PIT asla True degildir."""

        return self.verdict in {MutationVerdict.NOT_REQUIRED, MutationVerdict.MET}


def decide_mutation_acceptance(
    policy: MutationAcceptancePolicy,
    outcome: MutationOutcome,
    *,
    expected_plan_digest: str | None,
    expected_candidate_digest: str | None,
) -> MutationDecision:
    """Mutation kabul karari. Zorunlu degilse temel akisi engellemez; zorunluysa kanit ister."""

    report = outcome.report
    score = compute_score(report.counters, policy.score_kind) if report is not None else None
    review = report.review_required_count if report is not None else 0

    def decision(verdict: MutationVerdict, *reasons: str) -> MutationDecision:
        return MutationDecision(
            verdict, outcome.mutation_status, outcome.state, score, review, tuple(reasons)
        )

    if not policy.required:
        return decision(MutationVerdict.NOT_REQUIRED, "mutation kabul kosulu degil")
    assert policy.threshold_numerator is not None and policy.threshold_denominator is not None
    if outcome.state is not MutationRunState.COMPLETED or report is None:
        return decision(
            MutationVerdict.NOT_MET, f"{outcome.mutation_status}: {outcome.state.value}"
        )
    if not expected_plan_digest or not expected_candidate_digest:
        return decision(
            MutationVerdict.NOT_MET, "beklenen plan ve test aday digest'i verilmeden kabul yok"
        )
    if outcome.plan_digest != expected_plan_digest:
        return decision(MutationVerdict.NOT_MET, "mutation sonucu beklenen plana bagli degil")
    if outcome.candidate_digest != expected_candidate_digest:
        return decision(
            MutationVerdict.NOT_MET, "mutation sonucu baska bir test adayina ait (bayat aday)"
        )
    if not report.counters.complete:
        return decision(
            MutationVerdict.INCONCLUSIVE, "RUN_ERROR/bitmemis/taninmayan mutant durumu var"
        )
    if score is None:
        return decision(MutationVerdict.INCONCLUSIVE, "skor paydasi 0: kanit uretilmedi")
    if not score.meets(policy.threshold_numerator, policy.threshold_denominator):
        return decision(MutationVerdict.NOT_MET, f"{score.name} esigin altinda")
    return decision(MutationVerdict.MET)


# ------------------------------------------------------------------ iki kosu karsilastirmasi


@dataclass(frozen=True, slots=True)
class MutationComparison:
    """Ayni mutant kimlikleri uzerinde baseline -> candidate durum degisimi."""

    newly_killed: tuple[MutantKey, ...]
    regressed: tuple[MutantKey, ...]
    only_in_baseline: tuple[MutantKey, ...]
    only_in_candidate: tuple[MutantKey, ...]

    @property
    def comparable(self) -> bool:
        """Mutant kumeleri ayniysa dogrudan karsilastirilabilir (denominator degisimi yok)."""

        return not self.only_in_baseline and not self.only_in_candidate


_DETECTED: Final = frozenset({PitStatus.KILLED, PitStatus.TIMED_OUT, PitStatus.MEMORY_ERROR})


def compare_reports(baseline: PitReport, candidate: PitReport) -> MutationComparison:
    before = baseline.status_map()
    after = candidate.status_map()
    killed: list[MutantKey] = []
    regressed: list[MutantKey] = []
    for key in _ordered(before.keys() & after.keys()):
        old, new = before[key], after[key]
        if old in REVIEW_STATUSES and new in _DETECTED:
            killed.append(key)
        elif old in _DETECTED and new in REVIEW_STATUSES:
            regressed.append(key)
    return MutationComparison(
        tuple(killed),
        tuple(regressed),
        tuple(_ordered(before.keys() - after.keys())),
        tuple(_ordered(after.keys() - before.keys())),
    )


def _ordered(keys: Iterable[MutantKey]) -> list[MutantKey]:
    return sorted(keys, key=lambda k: digest(k.to_payload()))


def mutation_payload(outcome: MutationOutcome, decision: MutationDecision) -> dict[str, Any]:
    report = outcome.report
    return {
        "contract": MUTATION_CONTRACT,
        "mutation_status": outcome.mutation_status,
        "state": outcome.state.value,
        "verdict": decision.verdict.value,
        "satisfied": decision.satisfied,
        "plan_digest": outcome.plan_digest,
        "candidate_digest": outcome.candidate_digest,
        "report_digest": outcome.report_digest,
        "counters": report.counters.to_payload() if report else None,
        "complete": report.counters.complete if report else None,
        "score": decision.score.to_payload() if decision.score else None,
        "tool_reported_percent": outcome.tool_reported_percent,
        "review_required_count": decision.review_required_count,
        "review_items": [i.to_payload() for i in report.review_items] if report else [],
        "review_items_truncated": report.review_items_truncated if report else False,
        "reasons": list(decision.reasons) + list(outcome.reasons),
    }
