"""Basarisiz test/olcum hata siniflandirmasi, flaky tanisi ve production defect onerisi (W05).

Siniflandirma kanitla yapilir ve kesin teshis gibi sunulmaz: her sonuc ``confidence``
(proven/probable/unknown) tasir; ``UNKNOWN`` desteklenir ve eskalasyon demektir. Test
SONUCU yalniz Surefire XML'inden gelir; console kuyrugu yalniz NEDEN siniflandirmasi
icin sinirli isaretci (marker) uretir ve sonuc yetkisi degildir.

Gercek bug bulan testin beklentisi bozuk production ciktisina cevrilerek "duzeltilmez":
bagimsiz oracle'li assertion hatasi ``PRODUCTION_DEFECT`` adayidir, reproducer ve oracle
korunur, ``DefectProposal`` uretilir; basari yazilmaz.

Flaky icin tekrar kosu yalniz tanidir: sabit sayida tekrar ve sirasi kaydedilir; yesile
kadar retry yoktur ve ilk basarisiz kosuyu sonradan gecen kosu temize cikarmaz.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from zekam.application.unit_test_measurement import (
    MeasurementRecord,
    MeasurementStatus,
    RunStatus,
    TestRunKind,
)
from zekam.application.unit_test_plan import INDEPENDENT_ORACLES, OracleKind
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed

MAX_TAIL_CHARS: Final = 16_384
MAX_FAILED_CASES: Final = 32


class OutputMarker(StrEnum):
    COMPILATION_ERROR = "compilation-error"
    MISSING_IMPORT = "missing-import"
    SETUP_ERROR = "setup-error"
    ASSERTION_FAILURE = "assertion-failure"
    DEPENDENCY_RESOLUTION = "dependency-resolution"
    TOOLCHAIN = "toolchain"
    NO_TESTS = "no-tests"


_MARKER_PATTERNS: Final[tuple[tuple[OutputMarker, re.Pattern[str]], ...]] = (
    (OutputMarker.COMPILATION_ERROR, re.compile(r"COMPILATION ERROR|cannot find symbol|error: ")),
    (
        OutputMarker.MISSING_IMPORT,
        re.compile(
            r"package [\w.]+ does not exist|NoClassDefFoundError|ClassNotFoundException"
            r"|NoSuchMethodError"
        ),
    ),
    (
        OutputMarker.SETUP_ERROR,
        re.compile(
            r"ExceptionInInitializerError|UnfinishedStubbing|Failed to load ApplicationContext"
            r"|MockitoException|@BeforeEach|@BeforeAll|setUp\(|NullPointerException"
        ),
    ),
    (
        OutputMarker.ASSERTION_FAILURE,
        re.compile(r"AssertionFailedError|ComparisonFailure|AssertionError|expected:\s*<"),
    ),
    (
        OutputMarker.DEPENDENCY_RESOLUTION,
        re.compile(
            r"Could not resolve dependencies|Could not transfer artifact"
            r"|Non-resolvable|Connection refused|UnknownHostException"
        ),
    ),
    (
        OutputMarker.TOOLCHAIN,
        re.compile(
            r"UnsupportedClassVersionError|invalid target release|release version \d+ not supported"
            r"|JAVA_HOME|No compiler is provided"
        ),
    ),
    (OutputMarker.NO_TESTS, re.compile(r"No tests to run|No tests were executed")),
)


def extract_output_markers(tail: str) -> frozenset[OutputMarker]:
    """Sinirli console kuyrugundan neden isaretcileri; sonuc/basari yetkisi degildir."""

    bounded = tail[-MAX_TAIL_CHARS:]
    return frozenset(m for m, pattern in _MARKER_PATTERNS if pattern.search(bounded))


class FailureKind(StrEnum):
    COMPILE = "compile"
    IMPORT = "import"
    FIXTURE = "fixture"
    EXPECTATION = "expectation"
    PRODUCTION_DEFECT = "production-defect"
    ENVIRONMENT = "environment"
    FLAKY = "flaky"
    DISCOVERY = "discovery"
    MEASUREMENT_PARSER = "measurement-parser"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    PROVEN = "proven"
    PROBABLE = "probable"
    UNKNOWN = "unknown"


#: Builder'in yalniz kendi testini onararak cozebilecegi siniflar.
REPAIRABLE_KINDS: Final = frozenset(
    {
        FailureKind.COMPILE,
        FailureKind.IMPORT,
        FailureKind.FIXTURE,
        FailureKind.EXPECTATION,
        FailureKind.DISCOVERY,
    }
)


@dataclass(frozen=True, slots=True)
class FailureSignals:
    """Harness'in topladigi sanitize kanit; ham transcript/secret tasimaz."""

    run_status: RunStatus
    measurement_status: MeasurementStatus | None
    test_kind: TestRunKind | None
    plan_blocked: bool = False
    failed_cases: tuple[str, ...] = ()
    markers: frozenset[OutputMarker] = frozenset()
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.failed_cases) > MAX_FAILED_CASES:
            object.__setattr__(self, "failed_cases", self.failed_cases[:MAX_FAILED_CASES])

    @classmethod
    def from_record(
        cls,
        record: MeasurementRecord,
        *,
        failed_cases: Iterable[str] = (),
        output_tail: str = "",
    ) -> FailureSignals:
        return cls(
            run_status=record.evidence.run_status,
            measurement_status=record.status,
            test_kind=record.evidence.test_kind,
            failed_cases=tuple(failed_cases),
            markers=extract_output_markers(output_tail),
            blockers=record.blockers,
        )

    def signature_digest(self) -> str:
        return digest(
            {
                "run": self.run_status.value,
                "status": None
                if self.measurement_status is None
                else self.measurement_status.value,
                "kind": None if self.test_kind is None else self.test_kind.value,
                "cases": sorted(self.failed_cases),
                "markers": sorted(m.value for m in self.markers),
            }
        )


@dataclass(frozen=True, slots=True)
class RepeatRun:
    index: int
    order_label: str
    test_kind: TestRunKind
    failed_cases: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.test_kind is TestRunKind.PASSED


@dataclass(frozen=True, slots=True)
class FlakyDiagnosis:
    """Tanisal tekrarlar. ``runs`` ilk (basarisiz) kosuyu da icerir ve sirali kaydedilir."""

    runs: tuple[RepeatRun, ...]

    def __post_init__(self) -> None:
        if len(self.runs) < 2:
            raise ValidationFailed("Flaky tanisi en az iki kosu ister")
        if [r.index for r in self.runs] != list(range(1, len(self.runs) + 1)):
            raise ValidationFailed("Tekrar kosu indeksleri 1..n sirali olmali")

    @property
    def mixed(self) -> bool:
        outcomes = {r.passed for r in self.runs}
        return outcomes == {True, False}

    @property
    def deterministic_failure(self) -> bool:
        return all(not r.passed for r in self.runs)

    def to_payload(self) -> dict[str, Any]:
        return {
            "runs": [
                [r.index, r.order_label, r.test_kind.value, list(r.failed_cases)] for r in self.runs
            ],
            "mixed": self.mixed,
        }


@dataclass(frozen=True, slots=True)
class FailureClassification:
    kind: FailureKind
    confidence: Confidence
    evidence: tuple[str, ...]
    scenario_ids: tuple[str, ...] = ()
    needs_defect_triage: bool = False

    @property
    def repairable(self) -> bool:
        return self.kind in REPAIRABLE_KINDS

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "confidence": self.confidence.value,
            "evidence": list(self.evidence),
            "scenario_ids": list(self.scenario_ids),
            "needs_defect_triage": self.needs_defect_triage,
        }


def classify_failure(
    signals: FailureSignals,
    *,
    scenario_oracles: Mapping[str, OracleKind] | None = None,
    repeats: FlakyDiagnosis | None = None,
) -> FailureClassification:
    """Sabit sirali kural zinciri; kanit yetersizse UNKNOWN.

    ``scenario_oracles``: basarisiz test case'lerine bagli senaryo -> oracle turu.
    """

    oracles = dict(scenario_oracles or {})
    ids = tuple(sorted(oracles))
    markers = signals.markers
    if signals.plan_blocked or signals.run_status in {
        RunStatus.TOOL_MISSING,
        RunStatus.LAUNCH_FAILED,
    }:
        return FailureClassification(
            FailureKind.ENVIRONMENT, Confidence.PROVEN, (f"run:{signals.run_status.value}",)
        )
    if signals.run_status is not RunStatus.COMPLETED:
        return FailureClassification(
            FailureKind.UNKNOWN, Confidence.UNKNOWN, (f"run:{signals.run_status.value}",)
        )
    if repeats is not None and repeats.mixed:
        return FailureClassification(
            FailureKind.FLAKY, Confidence.PROVEN, ("repeat-runs-mixed",), ids
        )
    if signals.test_kind is TestRunKind.FLAKY_SUSPECTED:
        return FailureClassification(
            FailureKind.FLAKY, Confidence.PROBABLE, ("parser-flaky-marker",), ids
        )
    if signals.test_kind is TestRunKind.INCONSISTENT_REPORT or signals.measurement_status in {
        MeasurementStatus.UNBOUND_OR_STALE,
        MeasurementStatus.MEASUREMENT_INCOMPLETE,
    }:
        return FailureClassification(
            FailureKind.MEASUREMENT_PARSER,
            Confidence.PROBABLE,
            (
                *signals.blockers[:4],
                f"status:{signals.measurement_status}",
                f"tests:{signals.test_kind}",
            ),
        )
    if markers & {OutputMarker.DEPENDENCY_RESOLUTION, OutputMarker.TOOLCHAIN}:
        return FailureClassification(
            FailureKind.ENVIRONMENT,
            Confidence.PROBABLE,
            tuple(
                sorted(
                    m.value
                    for m in markers & {OutputMarker.DEPENDENCY_RESOLUTION, OutputMarker.TOOLCHAIN}
                )
            ),
        )
    if OutputMarker.MISSING_IMPORT in markers:
        return FailureClassification(FailureKind.IMPORT, Confidence.PROBABLE, ("missing-import",))
    if OutputMarker.COMPILATION_ERROR in markers and not signals.failed_cases:
        return FailureClassification(
            FailureKind.COMPILE, Confidence.PROBABLE, ("compilation-error",)
        )
    if signals.test_kind in {TestRunKind.NO_TESTS, TestRunKind.ALL_SKIPPED} or (
        OutputMarker.NO_TESTS in markers
    ):
        return FailureClassification(
            FailureKind.DISCOVERY, Confidence.PROBABLE, (f"tests:{signals.test_kind}",)
        )
    if signals.failed_cases and OutputMarker.ASSERTION_FAILURE in markers:
        independent = tuple(i for i, k in sorted(oracles.items()) if k in INDEPENDENT_ORACLES)
        if independent:
            return FailureClassification(
                FailureKind.PRODUCTION_DEFECT,
                Confidence.PROBABLE,
                ("assertion-failure", "independent-oracle"),
                independent,
                needs_defect_triage=True,
            )
        return FailureClassification(
            FailureKind.EXPECTATION,
            Confidence.PROBABLE,
            ("assertion-failure", "no-independent-oracle"),
            ids,
        )
    if signals.failed_cases and OutputMarker.SETUP_ERROR in markers:
        return FailureClassification(
            FailureKind.FIXTURE, Confidence.PROBABLE, ("setup-error",), ids
        )
    return FailureClassification(
        FailureKind.UNKNOWN, Confidence.UNKNOWN, ("insufficient-evidence",), ids
    )


@dataclass(frozen=True, slots=True)
class DefectProposal:
    """Production defect onerisi: reproducer + oracle korunur, basari degildir."""

    scenario_ids: tuple[str, ...]
    oracle_refs: tuple[str, ...]
    reproducer_candidate_digest: str
    reproducer_paths: tuple[str, ...]
    signature_digest: str
    failed_cases: tuple[str, ...]
    triage_envelope_digest: str | None
    grants_authority: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "scenario_ids": list(self.scenario_ids),
            "oracle_refs": list(self.oracle_refs),
            "reproducer_candidate_digest": self.reproducer_candidate_digest,
            "reproducer_paths": list(self.reproducer_paths),
            "signature_digest": self.signature_digest,
            "failed_cases": list(self.failed_cases),
            "triage_envelope_digest": self.triage_envelope_digest,
            "grants_authority": False,
        }

    @property
    def proposal_digest(self) -> str:
        return digest(self.to_payload())
