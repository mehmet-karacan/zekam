from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import uuid4

from zekam.application.memory_observability import MemoryDimensionStatus
from zekam.infrastructure.postgres.memory_observability_repository import (
    PostgresMemoryHealthReader,
)


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, parameters: object = None) -> None:
        self.connection.calls.append((query, parameters))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.connection.rows.pop(0)


class _Connection:
    def __init__(self, rows: list[tuple[Any, ...] | None]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, object]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def test_terminal_projection_age_does_not_create_live_freshness_debt(tmp_path: Path) -> None:
    connection = _Connection([(2,), (0,), None])
    reader = PostgresMemoryHealthReader(connection, tmp_path, tmp_path, uuid4())

    result = reader._projection(dt.datetime(2026, 8, 28, tzinfo=dt.UTC))

    assert result.status is MemoryDimensionStatus.PASSED
    stale_query, stale_parameters = connection.calls[1]
    assert "join work.work_item" in stale_query
    assert "item.state=any(%s)" in stale_query
    assert stale_parameters[1] == ["active", "verification"]


def test_stale_live_projection_remains_degraded(tmp_path: Path) -> None:
    connection = _Connection([(2,), (1,), None])
    reader = PostgresMemoryHealthReader(connection, tmp_path, tmp_path, uuid4())

    result = reader._projection(dt.datetime(2026, 8, 28, tzinfo=dt.UTC))

    assert result.status is MemoryDimensionStatus.DEGRADED
    assert result.failure_code == "memory.projection-stale"
