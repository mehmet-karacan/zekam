from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.config import DatabaseSettings
from zekam.application.diagnostics import CheckStatus
from zekam.application.memory_observability import (
    REQUIRED_MEMORY_DIMENSIONS,
    MemoryContinuityHealthReport,
    MemoryDimensionStatus,
    MemoryHealthDimension,
)
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.doctor import memory_checks

NOW = dt.datetime(2026, 8, 26, tzinfo=dt.UTC)


@contextmanager
def _connect(_settings: DatabaseSettings) -> Iterator[object]:
    yield object()


def _check(tmp_path: Path) -> memory_checks.MemoryContinuityCheck:
    return memory_checks.MemoryContinuityCheck(
        DatabaseSettings(host="127.0.0.1", port=5432, name="zekam", user="zekam"),
        tmp_path,
        tmp_path / "private",
    )


def test_root_memory_doctor_resolves_default_realm_before_collect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    realm_id = uuid4()
    captured: dict[str, Any] = {}

    class _Realms:
        def __init__(self, connection: object) -> None:
            captured["realm_connection"] = connection

        def find_by_slug(self, slug: str) -> Any:
            captured["slug"] = slug
            return SimpleNamespace(id=realm_id)

    class _Reader:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def collect(self) -> MemoryContinuityHealthReport:
            return MemoryContinuityHealthReport(
                tuple(
                    MemoryHealthDimension(
                        item,
                        MemoryDimensionStatus.PASSED,
                        "Passed",
                        f"db:memory/{item}",
                        0,
                    )
                    for item in REQUIRED_MEMORY_DIMENSIONS
                ),
                NOW,
                "current-realm",
            )

    monkeypatch.setattr(memory_checks, "PSYCOPG_AVAILABLE", True)
    monkeypatch.setattr(memory_checks, "connect", _connect)
    monkeypatch.setattr(memory_checks, "RealmRepository", _Realms)
    monkeypatch.setattr(memory_checks, "PostgresMemoryHealthReader", _Reader)

    result = _check(tmp_path).run()

    assert result.status is CheckStatus.PASSED
    assert captured["slug"] == DEFAULT_REALM_SLUG
    assert captured["realm_id"] == realm_id


def test_root_memory_doctor_fails_closed_when_default_realm_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Realms:
        def __init__(self, _connection: object) -> None:
            pass

        def find_by_slug(self, _slug: str) -> None:
            return None

    monkeypatch.setattr(memory_checks, "PSYCOPG_AVAILABLE", True)
    monkeypatch.setattr(memory_checks, "connect", _connect)
    monkeypatch.setattr(memory_checks, "RealmRepository", _Realms)

    result = _check(tmp_path).run()

    assert result.status is CheckStatus.FAILED
    assert result.findings[0].code == "memory.default-realm-missing"
    assert result.evidence["dimensions_expected"] == 15
