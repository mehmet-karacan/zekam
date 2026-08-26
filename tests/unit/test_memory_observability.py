from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.memory_observability import (
    REQUIRED_MEMORY_DIMENSIONS,
    MemoryContinuityHealthReport,
    MemoryDimensionStatus,
    MemoryHealthDimension,
    MemoryObservabilityService,
)
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.postgres.memory_observability_repository import (
    PostgresMemoryHealthReader,
)

NOW = dt.datetime(2026, 8, 26, tzinfo=dt.UTC)


def _dimension(
    dimension_id: str, status: MemoryDimensionStatus = MemoryDimensionStatus.PASSED
) -> MemoryHealthDimension:
    unhealthy = status in {
        MemoryDimensionStatus.DEGRADED,
        MemoryDimensionStatus.FAILED,
    }
    return MemoryHealthDimension(
        dimension_id,
        status,
        "Safe health summary",
        f"db:memory/{dimension_id}",
        0,
        "memory.test-failure" if unhealthy else None,
        "Exact read-only evidence'i yeniden denetleyin" if unhealthy else None,
    )


class _Reader:
    def __init__(self, report: MemoryContinuityHealthReport) -> None:
        self.report = report

    def collect(self, *, now: dt.datetime | None = None) -> MemoryContinuityHealthReport:
        del now
        return self.report


def test_doctor_requires_exact_15_dimensions_and_content_safe_views() -> None:
    dimensions = tuple(_dimension(item) for item in REQUIRED_MEMORY_DIMENSIONS)
    report = MemoryContinuityHealthReport(dimensions, NOW, "realm:test")
    service = MemoryObservabilityService(_Reader(report))

    document = report.as_dict()
    assert len(document["dimensions"]) == 15
    assert tuple(item["dimension_id"] for item in document["dimensions"]) == (
        REQUIRED_MEMORY_DIMENSIONS
    )
    assert document["grants_authority"] is False
    assert "content" not in str(document).lower()
    assert len(service.projection_freshness()["dimensions"]) == 1
    assert len(service.gap_report()["dimensions"]) == 2


def test_red_dimension_requires_failure_code_and_next_safe_action() -> None:
    with pytest.raises(ValidationFailed, match="code ve next action"):
        MemoryHealthDimension(
            REQUIRED_MEMORY_DIMENSIONS[0],
            MemoryDimensionStatus.FAILED,
            "Failed",
            "db:memory/failure",
            1,
        )


class _Cursor:
    def __init__(self) -> None:
        self.parameters: tuple[Any, ...] | None = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _query: str, parameters: tuple[Any, ...]) -> None:
        self.parameters = parameters

    def fetchall(self) -> list[Any]:
        return []


class _Connection:
    def __init__(self) -> None:
        self.last_cursor: _Cursor | None = None

    def cursor(self) -> _Cursor:
        self.last_cursor = _Cursor()
        return self.last_cursor


def test_required_hook_doctor_query_is_exact_realm_scoped(tmp_path: Path) -> None:
    connection = _Connection()
    realm_id = uuid4()
    reader = PostgresMemoryHealthReader(
        connection,
        tmp_path,
        tmp_path / "private",
        realm_id,
    )

    dimension = reader._required_hooks()

    assert connection.last_cursor is not None
    assert connection.last_cursor.parameters is not None
    assert connection.last_cursor.parameters[0] == realm_id
    assert dimension.status is MemoryDimensionStatus.FAILED
