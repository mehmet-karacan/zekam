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


def test_usage_event_rejects_naive_time_and_invalid_digest() -> None:
    leading_identifiers = [uuid4() for _ in range(10)]
    scoped_identifiers = [uuid4() for _ in range(2)]
    with pytest.raises(ValidationFailed):
        MemoryUsageEvent(
            *leading_identifiers,
            "build",
            *scoped_identifiers,
            digest("record"),
            digest("fragment"),
            digest("payload"),
            digest("context"),
            dt.datetime(2026, 8, 24),
            digest("event"),
        )
    with pytest.raises(ValidationFailed):
        MemoryUsageEvent(
            *leading_identifiers,
            "build",
            *scoped_identifiers,
            "forged",
            digest("fragment"),
            digest("payload"),
            digest("context"),
            dt.datetime.now(dt.UTC),
            digest("event"),
        )


def test_effectiveness_cannot_claim_more_successes_than_outcomes() -> None:
    with pytest.raises(ValidationFailed):
        MemoryEffectiveness(uuid4(), digest("record"), 1, 0, 1, None, None)
