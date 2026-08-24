"""Execution Plane alan modeli: job, attempt, lease, claim, receipt.

Ayrimlar:

```text
Job      : yapilacak is (mutable lifecycle head)
Attempt  : bir yurutme denemesi (append-only)
Lease    : gecici sahiplik; yetki degildir
Claim    : dis effect baslatma niyeti (append-only)
Receipt  : effect'in terminal sonucu (append-only)
```

Claim, effect'in **gerceklestigini kanitlamaz**; yalnizca baslatma niyetini
kanitlar. Terminal receipt olmadan claim varsa durum `recovery-required`'dir ve
sessiz retry yasaktir.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7
from zekam.domain.resources import ResourceRequest, lock_order


class JobState(StrEnum):
    """Job yasam dongusu."""

    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    RECOVERY_REQUIRED = "recovery-required"
    CANCELLED = "cancelled"


#: Terminal job durumlari.
TERMINAL_JOB_STATES: frozenset[JobState] = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
)


class JobKind(StrEnum):
    """Job'in ne yaptigi."""

    READ_ONLY = "read-only"
    MUTATION = "mutation"
    PROVIDER_CALL = "provider-call"
    VERIFICATION = "verification"


class AttemptOutcome(StrEnum):
    """Bir denemenin sonucu."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"
    RECOVERY_REQUIRED = "recovery-required"


class ReceiptStatus(StrEnum):
    """Terminal receipt durumu."""

    COMPLETED = "completed"
    FAILED = "failed"


class FailureCategory(StrEnum):
    """Sanitize edilmis hata sinifi. Ham hata metni tasinmaz."""

    TIMEOUT = "timeout"
    POLICY = "policy"
    AUTHORIZATION = "authorization"
    VALIDATION = "validation"
    CONFLICT = "conflict"
    ADAPTER = "adapter"
    PROVIDER = "provider"
    INTERNAL = "internal"
    CANCELLED = "cancelled"


class RouteKind(StrEnum):
    """Route planner karari."""

    DIRECT = "direct"
    SINGLE = "single"
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    BLOCKED = "blocked"
    RECOVERY = "recovery"


def new_owner_token() -> str:
    """Lease sahipligi icin tek kullanimlik token uretir.

    Token'in kendisi hicbir yere yazilmaz; yalnizca digest'i saklanir.
    """
    return secrets.token_urlsafe(32)


def owner_digest(token: str) -> str:
    """Owner token'in saklanabilir digest'i."""
    if not token:
        raise ValidationFailed("Owner token bos olamaz")
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Job:
    """Kuyruktaki tek bir is."""

    id: UUID
    realm_id: UUID
    project_id: UUID
    kind: JobKind
    state: JobState
    idempotency_key: str
    priority: int = 100
    attempt_count: int = 0
    max_attempts: int = 3
    fencing_token: int = 0
    required_capabilities: tuple[str, ...] = ()
    resources: tuple[ResourceRequest, ...] = ()
    work_item_id: UUID | None = None
    plan_id: UUID | None = None
    step_id: str | None = None
    assignment_id: UUID | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    available_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValidationFailed("Idempotency key bos olamaz")
        if self.max_attempts < 1:
            raise ValidationFailed("max_attempts en az 1 olmali")
        if self.attempt_count < 0:
            raise ValidationFailed("attempt_count negatif olamaz")
        if self.fencing_token < 0:
            raise ValidationFailed("fencing_token negatif olamaz")

    @classmethod
    def create(
        cls,
        *,
        realm_id: UUID,
        project_id: UUID,
        kind: JobKind,
        idempotency_key: str,
        resources: tuple[ResourceRequest, ...] = (),
        required_capabilities: tuple[str, ...] = (),
        priority: int = 100,
        max_attempts: int = 3,
        work_item_id: UUID | None = None,
        plan_id: UUID | None = None,
        step_id: str | None = None,
        assignment_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
        available_at: dt.datetime | None = None,
        now: dt.datetime | None = None,
    ) -> Job:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=realm_id,
            project_id=project_id,
            kind=kind,
            state=JobState.READY,
            idempotency_key=idempotency_key,
            priority=priority,
            max_attempts=max_attempts,
            required_capabilities=tuple(sorted(required_capabilities)),
            resources=lock_order(resources),
            work_item_id=work_item_id,
            plan_id=plan_id,
            step_id=step_id,
            assignment_id=assignment_id,
            payload=dict(payload or {}),
            available_at=available_at or moment,
            created_at=moment,
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_JOB_STATES

    @property
    def write_resources(self) -> tuple[ResourceRequest, ...]:
        return tuple(request for request in self.resources if request.is_write)

    @property
    def produces_effect(self) -> bool:
        """Read-only olmayan her job dis dunyaya dokunabilir."""
        return self.kind is not JobKind.READ_ONLY

    def has_attempts_left(self) -> bool:
        return self.attempt_count < self.max_attempts

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "kind": self.kind.value,
            "state": self.state.value,
            "idempotency_key": self.idempotency_key,
            "priority": self.priority,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "fencing_token": self.fencing_token,
            "required_capabilities": list(self.required_capabilities),
            "resources": [request.as_dict() for request in self.resources],
            "work_item_id": None if self.work_item_id is None else str(self.work_item_id),
            "step_id": self.step_id,
            "assignment_id": (None if self.assignment_id is None else str(self.assignment_id)),
        }


@dataclass(frozen=True, slots=True)
class Lease:
    """Bir job uzerindeki gecici sahiplik.

    Lease **yetki degildir**. Yalnizca "su an bu isi kim yurutuyor" sorusunu
    yanitlar ve fencing token ile eskimis sahibin sonuc yayinlamasini engeller.
    """

    id: UUID
    realm_id: UUID
    job_id: UUID
    attempt_id: UUID
    owner_digest: str
    fencing_token: int
    expires_at: dt.datetime
    heartbeat_at: dt.datetime
    worker_label: str

    def __post_init__(self) -> None:
        parse_digest(self.owner_digest)
        if self.fencing_token < 1:
            raise ValidationFailed("Fencing token 1'den kucuk olamaz")
        if not self.worker_label.strip():
            raise ValidationFailed("Worker etiketi bos olamaz")

    def is_valid_at(self, moment: dt.datetime) -> bool:
        return self.expires_at > moment

    def matches(self, *, token: str, fencing_token: int) -> bool:
        """Sahip token'i ve fencing token birlikte eslesmeli."""
        return owner_digest(token) == self.owner_digest and fencing_token == self.fencing_token

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "job_id": str(self.job_id),
            "attempt_id": str(self.attempt_id),
            "fencing_token": self.fencing_token,
            "worker_label": self.worker_label,
            "expires_at": self.expires_at,
            "heartbeat_at": self.heartbeat_at,
            # Owner token'in kendisi hicbir zaman raporlanmaz.
            "owner_digest": self.owner_digest,
        }


@dataclass(frozen=True, slots=True)
class EffectClaim:
    """Dis effect baslatma niyeti. Append-only.

    Claim, effect'in gerceklestigini **kanitlamaz**.
    """

    id: UUID
    realm_id: UUID
    job_id: UUID
    attempt_id: UUID
    operation: str
    effect_digest: str
    authorization_digest: str
    idempotency_key: str
    resources: tuple[ResourceRequest, ...]
    execution_identity: str
    fencing_token: int
    adapter_digest: str
    claimed_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        parse_digest(self.effect_digest)
        parse_digest(self.authorization_digest)
        parse_digest(self.adapter_digest)
        if not self.operation.strip():
            raise ValidationFailed("Claim operasyonu bos olamaz")
        if not self.idempotency_key.strip():
            raise ValidationFailed("Claim idempotency key bos olamaz")

    @classmethod
    def create(
        cls,
        *,
        realm_id: UUID,
        job_id: UUID,
        attempt_id: UUID,
        operation: str,
        effect_digest: str,
        authorization_digest: str,
        idempotency_key: str,
        resources: tuple[ResourceRequest, ...],
        execution_identity: str,
        fencing_token: int,
        adapter_digest: str,
        now: dt.datetime | None = None,
    ) -> EffectClaim:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=realm_id,
            job_id=job_id,
            attempt_id=attempt_id,
            operation=operation,
            effect_digest=effect_digest,
            authorization_digest=authorization_digest,
            idempotency_key=idempotency_key,
            resources=lock_order(resources),
            execution_identity=execution_identity,
            fencing_token=fencing_token,
            adapter_digest=adapter_digest,
            claimed_at=moment,
        )

    def body(self) -> dict[str, Any]:
        return {
            "job_id": str(self.job_id),
            "operation": self.operation,
            "effect_digest": self.effect_digest,
            "authorization_digest": self.authorization_digest,
            "idempotency_key": self.idempotency_key,
            "resources": [request.as_dict() for request in self.resources],
            "execution_identity": self.execution_identity,
            "fencing_token": self.fencing_token,
            "adapter_digest": self.adapter_digest,
        }

    @property
    def claim_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"id": str(self.id), "claim_digest": self.claim_digest}


@dataclass(frozen=True, slots=True)
class EffectReceipt:
    """Effect'in terminal sonucu. Append-only.

    Receipt secret veya ham saglayici yaniti tasimaz; yalnizca digest ve
    sanitize edilmis olcumler tasir.
    """

    id: UUID
    realm_id: UUID
    claim_id: UUID
    status: ReceiptStatus
    result_digest: str | None = None
    failure_category: FailureCategory | None = None
    failure_digest: str | None = None
    adapter_evidence_digest: str | None = None
    token_count: int = 0
    cost_micros: int = 0
    latency_ms: int = 0
    completed_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if self.status is ReceiptStatus.COMPLETED:
            if not self.result_digest:
                raise ValidationFailed("Completed receipt result digest tasimali")
            parse_digest(self.result_digest)
            if self.failure_category is not None:
                raise ValidationFailed("Completed receipt failure kategorisi tasiyamaz")
        else:
            if self.failure_category is None:
                raise ValidationFailed("Failed receipt failure kategorisi tasimali")
            if self.result_digest is not None:
                raise ValidationFailed("Failed receipt result digest tasiyamaz")
        if self.token_count < 0 or self.cost_micros < 0 or self.latency_ms < 0:
            raise ValidationFailed("Olcumler negatif olamaz")

    @classmethod
    def completed(
        cls,
        *,
        realm_id: UUID,
        claim: EffectClaim,
        result_digest: str,
        adapter_evidence_digest: str | None = None,
        token_count: int = 0,
        cost_micros: int = 0,
        latency_ms: int = 0,
        now: dt.datetime | None = None,
    ) -> EffectReceipt:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=realm_id,
            claim_id=claim.id,
            status=ReceiptStatus.COMPLETED,
            result_digest=result_digest,
            adapter_evidence_digest=adapter_evidence_digest,
            token_count=token_count,
            cost_micros=cost_micros,
            latency_ms=latency_ms,
            completed_at=moment,
        )

    @classmethod
    def failed(
        cls,
        *,
        realm_id: UUID,
        claim: EffectClaim,
        category: FailureCategory,
        failure_digest: str | None = None,
        adapter_evidence_digest: str | None = None,
        latency_ms: int = 0,
        now: dt.datetime | None = None,
    ) -> EffectReceipt:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=realm_id,
            claim_id=claim.id,
            status=ReceiptStatus.FAILED,
            failure_category=category,
            failure_digest=failure_digest,
            adapter_evidence_digest=adapter_evidence_digest,
            latency_ms=latency_ms,
            completed_at=moment,
        )

    @property
    def is_terminal(self) -> bool:
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "claim_id": str(self.claim_id),
            "status": self.status.value,
            "result_digest": self.result_digest,
            "failure_category": (
                None if self.failure_category is None else self.failure_category.value
            ),
            "failure_digest": self.failure_digest,
            "adapter_evidence_digest": self.adapter_evidence_digest,
            "token_count": self.token_count,
            "cost_micros": self.cost_micros,
            "latency_ms": self.latency_ms,
            "completed_at": self.completed_at,
        }


class RecoveryOutcome(StrEnum):
    """Recovery degerlendirmesinin sonucu."""

    NOTHING_TO_RECOVER = "nothing-to-recover"
    RECONCILED_COMPLETED = "reconciled-completed"
    RECONCILED_FAILED = "reconciled-failed"
    RECOVERY_REQUIRED = "recovery-required"
    BLOCKED_EFFECT_UNCERTAIN = "blocked-effect-uncertain"


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    """Claim/receipt ledgerinden turetilen recovery karari."""

    outcome: RecoveryOutcome
    claim_id: UUID | None
    reason: str
    silent_retry_allowed: bool = False
    next_safe_action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "claim_id": None if self.claim_id is None else str(self.claim_id),
            "reason": self.reason,
            "silent_retry_allowed": self.silent_retry_allowed,
            "next_safe_action": self.next_safe_action,
        }


@dataclass(frozen=True, slots=True)
class ReconciledCompletionRequest:
    """Bağımsız adapter kanıtıyla kapatılacak exact recovery kimliği.

    Bu nesne effect'in gerçekleştiğini iddia etmez. Caller, result ve adapter
    evidence digest'lerini bağımsız olarak üretir; repository bunları immutable
    claim, attempt, fence ve expired lease kayıtlarıyla yeniden eşleştirir.
    """

    job_id: UUID
    attempt_id: UUID
    claim_id: UUID
    fencing_token: int
    claim_digest: str
    effect_digest: str
    authorization_digest: str
    result_digest: str
    adapter_evidence_digest: str

    def __post_init__(self) -> None:
        if self.fencing_token < 1:
            raise ValidationFailed("Recovery fencing token pozitif olmali")
        for value in (
            self.claim_digest,
            self.effect_digest,
            self.authorization_digest,
            self.result_digest,
            self.adapter_evidence_digest,
        ):
            parse_digest(value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": str(self.job_id),
            "attempt_id": str(self.attempt_id),
            "claim_id": str(self.claim_id),
            "fencing_token": self.fencing_token,
            "claim_digest": self.claim_digest,
            "effect_digest": self.effect_digest,
            "authorization_digest": self.authorization_digest,
            "result_digest": self.result_digest,
            "adapter_evidence_digest": self.adapter_evidence_digest,
        }


@dataclass(frozen=True, slots=True)
class ReconciledFailureRequest:
    """Mevcut failed receipt ile yarim kalmis runtime job'ini kapatma kimligi."""

    job_id: UUID
    attempt_id: UUID
    claim_id: UUID
    receipt_id: UUID
    fencing_token: int
    claim_digest: str
    effect_digest: str
    authorization_digest: str
    failure_digest: str

    def __post_init__(self) -> None:
        if self.fencing_token < 1:
            raise ValidationFailed("Failure reconciliation fencing token pozitif olmali")
        for value in (
            self.claim_digest,
            self.effect_digest,
            self.authorization_digest,
            self.failure_digest,
        ):
            parse_digest(value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": str(self.job_id),
            "attempt_id": str(self.attempt_id),
            "claim_id": str(self.claim_id),
            "receipt_id": str(self.receipt_id),
            "fencing_token": self.fencing_token,
            "claim_digest": self.claim_digest,
            "effect_digest": self.effect_digest,
            "authorization_digest": self.authorization_digest,
            "failure_digest": self.failure_digest,
        }


def assess_recovery(
    *,
    claim: EffectClaim | None,
    receipt: EffectReceipt | None,
    adapter_evidence: ReceiptStatus | None = None,
) -> RecoveryAssessment:
    """Claim ve receipt durumundan recovery karari uretir.

    `adapter_evidence`, dis dunyanin gercekten degisip degismedigini soyleyen
    bagimsiz kanittir. Kanit yoksa karar `recovery-required`'dir ve sessiz retry
    yasaktir.
    """
    if claim is None:
        return RecoveryAssessment(
            outcome=RecoveryOutcome.NOTHING_TO_RECOVER,
            claim_id=None,
            reason="claim-yok",
            silent_retry_allowed=True,
            next_safe_action="Is yeniden kuyruga alinabilir",
        )
    if receipt is not None:
        outcome = (
            RecoveryOutcome.RECONCILED_COMPLETED
            if receipt.status is ReceiptStatus.COMPLETED
            else RecoveryOutcome.RECONCILED_FAILED
        )
        return RecoveryAssessment(
            outcome=outcome,
            claim_id=claim.id,
            reason="terminal-receipt-var",
            silent_retry_allowed=False,
            next_safe_action="Mevcut receipt kanonik sonuctur; tekrar yurutme",
        )
    if adapter_evidence is ReceiptStatus.COMPLETED:
        return RecoveryAssessment(
            outcome=RecoveryOutcome.RECONCILED_COMPLETED,
            claim_id=claim.id,
            reason="adapter-effect-gerceklesmis",
            silent_retry_allowed=False,
            next_safe_action="Kanonik receipt uret ve isi kapat",
        )
    if adapter_evidence is ReceiptStatus.FAILED:
        return RecoveryAssessment(
            outcome=RecoveryOutcome.RECONCILED_FAILED,
            claim_id=claim.id,
            reason="adapter-effect-gerceklesmemis",
            silent_retry_allowed=False,
            next_safe_action="Gozden gecirilmis yeni recovery plani hazirla",
        )
    return RecoveryAssessment(
        outcome=RecoveryOutcome.RECOVERY_REQUIRED,
        claim_id=claim.id,
        reason="claim-var-receipt-yok",
        silent_retry_allowed=False,
        next_safe_action="Adapter reconciliation yap; sessiz retry yasak",
    )


def assert_no_silent_retry(assessment: RecoveryAssessment) -> None:
    """Sessiz retry girisimini reddeder."""
    if not assessment.silent_retry_allowed:
        raise PolicyViolation(
            f"Sessiz retry yasak: {assessment.outcome.value} ({assessment.reason})"
        )
