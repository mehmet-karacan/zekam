"""SQLite minimum profilinin salt okunur doctor kontrolleri."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from zekam.application.diagnostics import CheckResult, CheckStatus, Finding, Severity
from zekam.infrastructure.sqlite.operational_schema import SCHEMA_DIGEST, SCHEMA_VERSION, status

if TYPE_CHECKING:
    from zekam.application.composition import ApplicationContext

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
class LocalCoreStoresCheck:
    """Validate every composed local store through its semantic contract."""

    context: ApplicationContext
    check_id: str = "sqlite.local-core-stores"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        from zekam.infrastructure.local_core_services import LocalCoreServices

        snapshot = LocalCoreServices.from_context(self.context).status(semantic_analytics=True)
        databases = snapshot.get("databases")
        analytics = snapshot.get("analytics")
        if not isinstance(databases, Mapping) or not isinstance(analytics, Mapping):
            raise ValueError("Local core status exact store census missing")
        if snapshot.get("all_ready") is True:
            return CheckResult(
                self.check_id,
                self.category,
                CheckStatus.PASSED,
                "Tum composed local store semantic fingerprintleri dogrulandi",
                evidence=snapshot,
            )

        missing = sorted(
            str(name)
            for name, item in databases.items()
            if isinstance(item, Mapping)
            and item.get("required") is True
            and item.get("exists") is False
        )
        invalid = sorted(
            str(name)
            for name, item in databases.items()
            if isinstance(item, Mapping)
            and item.get("exists") is True
            and (item.get("integrity") is not True or item.get("schema_ok") is not True)
        )
        findings: list[Finding] = []
        if missing:
            findings.append(
                Finding(
                    code="sqlite.local-store-missing",
                    severity=Severity.CRITICAL,
                    title="Zorunlu yerel store eksik",
                    detail=", ".join(missing),
                    next_action="`zekam doctor --repair-plan --json` ile exact onarim plani alin",
                )
            )
        if invalid:
            findings.append(
                Finding(
                    code="sqlite.local-store-semantic-drift",
                    severity=Severity.CRITICAL,
                    title="Yerel store semantic fingerprint gecersiz",
                    detail=", ".join(invalid),
                    next_action="Store'u yedekleyin ve explicit recovery plani hazirlayin",
                    authority_required=True,
                )
            )
        if analytics.get("ready") is not True:
            findings.append(
                Finding(
                    code="sqlite.local-analytics-semantic-drift",
                    severity=Severity.CRITICAL,
                    title="Yerel analytics store gecersiz",
                    detail=str(analytics.get("state", "unknown")),
                    next_action=(
                        "Analytics kaynagini koruyup bounded rebuild/recovery plani hazirlayin"
                    ),
                    authority_required=analytics.get("repairable") is not True,
                )
            )
        if not findings:
            raise ValueError("Local core unhealthy status has no exact cause")
        return CheckResult(
            self.check_id,
            self.category,
            CheckStatus.FAILED,
            "Composed local store butunluk kapisi gecmedi",
            findings=tuple(findings),
            evidence=snapshot,
        )


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    check_id: str = "sqlite.capabilities"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        return CheckResult(
            self.check_id,
            self.category,
            CheckStatus.PASSED,
            "Yerel SQLite authority ve dayanıklı runtime hazır",
            evidence={
                "supported": [
                    "project:add/list",
                    "work:create/list",
                    "knowledge:vector-index/search",
                    "json-vector-cosine",
                    "durable-runtime",
                    "continuity-v1-v4",
                    "learning-model-benchmark-routing-analytics-improvement",
                ],
                "unsupported": ["remote-provider-execution", "distributed-scheduler"],
                "fallback": False,
            },
        )
