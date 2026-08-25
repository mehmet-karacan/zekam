from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.application.agent_control import AgentControl
from zekam.domain.agent_graph import AgentGraphRoot, AgentMessage, SpawnEdge
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

NOW = dt.datetime(2026, 8, 25, 10, 0, tzinfo=dt.UTC)


def _root() -> AgentGraphRoot:
    return AgentGraphRoot.create(
        realm_id=uuid4(),
        run_id=uuid4(),
        coordinator_assignment_id=uuid4(),
        max_concurrency=2,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        created_at=NOW,
    )


def test_digest_bound_root_and_spawn_edge_are_authority_free() -> None:
    root = _root()
    edge = SpawnEdge.create(
        realm_id=root.realm_id,
        root_id=root.id,
        parent_assignment_id=root.coordinator_assignment_id,
        child_assignment_id=uuid4(),
        reserved_input_tokens=10,
        reserved_output_tokens=5,
        reserved_cost_micros=100,
        created_at=NOW,
    )
    assert root.root_digest == root.computed_digest
    assert edge.edge_digest == edge.computed_digest
    with pytest.raises(PolicyViolation, match="authority"):
        AgentGraphRoot.create(
            realm_id=root.realm_id,
            run_id=root.run_id,
            coordinator_assignment_id=root.coordinator_assignment_id,
            max_concurrency=1,
            max_input_tokens=1,
            max_output_tokens=1,
            max_cost_micros=1,
            created_at=NOW,
            grants_authority=True,
        )


def test_contextless_or_untyped_agent_message_is_rejected() -> None:
    root = _root()
    values = {
        "realm_id": root.realm_id,
        "root_id": root.id,
        "sender_assignment_id": root.coordinator_assignment_id,
        "recipient_assignment_id": uuid4(),
        "context_type": "assignment-context-manifest",
        "context_ref": "",
        "context_digest": digest("context"),
        "payload_schema": "zekam-task-request/v1",
        "payload": {"task": "inspect"},
        "created_at": NOW,
    }
    values["context_ref"] = str(values["recipient_assignment_id"])
    message = AgentMessage.create(**values)
    assert message.message_digest == message.computed_digest
    with pytest.raises(ValidationFailed, match="canonical context type"):
        AgentMessage.create(**{**values, "context_type": ""})
    with pytest.raises(ValidationFailed, match="context ref"):
        AgentMessage.create(**{**values, "context_ref": ""})
    with pytest.raises(ValidationFailed, match="typed context"):
        AgentMessage.create(**{**values, "payload_schema": ""})
    with pytest.raises(ValidationFailed, match="bos payload"):
        AgentMessage.create(**{**values, "payload": {}})


def test_agent_control_rejects_cross_root_objects() -> None:
    root = _root()

    class Store:
        def create_root(self, item):  # type: ignore[no-untyped-def]
            return item.id, True

    control = AgentControl(root, Store())  # type: ignore[arg-type]
    other = _root()
    edge = SpawnEdge.create(
        realm_id=other.realm_id,
        root_id=other.id,
        parent_assignment_id=other.coordinator_assignment_id,
        child_assignment_id=uuid4(),
        reserved_input_tokens=1,
        reserved_output_tokens=1,
        reserved_cost_micros=1,
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="root scope"):
        control.reserve(edge)
