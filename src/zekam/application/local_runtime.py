"""Database-neutral local queue, lease, claim and recovery contracts."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from zekam.domain.errors import ValidationFailed

# Compatibility publisher handles only the runtime's existing typed observations.
# Lifecycle compilation must register its own exact handler; there is no wildcard.
RUNTIME_OUTBOX_KINDS = (
    "job.enqueued",
    "job.ready",
    "job.completed",
    "job.failed",
    "job.recovery-required",
    "job.quarantined",
)
RESERVED_JOB_OPERATIONS = (
    "continuity.compile",
    "research.run",
    "knowledge.create",
    "knowledge.update",
    "knowledge.archive",
    "knowledge.restore",
    "project.odi-bind",
)
_RUNTIME_KIND = re.compile(r"[a-z][a-z0-9]*(?:[._/-][a-z0-9]+)*")


def validate_outbox_kinds(value: object) -> tuple[str, ...]:
    """Validate an exact, bounded consumer allowlist without coercion."""
    return _validate_kinds(value, "Outbox supported kinds")


def validate_job_operations(value: object) -> tuple[str, ...]:
    """Validate explicit operation routing without granting effect authority."""
    return _validate_kinds(value, "Job supported operations")


def _validate_kinds(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not 1 <= len(value) <= 64:
        raise ValidationFailed(f"{label} 1..64 elemanli tuple olmali")
    kinds: list[str] = []
    for kind in value:
        if (
            not isinstance(kind, str)
            or not 1 <= len(kind) <= 128
            or _RUNTIME_KIND.fullmatch(kind) is None
        ):
            raise ValidationFailed(f"{label} exact bounded token olmali")
        if kind in kinds:
            raise ValidationFailed(f"{label} duplicate olamaz")
        kinds.append(kind)
    return tuple(kinds)


@dataclass(frozen=True, slots=True)
class LocalJob:
    id: str
    state: str
    idempotency_key: str
    attempt_count: int
    max_attempts: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LocalLease:
    id: str
    job_id: str
    owner_id: str
    owner_pid: int
    owner_token: str
    fencing_token: int
    expires_at: str


@dataclass(frozen=True, slots=True)
class LocalClaim:
    id: str
    job_id: str
    lease_id: str
    fencing_token: int
    operation: str
    effect_digest: str


@dataclass(frozen=True, slots=True)
class LocalReceipt:
    id: str
    claim_id: str
    status: Literal["completed", "failed", "unknown"]
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class LocalRecoveryResolution:
    id: str
    recovery_case_id: str
    outcome: Literal["completed", "failed", "delivered"]
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class LocalRecoveryCase:
    id: str
    job_id: str
    case_kind: Literal["effect-unknown", "outbox-delivery-unknown"]
    evidence_digest: str
    state: Literal["open", "resolved"]


@dataclass(frozen=True, slots=True)
class LocalClaimedWork:
    job: LocalJob
    lease: LocalLease


@dataclass(frozen=True, slots=True)
class LocalOutboxEvent:
    id: str
    job_id: str
    idempotency_key: str
    event_kind: str
    payload_digest: str
    payload: dict[str, Any]
    state: Literal["pending", "claimed", "delivered", "failed", "recovery-required"]


@dataclass(frozen=True, slots=True)
class LocalOutboxClaim:
    event: LocalOutboxEvent
    claim_id: str
    owner_id: str
    owner_pid: int
    owner_token: str
    fencing_token: int
    expires_at: str


@dataclass(frozen=True, slots=True)
class RecoverySweep:
    requeued: int
    recovery_required: int
    released_locks: int
    finalized: int = 0
    timed_out: int = 0


@dataclass(frozen=True, slots=True)
class LocalRuntimeStatus:
    ready_jobs: int
    running_jobs: int
    recovery_jobs: int
    quarantined_jobs: int
    pending_outbox: int
    claimed_outbox: int
    recovery_outbox: int
    open_recovery_cases: int


class LocalRuntimeStore(Protocol):
    def status(self) -> LocalRuntimeStatus: ...

    def job_snapshot(self, reference: str) -> dict[str, Any] | None: ...

    def recovery_cases(self, *, open_only: bool = True) -> tuple[LocalRecoveryCase, ...]: ...

    def enqueue(
        self,
        *,
        idempotency_key: str,
        payload: dict[str, Any],
        max_attempts: int = 1,
        available_at: str | None = None,
        timeout_at: str | None = None,
    ) -> tuple[LocalJob, bool]: ...

    def claim_next(
        self,
        *,
        owner_id: str,
        owner_pid: int,
        owner_token: str,
        lease_seconds: int,
        resources: tuple[str, ...] = (),
        supported_operations: tuple[str, ...] | None = None,
        job_id: str | None = None,
        now: str | None = None,
    ) -> LocalClaimedWork | None: ...

    def heartbeat(
        self,
        lease_id: str,
        *,
        owner_id: str,
        owner_token: str,
        fencing_token: int,
        lease_seconds: int,
        now: str | None = None,
    ) -> LocalLease: ...

    def claim_effect(
        self,
        work: LocalClaimedWork,
        *,
        operation: str,
        effect_digest: str,
        idempotency_key: str,
        now: str | None = None,
    ) -> tuple[LocalClaim, bool]: ...

    def record_receipt(
        self,
        claim: LocalClaim,
        *,
        status: Literal["completed", "failed", "unknown"],
        evidence_digest: str,
        now: str | None = None,
    ) -> LocalReceipt: ...

    def finish(
        self,
        work: LocalClaimedWork,
        *,
        state: Literal["completed", "failed", "recovery-required"],
        evidence_digest: str | None = None,
        now: str | None = None,
    ) -> LocalJob: ...

    def recover_expired(self, *, now: str | None = None) -> RecoverySweep: ...

    def recover_orphans(
        self,
        process_token_for: Callable[[int], str | None],
        *,
        now: str | None = None,
    ) -> RecoverySweep: ...

    def pending_outbox(self, *, limit: int = 100) -> tuple[LocalOutboxEvent, ...]: ...

    def claim_outbox(
        self,
        *,
        supported_kinds: tuple[str, ...],
        outbox_id: str | None = None,
        require_completed_job: bool = False,
        owner_id: str,
        owner_pid: int,
        owner_token: str,
        lease_seconds: int,
        now: str | None = None,
    ) -> LocalOutboxClaim | None: ...

    def record_outbox_receipt(
        self,
        claim: LocalOutboxClaim,
        *,
        status: Literal["delivered", "failed", "unknown"],
        evidence_digest: str,
        now: str | None = None,
    ) -> LocalOutboxEvent: ...

    def recover_outbox(
        self,
        process_token_for: Callable[[int], str | None] | None = None,
        *,
        now: str | None = None,
    ) -> int: ...

    def resolve_recovery(
        self,
        recovery_case_id: str,
        *,
        outcome: Literal["completed", "failed", "delivered"],
        evidence_digest: str,
        now: str | None = None,
    ) -> LocalRecoveryResolution: ...

    def reconcile_recovery(self, job_id: str, *, now: str | None = None) -> LocalJob: ...

    def quarantine(
        self,
        work: LocalClaimedWork,
        *,
        evidence_digest: str,
        now: str | None = None,
    ) -> LocalJob: ...

    def destroy_terminal(self, job_id: str) -> None: ...

    def schedule_once(
        self,
        *,
        slot_key: str,
        schedule_digest: str,
        idempotency_key: str,
        payload: dict[str, Any],
        now: str | None = None,
    ) -> tuple[LocalJob, bool]: ...
