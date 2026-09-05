"""Prepare/apply service for atomic Memory v2 promotion."""

from __future__ import annotations

import datetime as dt
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from zekam.application.memory_service import PromotionGate
from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation
from zekam.domain.memory_promotion import (
    LockedMemoryPromotion,
    MemoryPromotionPlan,
    MemoryPromotionReceipt,
    MemoryReviewDecision,
    candidate_snapshot_digest,
)
from zekam.domain.security import Authorization


class AuthorizationConsumeResult(Protocol):
    consumed: bool
    reason: str


class MemoryPromotionStore(Protocol):
    realm_id: UUID

    def unit_of_work(self) -> AbstractContextManager[None]: ...

    def snapshot(
        self,
        *,
        candidate_id: str,
        logical_memory_id: str,
        expected_predecessor_storage_id: UUID | None,
        lock: bool = False,
    ) -> LockedMemoryPromotion: ...

    def persist(
        self,
        locked: LockedMemoryPromotion,
        plan: MemoryPromotionPlan,
        *,
        authorization_id: UUID,
        now: dt.datetime,
    ) -> MemoryPromotionReceipt: ...

    def store_receipt(
        self,
        receipt: MemoryPromotionReceipt,
        *,
        candidate_id: UUID,
        predecessor_id: UUID | None,
        effect_digest: str,
    ) -> None: ...

    def assert_constraints(self) -> None: ...


class MemoryPromotionAuthorizationStore(Protocol):
    def get(self, authorization_id: UUID) -> Authorization: ...

    def consume(
        self,
        authorization_id: UUID,
        *,
        effect_digest: str,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> AuthorizationConsumeResult: ...


class MemoryPromotionAuditStore(Protocol):
    def record(
        self,
        *,
        action: str,
        subject_type: str,
        subject_id: str,
        decision: str,
        reason: str,
        evidence: dict[str, Any] | None = None,
        actor_id: UUID | None = None,
        authorization_id: UUID | None = None,
        correlation_id: UUID | None = None,
        now: dt.datetime | None = None,
    ) -> UUID: ...


MEMORY_PROMOTION_CONSUMER = "memory-promotion/v2"


@dataclass(frozen=True, slots=True)
class MemoryPromotionService:
    repository: MemoryPromotionStore
    authorizations: MemoryPromotionAuthorizationStore
    audit: MemoryPromotionAuditStore
    gate: PromotionGate = field(default_factory=PromotionGate)

    def prepare(
        self,
        *,
        candidate_id: str,
        logical_memory_id: str,
        predecessor_storage_id: UUID | None,
        review: MemoryReviewDecision,
        embedding_profile_digest: str,
        external_target_ref: str,
        now: dt.datetime | None = None,
        _lock: bool = False,
    ) -> MemoryPromotionPlan:
        """Salt okunur deterministic plan; authorization veya mutation uretmez."""

        moment = now or dt.datetime.now(dt.UTC)
        locked = self.repository.snapshot(
            candidate_id=candidate_id,
            logical_memory_id=logical_memory_id,
            expected_predecessor_storage_id=predecessor_storage_id,
            lock=_lock,
        )
        allowed, reason = self.gate.evaluate(locked.candidate, review)  # type: ignore[arg-type]
        if not allowed:
            raise PolicyViolation(reason)
        next_revision = 1 if locked.predecessor is None else locked.predecessor.revision + 1
        return MemoryPromotionPlan(
            realm_id=self.repository.realm_id,
            candidate_storage_id=locked.candidate_storage_id,
            candidate_id=locked.candidate.candidate_id,
            candidate_digest=candidate_snapshot_digest(locked.candidate),
            logical_memory_id=logical_memory_id,
            predecessor_storage_id=locked.predecessor_storage_id,
            predecessor_digest=(
                None if locked.predecessor is None else locked.predecessor.record_digest
            ),
            next_revision=next_revision,
            review=review,
            evidence_digest=digest([item.as_dict() for item in locked.candidate.evidence]),
            embedding_profile_digest=embedding_profile_digest,
            external_target_ref=external_target_ref,
            prepared_at=moment,
        )

    def apply(
        self,
        plan: MemoryPromotionPlan,
        *,
        authorization_id: UUID,
        now: dt.datetime | None = None,
    ) -> MemoryPromotionReceipt:
        moment = now or dt.datetime.now(dt.UTC)
        with self.repository.unit_of_work():
            fresh = self.prepare(
                candidate_id=plan.candidate_id,
                logical_memory_id=plan.logical_memory_id,
                predecessor_storage_id=plan.predecessor_storage_id,
                review=plan.review,
                embedding_profile_digest=plan.embedding_profile_digest,
                external_target_ref=plan.external_target_ref,
                now=plan.prepared_at,
                _lock=True,
            )
            if fresh.plan_digest != plan.plan_digest:
                raise PolicyViolation("Memory promotion plan drift")
            authorization = self.authorizations.get(authorization_id)
            if (
                authorization.realm_id != plan.realm_id
                or authorization.plan_digest != plan.plan_digest
                or authorization.effect_digest != plan.effect_digest
                or tuple(sorted(authorization.scope.allowed_resources))
                != tuple(sorted(plan.resources))
                or authorization.scope.allowed_effects != ("database-write",)
            ):
                raise AuthorizationRequired("Memory promotion exact authorization binding yok")
            consumed = self.authorizations.consume(
                authorization_id,
                effect_digest=plan.effect_digest,
                consumed_by=MEMORY_PROMOTION_CONSUMER,
                now=moment,
            )
            if not consumed.consumed:
                raise AuthorizationRequired(f"Memory promotion authorization: {consumed.reason}")
            locked = self.repository.snapshot(
                candidate_id=plan.candidate_id,
                logical_memory_id=plan.logical_memory_id,
                expected_predecessor_storage_id=plan.predecessor_storage_id,
                lock=True,
            )
            receipt = self.repository.persist(
                locked,
                plan,
                authorization_id=authorization_id,
                now=moment,
            )
            self.audit.record(
                action="memory.promotion.applied",
                subject_type="memory-promotion",
                subject_id=plan.plan_digest,
                decision="allow",
                reason="transaction-committed",
                evidence={
                    "plan_digest": plan.plan_digest,
                    "result_digest": receipt.result_digest,
                    "record_id": str(receipt.record_storage_id),
                },
                actor_id=authorization.actor_id,
                authorization_id=authorization_id,
                now=moment,
            )
            self.repository.store_receipt(
                receipt,
                candidate_id=plan.candidate_storage_id,
                predecessor_id=plan.predecessor_storage_id,
                effect_digest=plan.effect_digest,
            )
            self.repository.assert_constraints()
            return receipt
