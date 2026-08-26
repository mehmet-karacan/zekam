"""PostgreSQL adapter for exact Memory Control prepare/apply plans."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import UUID

from zekam.application.memory_control import MemoryControlOperation, MemoryControlPlan
from zekam.domain.canonical import digest
from zekam.domain.errors import NotFound
from zekam.infrastructure.postgres.memory_continuity_repository import (
    MemoryContinuityRepository,
)


@dataclass(frozen=True, slots=True)
class PostgresMemoryControlRepository:
    continuity: MemoryContinuityRepository

    @property
    def connection(self) -> object:
        return self.continuity.connection

    @property
    def realm_id(self) -> UUID:
        return self.continuity.realm_id

    def read_control_state(
        self, operation: MemoryControlOperation, subject_id: str
    ) -> tuple[str, str]:
        with self.continuity.connection.cursor() as cursor:
            if operation is MemoryControlOperation.GAP_REPAIR:
                try:
                    identifier = UUID(subject_id)
                except ValueError as exc:
                    raise NotFound("Continuity gap kimligi gecersiz") from exc
                cursor.execute(
                    "select state,evidence_digest from continuity.gap_recovery_reference"
                    " where realm_id=%s and id=%s",
                    (self.realm_id, identifier),
                )
            elif operation is MemoryControlOperation.CANDIDATE_PROMOTE:
                cursor.execute(
                    "select state,candidate_digest from memory.compiler_candidate"
                    " where realm_id=%s and logical_candidate_id=%s",
                    (self.realm_id, subject_id),
                )
            else:
                try:
                    identifier = UUID(subject_id)
                except ValueError as exc:
                    raise NotFound("Lifecycle outbox kimligi gecersiz") from exc
                cursor.execute(
                    "select state,plan_digest,payload_digest"
                    " from continuity.lifecycle_delivery_outbox"
                    " where realm_id=%s and id=%s",
                    (self.realm_id, identifier),
                )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Memory control subject bulunamadi")
        if operation is MemoryControlOperation.CLOSE_FINALIZE:
            return str(row[0]), digest({"plan_digest": str(row[1]), "payload_digest": str(row[2])})
        return str(row[0]), str(row[1])

    def apply_control(
        self,
        plan: MemoryControlPlan,
        *,
        authorization_id: UUID,
        completed_at: dt.datetime,
    ) -> bool:
        if plan.operation is MemoryControlOperation.GAP_REPAIR:
            return self.continuity.resolve_gap(
                gap_id=UUID(plan.subject_id),
                recovery_receipt_ref=plan.evidence_ref,
                recovery_receipt_digest=plan.evidence_digest,
                resolved_at=completed_at,
            )
        if plan.operation is MemoryControlOperation.CANDIDATE_PROMOTE:
            return self.continuity.promote_reviewed_candidate(
                candidate_id=plan.subject_id,
                promotion_ref=plan.evidence_ref,
                promotion_digest=plan.evidence_digest,
                authorization_id=authorization_id,
                promoted_at=completed_at,
            )
        self.continuity.finalize_lifecycle_delivery(
            outbox_id=UUID(plan.subject_id),
            receipt_digest=plan.evidence_digest,
            status=plan.target_state,
            completed_at=completed_at,
        )
        return True
