"""Root Work/Run tree scoped multi-agent controller."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from zekam.application.diagnostic_trace import RuntimeTraceSink
from zekam.domain.agent_graph import AgentGraphRoot, AgentMessage, ChildRuntimeStatus, SpawnEdge
from zekam.domain.diagnostic_trace import TraceEventType, TraceVisibility


class AgentGraphStore(Protocol):
    def create_root(self, root: AgentGraphRoot) -> tuple[UUID, bool]: ...

    def reserve_spawn(self, edge: SpawnEdge) -> tuple[UUID, bool]: ...

    def transition_child(
        self,
        edge_id: UUID,
        *,
        status: ChildRuntimeStatus,
        occurred_at: dt.datetime,
        input_tokens_used: int = 0,
        output_tokens_used: int = 0,
        cost_micros_used: int = 0,
    ) -> bool: ...

    def send_message(self, message: AgentMessage) -> tuple[UUID, bool]: ...

    def snapshot(self, root_id: UUID) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class AgentControl:
    """One controller per canonical execution root; never a process singleton."""

    root: AgentGraphRoot
    store: AgentGraphStore
    trace_sink: RuntimeTraceSink | None = None

    def initialize(self) -> tuple[UUID, bool]:
        return self.store.create_root(self.root)

    def reserve(self, edge: SpawnEdge) -> tuple[UUID, bool]:
        if edge.root_id != self.root.id or edge.realm_id != self.root.realm_id:
            raise ValueError("Spawn edge controller root scope disinda")
        result = self.store.reserve_spawn(edge)
        if self.trace_sink is not None:
            self.trace_sink.record(
                event_type=TraceEventType.AGENT_SPAWN,
                visibility=TraceVisibility.RUNTIME_ONLY,
                payload=edge.body(),
                correlation={
                    "root_id": str(self.root.id),
                    "edge_id": str(edge.id),
                    "parent_assignment_id": str(edge.parent_assignment_id),
                    "child_assignment_id": str(edge.child_assignment_id),
                },
                occurred_at=edge.created_at,
            )
        return result

    def transition(
        self,
        edge_id: UUID,
        *,
        status: ChildRuntimeStatus,
        occurred_at: dt.datetime,
        input_tokens_used: int = 0,
        output_tokens_used: int = 0,
        cost_micros_used: int = 0,
    ) -> bool:
        result = self.store.transition_child(
            edge_id,
            status=status,
            occurred_at=occurred_at,
            input_tokens_used=input_tokens_used,
            output_tokens_used=output_tokens_used,
            cost_micros_used=cost_micros_used,
        )
        if self.trace_sink is not None:
            event_type = (
                TraceEventType.AGENT_CLOSE
                if status
                in {
                    ChildRuntimeStatus.COMPLETED,
                    ChildRuntimeStatus.FAILED,
                    ChildRuntimeStatus.CANCELLED,
                }
                else TraceEventType.AGENT_RESULT
            )
            self.trace_sink.record(
                event_type=event_type,
                visibility=TraceVisibility.RUNTIME_ONLY,
                payload={
                    "status": status.value,
                    "input_tokens_used": input_tokens_used,
                    "output_tokens_used": output_tokens_used,
                    "cost_micros_used": cost_micros_used,
                },
                correlation={"root_id": str(self.root.id), "edge_id": str(edge_id)},
                occurred_at=occurred_at,
            )
        return result

    def send(self, message: AgentMessage) -> tuple[UUID, bool]:
        if message.root_id != self.root.id or message.realm_id != self.root.realm_id:
            raise ValueError("Agent message controller root scope disinda")
        result = self.store.send_message(message)
        if self.trace_sink is not None:
            self.trace_sink.record(
                event_type=TraceEventType.AGENT_TASK,
                visibility=TraceVisibility.RUNTIME_ONLY,
                payload={
                    "message_digest": message.message_digest,
                    "payload_schema": message.payload_schema,
                    "context_digest": message.context_digest,
                },
                correlation={
                    "root_id": str(self.root.id),
                    "sender_assignment_id": str(message.sender_assignment_id),
                    "recipient_assignment_id": str(message.recipient_assignment_id),
                },
                occurred_at=message.created_at,
            )
        return result

    def status(self) -> dict[str, object]:
        return self.store.snapshot(self.root.id)
