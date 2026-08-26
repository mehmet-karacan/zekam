"""`zekam doctor` cekirdegi.

Doctor salt okunurdur: migration uygulamaz, secret cozmez, mutation yapmaz. Her bulgu
kod, siddet, kanit referansi, guvenli sonraki adim ve yetki gereksinimi tasir.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class Severity(StrEnum):
    """Bulgu siddeti."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class CheckStatus(StrEnum):
    """Tek bir kontrolun sonucu."""

    PASSED = "passed"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


class OverallStatus(StrEnum):
    """Doctor raporunun toplam durumu."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    RECOVERY_REQUIRED = "recovery-required"


@dataclass(frozen=True, slots=True)
class Finding:
    """Tek bir doctor bulgusu."""

    code: str
    severity: Severity
    title: str
    detail: str
    next_action: str
    authority_required: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "title": self.title,
            "detail": self.detail,
            "next_action": self.next_action,
            "authority_required": self.authority_required,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Bir kontrolun sonucu ve bulgulari."""

    check_id: str
    category: str
    status: CheckStatus
    summary: str
    findings: tuple[Finding, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "category": self.category,
            "status": self.status.value,
            "summary": self.summary,
            "findings": [finding.as_dict() for finding in self.findings],
            "evidence": self.evidence,
        }


class DoctorCheck(Protocol):
    """Doctor kontrol sozlesmesi.

    Kimlik alanlari salt okunurdur; kontroller degismez (frozen) nesneler olarak
    tanimlanir.
    """

    @property
    def check_id(self) -> str:
        """Kararli kontrol kimligi."""
        ...

    @property
    def category(self) -> str:
        """Rapor kategorisi."""
        ...

    def run(self) -> CheckResult:
        """Kontrolu salt okunur olarak calistirir."""
        ...


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Toplam doctor raporu."""

    schema: str
    generated_at: dt.datetime
    overall: OverallStatus
    results: tuple[CheckResult, ...]

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(finding for result in self.results for finding in result.findings)

    def as_dict(self) -> dict[str, Any]:
        repair_plan: list[dict[str, Any]] = []
        seen_actions: set[str] = set()
        for finding in self.findings:
            if finding.next_action in seen_actions:
                continue
            seen_actions.add(finding.next_action)
            repair_plan.append(
                {
                    "finding_code": finding.code,
                    "action": finding.next_action,
                    "authority_required": finding.authority_required,
                }
            )
        return {
            "schema": self.schema,
            "generated_at": self.generated_at.isoformat(),
            "overall": self.overall.value,
            "results": [result.as_dict() for result in self.results],
            "repair_plan": repair_plan,
        }


DOCTOR_REPORT_SCHEMA = "zekam-doctor-report/v1"

#: Recovery gerektiren bulgular icin kararli kod oneki.
RECOVERY_CODE_PREFIX = "recovery."


def aggregate_status(results: Sequence[CheckResult]) -> OverallStatus:
    """Kontrol sonuclarindan toplam durumu hesaplar.

    - Herhangi bir `recovery.*` bulgusu varsa `recovery-required`.
    - `critical` bulgu veya `failed` kontrol varsa `blocked`.
    - `error`/`warning` bulgu veya `degraded` kontrol varsa `degraded`.
    - Aksi halde `healthy`.
    """
    has_recovery = any(
        finding.code.startswith(RECOVERY_CODE_PREFIX)
        for result in results
        for finding in result.findings
    )
    if has_recovery:
        return OverallStatus.RECOVERY_REQUIRED

    severities = {finding.severity for result in results for finding in result.findings}
    statuses = {result.status for result in results}

    if Severity.CRITICAL in severities or CheckStatus.FAILED in statuses:
        return OverallStatus.BLOCKED
    if (
        Severity.ERROR in severities
        or Severity.WARNING in severities
        or CheckStatus.DEGRADED in statuses
    ):
        return OverallStatus.DEGRADED
    return OverallStatus.HEALTHY


class DoctorService:
    """Kayitli kontrolleri calistirip rapor uretir."""

    def __init__(self, checks: Iterable[DoctorCheck]) -> None:
        self._checks: tuple[DoctorCheck, ...] = tuple(checks)
        seen: set[str] = set()
        for check in self._checks:
            if check.check_id in seen:
                raise ValueError(f"Yinelenen doctor kontrol kimligi: {check.check_id}")
            seen.add(check.check_id)

    @property
    def checks(self) -> tuple[DoctorCheck, ...]:
        return self._checks

    def categories(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for check in self._checks:
            if check.category not in ordered:
                ordered.append(check.category)
        return tuple(ordered)

    def run(
        self,
        *,
        categories: Sequence[str] | None = None,
        now: dt.datetime | None = None,
    ) -> DoctorReport:
        """Kontrolleri calistirir. Hicbir kontrol istisna ile raporu bozamaz."""
        selected = [
            check
            for check in self._checks
            if categories is None or check.category in set(categories)
        ]
        results: list[CheckResult] = []
        for check in selected:
            results.append(_run_safely(check))
        generated_at = now or dt.datetime.now(dt.UTC)
        return DoctorReport(
            schema=DOCTOR_REPORT_SCHEMA,
            generated_at=generated_at,
            overall=aggregate_status(results),
            results=tuple(results),
        )


def _run_safely(check: DoctorCheck) -> CheckResult:
    try:
        return check.run()
    except Exception as exc:
        return CheckResult(
            check_id=check.check_id,
            category=check.category,
            status=CheckStatus.FAILED,
            summary="Kontrol beklenmedik sekilde basarisiz oldu",
            findings=(
                Finding(
                    code="doctor.check-crashed",
                    severity=Severity.ERROR,
                    title="Kontrol calistirilamadi",
                    detail=f"{type(exc).__name__}",
                    next_action=(
                        "Kontrolu yalitilmis sekilde tekrar calistirin ve loglari inceleyin"
                    ),
                ),
            ),
        )
