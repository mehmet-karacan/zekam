"""Safe runtime eviction/reload orchestration without authority inheritance."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from zekam.domain.agent_residency import (
    AssignmentRuntimeSnapshot,
    ReloadRequest,
    ReloadResult,
    ResidencyState,
)


class ResidencyStore(Protocol):
    def register_loaded(
        self, snapshot: AssignmentRuntimeSnapshot, *, runtime_session_ref: str
    ) -> tuple[UUID, bool]: ...

    def transition(
        self,
        edge_id: UUID,
        *,
        state: ResidencyState,
        occurred_at: dt.datetime,
        reason: str | None = None,
    ) -> bool: ...

    def reload(self, request: ReloadRequest) -> ReloadResult: ...

    def get(self, edge_id: UUID) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class AgentResidencyManager:
    realm_id: UUID
    store: ResidencyStore

    def register(
        self, snapshot: AssignmentRuntimeSnapshot, *, runtime_session_ref: str
    ) -> tuple[UUID, bool]:
        if snapshot.realm_id != self.realm_id:
            raise ValueError("Runtime snapshot manager realm scope disinda")
        return self.store.register_loaded(snapshot, runtime_session_ref=runtime_session_ref)

    def evict(self, edge_id: UUID, *, occurred_at: dt.datetime) -> bool:
        """Eviction only releases live residency; it never completes work or grants authority."""
        return self.store.transition(edge_id, state=ResidencyState.EVICTED, occurred_at=occurred_at)

    def mark_idle(self, edge_id: UUID, *, occurred_at: dt.datetime) -> bool:
        return self.store.transition(edge_id, state=ResidencyState.IDLE, occurred_at=occurred_at)

    def begin_close(self, edge_id: UUID, *, occurred_at: dt.datetime) -> bool:
        return self.store.transition(edge_id, state=ResidencyState.CLOSING, occurred_at=occurred_at)

    def mark_dead(self, edge_id: UUID, *, occurred_at: dt.datetime, reason: str) -> bool:
        return self.store.transition(
            edge_id, state=ResidencyState.DEAD, occurred_at=occurred_at, reason=reason
        )

    def reload(self, request: ReloadRequest) -> ReloadResult:
        if request.realm_id != self.realm_id:
            raise ValueError("Reload request manager realm scope disinda")
        return self.store.reload(request)

    def status(self, edge_id: UUID) -> dict[str, object]:
        return self.store.get(edge_id)
