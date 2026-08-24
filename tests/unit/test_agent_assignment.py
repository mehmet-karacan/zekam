from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.domain.agents import AgentAssignment, AssignmentRole
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

pytestmark = pytest.mark.unit
D = "sha256:" + "a" * 64


def _assignment(**changes):  # type: ignore[no-untyped-def]
    values = {
        "id": uuid4(),
        "realm_id": uuid4(),
        "project_id": uuid4(),
        "work_item_id": uuid4(),
        "role": AssignmentRole.COORDINATOR,
        "agent_ref": "coordinator",
        "instruction_digest": D,
        "context_manifest_digest": D,
        "assignment_digest": D,
        "created_at": dt.datetime.now(dt.UTC),
    }
    values.update(changes)
    return AgentAssignment(**values)


def test_coordinator_is_not_a_child() -> None:
    assert not _assignment().is_child
    with pytest.raises(PolicyViolation, match="Koordinator child olamaz"):
        _assignment(parent_assignment_id=uuid4())


def test_child_requires_parent_for_child_classification() -> None:
    assert _assignment(role=AssignmentRole.BUILDER, parent_assignment_id=uuid4()).is_child


def test_assignment_supplied_digest_is_recomputed() -> None:
    assignment = _assignment()
    with pytest.raises(ValidationFailed, match="supplied digest"):
        assignment.assert_digest()
    body = assignment.identity_body()
    valid = _assignment(
        id=assignment.id,
        realm_id=assignment.realm_id,
        project_id=assignment.project_id,
        work_item_id=assignment.work_item_id,
        assignment_digest=digest(body),
    )
    valid.assert_digest()
