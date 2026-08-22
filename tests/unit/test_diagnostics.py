"""Doctor cekirdeginin toplama ve hata yalitim davranisi."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from zekam.application.diagnostics import (
    CheckResult,
    CheckStatus,
    DoctorService,
    Finding,
    OverallStatus,
    Severity,
    aggregate_status,
)

pytestmark = pytest.mark.unit


def _result(
    status: CheckStatus,
    findings: tuple[Finding, ...] = (),
    check_id: str = "ornek.kontrol",
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        category="ornek",
        status=status,
        summary="ozet",
        findings=findings,
    )


def _finding(code: str, severity: Severity) -> Finding:
    return Finding(
        code=code,
        severity=severity,
        title="baslik",
        detail="ayrinti",
        next_action="adim",
    )


@dataclass(frozen=True, slots=True)
class _StaticCheck:
    result: CheckResult
    check_id: str = "ornek.kontrol"
    category: str = "ornek"

    def run(self) -> CheckResult:
        return self.result


@dataclass(frozen=True, slots=True)
class _CrashingCheck:
    check_id: str = "ornek.cokme"
    category: str = "ornek"

    def run(self) -> CheckResult:
        raise RuntimeError("beklenmedik")


def test_all_passed_is_healthy() -> None:
    assert aggregate_status([_result(CheckStatus.PASSED)]) is OverallStatus.HEALTHY


def test_warning_finding_degrades() -> None:
    result = _result(CheckStatus.PASSED, (_finding("a.b", Severity.WARNING),))
    assert aggregate_status([result]) is OverallStatus.DEGRADED


def test_failed_check_blocks() -> None:
    assert aggregate_status([_result(CheckStatus.FAILED)]) is OverallStatus.BLOCKED


def test_critical_finding_blocks() -> None:
    result = _result(CheckStatus.PASSED, (_finding("a.b", Severity.CRITICAL),))
    assert aggregate_status([result]) is OverallStatus.BLOCKED


def test_recovery_finding_outranks_blocked() -> None:
    result = _result(
        CheckStatus.FAILED,
        (
            _finding("recovery.claim-without-receipt", Severity.ERROR),
            _finding("a.b", Severity.CRITICAL),
        ),
    )
    assert aggregate_status([result]) is OverallStatus.RECOVERY_REQUIRED


def test_skipped_check_alone_is_healthy() -> None:
    assert aggregate_status([_result(CheckStatus.SKIPPED)]) is OverallStatus.HEALTHY


def test_empty_report_is_healthy() -> None:
    assert aggregate_status([]) is OverallStatus.HEALTHY


def test_duplicate_check_id_is_rejected() -> None:
    check = _StaticCheck(result=_result(CheckStatus.PASSED))
    with pytest.raises(ValueError, match="Yinelenen"):
        DoctorService([check, check])


def test_crashing_check_does_not_break_report() -> None:
    service = DoctorService([_StaticCheck(result=_result(CheckStatus.PASSED)), _CrashingCheck()])
    report = service.run()
    assert len(report.results) == 2
    crashed = next(item for item in report.results if item.check_id == "ornek.cokme")
    assert crashed.status is CheckStatus.FAILED
    assert crashed.findings[0].code == "doctor.check-crashed"
    assert report.overall is OverallStatus.BLOCKED


def test_category_filter_selects_subset() -> None:
    first = _StaticCheck(
        result=_result(CheckStatus.PASSED, check_id="bir.kontrol"),
        check_id="bir.kontrol",
        category="bir",
    )
    second = _StaticCheck(
        result=_result(CheckStatus.PASSED, check_id="iki.kontrol"),
        check_id="iki.kontrol",
        category="iki",
    )
    service = DoctorService([first, second])
    report = service.run(categories=["iki"])
    assert [item.check_id for item in report.results] == ["iki.kontrol"]
    assert service.categories() == ("bir", "iki")


def test_report_serialization_is_json_ready() -> None:
    service = DoctorService(
        [_StaticCheck(result=_result(CheckStatus.PASSED, (_finding("a.b", Severity.INFO),)))]
    )
    document = service.run().as_dict()
    assert document["schema"] == "zekam-doctor-report/v1"
    assert document["overall"] == "healthy"
    assert document["results"][0]["findings"][0]["code"] == "a.b"
