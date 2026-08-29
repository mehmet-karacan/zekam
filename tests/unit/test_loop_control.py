from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from zekam.application.loop_control import (
    LoopControlPlan,
    LoopControlService,
    LoopControlSnapshot,
    LoopControlState,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation
from zekam.domain.security import Authorization, AuthorizationScope

NOW = dt.datetime(2026, 8, 29, 8, 0, tzinfo=dt.UTC)


class FakeConnection:
    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield


class FakeRepository:
    def __init__(self, snapshot: LoopControlSnapshot) -> None:
        self.realm_id = snapshot.realm_id
        self.connection = FakeConnection()
        self.snapshot = snapshot
        self.events: list[tuple[LoopControlPlan, UUID, UUID, str]] = []

    def read_loop_control_snapshot(self, loop_id: UUID) -> LoopControlSnapshot:
        assert loop_id == self.snapshot.loop_id
        return self.snapshot

    def record_loop_control_event(
        self,
        plan: LoopControlPlan,
        *,
        event_id: UUID,
        authorization_id: UUID,
        authorization_digest: str,
    ) -> dt.datetime:
        assert plan.source_state is self.snapshot.current_state
        self.events.append((plan, event_id, authorization_id, authorization_digest))
        self.snapshot = replace(self.snapshot, current_state=plan.target_state)
        return NOW


class FakeAuthorizations:
    def __init__(self, authorization: Authorization) -> None:
        self.authorization = authorization

    def get(self, authorization_id: UUID) -> Authorization:
        assert authorization_id == self.authorization.id
        return self.authorization


class UnusedAuthorizations:
    def get(self, authorization_id: UUID) -> Authorization:
        raise AssertionError(f"unexpected authorization read: {authorization_id}")


def _snapshot(**changes: Any) -> LoopControlSnapshot:
    values: dict[str, Any] = {
        "realm_id": uuid4(),
        "loop_id": uuid4(),
        "work_item_id": uuid4(),
        "plan_id": uuid4(),
        "plan_digest": digest("task-plan"),
        "current_state": LoopControlState.ACTIVE,
        "terminal_state": None,
        "work_state": "active",
        "current_plan": True,
    }
    values.update(changes)
    return LoopControlSnapshot(**values)


def _authorization(
    plan: LoopControlPlan,
    *,
    effect_digest: str | None = None,
    resources: tuple[str, ...] | None = None,
) -> Authorization:
    return Authorization.issue(
        realm_id=plan.realm_id,
        actor_id=uuid4(),
        work_item_id=plan.work_item_id,
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        effect_digest=effect_digest or plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=resources or (plan.resource,),
            allowed_effects=("database-write",),
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )


@pytest.mark.parametrize(
    ("source", "target"),
    (
        (LoopControlState.ACTIVE, LoopControlState.PAUSED),
        (LoopControlState.ACTIVE, LoopControlState.DRAINING),
        (LoopControlState.ACTIVE, LoopControlState.CANCELLED),
        (LoopControlState.PAUSED, LoopControlState.ACTIVE),
        (LoopControlState.DRAINING, LoopControlState.ACTIVE),
    ),
)
def test_reviewed_control_transition_is_exact_and_receipt_bound(
    source: LoopControlState, target: LoopControlState
) -> None:
    repository = FakeRepository(_snapshot(current_state=source))
    preview = LoopControlService(repository, UnusedAuthorizations()).prepare(
        repository.snapshot.loop_id,
        target_state=target,
        reason_digest=digest((source, target, "reviewed")),
    )
    authorization = _authorization(preview)
    service = LoopControlService(repository, FakeAuthorizations(authorization))

    receipt = service.apply(preview, authorization_id=authorization.id, now=NOW)

    assert receipt.source_state is source
    assert receipt.target_state is target
    assert receipt.plan_digest == preview.plan_digest
    assert receipt.control_digest == preview.control_digest
    assert receipt.authorization_id == authorization.id
    assert repository.snapshot.current_state is target
    assert len(repository.events) == 1


def test_cancelled_terminal_or_stale_plan_cannot_resume() -> None:
    for snapshot in (
        _snapshot(current_state=LoopControlState.CANCELLED),
        _snapshot(current_state=LoopControlState.PAUSED, terminal_state="manual-review"),
        _snapshot(current_state=LoopControlState.PAUSED, current_plan=False),
    ):
        repository = FakeRepository(snapshot)
        service = LoopControlService(repository, UnusedAuthorizations())
        with pytest.raises(PolicyViolation):
            service.prepare(
                snapshot.loop_id,
                target_state=LoopControlState.ACTIVE,
                reason_digest=digest("resume"),
            )


def test_apply_rejects_effect_scope_and_state_drift_before_write() -> None:
    repository = FakeRepository(_snapshot())
    preview = LoopControlService(repository, UnusedAuthorizations()).prepare(
        repository.snapshot.loop_id,
        target_state=LoopControlState.PAUSED,
        reason_digest=digest("pause"),
    )
    wrong_effect = _authorization(preview, effect_digest=digest("wrong-effect"))
    with pytest.raises(AuthorizationRequired):
        LoopControlService(repository, FakeAuthorizations(wrong_effect)).apply(
            preview, authorization_id=wrong_effect.id, now=NOW
        )
    assert repository.events == []

    exact = _authorization(preview)
    repository.snapshot = replace(repository.snapshot, current_plan=False)
    with pytest.raises(PolicyViolation, match="replan"):
        LoopControlService(repository, FakeAuthorizations(exact)).apply(
            preview, authorization_id=exact.id, now=NOW
        )
    assert repository.events == []
