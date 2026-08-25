"""PostgreSQL residency store with fail-closed reload validation."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from zekam.domain.agent_residency import (
    AssignmentRuntimeSnapshot,
    ReloadDisposition,
    ReloadRequest,
    ReloadResult,
    ResidencyState,
)
from zekam.domain.canonical import canonical_json
from zekam.domain.errors import NotFound, PolicyViolation


@dataclass(frozen=True, slots=True)
class AgentResidencyRepository:
    connection: Any
    realm_id: UUID

    def _realm(self, realm_id: UUID) -> None:
        if realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm residency islemi reddedildi")

    def register_loaded(
        self, snapshot: AssignmentRuntimeSnapshot, *, runtime_session_ref: str
    ) -> tuple[UUID, bool]:
        self._realm(snapshot.realm_id)
        if not runtime_session_ref.strip():
            raise PolicyViolation("Runtime session ref bos olamaz")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select id from agents.assignment_runtime_snapshot"
                " where realm_id=%s and snapshot_digest=%s",
                (self.realm_id, snapshot.snapshot_digest),
            )
            existing_snapshot = cursor.fetchone()
            if existing_snapshot is None:
                cursor.execute(
                    "insert into agents.assignment_runtime_snapshot"
                    " (id,realm_id,edge_id,assignment_id,execution_envelope_id,role,model_id,"
                    " provider_binding_id,provider_binding_digest,route_decision_id,"
                    " route_decision_digest,environment_snapshot_digest,permission_profile_digest,"
                    " config_effective_digest,source_revision,policy_digest,snapshot_digest,"
                    " snapshot_body,created_at) values"
                    " (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
                    (
                        snapshot.id,
                        snapshot.realm_id,
                        snapshot.edge_id,
                        snapshot.assignment_id,
                        snapshot.execution_envelope_id,
                        snapshot.role,
                        snapshot.model_id,
                        snapshot.provider_binding_id,
                        snapshot.provider_binding_digest,
                        snapshot.route_decision_id,
                        snapshot.route_decision_digest,
                        snapshot.environment_snapshot_digest,
                        snapshot.permission_profile_digest,
                        snapshot.config_effective_digest,
                        snapshot.source_revision,
                        snapshot.policy_digest,
                        snapshot.snapshot_digest,
                        canonical_json(snapshot.body()),
                        snapshot.created_at,
                    ),
                )
                snapshot_id = snapshot.id
            else:
                snapshot_id = UUID(str(existing_snapshot[0]))
            cursor.execute(
                "select id from agents.runtime_residency where realm_id=%s and edge_id=%s",
                (self.realm_id, snapshot.edge_id),
            )
            existing = cursor.fetchone()
            if existing is not None:
                return UUID(str(existing[0])), False
            residency_id = uuid4()
            cursor.execute(
                "select agents.register_runtime_residency(%s,%s,%s,%s,%s,%s)",
                (
                    residency_id,
                    self.realm_id,
                    snapshot.edge_id,
                    snapshot_id,
                    runtime_session_ref,
                    snapshot.created_at,
                ),
            )
            return residency_id, True

    def transition(
        self,
        edge_id: UUID,
        *,
        state: ResidencyState,
        occurred_at: dt.datetime,
        reason: str | None = None,
    ) -> bool:
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select agents.transition_runtime_residency(%s,%s,%s,%s,%s)",
                (self.realm_id, edge_id, state.value, occurred_at, reason),
            )
            return bool(cursor.fetchone()[0])

    def reload(self, request: ReloadRequest) -> ReloadResult:
        self._realm(request.realm_id)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select accepted,state,generation,reason from agents.reload_runtime_residency"
                " (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (
                    request.realm_id,
                    request.edge_id,
                    request.current_environment_snapshot_digest,
                    request.current_route_decision_id,
                    request.current_provider_binding_id,
                    request.runtime_session_ref,
                    request.requested_at,
                    request.request_digest,
                    canonical_json(request.body()),
                ),
            )
            row = cursor.fetchone()
        return ReloadResult(
            disposition=(ReloadDisposition.LOADED if row[0] else ReloadDisposition.REJECTED),
            state=ResidencyState(str(row[1])),
            generation=int(row[2]),
            reason=None if row[3] is None else str(row[3]),
        )

    def get(self, edge_id: UUID) -> dict[str, object]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select r.id,r.runtime_snapshot_id,r.state,r.generation,r.runtime_session_ref,"
                " r.last_seen_at,r.state_changed_at,r.dead_reason,s.snapshot_digest"
                " from agents.runtime_residency r join agents.assignment_runtime_snapshot s"
                " on s.realm_id=r.realm_id and s.id=r.runtime_snapshot_id"
                " where r.realm_id=%s and r.edge_id=%s",
                (self.realm_id, edge_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise NotFound("Runtime residency bulunamadi")
            cursor.execute(
                "select sequence,state,generation,event_digest,occurred_at"
                " from agents.residency_event where realm_id=%s and residency_id=%s"
                " order by sequence",
                (self.realm_id, row[0]),
            )
            events = cursor.fetchall()
        return {
            "id": str(row[0]),
            "edge_id": str(edge_id),
            "runtime_snapshot_id": str(row[1]),
            "state": str(row[2]),
            "generation": int(row[3]),
            "runtime_session_ref": None if row[4] is None else str(row[4]),
            "last_seen_at": row[5].isoformat(),
            "state_changed_at": row[6].isoformat(),
            "dead_reason": None if row[7] is None else str(row[7]),
            "snapshot_digest": str(row[8]),
            "grants_authority": False,
            "events": [
                {
                    "sequence": int(event[0]),
                    "state": str(event[1]),
                    "generation": int(event[2]),
                    "event_digest": str(event[3]),
                    "occurred_at": event[4].isoformat(),
                }
                for event in events
            ],
        }
