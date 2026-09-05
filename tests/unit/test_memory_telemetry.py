from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.memory_telemetry import (
    MemoryEffectiveness,
    MemoryUsageEvent,
    memory_source_ref,
)


def test_memory_source_ref_is_exact_storage_identity() -> None:
    record_id = uuid4()
    assert memory_source_ref(record_id) == f"memory-record/{record_id}"


def _usage_event(*, used_at: dt.datetime, record_digest: str) -> MemoryUsageEvent:
    identifiers = [uuid4() for _ in range(12)]
    return MemoryUsageEvent(
        identifiers[0],
        identifiers[1],
        identifiers[2],
        identifiers[3],
        identifiers[4],
        identifiers[5],
        identifiers[6],
        identifiers[7],
        identifiers[8],
        identifiers[9],
        "build",
        identifiers[10],
        identifiers[11],
        record_digest,
        digest("fragment"),
        digest("payload"),
        digest("context"),
        used_at,
        digest("event"),
    )


def test_usage_event_rejects_naive_time_and_invalid_digest() -> None:
    with pytest.raises(ValidationFailed):
        _usage_event(used_at=dt.datetime(2026, 8, 24), record_digest=digest("record"))
    with pytest.raises(ValidationFailed):
        _usage_event(used_at=dt.datetime.now(dt.UTC), record_digest="forged")


def test_effectiveness_cannot_claim_more_successes_than_outcomes() -> None:
    with pytest.raises(ValidationFailed):
        MemoryEffectiveness(uuid4(), digest("record"), 1, 0, 1, None, None)
