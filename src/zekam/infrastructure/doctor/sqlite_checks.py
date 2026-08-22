"""SQLite minimum profilinin salt okunur doctor kontrolleri."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zekam.application.diagnostics import CheckResult, CheckStatus, Finding, Severity
from zekam.infrastructure.sqlite.repository import SCHEMA_DIGEST, SCHEMA_VERSION, status

CATEGORY = "sqlite"


@dataclass(frozen=True, slots=True)
class PersistenceCheck:
    path: Path
    path_ref: str
    check_id: str = "sqlite.persistence"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        current = status(self.path)
        evidence = {
            "exists": current.exists,
            "schema_version": current.schema_version,
            "expected_schema_version": SCHEMA_VERSION,
            "integrity_ok": current.integrity_ok,
            "schema_ok": current.schema_ok,
            "expected_schema_digest": SCHEMA_DIGEST,
            "path_ref": self.path_ref,
        }
        if not current.exists:
            return CheckResult(
                self.check_id,
                self.category,
                CheckStatus.FAILED,
                "SQLite dosyasi bulunamadi",
                findings=(
                    Finding(
                        code="sqlite.database-missing",
                        severity=Severity.CRITICAL,
                        title="SQLite bootstrap eksik",
                        detail="Secili SQLite dosyasi ZEKAM_HOME icinde yok",
                        next_action="`zekam init --persistence sqlite` calistirin",
                    ),
                ),
                evidence=evidence,
            )
        if (
            not current.integrity_ok
            or not current.schema_ok
            or current.schema_version != SCHEMA_VERSION
        ):
            return CheckResult(
                self.check_id,
                self.category,
                CheckStatus.FAILED,
                "SQLite integrity veya migration drift",
                findings=(
                    Finding(
                        code="sqlite.integrity-or-schema-drift",
                        severity=Severity.CRITICAL,
                        title="SQLite kanonik dosyasi kullanilamaz",
                        detail=(
                            "integrity_check, schema manifest veya version beklenen degerde degil"
                        ),
                        next_action="Dosyayi yedekleyin ve exact recovery plani hazirlayin",
                        authority_required=True,
                    ),
                ),
                evidence=evidence,
            )
        return CheckResult(
            self.check_id,
            self.category,
            CheckStatus.PASSED,
            "SQLite minimum persistence hazir",
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    check_id: str = "sqlite.capabilities"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        return CheckResult(
            self.check_id,
            self.category,
            CheckStatus.DEGRADED,
            "SQLite minimum profil; dagitik runtime yetenekleri PostgreSQL gerektirir",
            findings=(
                Finding(
                    code="sqlite.minimum-profile",
                    severity=Severity.WARNING,
                    title="SQLite capability siniri",
                    detail=(
                        "project add/list, work create/list ve knowledge JSON-vector cosine "
                        "desteklenir; durable queue, "
                        "RLS, governance, scheduler ve native memory desteklenmez"
                    ),
                    next_action="Bu yetenekler gerekiyorsa yeni ZEKAM_HOME ile PostgreSQL secin",
                ),
            ),
            evidence={
                "supported": [
                    "project:add/list",
                    "work:create/list",
                    "knowledge:vector-index/search",
                    "json-vector-cosine",
                ],
                "unsupported": ["queue", "rls", "governance", "scheduler", "native-memory"],
                "fallback": False,
            },
        )
