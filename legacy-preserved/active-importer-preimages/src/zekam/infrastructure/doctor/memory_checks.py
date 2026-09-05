"""Single structured, read-only Memory Continuity doctor check."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zekam.application.config import DatabaseSettings
from zekam.application.diagnostics import CheckResult, CheckStatus, Finding, Severity
from zekam.application.memory_observability import MemoryDimensionStatus
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.postgres.connection import PSYCOPG_AVAILABLE, connect
from zekam.infrastructure.postgres.core_repository import RealmRepository
from zekam.infrastructure.postgres.memory_observability_repository import (
    PostgresMemoryHealthReader,
)


@dataclass(frozen=True, slots=True)
class MemoryContinuityCheck:
    settings: DatabaseSettings
    core_path: Path
    private_store_path: Path
    check_id: str = "memory.continuity"
    category: str = "memory"

    def run(self) -> CheckResult:
        if not PSYCOPG_AVAILABLE:
            return CheckResult(
                self.check_id,
                self.category,
                CheckStatus.SKIPPED,
                "PostgreSQL driver yok; memory check atlandi",
                evidence={"dimensions_expected": 15, "reason": "driver-unavailable"},
            )
        with connect(self.settings) as connection:
            realm = RealmRepository(connection).find_by_slug(DEFAULT_REALM_SLUG)
            if realm is None:
                return CheckResult(
                    self.check_id,
                    self.category,
                    CheckStatus.FAILED,
                    "Default realm bulunamadi; memory continuity scope dogrulanamadi",
                    findings=(
                        Finding(
                            code="memory.default-realm-missing",
                            severity=Severity.ERROR,
                            title="Default realm bulunamadi",
                            detail="db:core.realm/default",
                            next_action="Default realm bootstrap kaydini salt okunur dogrulayin",
                            authority_required=False,
                        ),
                    ),
                    evidence={
                        "dimensions_expected": 15,
                        "realm_slug": DEFAULT_REALM_SLUG,
                        "grants_authority": False,
                    },
                )
            report = PostgresMemoryHealthReader(
                connection=connection,
                core_path=self.core_path,
                private_store_path=self.private_store_path,
                realm_id=realm.id,
            ).collect()
        findings = tuple(
            Finding(
                code=item.failure_code or "memory.unavailable",
                severity=(
                    Severity.ERROR
                    if item.status is MemoryDimensionStatus.FAILED
                    else Severity.WARNING
                ),
                title=item.summary,
                detail=item.evidence_ref,
                next_action=item.next_safe_action or "Kanonik component durumunu tekrar okuyun",
                authority_required=False,
                evidence={
                    "dimension_id": item.dimension_id,
                    "evidence_ref": item.evidence_ref,
                    "observed_count": item.observed_count,
                },
            )
            for item in report.dimensions
            if item.status in {MemoryDimensionStatus.DEGRADED, MemoryDimensionStatus.FAILED}
        )
        if report.status is MemoryDimensionStatus.FAILED:
            status = CheckStatus.FAILED
        elif report.status in {
            MemoryDimensionStatus.DEGRADED,
            MemoryDimensionStatus.UNAVAILABLE,
        }:
            status = CheckStatus.DEGRADED
        else:
            status = CheckStatus.PASSED
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=status,
            summary=f"Memory Continuity: {report.status.value}; 15/15 dimension raporlandi",
            findings=findings,
            evidence={
                "schema": "zekam-memory-continuity-health/v1",
                "dimension_count": len(report.dimensions),
                "dimensions": [item.as_dict() for item in report.dimensions],
                "report_digest": report.report_digest,
                "grants_authority": False,
            },
        )
