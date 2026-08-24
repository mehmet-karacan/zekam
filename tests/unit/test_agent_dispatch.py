"""Canonical assignment-first dispatch tests."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from zekam.application.agent_dispatch import CanonicalAgentDispatchService
from zekam.domain.agents import AgentAssignment, AgentInvocation, AssignmentRole
from zekam.domain.canonical import digest
from zekam.domain.clients import DispatchOutcome, DispatchResult
from zekam.domain.errors import PolicyViolation


def _assignment(*, coordinator: bool = False) -> AgentAssignment:
    assignment_id = uuid4()
    realm_id = uuid4()
    project_id = uuid4()
    work_item_id = uuid4()
    parent_assignment_id = None if coordinator else uuid4()
    identity = {
        "id": str(assignment_id),
        "realm_id": str(realm_id),
        "project_id": str(project_id),
        "work_item_id": str(work_item_id),
        "plan_id": None,
        "step_id": "inspect",
        "parent_assignment_id": None if coordinator else str(parent_assignment_id),
        "role": "coordinator" if coordinator else "researcher",
        "agent_ref": "coordinator" if coordinator else "researcher-1",
        "risk": "medium",
        "instruction_digest": digest("instruction"),
        "context_manifest_digest": digest("context"),
        "read_resources": [],
        "write_resources": [],
    }
    return AgentAssignment(
        id=assignment_id,
        realm_id=realm_id,
        project_id=project_id,
        work_item_id=work_item_id,
        parent_assignment_id=parent_assignment_id,
        role=AssignmentRole(identity["role"]),
        agent_ref=identity["agent_ref"],
        instruction_digest=identity["instruction_digest"],
        context_manifest_digest=identity["context_manifest_digest"],
        assignment_digest=digest(identity),
        step_id="inspect",
        created_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    )


def _invocation(
    assignment: AgentAssignment, *, assignment_id: UUID | None = None
) -> AgentInvocation:
    invocation_id = uuid4()
    bound_assignment_id = assignment_id or assignment.id
    body = {
        "id": str(invocation_id),
        "realm_id": str(assignment.realm_id),
        "assignment_id": str(bound_assignment_id),
        "client_id": "opencode",
        "execution_identity": "opencode:test",
    }
    return AgentInvocation(
        id=invocation_id,
        realm_id=assignment.realm_id,
        assignment_id=bound_assignment_id,
        client_id="opencode",
        execution_identity="opencode:test",
        invocation_digest=digest(body),
        created_at=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    )


class RecordingStore:
    def __init__(self) -> None:
        self.events: list[str] = []

    def create(self, assignment):
        self.events.append("assignment")
        return assignment.id, True

    def record_invocation(self, invocation):
        self.events.append("invocation")
        return invocation.id, True

    def store_result(self, **_kwargs):
        self.events.append("receipt")


class RecordingAdapter:
    descriptor = SimpleNamespace(client_id="opencode")

    def __init__(self, store: RecordingStore) -> None:
        self.store = store

    def dispatch(self, request, *, cwd: Path, permit):
        permit.assert_valid(request)
        self.store.events.append("effect")
        return DispatchResult(
            assignment_id=request.assignment_id,
            invocation_id=request.invocation_id,
            client_id=request.client_id,
            role=request.role,
            outcome=DispatchOutcome.SUCCESS,
            exit_code=0,
            payload={"cwd": str(cwd)},
        )


def test_dispatch_persists_canonical_bindings_before_effect_and_receipt(tmp_path) -> None:
    assignment = _assignment()
    invocation = _invocation(assignment)
    store = RecordingStore()

    CanonicalAgentDispatchService(store).dispatch(
        assignment, invocation, RecordingAdapter(store), cwd=tmp_path, timeout_seconds=30
    )

    assert store.events == ["assignment", "invocation", "effect", "receipt"]


def test_coordinator_cannot_be_dispatched_as_child(tmp_path) -> None:
    assignment = _assignment(coordinator=True)
    invocation = _invocation(assignment)
    store = RecordingStore()
    with pytest.raises(PolicyViolation, match="child assignment"):
        CanonicalAgentDispatchService(store).dispatch(
            assignment, invocation, RecordingAdapter(store), cwd=tmp_path, timeout_seconds=30
        )
    assert store.events == []


def test_invocation_cannot_cross_assignment(tmp_path) -> None:
    assignment = _assignment()
    invocation = _invocation(assignment, assignment_id=uuid4())
    with pytest.raises(PolicyViolation, match="baska assignment"):
        CanonicalAgentDispatchService(RecordingStore()).dispatch(
            assignment,
            invocation,
            RecordingAdapter(RecordingStore()),
            cwd=tmp_path,
            timeout_seconds=30,
        )
