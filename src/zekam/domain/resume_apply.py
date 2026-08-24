"""Durable resume apply saga contracts; no inherited authority or lease token."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.resume import ResumePlan


class ResumeApplyPhase(StrEnum):
    CLAIM = "claim"
    DISPATCH = "dispatch"
    TERMINAL = "terminal"


class ResumeApplyState(StrEnum):
    CLAIMED = "claimed"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    RECOVERY_REQUIRED = "recovery-required"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ResumeApplyRequest:
    plan: ResumePlan
    supplied_plan_digest: str
    actor_id: UUID
    authorization_id: UUID
    worker_label: str
    capabilities: tuple[str, ...]
    lease_seconds: int = 60

    def __post_init__(self) -> None:
        parse_digest(self.supplied_plan_digest)
        if self.supplied_plan_digest != self.plan.plan_digest:
            raise PolicyViolation("Resume apply supplied plan digest eslesmiyor")
        if not self.worker_label.strip() or not self.capabilities:
            raise ValidationFailed("Resume apply worker ve capability ister")
        if self.lease_seconds < 1 or self.lease_seconds > 3600:
            raise ValidationFailed("Resume apply lease suresi 1..3600 olmali")


@dataclass(frozen=True, slots=True)
class ResumeApplyEvent:
    apply_id: UUID
    sequence: int
    phase: ResumeApplyPhase
    state: ResumeApplyState
    reason_code: str
    occurred_at: dt.datetime
    attempt_id: UUID | None = None
    lease_id: UUID | None = None
    fencing_token: int | None = None
    claim_id: UUID | None = None
    receipt_id: UUID | None = None
    result_digest: str | None = None
    previous_digest: str | None = None

    def __post_init__(self) -> None:
        if self.sequence < 1 or not self.reason_code.strip():
            raise ValidationFailed("Resume apply event sequence/reason gecersiz")
        if self.occurred_at.tzinfo is None:
            raise ValidationFailed("Resume apply event timezone-aware olmali")
        runtime = (self.attempt_id, self.lease_id, self.fencing_token)
        if any(value is not None for value in runtime) and any(value is None for value in runtime):
            raise ValidationFailed("Resume apply runtime identity exact olmali")
        if self.fencing_token is not None and self.fencing_token < 1:
            raise ValidationFailed("Resume apply fence pozitif olmali")
        if self.result_digest is not None:
            parse_digest(self.result_digest)
        if self.previous_digest is not None:
            parse_digest(self.previous_digest)

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-resume-apply-event/v1",
            "apply_id": str(self.apply_id),
            "sequence": self.sequence,
            "phase": self.phase.value,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "attempt_id": None if self.attempt_id is None else str(self.attempt_id),
            "lease_id": None if self.lease_id is None else str(self.lease_id),
            "fencing_token": self.fencing_token,
            "claim_id": None if self.claim_id is None else str(self.claim_id),
            "receipt_id": None if self.receipt_id is None else str(self.receipt_id),
            "result_digest": self.result_digest,
            "previous_digest": self.previous_digest,
            "occurred_at": self.occurred_at,
        }

    @property
    def event_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class ResumeApplyResult:
    apply_id: UUID
    state: ResumeApplyState
    plan_digest: str
    event_digest: str
    attempt_id: UUID | None = None
    lease_id: UUID | None = None
    fencing_token: int | None = None
    claim_id: UUID | None = None
    receipt_id: UUID | None = None
    result_digest: str | None = None
    reprepare_required: bool = False

    def __post_init__(self) -> None:
        parse_digest(self.plan_digest)
        parse_digest(self.event_digest)
        if self.result_digest is not None:
            parse_digest(self.result_digest)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-resume-apply-result/v1",
            "apply_id": str(self.apply_id),
            "state": self.state.value,
            "resume_plan_digest": self.plan_digest,
            "event_digest": self.event_digest,
            "attempt_id": None if self.attempt_id is None else str(self.attempt_id),
            "lease_id": None if self.lease_id is None else str(self.lease_id),
            "fencing_token": self.fencing_token,
            "claim_id": None if self.claim_id is None else str(self.claim_id),
            "receipt_id": None if self.receipt_id is None else str(self.receipt_id),
            "result_digest": self.result_digest,
            "reprepare_required": self.reprepare_required,
            "grants_authority": False,
        }
