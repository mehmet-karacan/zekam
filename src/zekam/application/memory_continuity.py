"""Prepare/apply orchestration for immutable continuity receipts."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.security import Authorization
from zekam.domain.session_continuity import (
    CompactionReceipt,
    ContextOmissionReference,
    ContextSelectionReference,
    DigestReference,
    FreshnessDimension,
    SessionCloseReceipt,
    SessionHydrationReceipt,
)

_SAFE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class AuthorizationStore(Protocol):
    def get(self, authorization_id: UUID) -> Authorization: ...

    def consume(
        self,
        authorization_id: UUID,
        *,
        effect_digest: str,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> Any: ...


class MemoryContinuityStore(Protocol):
    connection: Any

    def store_hydration_receipt(
        self, receipt: SessionHydrationReceipt, *, idempotency_key: str
    ) -> bool: ...

    def store_close_receipt(
        self, receipt: SessionCloseReceipt, *, idempotency_key: str
    ) -> bool: ...

    def store_compaction_receipt(
        self, receipt: CompactionReceipt, *, idempotency_key: str
    ) -> bool: ...

    def read_session_snapshot(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
    ) -> SessionSnapshot: ...


class SessionSnapshot(Protocol):
    hydration_receipt_digest: str | None
    hydration_fresh: bool
    hydration_complete: bool
    open_gaps: tuple[Any, ...]

    @property
    def ready_for_mutation(self) -> bool: ...


class ContinuityReceiptKind(StrEnum):
    HYDRATION = "hydration"
    CLOSE = "close"
    COMPACTION = "compaction"


ContinuityReceipt = SessionHydrationReceipt | SessionCloseReceipt | CompactionReceipt


@dataclass(frozen=True, slots=True)
class HydrationPreparation:
    receipt_id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID
    session_id: str
    client_id: str
    plan_ref: str
    checkpoint_ref: str
    source_digest: str
    policy_digest: str
    migration_digest: str
    inventory_digest: str
    context_digest: str
    required_candidates: tuple[ContextSelectionReference, ...]
    optional_candidates: tuple[ContextSelectionReference, ...]
    known_omissions: tuple[ContextOmissionReference, ...]
    token_budget: int
    freshness: tuple[FreshnessDimension, ...]
    projection_refs: tuple[DigestReference, ...]
    hydration_event_digest: str
    idempotency_key: str
    created_at: dt.datetime


@dataclass(frozen=True, slots=True)
class ContinuityReceiptPlan:
    kind: ContinuityReceiptKind
    receipt: ContinuityReceipt
    receipt_digest: str
    idempotency_key: str
    resource: str
    source_digest: str
    policy_digest: str
    migration_digest: str
    context_digest: str
    effect_digest: str
    plan_digest: str
    grants_authority: bool = False

    @classmethod
    def create(
        cls,
        *,
        kind: ContinuityReceiptKind,
        receipt: ContinuityReceipt,
        receipt_digest: str,
        idempotency_key: str,
        source_digest: str,
        policy_digest: str,
        migration_digest: str,
        context_digest: str,
    ) -> ContinuityReceiptPlan:
        for value in (
            receipt_digest,
            source_digest,
            policy_digest,
            migration_digest,
            context_digest,
        ):
            parse_digest(value)
        key = _idempotency_key(idempotency_key)
        resource = f"continuity:{kind.value}:{receipt.receipt_id}"
        effect_digest = digest(
            {
                "effect": "database-write",
                "resource": resource,
                "receipt_digest": receipt_digest,
            }
        )
        draft = cls(
            kind,
            receipt,
            receipt_digest,
            key,
            resource,
            source_digest,
            policy_digest,
            migration_digest,
            context_digest,
            effect_digest,
            "",
            False,
        )
        return replace(draft, plan_digest=digest(draft.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-continuity-receipt-plan/v1",
            "kind": self.kind.value,
            "receipt_id": str(self.receipt.receipt_id),
            "receipt_digest": self.receipt_digest,
            "idempotency_key": self.idempotency_key,
            "resource": self.resource,
            "source_digest": self.source_digest,
            "policy_digest": self.policy_digest,
            "migration_digest": self.migration_digest,
            "context_digest": self.context_digest,
            "effect_digest": self.effect_digest,
            "grants_authority": False,
        }

    def assert_integrity(self) -> None:
        actual_receipt_digest = _receipt_digest(self.receipt)
        if actual_receipt_digest != self.receipt_digest:
            raise PolicyViolation("Continuity receipt plan body drift")
        if self.plan_digest != digest(self.body()):
            raise PolicyViolation("Continuity receipt plan digest mismatch")


@dataclass(frozen=True, slots=True)
class ContinuityApplyReceipt:
    kind: ContinuityReceiptKind
    receipt_id: UUID
    receipt_digest: str
    plan_digest: str
    authorization_id: UUID
    created: bool
    applied_at: dt.datetime
    grants_authority: bool = False

    @property
    def result_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-continuity-apply-receipt/v1",
                "kind": self.kind.value,
                "receipt_id": str(self.receipt_id),
                "receipt_digest": self.receipt_digest,
                "plan_digest": self.plan_digest,
                "authorization_id": str(self.authorization_id),
                "created": self.created,
                "applied_at": self.applied_at,
                "grants_authority": False,
            }
        )


@dataclass(frozen=True, slots=True)
class MemoryContinuityService:
    repository: MemoryContinuityStore
    authorizations: AuthorizationStore

    def inspect_session(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
    ) -> SessionSnapshot:
        """Read canonical hydration/gap status without producing authority."""

        return self.repository.read_session_snapshot(
            project_id=project_id,
            work_item_id=work_item_id,
            run_id=run_id,
            session_id=session_id,
            client_id=client_id,
        )

    def assert_mutating_admission(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
    ) -> SessionSnapshot:
        """A client may mutate only with fresh hydration and no open recovery gap."""

        snapshot = self.inspect_session(
            project_id=project_id,
            work_item_id=work_item_id,
            run_id=run_id,
            session_id=session_id,
            client_id=client_id,
        )
        if not snapshot.ready_for_mutation:
            dimensions: list[str] = []
            if snapshot.hydration_receipt_digest is None:
                dimensions.append("hydration-missing")
            elif not snapshot.hydration_fresh:
                dimensions.append("hydration-stale")
            elif not snapshot.hydration_complete:
                dimensions.append("hydration-incomplete")
            if snapshot.open_gaps:
                dimensions.append(f"open-gaps:{len(snapshot.open_gaps)}")
            raise PolicyViolation(
                "Continuity mutating admission reddedildi: " + ",".join(dimensions)
            )
        return snapshot

    def prepare_hydration(self, request: HydrationPreparation) -> ContinuityReceiptPlan:
        """Select required context first; never silently truncate it."""

        if request.token_budget < 1:
            raise ValidationFailed("Hydration token budget pozitif olmali")
        required = tuple(sorted(request.required_candidates, key=lambda item: item.ref))
        optional = tuple(sorted(request.optional_candidates, key=lambda item: item.ref))
        required_tokens = sum(item.token_count for item in required)
        if required_tokens > request.token_budget:
            raise PolicyViolation("Required continuity set token budget'e sigmiyor")
        remaining = request.token_budget - required_tokens
        selected_optional: list[ContextSelectionReference] = []
        budget_omissions: list[ContextOmissionReference] = []
        for item in optional:
            if item.token_count <= remaining:
                selected_optional.append(item)
                remaining -= item.token_count
            else:
                budget_omissions.append(
                    ContextOmissionReference(item.ref, "token-budget", required=False)
                )
        omissions = tuple(
            sorted(request.known_omissions + tuple(budget_omissions), key=lambda item: item.ref)
        )
        if any(item.required for item in omissions):
            raise PolicyViolation("Required continuity omission fail-closed")
        tokens_used = required_tokens + sum(item.token_count for item in selected_optional)
        receipt = SessionHydrationReceipt(
            request.receipt_id,
            request.realm_id,
            request.project_id,
            request.work_item_id,
            request.run_id,
            request.session_id,
            request.client_id,
            request.plan_ref,
            request.checkpoint_ref,
            request.source_digest,
            request.policy_digest,
            request.migration_digest,
            request.inventory_digest,
            request.context_digest,
            required,
            tuple(selected_optional),
            omissions,
            request.token_budget,
            tokens_used,
            request.freshness,
            request.projection_refs,
            request.hydration_event_digest,
            request.created_at,
            fresh=bool(request.freshness) and all(item.current for item in request.freshness),
            complete=True,
        )
        return ContinuityReceiptPlan.create(
            kind=ContinuityReceiptKind.HYDRATION,
            receipt=receipt,
            receipt_digest=receipt.receipt_digest,
            idempotency_key=request.idempotency_key,
            source_digest=request.source_digest,
            policy_digest=request.policy_digest,
            migration_digest=request.migration_digest,
            context_digest=request.context_digest,
        )

    def prepare_close(
        self, receipt: SessionCloseReceipt, *, idempotency_key: str
    ) -> ContinuityReceiptPlan:
        return ContinuityReceiptPlan.create(
            kind=ContinuityReceiptKind.CLOSE,
            receipt=receipt,
            receipt_digest=receipt.receipt_digest,
            idempotency_key=idempotency_key,
            source_digest=receipt.source_digest,
            policy_digest=receipt.policy_digest,
            migration_digest=receipt.migration_digest,
            context_digest=receipt.context_digest,
        )

    def prepare_compaction(
        self,
        receipt: CompactionReceipt,
        *,
        idempotency_key: str,
        source_digest: str,
        policy_digest: str,
        migration_digest: str,
        context_digest: str,
    ) -> ContinuityReceiptPlan:
        return ContinuityReceiptPlan.create(
            kind=ContinuityReceiptKind.COMPACTION,
            receipt=receipt,
            receipt_digest=receipt.receipt_digest,
            idempotency_key=idempotency_key,
            source_digest=source_digest,
            policy_digest=policy_digest,
            migration_digest=migration_digest,
            context_digest=context_digest,
        )

    def apply(
        self,
        plan: ContinuityReceiptPlan,
        *,
        authorization_id: UUID,
        current_source_digest: str,
        current_policy_digest: str,
        current_migration_digest: str,
        current_context_digest: str,
        now: dt.datetime | None = None,
    ) -> ContinuityApplyReceipt:
        moment = now or dt.datetime.now(dt.UTC)
        if moment.tzinfo is None:
            raise ValidationFailed("Continuity apply zamani timezone-aware olmali")
        plan.assert_integrity()
        if (
            current_source_digest != plan.source_digest
            or current_policy_digest != plan.policy_digest
            or current_migration_digest != plan.migration_digest
            or current_context_digest != plan.context_digest
        ):
            raise PolicyViolation("Continuity receipt apply binding drift; replan required")

        with self.repository.connection.transaction():
            authorization = self.authorizations.get(authorization_id)
            rejection = authorization.rejection_reason(moment)
            if (
                rejection is not None
                or authorization.realm_id != plan.receipt.realm_id
                or authorization.plan_digest != plan.plan_digest
                or authorization.effect_digest != plan.effect_digest
                or not authorization.scope.covers_effect("database-write")
                or not authorization.scope.covers_resource(plan.resource)
            ):
                raise AuthorizationRequired(
                    f"Continuity receipt exact authorization binding yok: "
                    f"{rejection or 'scope-mismatch'}"
                )
            consumed = self.authorizations.consume(
                authorization_id,
                effect_digest=plan.effect_digest,
                consumed_by="memory-continuity/v1",
                now=moment,
            )
            if not bool(getattr(consumed, "consumed", False)):
                reason = getattr(consumed, "reason", "unknown")
                raise AuthorizationRequired(f"Continuity authorization tuketilemedi: {reason}")
            created = self._store(plan)
        return ContinuityApplyReceipt(
            plan.kind,
            plan.receipt.receipt_id,
            plan.receipt_digest,
            plan.plan_digest,
            authorization_id,
            created,
            moment,
        )

    def _store(self, plan: ContinuityReceiptPlan) -> bool:
        if plan.kind is ContinuityReceiptKind.HYDRATION:
            if not isinstance(plan.receipt, SessionHydrationReceipt):
                raise PolicyViolation("Hydration plan receipt type mismatch")
            return self.repository.store_hydration_receipt(
                plan.receipt, idempotency_key=plan.idempotency_key
            )
        if plan.kind is ContinuityReceiptKind.CLOSE:
            if not isinstance(plan.receipt, SessionCloseReceipt):
                raise PolicyViolation("Close plan receipt type mismatch")
            return self.repository.store_close_receipt(
                plan.receipt, idempotency_key=plan.idempotency_key
            )
        if not isinstance(plan.receipt, CompactionReceipt):
            raise PolicyViolation("Compaction plan receipt type mismatch")
        return self.repository.store_compaction_receipt(
            plan.receipt, idempotency_key=plan.idempotency_key
        )


def _receipt_digest(receipt: ContinuityReceipt) -> str:
    return receipt.receipt_digest


def _idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not _SAFE_IDEMPOTENCY_KEY.fullmatch(normalized):
        raise ValidationFailed("Continuity idempotency key gecersiz")
    return normalized
