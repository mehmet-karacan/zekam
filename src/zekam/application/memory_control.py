"""Exact prepare/apply control plane for continuity repair and promotion effects."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.security import Authorization


class MemoryControlOperation(StrEnum):
    GAP_REPAIR = "gap-repair"
    CANDIDATE_PROMOTE = "candidate-promote"
    CLOSE_FINALIZE = "close-finalize"


@dataclass(frozen=True, slots=True)
class MemoryControlPlan:
    operation: MemoryControlOperation
    realm_id: UUID
    subject_id: str
    resource: str
    current_state: str
    current_digest: str
    evidence_ref: str
    evidence_digest: str
    target_state: str
    effect_digest: str
    plan_digest: str
    grants_authority: bool = False

    @classmethod
    def create(
        cls,
        *,
        operation: MemoryControlOperation,
        realm_id: UUID,
        subject_id: str,
        resource: str,
        current_state: str,
        current_digest: str,
        evidence_ref: str,
        evidence_digest: str,
        target_state: str,
    ) -> MemoryControlPlan:
        for value in (current_digest, evidence_digest):
            parse_digest(value)
        for value, label in (
            (subject_id, "subject"),
            (resource, "resource"),
            (current_state, "current state"),
            (evidence_ref, "evidence ref"),
            (target_state, "target state"),
        ):
            if not value or value != value.strip() or len(value) > 512:
                raise ValidationFailed(f"Memory control {label} bounded olmali")
            if "\\" in value or value.startswith("/") or ".." in value.split("/"):
                raise PolicyViolation(f"Memory control {label} portable olmali")
        effect_digest = digest(
            {
                "effect": "database-write",
                "resource": resource,
                "operation": operation.value,
                "subject_id": subject_id,
                "current_digest": current_digest,
                "evidence_digest": evidence_digest,
                "target_state": target_state,
            }
        )
        draft = cls(
            operation,
            realm_id,
            subject_id,
            resource,
            current_state,
            current_digest,
            evidence_ref,
            evidence_digest,
            target_state,
            effect_digest,
            "",
            False,
        )
        return replace(draft, plan_digest=digest(draft.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-control-plan/v1",
            "operation": self.operation.value,
            "realm_id": str(self.realm_id),
            "subject_id": self.subject_id,
            "resource": self.resource,
            "current_state": self.current_state,
            "current_digest": self.current_digest,
            "evidence_ref": self.evidence_ref,
            "evidence_digest": self.evidence_digest,
            "target_state": self.target_state,
            "effect_digest": self.effect_digest,
            "grants_authority": False,
        }

    def assert_integrity(self) -> None:
        if self.plan_digest != digest(self.body()):
            raise PolicyViolation("Memory control plan digest mismatch")


@dataclass(frozen=True, slots=True)
class MemoryControlReceipt:
    operation: MemoryControlOperation
    subject_id: str
    target_state: str
    plan_digest: str
    authorization_id: UUID
    created: bool
    completed_at: dt.datetime
    receipt_digest: str
    grants_authority: bool = False


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


class MemoryControlStore(Protocol):
    connection: Any
    realm_id: UUID

    def read_control_state(
        self, operation: MemoryControlOperation, subject_id: str
    ) -> tuple[str, str]: ...

    def apply_control(
        self,
        plan: MemoryControlPlan,
        *,
        authorization_id: UUID,
        completed_at: dt.datetime,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class MemoryControlService:
    repository: MemoryControlStore
    authorizations: AuthorizationStore

    def prepare(
        self,
        *,
        operation: MemoryControlOperation,
        subject_id: str,
        evidence_ref: str,
        evidence_digest: str,
        target_state: str,
    ) -> MemoryControlPlan:
        current_state, current_digest = self.repository.read_control_state(operation, subject_id)
        if operation is MemoryControlOperation.GAP_REPAIR:
            resource = f"continuity:gap:{subject_id}"
            allowed = current_state in {"open", "recovery-required"} and target_state == "resolved"
        elif operation is MemoryControlOperation.CANDIDATE_PROMOTE:
            resource = f"memory:compiler-candidate:{subject_id}"
            allowed = current_state == "reviewed" and target_state == "promoted"
        else:
            resource = f"continuity:lifecycle-outbox:{subject_id}"
            allowed = current_state in {"pending", "processing"} and target_state in {
                "completed",
                "failed",
                "recovery-required",
            }
        if not allowed:
            raise PolicyViolation("Memory control source/target state transition gecersiz")
        return MemoryControlPlan.create(
            operation=operation,
            realm_id=self.repository.realm_id,
            subject_id=subject_id,
            resource=resource,
            current_state=current_state,
            current_digest=current_digest,
            evidence_ref=evidence_ref,
            evidence_digest=evidence_digest,
            target_state=target_state,
        )

    def apply(
        self,
        plan: MemoryControlPlan,
        *,
        authorization_id: UUID,
        now: dt.datetime | None = None,
    ) -> MemoryControlReceipt:
        moment = now or dt.datetime.now(dt.UTC)
        plan.assert_integrity()
        if moment.tzinfo is None:
            raise ValidationFailed("Memory control apply zamani timezone-aware olmali")
        state, current_digest = self.repository.read_control_state(plan.operation, plan.subject_id)
        if state != plan.current_state or current_digest != plan.current_digest:
            raise PolicyViolation("Memory control apply state drift; replan required")
        authorization = self.authorizations.get(authorization_id)
        rejection = authorization.rejection_reason(moment)
        if (
            rejection is not None
            or authorization.realm_id != plan.realm_id
            or authorization.plan_digest != plan.plan_digest
            or authorization.effect_digest != plan.effect_digest
            or not authorization.scope.covers_effect("database-write")
            or not authorization.scope.covers_resource(plan.resource)
        ):
            raise AuthorizationRequired(
                f"Memory control exact authorization binding yok: {rejection or 'scope-mismatch'}"
            )
        with self.repository.connection.transaction():
            consumed_by = {
                MemoryControlOperation.CANDIDATE_PROMOTE: (
                    "memory-compiler-candidate-promotion/v1"
                ),
                MemoryControlOperation.GAP_REPAIR: "memory-continuity-gap-repair/v1",
                MemoryControlOperation.CLOSE_FINALIZE: "memory-continuity-close-finalize/v1",
            }[plan.operation]
            consumed = self.authorizations.consume(
                authorization_id,
                effect_digest=plan.effect_digest,
                consumed_by=consumed_by,
                now=moment,
            )
            if not bool(getattr(consumed, "consumed", False)):
                reason = getattr(consumed, "reason", "unknown")
                raise AuthorizationRequired(f"Memory control authorization tuketilemedi: {reason}")
            created = self.repository.apply_control(
                plan,
                authorization_id=authorization_id,
                completed_at=moment,
            )
        receipt_digest = digest(
            {
                "schema": "zekam-memory-control-receipt/v1",
                "operation": plan.operation.value,
                "subject_id": plan.subject_id,
                "target_state": plan.target_state,
                "plan_digest": plan.plan_digest,
                "authorization_id": str(authorization_id),
                "created": created,
                "completed_at": moment,
                "grants_authority": False,
            }
        )
        return MemoryControlReceipt(
            plan.operation,
            plan.subject_id,
            plan.target_state,
            plan.plan_digest,
            authorization_id,
            created,
            moment,
            receipt_digest,
        )
