"""PostgreSQL store for the persisted, root-scoped agent runtime graph."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.agent_graph import (
    TERMINAL_CHILD_STATUSES,
    AgentGraphRoot,
    AgentMessage,
    ChildRuntimeStatus,
    SpawnEdge,
)
from zekam.domain.canonical import canonical_json
from zekam.domain.errors import NotFound, PolicyViolation


@dataclass(frozen=True, slots=True)
class AgentGraphRepository:
    connection: Any
    realm_id: UUID

    def _assert_realm(self, realm_id: UUID) -> None:
        if realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm agent graph islemi reddedildi")

    def create_root(self, root: AgentGraphRoot) -> tuple[UUID, bool]:
        self._assert_realm(root.realm_id)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into agents.graph_root"
                " (id,realm_id,run_id,coordinator_assignment_id,max_concurrency,"
                " max_input_tokens,max_output_tokens,max_cost_micros,root_digest,root_body,"
                " created_at)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)"
                " on conflict (realm_id,root_digest) do nothing returning id",
                (
                    root.id,
                    root.realm_id,
                    root.run_id,
                    root.coordinator_assignment_id,
                    root.max_concurrency,
                    root.max_input_tokens,
                    root.max_output_tokens,
                    root.max_cost_micros,
                    root.root_digest,
                    canonical_json(root.body()),
                    root.created_at,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from agents.graph_root where realm_id=%s and root_digest=%s",
                (self.realm_id, root.root_digest),
            )
            return UUID(str(cursor.fetchone()[0])), False

    def reserve_spawn(self, edge: SpawnEdge) -> tuple[UUID, bool]:
        self._assert_realm(edge.realm_id)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select id from agents.spawn_edge where realm_id=%s and edge_digest=%s",
                (self.realm_id, edge.edge_digest),
            )
            existing = cursor.fetchone()
            if existing is not None:
                return UUID(str(existing[0])), False
            cursor.execute(
                "select id from agents.spawn_edge where realm_id=%s and child_assignment_id=%s",
                (self.realm_id, edge.child_assignment_id),
            )
            if cursor.fetchone() is not None:
                raise PolicyViolation("Child assignment duplicate parent edge alamaz")
            cursor.execute(
                "select active_count,max_concurrency,reserved_input_tokens,used_input_tokens,"
                " max_input_tokens,reserved_output_tokens,used_output_tokens,max_output_tokens,"
                " reserved_cost_micros,used_cost_micros,max_cost_micros"
                " from agents.graph_root where realm_id=%s and id=%s",
                (self.realm_id, edge.root_id),
            )
            root = cursor.fetchone()
            if root is None:
                raise NotFound("Agent graph root bulunamadi")
            if int(root[0]) >= int(root[1]):
                raise PolicyViolation("Agent graph concurrency butcesi tukendi")
            if (
                int(root[2]) + int(root[3]) + edge.reserved_input_tokens > int(root[4])
                or int(root[5]) + int(root[6]) + edge.reserved_output_tokens > int(root[7])
                or int(root[8]) + int(root[9]) + edge.reserved_cost_micros > int(root[10])
            ):
                raise PolicyViolation("Agent graph token/maliyet butcesi tukendi")
            cursor.execute(
                "select agents.reserve_spawn_edge(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (
                    edge.id,
                    edge.realm_id,
                    edge.root_id,
                    edge.parent_assignment_id,
                    edge.child_assignment_id,
                    edge.reserved_input_tokens,
                    edge.reserved_output_tokens,
                    edge.reserved_cost_micros,
                    edge.edge_digest,
                    canonical_json(edge.body()),
                    edge.created_at,
                ),
            )
            return edge.id, True

    def transition_child(
        self,
        edge_id: UUID,
        *,
        status: ChildRuntimeStatus,
        occurred_at: dt.datetime,
        input_tokens_used: int = 0,
        output_tokens_used: int = 0,
        cost_micros_used: int = 0,
    ) -> bool:
        if min(input_tokens_used, output_tokens_used, cost_micros_used) < 0:
            raise PolicyViolation("Agent child kullanimi negatif olamaz")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select root_id,status,reserved_input_tokens,reserved_output_tokens,"
                " reserved_cost_micros,terminal_at from agents.spawn_edge"
                " where realm_id=%s and id=%s",
                (self.realm_id, edge_id),
            )
            edge = cursor.fetchone()
            if edge is None:
                raise NotFound("Spawn edge bulunamadi")
            current = ChildRuntimeStatus(str(edge[1]))
            if current == status:
                return False
            if current in TERMINAL_CHILD_STATUSES:
                raise PolicyViolation("Terminal child status degistirilemez")
            terminal = status in TERMINAL_CHILD_STATUSES
            if not terminal and any((input_tokens_used, output_tokens_used, cost_micros_used)):
                raise PolicyViolation("Kullanim yalniz terminal child statusunda kaydedilir")
            if (
                input_tokens_used > int(edge[2])
                or output_tokens_used > int(edge[3])
                or cost_micros_used > int(edge[4])
            ):
                raise PolicyViolation("Child kullanimi rezervasyonu asti")
            cursor.execute(
                "select agents.transition_graph_child(%s,%s,%s,%s,%s,%s,%s)",
                (
                    self.realm_id,
                    edge_id,
                    status.value,
                    occurred_at,
                    input_tokens_used,
                    output_tokens_used,
                    cost_micros_used,
                ),
            )
            return True

    def send_message(self, message: AgentMessage) -> tuple[UUID, bool]:
        self._assert_realm(message.realm_id)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into agents.message"
                " (id,realm_id,root_id,sender_assignment_id,recipient_assignment_id,context_type,"
                " context_ref,context_digest,payload_schema,payload,message_body,message_digest,"
                " created_at)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)"
                " on conflict (realm_id,message_digest) do nothing returning id",
                (
                    message.id,
                    message.realm_id,
                    message.root_id,
                    message.sender_assignment_id,
                    message.recipient_assignment_id,
                    message.context_type,
                    message.context_ref,
                    message.context_digest,
                    message.payload_schema,
                    canonical_json(message.payload),
                    canonical_json(message.body()),
                    message.message_digest,
                    message.created_at,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from agents.message where realm_id=%s and message_digest=%s",
                (self.realm_id, message.message_digest),
            )
            return UUID(str(cursor.fetchone()[0])), False

    def snapshot(self, root_id: UUID) -> dict[str, object]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select run_id,coordinator_assignment_id,max_concurrency,active_count,"
                " max_input_tokens,reserved_input_tokens,used_input_tokens,max_output_tokens,"
                " reserved_output_tokens,used_output_tokens,max_cost_micros,reserved_cost_micros,"
                " used_cost_micros,root_digest from agents.graph_root"
                " where realm_id=%s and id=%s",
                (self.realm_id, root_id),
            )
            root = cursor.fetchone()
            if root is None:
                raise NotFound("Agent graph root bulunamadi")
            cursor.execute(
                "select id,parent_assignment_id,child_assignment_id,status,edge_digest,created_at,"
                " terminal_at from agents.spawn_edge where realm_id=%s and root_id=%s"
                " order by created_at,id",
                (self.realm_id, root_id),
            )
            edges = cursor.fetchall()
        return {
            "root_id": str(root_id),
            "run_id": str(root[0]),
            "coordinator_assignment_id": str(root[1]),
            "limits": {
                "concurrency": int(root[2]),
                "input_tokens": int(root[4]),
                "output_tokens": int(root[7]),
                "cost_micros": int(root[10]),
            },
            "usage": {
                "active": int(root[3]),
                "reserved_input_tokens": int(root[5]),
                "used_input_tokens": int(root[6]),
                "reserved_output_tokens": int(root[8]),
                "used_output_tokens": int(root[9]),
                "reserved_cost_micros": int(root[11]),
                "used_cost_micros": int(root[12]),
            },
            "root_digest": str(root[13]),
            "edges": [
                {
                    "id": str(row[0]),
                    "parent_assignment_id": str(row[1]),
                    "child_assignment_id": str(row[2]),
                    "status": str(row[3]),
                    "edge_digest": str(row[4]),
                    "created_at": row[5].isoformat(),
                    "terminal_at": None if row[6] is None else row[6].isoformat(),
                }
                for row in edges
            ],
        }
