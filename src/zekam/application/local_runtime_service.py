"""Database-neutral production services for one local worker/outbox tick."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from zekam.application.local_runtime import (
    RUNTIME_OUTBOX_KINDS,
    LocalClaimedWork,
    LocalOutboxClaim,
    LocalOutboxEvent,
    LocalRuntimeStore,
    RecoverySweep,
    validate_outbox_kinds,
)
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed


@dataclass(frozen=True, slots=True)
class LocalEffectRequest:
    operation: str
    idempotency_key: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LocalEffectResult:
    status: Literal["completed", "failed", "unknown"]
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class LocalDeliveryResult:
    status: Literal["delivered", "failed", "unknown"]
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class LocalStartupRecovery:
    orphans: RecoverySweep
    expired: RecoverySweep
    recovered_outbox: int


class LocalEffectExecutor(Protocol):
    def __call__(self, request: LocalEffectRequest) -> LocalEffectResult: ...


class LocalOutboxPublisher(Protocol):
    def __call__(self, claim: LocalOutboxClaim, /) -> LocalDeliveryResult: ...


@dataclass(frozen=True, slots=True)
class LocalOutboxDispatcher:
    """Immutable exact kind-to-handler routing; registration grants no authority."""

    routes: tuple[tuple[str, LocalOutboxPublisher], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.routes, tuple) or not 1 <= len(self.routes) <= 64:
            raise ValidationFailed("Outbox routes 1..64 elemanli tuple olmali")
        for route in self.routes:
            if not isinstance(route, tuple) or len(route) != 2 or not callable(route[1]):
                raise ValidationFailed("Outbox route exact kind/callable pair olmali")
        validate_outbox_kinds(tuple(route[0] for route in self.routes))

    @property
    def supported_kinds(self) -> tuple[str, ...]:
        return tuple(kind for kind, _ in self.routes)

    def __call__(self, claim: LocalOutboxClaim) -> LocalDeliveryResult:
        if not isinstance(claim, LocalOutboxClaim) or not isinstance(claim.event, LocalOutboxEvent):
            raise ValidationFailed("Outbox dispatch typed claim ister")
        if claim.event.event_kind not in self.supported_kinds:
            raise PolicyViolation("Outbox consumer event kind desteklemiyor")
        if (
            claim.event.state != "claimed"
            or not isinstance(claim.event.payload, dict)
            or digest(claim.event.payload) != claim.event.payload_digest
        ):
            raise ValidationFailed("Outbox dispatch claim payload/state drift")
        for kind, handler in self.routes:
            if kind == claim.event.event_kind:
                result = handler(claim)
                if not isinstance(result, LocalDeliveryResult) or result.status not in {
                    "delivered",
                    "failed",
                    "unknown",
                }:
                    raise ValidationFailed("Outbox handler typed terminal result ister")
                if not isinstance(result.evidence_digest, str):
                    raise ValidationFailed("Outbox handler evidence digest metin olmali")
                parse_digest(result.evidence_digest)
                return result
        raise PolicyViolation("Outbox consumer event kind desteklemiyor")


class LocalRuntimeService:
    """Execute bounded ticks through the LocalRuntimeStore port only."""

    def __init__(
        self,
        store: LocalRuntimeStore,
        *,
        effect_executor: LocalEffectExecutor,
        outbox_publisher: LocalOutboxPublisher | None = None,
        outbox_dispatcher: LocalOutboxDispatcher | None = None,
    ) -> None:
        if (outbox_publisher is None) == (outbox_dispatcher is None):
            raise ValidationFailed("Outbox exact tek publisher veya dispatcher ister")
        if outbox_dispatcher is not None and not isinstance(
            outbox_dispatcher, LocalOutboxDispatcher
        ):
            raise ValidationFailed("Outbox dispatcher typed olmali")
        if outbox_publisher is not None:
            if not callable(outbox_publisher):
                raise ValidationFailed("Outbox publisher callable olmali")
            outbox_dispatcher = LocalOutboxDispatcher(
                tuple((kind, outbox_publisher) for kind in RUNTIME_OUTBOX_KINDS)
            )
        assert outbox_dispatcher is not None
        self._store = store
        self._effect_executor = effect_executor
        self._outbox_dispatcher = outbox_dispatcher

    def startup(
        self,
        process_token_for: Callable[[int], str | None],
    ) -> LocalStartupRecovery:
        """Fail closed before new work is claimed after every process start."""

        return LocalStartupRecovery(
            self._store.recover_orphans(process_token_for),
            self._store.recover_expired(),
            self._store.recover_outbox(process_token_for),
        )

    def startup_outbox(
        self,
        process_token_for: Callable[[int], str | None],
    ) -> int:
        """Recover only delivery claims so a full outbox can always be drained."""

        return self._store.recover_outbox(process_token_for)

    def run_worker_once(
        self,
        *,
        owner_id: str,
        owner_pid: int,
        owner_token: str,
        lease_seconds: int = 30,
    ) -> LocalClaimedWork | None:
        work = self._store.claim_next(
            owner_id=owner_id,
            owner_pid=owner_pid,
            owner_token=owner_token,
            lease_seconds=lease_seconds,
        )
        if work is None:
            return None
        operation = work.job.payload.get("operation")
        effect_payload = work.job.payload.get("effect")
        if not isinstance(operation, str) or not operation.strip():
            self._store.quarantine(
                work,
                evidence_digest=digest("invalid-operation"),
            )
            raise ValidationFailed("Local job operation metin olmali")
        if not isinstance(effect_payload, dict):
            self._store.quarantine(
                work,
                evidence_digest=digest("invalid-effect-payload"),
            )
            raise ValidationFailed("Local job effect object olmali")
        effect_digest = digest(effect_payload)
        idempotency_key = f"job:{work.job.id}:effect:{effect_digest}"
        claim, created = self._store.claim_effect(
            work,
            operation=operation,
            effect_digest=effect_digest,
            idempotency_key=idempotency_key,
        )
        if not created:
            raise ConcurrencyConflict("Persisted effect claim yeniden calistirilamaz")
        request = LocalEffectRequest(operation.strip(), idempotency_key, effect_payload)
        try:
            result = self._effect_executor(request)
        except Exception:
            result = LocalEffectResult("unknown", digest("effect-executor-exception"))
        self._store.record_receipt(
            claim,
            status=result.status,
            evidence_digest=result.evidence_digest,
        )
        if result.status == "unknown":
            self._store.finish(work, state="recovery-required")
        else:
            self._store.finish(
                work,
                state="completed" if result.status == "completed" else "failed",
                evidence_digest=result.evidence_digest,
            )
        return work

    def publish_outbox_once(
        self,
        *,
        owner_id: str,
        owner_pid: int,
        owner_token: str,
        lease_seconds: int = 30,
    ) -> LocalOutboxClaim | None:
        claim = self._store.claim_outbox(
            supported_kinds=self._outbox_dispatcher.supported_kinds,
            owner_id=owner_id,
            owner_pid=owner_pid,
            owner_token=owner_token,
            lease_seconds=lease_seconds,
        )
        if claim is None:
            return None
        if claim.event.event_kind not in self._outbox_dispatcher.supported_kinds:
            raise PolicyViolation("Outbox store unsupported kind claim dondurdu")
        try:
            result = self._outbox_dispatcher(claim)
        except Exception:
            result = LocalDeliveryResult("unknown", digest("outbox-publisher-exception"))
        self._store.record_outbox_receipt(
            claim,
            status=result.status,
            evidence_digest=result.evidence_digest,
        )
        return claim
