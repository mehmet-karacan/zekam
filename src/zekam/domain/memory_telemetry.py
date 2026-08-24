"""Authority-free Memory v2 usage and verified outcome projections."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import UUID

from zekam.domain.canonical import parse_digest
from zekam.domain.errors import ValidationFailed


def memory_source_ref(record_id: UUID) -> str:
    """Typed context fragment convention for one exact memory revision."""

    return f"memory-record/{record_id}"


@dataclass(frozen=True, slots=True)
class MemoryUsageEvent:
    id: UUID
    record_id: UUID
    request_manifest_id: UUID
    invocation_attempt_id: UUID
    invocation_result_id: UUID
    task_plan_id: UUID
    run_id: UUID
    job_id: UUID
    runtime_attempt_id: UUID
    assignment_id: UUID
    step_id: str
    project_id: UUID
    work_item_id: UUID
    record_digest: str
    fragment_digest: str
    model_visible_payload_digest: str
    context_manifest_digest: str
    used_at: dt.datetime
    event_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.record_digest,
            self.fragment_digest,
            self.model_visible_payload_digest,
            self.context_manifest_digest,
            self.event_digest,
        ):
            parse_digest(value)
        if self.used_at.tzinfo is None or not self.step_id.strip():
            raise ValidationFailed("Memory usage step ve timezone-aware zaman ister")


@dataclass(frozen=True, slots=True)
class MemoryUsageOutcome:
    id: UUID
    usage_event_id: UUID
    checkpoint_id: UUID
    step_id: str
    verifier_assignment_id: UUID
    verifier_invocation_id: UUID
    verifier_envelope_digest: str
    checkpoint_digest: str
    result_digest: str
    outcome_status: str
    correlated_at: dt.datetime
    outcome_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.verifier_envelope_digest,
            self.checkpoint_digest,
            self.result_digest,
            self.outcome_digest,
        ):
            parse_digest(value)
        if self.outcome_status != "verified-success":
            raise ValidationFailed("Memory outcome status taninmiyor")
        if not self.step_id.strip() or self.correlated_at.tzinfo is None:
            raise ValidationFailed("Memory outcome step ve timezone-aware zaman ister")


@dataclass(frozen=True, slots=True)
class MemoryEffectiveness:
    record_id: UUID
    record_digest: str
    usage_count: int
    verified_outcome_count: int
    verified_success_count: int
    last_used_at: dt.datetime | None
    last_verified_outcome_at: dt.datetime | None

    def __post_init__(self) -> None:
        parse_digest(self.record_digest)
        if min(self.usage_count, self.verified_outcome_count, self.verified_success_count) < 0:
            raise ValidationFailed("Memory effectiveness sayaclari negatif olamaz")
        if self.verified_success_count > self.verified_outcome_count:
            raise ValidationFailed("Memory basari sayisi outcome sayisini asamaz")
