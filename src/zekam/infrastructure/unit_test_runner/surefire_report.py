"""Surefire/JUnit XML test raporu okuyucusu (W04, U06).

Sonuc yalniz rapor XML'inden gelir; console metni regex'le sonuca cevrilmez. Kabul sarti:
en az bir test kesfedilmis ve calistirilmis olmali, hicbiri failed/error olmamali, rapor
iceride tutarli olmali ve flaky/rerun izi tasimamalidir. Hic test yok (``NO_TESTS``) ve
yalniz-skip kosusu kabul degildir; ``NO_TESTS`` baseline olarak ayri gorunur.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.unit_test_runner.safe_xml import XmlElement, parse_xml_bounded

MAX_REPORT_BYTES: Final = 16 * 1024 * 1024
_JVM_PROPERTIES: Final = frozenset({"java.version", "java.specification.version", "java.vendor"})
_ABORT_MARKERS: Final = ("TestAbortedException", "AssumptionViolatedException")
_FLAKY_TAGS: Final = frozenset({"flakyFailure", "flakyError", "rerunFailure", "rerunError"})
_INTEGRATION_NAME = re.compile(r"(?:^|\.)(?:IT[A-Z0-9_]\w*|\w*(?:IT|ITCase))$")


class TestOutcome(StrEnum):
    __test__ = False  # pytest toplamasin

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    ABORTED = "aborted"


class TestRunStatus(StrEnum):
    __test__ = False

    PASSED = "passed"
    NO_TESTS = "no-tests"
    ALL_SKIPPED = "all-skipped"
    FAILED = "failed"
    INCONSISTENT_REPORT = "inconsistent-report"
    FLAKY_SUSPECTED = "flaky-suspected"
    INTEGRATION_TESTS_PRESENT = "integration-tests-present"


@dataclass(frozen=True, slots=True)
class TestCaseResult:
    __test__ = False

    classname: str
    name: str
    outcome: TestOutcome
    flaky: bool = False


@dataclass(frozen=True, slots=True)
class SurefireSuite:
    suite_name: str
    report_digest: str
    cases: tuple[TestCaseResult, ...]
    declared_tests: int | None
    declared_failures: int | None
    declared_errors: int | None
    declared_skipped: int | None
    jvm: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class TestRunSummary:
    __test__ = False

    suites: tuple[SurefireSuite, ...]
    discovered: int
    executed: int
    passed: int
    failed: int
    errors: int
    skipped: int
    aborted: int
    flaky: int
    integration_like: int
    inconsistencies: tuple[str, ...]
    report_digests: tuple[str, ...]
    jvm: tuple[tuple[str, str], ...]

    @property
    def status(self) -> TestRunStatus:
        if self.inconsistencies:
            return TestRunStatus.INCONSISTENT_REPORT
        if self.discovered == 0:
            return TestRunStatus.NO_TESTS
        if self.integration_like:
            return TestRunStatus.INTEGRATION_TESTS_PRESENT
        if self.flaky:
            return TestRunStatus.FLAKY_SUSPECTED
        if self.failed or self.errors:
            return TestRunStatus.FAILED
        if self.executed == 0:
            return TestRunStatus.ALL_SKIPPED
        return TestRunStatus.PASSED


def _optional_int(element: XmlElement, key: str) -> int | None:
    raw = element.attrs.get(key)
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdigit():
        raise ValidationFailed("Surefire sayac degeri tamsayi degil")
    return int(raw)


def _case_outcome(case: XmlElement) -> tuple[TestOutcome, bool]:
    tags = {child.tag for child in case.children}
    flaky = bool(tags & _FLAKY_TAGS)
    if "error" in tags:
        return TestOutcome.ERROR, flaky
    if "failure" in tags:
        return TestOutcome.FAILED, flaky
    if "skipped" in tags:
        skipped = case.find_all("skipped")[0]
        marker = skipped.attrs.get("type", "") + skipped.attrs.get("message", "")
        aborted = any(token in marker for token in _ABORT_MARKERS)
        return (TestOutcome.ABORTED if aborted else TestOutcome.SKIPPED), flaky
    return TestOutcome.PASSED, flaky


def _parse_suite(element: XmlElement, report_digest: str) -> SurefireSuite:
    cases = []
    for case in element.find_all("testcase"):
        outcome, flaky = _case_outcome(case)
        cases.append(
            TestCaseResult(
                case.attrs.get("classname", ""), case.attrs.get("name", ""), outcome, flaky
            )
        )
    jvm: list[tuple[str, str]] = []
    for properties in element.find_all("properties"):
        for prop in properties.find_all("property"):
            name = prop.attrs.get("name", "")
            if name in _JVM_PROPERTIES:
                jvm.append((name, prop.attrs.get("value", "")))
    return SurefireSuite(
        element.attrs.get("name", ""),
        report_digest,
        tuple(cases),
        _optional_int(element, "tests"),
        _optional_int(element, "failures"),
        _optional_int(element, "errors"),
        _optional_int(element, "skipped"),
        tuple(sorted(jvm)),
    )


def parse_surefire_report(data: bytes) -> tuple[SurefireSuite, ...]:
    root = parse_xml_bounded(data, max_bytes=MAX_REPORT_BYTES)
    report_digest = digest_of_bytes(data)
    if root.tag == "testsuite":
        return (_parse_suite(root, report_digest),)
    if root.tag == "testsuites":
        return tuple(_parse_suite(item, report_digest) for item in root.find_all("testsuite"))
    raise ValidationFailed("Surefire raporu testsuite/testsuites koku tasimali")


def summarize(reports: Iterable[bytes]) -> TestRunSummary:
    suites: list[SurefireSuite] = []
    for data in reports:
        suites.extend(parse_surefire_report(data))
    counts: dict[TestOutcome, int] = dict.fromkeys(TestOutcome, 0)
    inconsistencies: list[str] = []
    flaky = 0
    integration_like = 0
    jvm: dict[str, str] = {}
    for suite in suites:
        local = dict.fromkeys(TestOutcome, 0)
        for case in suite.cases:
            local[case.outcome] += 1
            flaky += int(case.flaky)
            integration_like += int(_INTEGRATION_NAME.search(case.classname) is not None)
        for outcome, value in local.items():
            counts[outcome] += value
        declared = (
            (suite.declared_tests, len(suite.cases), "tests"),
            (suite.declared_failures, local[TestOutcome.FAILED], "failures"),
            (suite.declared_errors, local[TestOutcome.ERROR], "errors"),
            (
                suite.declared_skipped,
                local[TestOutcome.SKIPPED] + local[TestOutcome.ABORTED],
                "skipped",
            ),
        )
        for claimed, actual, label in declared:
            if claimed is not None and claimed != actual:
                inconsistencies.append(f"{suite.suite_name or '?'}: {label} {claimed}!={actual}")
        jvm.update(dict(suite.jvm))
    discovered = sum(counts.values())
    skipped_like = counts[TestOutcome.SKIPPED] + counts[TestOutcome.ABORTED]
    return TestRunSummary(
        suites=tuple(suites),
        discovered=discovered,
        executed=discovered - skipped_like,
        passed=counts[TestOutcome.PASSED],
        failed=counts[TestOutcome.FAILED],
        errors=counts[TestOutcome.ERROR],
        skipped=counts[TestOutcome.SKIPPED],
        aborted=counts[TestOutcome.ABORTED],
        flaky=flaky,
        integration_like=integration_like,
        inconsistencies=tuple(inconsistencies),
        report_digests=tuple(sorted({suite.report_digest for suite in suites})),
        jvm=tuple(sorted(jvm.items())),
    )


def evaluate_modules(
    summaries: Mapping[str, TestRunSummary], target_modules: Iterable[str]
) -> dict[str, TestRunStatus]:
    """Hedef modul icin NO_TESTS fail-closed; hedef olmayan modulde zararsiz."""

    result: dict[str, TestRunStatus] = {}
    targets = set(target_modules)
    for module in targets:
        summary = summaries.get(module)
        result[module] = summary.status if summary else TestRunStatus.NO_TESTS
    return result
