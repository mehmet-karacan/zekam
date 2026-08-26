"""Content-safe read models for Memory Continuity observability."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class MemoryDimensionStatus(StrEnum):
    PASSED = "passed"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


REQUIRED_MEMORY_DIMENSIONS: tuple[str, ...] = (
    "migration-component-state",
    "required-hook-runtime",
    "origin-recursion-guard",
    "hydration-freshness-completeness",
    "close-compaction-completeness",
    "continuity-gaps-recovery",
    "compiler-watermark-backlog",
    "quarantined-candidates",
    "projection-freshness",
    "context-omissions",
    "claim-without-receipt",
    "tracked-secret-public-leak",
    "private-store-retention",
    "stale-review-debt",
    "feature-mode",
)


@dataclass(frozen=True, slots=True)
class MemoryHealthDimension:
    dimension_id: str
    status: MemoryDimensionStatus
    summary: str
    evidence_ref: str
    observed_count: int
    failure_code: str | None = None
    next_safe_action: str | None = None

    def __post_init__(self) -> None:
        if self.dimension_id not in REQUIRED_MEMORY_DIMENSIONS:
            raise ValidationFailed("Memory doctor dimension registry disinda")
        if not self.summary.strip() or len(self.summary) > 256:
            raise ValidationFailed("Memory doctor summary bounded olmali")
        if not self.evidence_ref.startswith(("db:", "code:", "policy:", "git:")):
            raise ValidationFailed("Memory doctor portable evidence ref ister")
        if self.observed_count < 0:
            raise ValidationFailed("Memory doctor count negatif olamaz")
        unhealthy = self.status in {
            MemoryDimensionStatus.DEGRADED,
            MemoryDimensionStatus.FAILED,
        }
        if unhealthy != bool(self.failure_code and self.next_safe_action):
            raise ValidationFailed("Memory doctor unhealthy bulgu code ve next action ister")
        if self.failure_code is not None and not self.failure_code.startswith("memory."):
            raise ValidationFailed("Memory doctor failure code canonical olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension_id": self.dimension_id,
            "status": self.status.value,
            "summary": self.summary,
            "evidence_ref": self.evidence_ref,
            "observed_count": self.observed_count,
            "failure_code": self.failure_code,
            "next_safe_action": self.next_safe_action,
        }


@dataclass(frozen=True, slots=True)
class MemoryContinuityHealthReport:
    dimensions: tuple[MemoryHealthDimension, ...]
    generated_at: dt.datetime
    realm_scope: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None:
            raise ValidationFailed("Memory doctor zamani timezone-aware olmali")
        ids = tuple(item.dimension_id for item in self.dimensions)
        if ids != REQUIRED_MEMORY_DIMENSIONS:
            raise ValidationFailed("Memory doctor exact 15 dimension sirasi ister")
        if not self.realm_scope.strip() or len(self.realm_scope) > 128:
            raise ValidationFailed("Memory doctor realm scope bounded olmali")
        if self.grants_authority:
            raise PolicyViolation("Memory doctor authority uretemez")

    @property
    def status(self) -> MemoryDimensionStatus:
        statuses = {item.status for item in self.dimensions}
        if MemoryDimensionStatus.FAILED in statuses:
            return MemoryDimensionStatus.FAILED
        if MemoryDimensionStatus.DEGRADED in statuses:
            return MemoryDimensionStatus.DEGRADED
        if MemoryDimensionStatus.UNAVAILABLE in statuses:
            return MemoryDimensionStatus.UNAVAILABLE
        return MemoryDimensionStatus.PASSED

    @property
    def report_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-memory-continuity-health/v1",
                "dimensions": [item.as_dict() for item in self.dimensions],
                "realm_scope": self.realm_scope,
                "grants_authority": False,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-continuity-health/v1",
            "status": self.status.value,
            "dimensions": [item.as_dict() for item in self.dimensions],
            "generated_at": self.generated_at.isoformat(),
            "realm_scope": self.realm_scope,
            "report_digest": self.report_digest,
            "grants_authority": False,
        }


class MemoryHealthReader(Protocol):
    def collect(self, *, now: dt.datetime | None = None) -> MemoryContinuityHealthReport: ...


@dataclass(frozen=True, slots=True)
class MemoryObservabilityService:
    reader: MemoryHealthReader

    def doctor(self, *, now: dt.datetime | None = None) -> MemoryContinuityHealthReport:
        return self.reader.collect(now=now)

    def status(self, *, now: dt.datetime | None = None) -> dict[str, Any]:
        report = self.doctor(now=now)
        selected = tuple(
            item
            for item in report.dimensions
            if item.dimension_id
            in {
                "migration-component-state",
                "hydration-freshness-completeness",
                "close-compaction-completeness",
                "continuity-gaps-recovery",
                "feature-mode",
            }
        )
        return self._view("status", report, selected)

    def contract_check(self, *, now: dt.datetime | None = None) -> dict[str, Any]:
        report = self.doctor(now=now)
        selected = tuple(
            item
            for item in report.dimensions
            if item.dimension_id
            in {
                "origin-recursion-guard",
                "hydration-freshness-completeness",
                "close-compaction-completeness",
                "context-omissions",
                "feature-mode",
            }
        )
        return self._view("contract-check", report, selected)

    def gap_report(self, *, now: dt.datetime | None = None) -> dict[str, Any]:
        report = self.doctor(now=now)
        selected = tuple(
            item
            for item in report.dimensions
            if item.dimension_id in {"continuity-gaps-recovery", "claim-without-receipt"}
        )
        return self._view("gap-report", report, selected)

    def compiler_shadow_report(self, *, now: dt.datetime | None = None) -> dict[str, Any]:
        report = self.doctor(now=now)
        selected = tuple(
            item
            for item in report.dimensions
            if item.dimension_id
            in {"compiler-watermark-backlog", "quarantined-candidates", "feature-mode"}
        )
        return self._view("compiler-shadow-report", report, selected)

    def projection_freshness(self, *, now: dt.datetime | None = None) -> dict[str, Any]:
        report = self.doctor(now=now)
        selected = tuple(
            item for item in report.dimensions if item.dimension_id == "projection-freshness"
        )
        return self._view("projection-freshness", report, selected)

    @staticmethod
    def _view(
        name: str,
        report: MemoryContinuityHealthReport,
        selected: tuple[MemoryHealthDimension, ...],
    ) -> dict[str, Any]:
        return {
            "schema": f"zekam-memory-{name}/v1",
            "status": report.status.value,
            "dimensions": [item.as_dict() for item in selected],
            "source_report_digest": report.report_digest,
            "grants_authority": False,
        }
