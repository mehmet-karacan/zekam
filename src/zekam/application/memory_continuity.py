"""Prepare/apply orchestration for immutable continuity receipts."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from zekam.application.continuity_projection import ProjectionReleaseSnapshot
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.security import Authorization
from zekam.domain.session_continuity import (
    AUTO_HYDRATION_CLASSIFICATIONS,
    CompactionReceipt,
    ContextOmissionReference,
    ContextSelectionReference,
    DigestReference,
    FreshnessDimension,
    HydrationInventorySnapshot,
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

    def read_projection_release_snapshot(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
    ) -> ProjectionReleaseSnapshot: ...

    def read_hydration_inventory(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
    ) -> HydrationInventorySnapshot: ...


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
    token_budget: int
    idempotency_key: str
    created_at: dt.datetime
    # Legacy receipt-shaped hints remain readable during the CLI transition,
    # but canonical preparation never treats them as current authority.
    plan_ref: str | None = None
    checkpoint_ref: str | None = None
    source_digest: str | None = None
    policy_digest: str | None = None
    migration_digest: str | None = None
    inventory_digest: str | None = None
    context_digest: str | None = None
    required_candidates: tuple[ContextSelectionReference, ...] = ()
    optional_candidates: tuple[ContextSelectionReference, ...] = ()
    known_omissions: tuple[ContextOmissionReference, ...] = ()
    freshness: tuple[FreshnessDimension, ...] = ()
    projection_refs: tuple[DigestReference, ...] = ()
    hydration_event_digest: str | None = None


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
    release_snapshot_digest: str | None = None
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
        release_snapshot_digest: str | None = None,
    ) -> ContinuityReceiptPlan:
        for value in (
            receipt_digest,
            source_digest,
            policy_digest,
            migration_digest,
            context_digest,
        ):
            parse_digest(value)
        if release_snapshot_digest is not None:
            parse_digest(release_snapshot_digest)
        if kind is ContinuityReceiptKind.CLOSE and release_snapshot_digest is None:
            raise PolicyViolation("Close plan exact projection release snapshot ister")
        if kind is not ContinuityReceiptKind.CLOSE and release_snapshot_digest is not None:
            raise PolicyViolation("Yalniz close plan projection release snapshot tasiyabilir")
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
            kind=kind,
            receipt=receipt,
            receipt_digest=receipt_digest,
            idempotency_key=key,
            resource=resource,
            source_digest=source_digest,
            policy_digest=policy_digest,
            migration_digest=migration_digest,
            context_digest=context_digest,
            effect_digest=effect_digest,
            plan_digest="",
            release_snapshot_digest=release_snapshot_digest,
            grants_authority=False,
        )
        return replace(draft, plan_digest=digest(draft.body()))

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
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
        if self.release_snapshot_digest is not None:
            body["schema"] = "zekam-continuity-receipt-plan/v2"
            body["release_snapshot_digest"] = self.release_snapshot_digest
        return body

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
        """Build hydration only from the current canonical PostgreSQL inventory."""

        inventory = self._hydration_inventory(
            project_id=request.project_id,
            work_item_id=request.work_item_id,
            run_id=request.run_id,
            session_id=request.session_id,
            client_id=request.client_id,
        )
        return self.prepare_from_inventory(request, inventory)

    def prepare_from_inventory(
        self,
        request: HydrationPreparation,
        inventory: HydrationInventorySnapshot,
    ) -> ContinuityReceiptPlan:
        """Reuse the required-first bounded selector against an immutable inventory."""

        if request.token_budget < 1:
            raise ValidationFailed("Hydration token budget pozitif olmali")
        expected_identity = (
            request.realm_id,
            request.project_id,
            request.work_item_id,
            request.run_id,
            request.session_id,
            request.client_id,
        )
        actual_identity = (
            inventory.realm_id,
            inventory.project_id,
            inventory.work_item_id,
            inventory.run_id,
            inventory.session_id,
            inventory.client_id,
        )
        if actual_identity != expected_identity:
            raise PolicyViolation("Hydration inventory exact identity binding drift")
        legacy_bindings = (
            (request.plan_ref, inventory.plan_ref),
            (request.checkpoint_ref, inventory.checkpoint_ref),
            (request.source_digest, inventory.source_digest),
            (request.policy_digest, inventory.policy_digest),
            (request.migration_digest, inventory.migration_digest),
            (request.inventory_digest, inventory.inventory_digest),
            (request.context_digest, inventory.context_digest),
            (request.hydration_event_digest, inventory.hydration_event_digest),
        )
        if any(
            provided is not None and provided != current for provided, current in legacy_bindings
        ):
            raise PolicyViolation("Hydration input canonical inventory ile stale; replan required")

        forbidden_required = tuple(
            item
            for item in inventory.entries
            if item.required and item.classification not in AUTO_HYDRATION_CLASSIFICATIONS
        )
        if forbidden_required:
            raise PolicyViolation("Required hydration classification policy tarafindan reddedildi")
        required = tuple(
            sorted(
                (
                    item.selection
                    for item in inventory.entries
                    if item.required and item.classification in AUTO_HYDRATION_CLASSIFICATIONS
                ),
                key=lambda item: item.ref,
            )
        )
        optional = tuple(
            sorted(
                (
                    item.selection
                    for item in inventory.entries
                    if not item.required and item.classification in AUTO_HYDRATION_CLASSIFICATIONS
                ),
                key=lambda item: item.ref,
            )
        )
        classification_omissions = tuple(
            ContextOmissionReference(item.ref, "classification-excluded", required=False)
            for item in inventory.entries
            if not item.required and item.classification not in AUTO_HYDRATION_CLASSIFICATIONS
        )
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
            sorted(
                inventory.known_omissions + classification_omissions + tuple(budget_omissions),
                key=lambda item: item.ref,
            )
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
            inventory.plan_ref,
            inventory.checkpoint_ref,
            inventory.source_digest,
            inventory.policy_digest,
            inventory.migration_digest,
            inventory.inventory_digest,
            inventory.context_digest,
            required,
            tuple(selected_optional),
            omissions,
            request.token_budget,
            tokens_used,
            inventory.freshness,
            inventory.projection_refs,
            inventory.hydration_event_digest,
            request.created_at,
            fresh=True,
            complete=True,
        )
        return ContinuityReceiptPlan.create(
            kind=ContinuityReceiptKind.HYDRATION,
            receipt=receipt,
            receipt_digest=receipt.receipt_digest,
            idempotency_key=request.idempotency_key,
            source_digest=inventory.source_digest,
            policy_digest=inventory.policy_digest,
            migration_digest=inventory.migration_digest,
            context_digest=inventory.context_digest,
        )

    def prepare_close(
        self, receipt: SessionCloseReceipt, *, idempotency_key: str
    ) -> ContinuityReceiptPlan:
        if receipt.status.value == "closed":
            raise PolicyViolation(
                "Closed Work raw continuity close ile kapatilamaz; projection-aware close gerekir"
            )
        snapshot = self._release_snapshot(receipt)
        snapshot.assert_release_ready(expected_source_digest=receipt.source_digest)
        return ContinuityReceiptPlan.create(
            kind=ContinuityReceiptKind.CLOSE,
            receipt=receipt,
            receipt_digest=receipt.receipt_digest,
            idempotency_key=idempotency_key,
            source_digest=receipt.source_digest,
            policy_digest=receipt.policy_digest,
            migration_digest=receipt.migration_digest,
            context_digest=receipt.context_digest,
            release_snapshot_digest=snapshot.snapshot_digest,
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
        now: dt.datetime | None = None,
    ) -> ContinuityApplyReceipt:
        moment = now or dt.datetime.now(dt.UTC)
        if moment.tzinfo is None:
            raise ValidationFailed("Continuity apply zamani timezone-aware olmali")
        plan.assert_integrity()

        with self.repository.connection.transaction():
            if plan.kind is ContinuityReceiptKind.HYDRATION:
                if not isinstance(plan.receipt, SessionHydrationReceipt):
                    raise PolicyViolation("Hydration plan receipt type mismatch")
                current_inventory = self._hydration_inventory(
                    project_id=plan.receipt.project_id,
                    work_item_id=plan.receipt.work_item_id,
                    run_id=plan.receipt.run_id,
                    session_id=plan.receipt.session_id,
                    client_id=plan.receipt.client_id,
                )
                self._assert_hydration_current(plan.receipt, current_inventory)
            elif plan.kind is ContinuityReceiptKind.CLOSE:
                if not isinstance(plan.receipt, SessionCloseReceipt):
                    raise PolicyViolation("Close plan receipt type mismatch")
                current_release = self._release_snapshot(plan.receipt)
                current_release.assert_release_ready(expected_source_digest=plan.source_digest)
                if current_release.snapshot_digest != plan.release_snapshot_digest:
                    raise PolicyViolation(
                        "Projection release snapshot apply sirasinda degisti; replan required"
                    )
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

    def _release_snapshot(self, receipt: SessionCloseReceipt) -> ProjectionReleaseSnapshot:
        reader = getattr(self.repository, "read_projection_release_snapshot", None)
        if not callable(reader):
            raise PolicyViolation("Projection release freshness gate repository'de yok")
        snapshot = reader(
            project_id=receipt.project_id,
            work_item_id=receipt.work_item_id,
            run_id=receipt.run_id,
            session_id=receipt.session_id,
            client_id=receipt.client_id,
        )
        if not isinstance(snapshot, ProjectionReleaseSnapshot):
            raise PolicyViolation("Projection release snapshot exact contract ile uyusmuyor")
        return snapshot

    def _hydration_inventory(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
    ) -> HydrationInventorySnapshot:
        reader = getattr(self.repository, "read_hydration_inventory", None)
        if not callable(reader):
            raise PolicyViolation("Canonical hydration inventory repository'de yok")
        snapshot = reader(
            project_id=project_id,
            work_item_id=work_item_id,
            run_id=run_id,
            session_id=session_id,
            client_id=client_id,
        )
        if not isinstance(snapshot, HydrationInventorySnapshot):
            raise PolicyViolation("Hydration inventory exact contract ile uyusmuyor")
        return snapshot

    @staticmethod
    def _assert_hydration_current(
        receipt: SessionHydrationReceipt,
        inventory: HydrationInventorySnapshot,
    ) -> None:
        identity = (
            receipt.realm_id,
            receipt.project_id,
            receipt.work_item_id,
            receipt.run_id,
            receipt.session_id,
            receipt.client_id,
        )
        current_identity = (
            inventory.realm_id,
            inventory.project_id,
            inventory.work_item_id,
            inventory.run_id,
            inventory.session_id,
            inventory.client_id,
        )
        bindings = (
            (receipt.source_digest, inventory.source_digest),
            (receipt.policy_digest, inventory.policy_digest),
            (receipt.migration_digest, inventory.migration_digest),
            (receipt.context_digest, inventory.context_digest),
            (receipt.inventory_digest, inventory.inventory_digest),
            (receipt.plan_ref, inventory.plan_ref),
            (receipt.checkpoint_ref, inventory.checkpoint_ref),
            (receipt.hydration_event_digest, inventory.hydration_event_digest),
        )
        if identity != current_identity or any(left != right for left, right in bindings):
            raise PolicyViolation("Hydration inventory apply sirasinda degisti; replan required")
        if (
            receipt.projection_refs != inventory.projection_refs
            or receipt.freshness != inventory.freshness
            or not receipt.fresh
            or not receipt.complete
        ):
            raise PolicyViolation(
                "Hydration projection/freshness apply sirasinda degisti; replan required"
            )
        current_entries = {item.ref: item for item in inventory.entries}
        for selected in receipt.required_selections + receipt.optional_selections:
            entry = current_entries.get(selected.ref)
            if (
                entry is None
                or entry.selection != selected
                or entry.classification not in AUTO_HYDRATION_CLASSIFICATIONS
            ):
                raise PolicyViolation("Hydration selected entry classification/provenance drift")
        required_refs = {
            item.ref
            for item in inventory.entries
            if item.required and item.classification in AUTO_HYDRATION_CLASSIFICATIONS
        }
        if required_refs != {item.ref for item in receipt.required_selections}:
            raise PolicyViolation("Hydration required inventory binding drift")


def _receipt_digest(receipt: ContinuityReceipt) -> str:
    return receipt.receipt_digest


def _idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not _SAFE_IDEMPOTENCY_KEY.fullmatch(normalized):
        raise ValidationFailed("Continuity idempotency key gecersiz")
    return normalized
