"""PostgreSQL doctor kontrolleri.

Kontroller salt okunurdur: migration uygulamaz, sema olusturmaz, satir yazmaz.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zekam.application.config import DatabaseSettings
from zekam.application.diagnostics import CheckResult, CheckStatus, Finding, Severity
from zekam.domain.identity import PRODUCT
from zekam.infrastructure.postgres import migrations
from zekam.infrastructure.postgres.connection import (
    PSYCOPG_AVAILABLE,
    ServerInfo,
    connect,
    read_server_info,
)

CATEGORY = "postgres"


@dataclass(frozen=True, slots=True)
class DriverCheck:
    """PostgreSQL surucusunun kurulu oldugunu dogrular."""

    check_id: str = "postgres.driver"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        if PSYCOPG_AVAILABLE:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.PASSED,
                summary="psycopg kullanilabilir",
                evidence={"driver": "psycopg", "available": True},
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.FAILED,
            summary="PostgreSQL surucusu kurulu degil",
            findings=(
                Finding(
                    code="postgres.driver-missing",
                    severity=Severity.CRITICAL,
                    title="psycopg yok",
                    detail="Kanonik depolama yetenegi kullanilamaz",
                    next_action="`pip install 'zekam[db]'` calistirin",
                ),
            ),
            evidence={"driver": "psycopg", "available": False},
        )


@dataclass(frozen=True, slots=True)
class ConnectionCheck:
    """Baglanti, sunucu surumu ve gerekli eklentileri dogrular."""

    settings: DatabaseSettings
    check_id: str = "postgres.connection"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        evidence = self.settings.sanitized()
        if not PSYCOPG_AVAILABLE:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.SKIPPED,
                summary="Surucu olmadigi icin atlandi",
                evidence=evidence,
            )
        try:
            with connect(self.settings) as connection:
                info = read_server_info(connection)
        except Exception as exc:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.FAILED,
                summary="PostgreSQL baglantisi kurulamadi",
                findings=(
                    Finding(
                        code="postgres.connection-failed",
                        severity=Severity.CRITICAL,
                        title="Veritabanina baglanilamadi",
                        detail=type(exc).__name__,
                        next_action=(
                            "compose/docker-compose.yml servisini baslatin ve "
                            "ZEKAM_DATABASE_* ayarlarini dogrulayin"
                        ),
                    ),
                ),
                evidence=evidence,
            )

        findings = tuple(self._evaluate(info))
        evidence |= {
            "server_version": info.server_version.split(" on ")[0],
            "server_major_version": info.major_version,
            "extensions": info.extensions,
        }
        if any(finding.severity in {Severity.CRITICAL, Severity.ERROR} for finding in findings):
            status = CheckStatus.FAILED
            summary = "PostgreSQL gereksinimleri karsilanmiyor"
        elif findings:
            status = CheckStatus.DEGRADED
            summary = "PostgreSQL kismen uygun"
        else:
            status = CheckStatus.PASSED
            summary = f"PostgreSQL {info.major_version} ve gerekli eklentiler hazir"
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=status,
            summary=summary,
            findings=findings,
            evidence=evidence,
        )

    def _evaluate(self, info: ServerInfo) -> list[Finding]:
        findings: list[Finding] = []
        if info.major_version < self.settings.minimum_server_version:
            findings.append(
                Finding(
                    code="postgres.version-too-old",
                    severity=Severity.CRITICAL,
                    title="Desteklenmeyen PostgreSQL surumu",
                    detail=(
                        f"Bulunan {info.major_version}, "
                        f"gereken en az {self.settings.minimum_server_version}"
                    ),
                    next_action="PostgreSQL 18 imajini kullanin",
                )
            )
        for extension in self.settings.required_extensions:
            if extension not in info.extensions:
                findings.append(
                    Finding(
                        code="postgres.extension-missing",
                        severity=Severity.ERROR,
                        title="Gerekli eklenti yok",
                        detail=f"{extension} yuklu degil",
                        next_action=f"Veritabaninda `CREATE EXTENSION {extension}` calistirin",
                        authority_required=True,
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class MigrationCheck:
    """Migration head'ini ve drift durumunu salt okunur raporlar."""

    settings: DatabaseSettings
    directory: Path | None = None
    check_id: str = "postgres.migrations"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        if not PSYCOPG_AVAILABLE:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.SKIPPED,
                summary="Surucu olmadigi icin atlandi",
            )
        try:
            with connect(self.settings) as connection:
                current = migrations.status(connection, self.directory)
        except Exception as exc:  # hata metni sanitize edilir
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.SKIPPED,
                summary="Migration durumu okunamadi",
                findings=(
                    Finding(
                        code="postgres.migration-status-unavailable",
                        severity=Severity.WARNING,
                        title="Migration durumu okunamadi",
                        detail=type(exc).__name__,
                        next_action="Veritabani baglantisini dogrulayin",
                    ),
                ),
            )

        evidence = current.as_dict()
        findings: list[Finding] = []
        for drift in current.drift:
            findings.append(
                Finding(
                    code=f"postgres.migration-{drift.kind.value}",
                    severity=Severity.CRITICAL,
                    title="Migration drift",
                    detail=drift.detail,
                    next_action=(
                        "Uygulanmis migration dosyasini eski haline getirin veya yeni bir "
                        "migration ekleyin; uygulanmis dosya duzenlenmez"
                    ),
                    authority_required=True,
                )
            )
        if current.pending:
            findings.append(
                Finding(
                    code="postgres.migration-pending",
                    severity=Severity.WARNING,
                    title="Bekleyen migration var",
                    detail=", ".join(migration.label for migration in current.pending),
                    next_action=f"`{PRODUCT.cli} db upgrade --uygula` calistirin",
                    authority_required=True,
                )
            )

        if current.drift:
            status_value = CheckStatus.FAILED
            summary = "Migration drift tespit edildi"
        elif current.pending:
            status_value = CheckStatus.DEGRADED
            summary = f"{len(current.pending)} migration bekliyor"
        else:
            summary = f"Migration head {current.head}" if current.head else "Migration yok"
            status_value = CheckStatus.PASSED
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=status_value,
            summary=summary,
            findings=tuple(findings),
            evidence=evidence,
        )
