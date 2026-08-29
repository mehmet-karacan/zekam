"""Exact, authorization-bound pause/drain/cancel control for measured loops."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7
from zekam.domain.security import Authorization
from zekam.domain.work import OPEN_STATES, WorkState


class LoopControlState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DRAINING = "draining"
    CANCELLED = "cancelled"


_TRANSITIONS: dict[LoopControlState, frozenset[LoopControlState]] = {
    LoopControlState.ACTIVE: frozenset(
        {LoopControlState.PAUSED, LoopControlState.DRAINING, LoopControlState.CANCELLED}
    ),
    LoopControlState.PAUSED: frozenset(
        {LoopControlState.ACTIVE, LoopControlState.DRAINING, LoopControlState.CANCELLED}
    ),
    LoopControlState.DRAINING: frozenset({LoopControlState.ACTIVE, LoopControlState.CANCELLED}),
    LoopControlState.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class LoopControlSnapshot:
    realm_id: UUID
    loop_id: UUID
    work_item_id: UUID
    plan_id: UUID
    plan_digest: str
    current_state: LoopControlState
    terminal_state: str | None
    work_state: str
    current_plan: bool

    def __post_init__(self) -> None:
        parse_digest(self.plan_digest)


@dataclass(frozen=True, slots=True)
class LoopControlPlan:
    realm_id: UUID
    loop_id: UUID
    work_item_id: UUID
    plan_id: UUID
    plan_digest: str
    source_state: LoopControlState
    target_state: LoopControlState
    reason_digest: str
    effect_digest: str
    control_digest: str
    grants_authority: bool = False

    @classmethod
    def create(
        cls,
        snapshot: LoopControlSnapshot,
        *,
        target_state: LoopControlState,
        reason_digest: str,
    ) -> LoopControlPlan:
        parse_digest(reason_digest)
        if snapshot.terminal_state is not None:
            raise PolicyViolation("Terminal loop control state degistiremez")
        try:
            work_state = WorkState(snapshot.work_state)
        except ValueError as exc:
            raise PolicyViolation("Loop control Work state gecersiz") from exc
        if work_state not in OPEN_STATES or not snapshot.current_plan:
            raise PolicyViolation("Loop control current open Work ve TaskPlan ister")
        if target_state not in _TRANSITIONS[snapshot.current_state]:
            raise PolicyViolation("Loop control state transition gecersiz")
        resource = f"loop:{snapshot.loop_id}"
        effect_digest = digest(
            {
                "effect": "database-write",
                "resource": resource,
                "loop_id": str(snapshot.loop_id),
                "plan_digest": snapshot.plan_digest,
                "source_state": snapshot.current_state.value,
                "target_state": target_state.value,
                "reason_digest": reason_digest,
            }
        )
        draft = cls(
            snapshot.realm_id,
            snapshot.loop_id,
            snapshot.work_item_id,
            snapshot.plan_id,
            snapshot.plan_digest,
            snapshot.current_state,
            target_state,
            reason_digest,
            effect_digest,
            "",
            False,
        )
        return replace(draft, control_digest=digest(draft.body()))

    @property
    def resource(self) -> str:
        return f"loop:{self.loop_id}"

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-loop-control-plan/v1",
            "realm_id": str(self.realm_id),
            "loop_id": str(self.loop_id),
            "work_item_id": str(self.work_item_id),
            "plan_id": str(self.plan_id),
            "plan_digest": self.plan_digest,
            "resource": self.resource,
            "source_state": self.source_state.value,
            "target_state": self.target_state.value,
            "reason_digest": self.reason_digest,
            "effect_digest": self.effect_digest,
            "grants_authority": False,
        }

    def assert_integrity(self) -> None:
        parse_digest(self.plan_digest)
        parse_digest(self.reason_digest)
        parse_digest(self.effect_digest)
        parse_digest(self.control_digest)
        if self.control_digest != digest(self.body()):
            raise PolicyViolation("Loop control plan digest mismatch")


@dataclass(frozen=True, slots=True)
class LoopControlReceipt:
    event_id: UUID
    loop_id: UUID
    source_state: LoopControlState
    target_state: LoopControlState
    plan_digest: str
    control_digest: str
    authorization_id: UUID
    reason_digest: str
    created_at: dt.datetime
    receipt_digest: str
    grants_authority: bool = False


class LoopControlStore(Protocol):
    @property
    def connection(self) -> Any: ...

    @property
    def realm_id(self) -> UUID: ...

    def read_loop_control_snapshot(self, loop_id: UUID) -> LoopControlSnapshot: ...

    def record_loop_control_event(
        self,
        plan: LoopControlPlan,
        *,
        event_id: UUID,
        authorization_id: UUID,
        authorization_digest: str,
    ) -> dt.datetime: ...


class LoopControlAuthorizationStore(Protocol):
    def get(self, authorization_id: UUID) -> Authorization: ...


@dataclass(frozen=True, slots=True)
class LoopControlService:
    repository: LoopControlStore
    authorizations: LoopControlAuthorizationStore

    def prepare(
        self,
        loop_id: UUID,
        *,
        target_state: LoopControlState,
        reason_digest: str,
    ) -> LoopControlPlan:
        snapshot = self.repository.read_loop_control_snapshot(loop_id)
        return LoopControlPlan.create(
            snapshot,
            target_state=target_state,
            reason_digest=reason_digest,
        )

    def apply(
        self,
        plan: LoopControlPlan,
        *,
        authorization_id: UUID,
        now: dt.datetime | None = None,
    ) -> LoopControlReceipt:
        moment = now or dt.datetime.now(dt.UTC)
        if moment.tzinfo is None:
            raise ValidationFailed("Loop control apply zamani timezone-aware olmali")
        plan.assert_integrity()
        snapshot = self.repository.read_loop_control_snapshot(plan.loop_id)
        try:
            work_is_open = WorkState(snapshot.work_state) in OPEN_STATES
        except ValueError:
            work_is_open = False
        if (
            snapshot.realm_id != plan.realm_id
            or snapshot.loop_id != plan.loop_id
            or snapshot.work_item_id != plan.work_item_id
            or snapshot.plan_id != plan.plan_id
            or snapshot.plan_digest != plan.plan_digest
            or snapshot.current_state is not plan.source_state
            or snapshot.terminal_state is not None
            or not work_is_open
            or not snapshot.current_plan
        ):
            raise PolicyViolation("Loop control apply state/plan drift; replan required")
        authorization = self.authorizations.get(authorization_id)
        rejection = authorization.rejection_reason(moment)
        if (
            rejection is not None
            or authorization.realm_id != plan.realm_id
            or authorization.work_item_id != plan.work_item_id
            or authorization.plan_id != plan.plan_id
            or authorization.plan_digest != plan.plan_digest
            or authorization.effect_digest != plan.effect_digest
            or not authorization.scope.covers_effect("database-write")
            or not authorization.scope.covers_resource(plan.resource)
        ):
            raise AuthorizationRequired(
                f"Loop control exact authorization binding yok: {rejection or 'scope-mismatch'}"
            )
        event_id = new_uuid7(now=moment)
        with self.repository.connection.transaction():
            created_at = self.repository.record_loop_control_event(
                plan,
                event_id=event_id,
                authorization_id=authorization.id,
                authorization_digest=authorization.authorization_digest,
            )
        receipt_digest = digest(
            {
                "schema": "zekam-loop-control-receipt/v1",
                "event_id": str(event_id),
                "loop_id": str(plan.loop_id),
                "source_state": plan.source_state.value,
                "target_state": plan.target_state.value,
                "plan_digest": plan.plan_digest,
                "control_digest": plan.control_digest,
                "authorization_id": str(authorization.id),
                "reason_digest": plan.reason_digest,
                "created_at": created_at,
                "grants_authority": False,
            }
        )
        return LoopControlReceipt(
            event_id,
            plan.loop_id,
            plan.source_state,
            plan.target_state,
            plan.plan_digest,
            plan.control_digest,
            authorization.id,
            plan.reason_digest,
            created_at,
            receipt_digest,
        )
