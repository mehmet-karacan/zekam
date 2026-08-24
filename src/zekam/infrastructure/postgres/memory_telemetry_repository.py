"""Read-only PostgreSQL projections for Memory v2 telemetry.

Writes are deliberately absent: canonical database triggers derive usage from a
verified model invocation and outcomes from an independently verified checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.memory_telemetry import (
    MemoryEffectiveness,
    MemoryUsageEvent,
    MemoryUsageOutcome,
)


@dataclass(frozen=True, slots=True)
class MemoryTelemetryRepository:
    connection: Any
    realm_id: UUID

    def usage_for_record(
        self, record_id: UUID, *, limit: int = 100
    ) -> tuple[MemoryUsageEvent, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,record_id,request_manifest_id,invocation_attempt_id,"
                "invocation_result_id,task_plan_id,run_id,job_id,runtime_attempt_id,"
                "assignment_id,step_id,project_id,work_item_id,record_digest,fragment_digest,"
                "model_visible_payload_digest,context_manifest_digest,used_at,event_digest"
                " from memory.usage_event"
                " where realm_id=%s and record_id=%s order by used_at desc,id desc limit %s",
                (self.realm_id, record_id, limit),
            )
            rows = cursor.fetchall()
        return tuple(MemoryUsageEvent(*row) for row in rows)

    def outcomes_for_record(
        self, record_id: UUID, *, limit: int = 100
    ) -> tuple[MemoryUsageOutcome, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select o.id,o.usage_event_id,o.checkpoint_id,o.step_id,"
                "o.verifier_assignment_id,o.verifier_invocation_id,o.verifier_envelope_digest,"
                "o.checkpoint_digest,o.result_digest,o.outcome_status,o.correlated_at,"
                "o.outcome_digest from memory.usage_outcome o join memory.usage_event u"
                " on u.realm_id=o.realm_id and u.id=o.usage_event_id"
                " where o.realm_id=%s and u.record_id=%s"
                " order by o.correlated_at desc,o.id desc limit %s",
                (self.realm_id, record_id, limit),
            )
            rows = cursor.fetchall()
        return tuple(MemoryUsageOutcome(*row) for row in rows)

    def effectiveness(self, record_id: UUID) -> MemoryEffectiveness | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select record_id,record_digest,usage_count,verified_outcome_count,"
                "verified_success_count,last_used_at,last_verified_outcome_at"
                " from memory.usage_effectiveness where realm_id=%s and record_id=%s",
                (self.realm_id, record_id),
            )
            row = cursor.fetchone()
        return None if row is None else MemoryEffectiveness(*row)

    def rebuild_last_used_projection(self, record_id: UUID) -> Any:
        with self.connection.cursor() as cursor:
            cursor.execute("select memory.rebuild_last_used_projection(%s)", (record_id,))
            return cursor.fetchone()[0]
