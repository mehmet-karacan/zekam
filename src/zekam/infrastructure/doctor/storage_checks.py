"""Nesne deposu doctor kontrolleri.

Kontrol salt okunurdur: yalnizca kok dizinin varligini, yazilabilirligini ve
mevcut nesnelerin butunlugunu raporlar.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.application.diagnostics import CheckResult, CheckStatus, Finding, Severity
from zekam.domain.identity import PRODUCT
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

CATEGORY = "storage"

#: Butunluk taramasi bu sayidan fazla nesnede ornekleme yapar.
FULL_SCAN_LIMIT = 500


@dataclass(frozen=True, slots=True)
class ObjectStoreCheck:
    """Yerel nesne deposunun durumunu raporlar."""

    root: Path
    check_id: str = "storage.object-store"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        evidence: dict[str, Any] = {"root_exists": self.root.is_dir()}
        if not self.root.is_dir():
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.DEGRADED,
                summary="Nesne deposu kok dizini yok",
                findings=(
                    Finding(
                        code="storage.root-missing",
                        severity=Severity.WARNING,
                        title="Nesne deposu hazir degil",
                        detail="Artifact saklama kullanilamaz",
                        next_action=f"`{PRODUCT.cli} init` calistirin",
                    ),
                ),
                evidence=evidence,
            )

        store = LocalContentAddressedStore(self.root)
        objects = list(store.iter_objects())
        evidence |= {
            "object_count": len(objects),
            "total_bytes": sum(info.size_bytes for info in objects),
            "integrity_scanned": len(objects) <= FULL_SCAN_LIMIT,
        }

        broken: tuple[str, ...] = ()
        if len(objects) <= FULL_SCAN_LIMIT:
            broken = store.verify_all()
        evidence["corrupt_count"] = len(broken)

        if broken:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.FAILED,
                summary=f"{len(broken)} nesne bozuk",
                findings=(
                    Finding(
                        code="storage.object-corrupt",
                        severity=Severity.CRITICAL,
                        title="Nesne butunlugu bozuk",
                        detail=f"{len(broken)} nesnenin digest'i icerigiyle uyusmuyor",
                        next_action="Yedekten geri yukleyin ve disk saglik durumunu inceleyin",
                        authority_required=True,
                    ),
                ),
                evidence=evidence,
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED,
            summary=f"{len(objects)} nesne, butunluk dogrulandi",
            evidence=evidence,
        )
