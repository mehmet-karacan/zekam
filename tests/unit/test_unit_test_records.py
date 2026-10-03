import datetime as dt
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.domain.agents import AgentAssignment, AgentInvocation, AssignmentRole
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.infrastructure.unit_test_records import CasAssignmentStore, CasRecordIndex


def _assignment() -> AgentAssignment:
    return AgentAssignment(
        id=uuid4(),
        realm_id=uuid4(),
        project_id=uuid4(),
        work_item_id=uuid4(),
        role=AssignmentRole.BUILDER,
        agent_ref="unit-test-builder",
        instruction_digest=digest("instruction"),
        context_manifest_digest=digest("context"),
        assignment_digest=digest("assignment-placeholder"),
        risk="low",
    )


def test_result_must_bind_to_the_invocation_assignment(tmp_path: Path) -> None:
    assignment = _assignment()
    assignment = replace(assignment, assignment_digest=digest(assignment.identity_body()))
    invocation = AgentInvocation(
        id=uuid4(),
        realm_id=assignment.realm_id,
        assignment_id=assignment.id,
        client_id="local",
        execution_identity="local:test",
        invocation_digest=digest("invocation-placeholder"),
        created_at=dt.datetime.now(dt.UTC),
    )
    invocation = replace(
        invocation,
        invocation_digest=digest(
            {
                "id": str(invocation.id),
                "realm_id": str(invocation.realm_id),
                "assignment_id": str(invocation.assignment_id),
                "client_id": invocation.client_id,
                "execution_identity": invocation.execution_identity,
            }
        ),
    )
    store = CasAssignmentStore(
        CasRecordIndex(LocalContentAddressedStore(tmp_path / "cas").ensure()),
        digest("request"),
    )
    store.create(assignment)
    store.record_invocation(invocation)
    with pytest.raises(PolicyViolation, match="invocation-assignment"):
        store.store_result(
            assignment_id=uuid4(),
            invocation_id=invocation.id,
            envelope_digest=digest("envelope"),
        )
