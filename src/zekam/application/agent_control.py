"""Root Work/Run tree scoped multi-agent controller."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from zekam.domain.agent_graph import AgentGraphRoot, AgentMessage, ChildRuntimeStatus, SpawnEdge


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

    def initialize(self) -> tuple[UUID, bool]:
        return self.store.create_root(self.root)

    def reserve(self, edge: SpawnEdge) -> tuple[UUID, bool]:
        if edge.root_id != self.root.id or edge.realm_id != self.root.realm_id:
            raise ValueError("Spawn edge controller root scope disinda")
        return self.store.reserve_spawn(edge)

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
        return self.store.transition_child(
            edge_id,
            status=status,
            occurred_at=occurred_at,
            input_tokens_used=input_tokens_used,
            output_tokens_used=output_tokens_used,
            cost_micros_used=cost_micros_used,
        )

    def send(self, message: AgentMessage) -> tuple[UUID, bool]:
        if message.root_id != self.root.id or message.realm_id != self.root.realm_id:
            raise ValueError("Agent message controller root scope disinda")
        return self.store.send_message(message)

    def status(self) -> dict[str, object]:
        return self.store.snapshot(self.root.id)
