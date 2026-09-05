from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.agents import AgentAssignment, AgentInvocation, AssignmentRole
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.realm import Realm
from zekam.infrastructure.postgres.agent_assignment_repository import AgentAssignmentRepository
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import RealmRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 24, 13, 0, tzinfo=dt.UTC)
D = "sha256:" + "a" * 64


@pytest.fixture
def scope(realm_session: tuple[Realm, Any], tmp_path: Path):  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    root = tmp_path / "source"
    root.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=root)
    work_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into work.work_item"
            " (id,realm_id,project_id,type,state,title,record_digest)"
            " values (%s,%s,%s,'task','active','assignment test',%s)",
            (work_id, realm.id, project.id, D),
        )
    return realm, connection, project.id, work_id


def _make(
    realm_id: UUID,
    project_id: UUID,
    work_id: UUID,
    *,
    role=AssignmentRole.COORDINATOR,
    parent=None,
    agent="coordinator",
    risk="medium",
    writes=(),
):  # type: ignore[no-untyped-def]
    item = AgentAssignment(
        id=uuid4(),
        realm_id=realm_id,
        project_id=project_id,
        work_item_id=work_id,
        role=role,
        parent_assignment_id=parent,
        agent_ref=agent,
        risk=risk,
        instruction_digest=D,
        context_manifest_digest=D,
        assignment_digest=D,
        write_resources=tuple(writes),
        created_at=NOW,
    )
    return AgentAssignment(
        **{
            **{field: getattr(item, field) for field in item.__dataclass_fields__},
            "assignment_digest": digest(item.identity_body()),
        }
    )


def _invocation(assignment: AgentAssignment, execution="exec-1"):  # type: ignore[no-untyped-def]
    item_id = uuid4()
    body = {
        "id": str(item_id),
        "realm_id": str(assignment.realm_id),
        "assignment_id": str(assignment.id),
        "client_id": "opencode",
        "execution_identity": execution,
    }
    return AgentInvocation(
        item_id, assignment.realm_id, assignment.id, "opencode", execution, digest(body), NOW
    )


def test_create_is_idempotent_and_invocation_requires_assignment(scope) -> None:  # type: ignore[no-untyped-def]
    realm, connection, project_id, work_id = scope
    repository = AgentAssignmentRepository(connection, realm.id)
    coordinator = _make(realm.id, project_id, work_id)
    assert repository.create(coordinator) == (coordinator.id, True)
    assert repository.create(coordinator) == (coordinator.id, False)
    invocation = _invocation(coordinator)
    assert repository.record_invocation(invocation) == (invocation.id, True)
    assert repository.record_invocation(invocation) == (invocation.id, False)
    missing_id, missing_assignment = uuid4(), uuid4()
    missing_body = {
        "id": str(missing_id),
        "realm_id": str(realm.id),
        "assignment_id": str(missing_assignment),
        "client_id": "opencode",
        "execution_identity": "missing",
    }
    missing = AgentInvocation(
        missing_id, realm.id, missing_assignment, "opencode", "missing", digest(missing_body), NOW
    )
    with pytest.raises(Exception, match="foreign key"):
        repository.record_invocation(missing)


def test_child_binding_write_conflict_and_exact_runtime_lock(  # type: ignore[no-untyped-def]
    scope, migrated_database
) -> None:
    realm, connection, project_id, work_id = scope
    repository = AgentAssignmentRepository(connection, realm.id)
    coordinator = _make(realm.id, project_id, work_id)
    repository.create(coordinator)
    first = _make(
        realm.id,
        project_id,
        work_id,
        role=AssignmentRole.BUILDER,
        parent=coordinator.id,
        agent="builder-a",
        writes=("path:repo:src",),
    )
    repository.create(first)
    conflict = _make(
        realm.id,
        project_id,
        work_id,
        role=AssignmentRole.BUILDER,
        parent=coordinator.id,
        agent="builder-b",
        writes=("path:repo:src/a.py",),
    )
    with pytest.raises(Exception, match="ownership catismasi"):
        repository.create(conflict)
    job_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,kind,idempotency_key,assignment_id)"
            " values (%s,%s,%s,%s,'mutation',%s,%s)",
            (job_id, realm.id, project_id, work_id, f"job-{job_id}", first.id),
        )
        cursor.execute(
            "insert into runtime.resource_lock"
            " (id,realm_id,resource,mode,job_id) values (%s,%s,%s,'write',%s)",
            (lock_id := uuid4(), realm.id, "path:repo:src", job_id),
        )
        with pytest.raises(Exception, match=r"exact declare|append-only"):
            cursor.execute(
                "update runtime.resource_lock set resource='path:repo:other' where id=%s",
                (lock_id,),
            )
        with pytest.raises(Exception, match="exact declare"):
            cursor.execute(
                "insert into runtime.resource_lock"
                " (id,realm_id,resource,mode,job_id) values (%s,%s,%s,'read',%s)",
                (uuid4(), realm.id, "path:repo:other", job_id),
            )
        legacy_job_id = uuid4()
        cursor.execute(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,kind,idempotency_key)"
            " values (%s,%s,%s,%s,'mutation',%s)",
            (legacy_job_id, realm.id, project_id, work_id, f"job-{legacy_job_id}"),
        )
        cursor.execute(
            "insert into runtime.resource_lock"
            " (id,realm_id,resource,mode,job_id) values (%s,%s,%s,'write',%s)",
            (uuid4(), realm.id, "path:repo:legacy", legacy_job_id),
        )
        with pytest.raises(Exception, match="assignment kimligi"):
            cursor.execute(
                "update runtime.job set assignment_id=%s where id=%s",
                (first.id, legacy_job_id),
            )
        with pytest.raises(Exception, match=r"mutation yasak|permission denied"):
            cursor.execute(
                "update agents.assignment_resource set resource='path:repo:other'"
                " where realm_id=%s and assignment_id=%s and mode='write'",
                (realm.id, first.id),
            )
        with pytest.raises(Exception, match=r"mutation yasak|permission denied"):
            cursor.execute(
                "delete from agents.assignment_resource"
                " where realm_id=%s and assignment_id=%s and mode='write'",
                (realm.id, first.id),
            )
    with connect(migrated_database) as owner:
        configure_session(owner, role=None)
        with owner.cursor() as cursor, pytest.raises(Exception, match="append-only"):
            cursor.execute(
                "update runtime.resource_lock set resource='path:repo:other' where id=%s",
                (lock_id,),
            )


def test_coordinator_result_terminal_immutability_and_verifier_gate(scope) -> None:  # type: ignore[no-untyped-def]
    realm, connection, project_id, work_id = scope
    repository = AgentAssignmentRepository(connection, realm.id)
    coordinator = _make(realm.id, project_id, work_id)
    repository.create(coordinator)
    cinv = _invocation(coordinator)
    repository.record_invocation(cinv)
    with pytest.raises(PolicyViolation, match="Koordinator"):
        repository.store_result(
            assignment_id=coordinator.id, invocation_id=cinv.id, envelope_digest=D
        )
    builder = _make(
        realm.id,
        project_id,
        work_id,
        role=AssignmentRole.BUILDER,
        parent=coordinator.id,
        agent="builder",
        risk="high",
    )
    repository.create(builder)
    builder_invocation = _invocation(builder, "shared")
    repository.record_invocation(builder_invocation)
    with pytest.raises(PolicyViolation, match="verifier"):
        repository.assert_verifier_gate(builder.id)
    with pytest.raises(Exception, match="verifier"):
        repository.store_result(
            assignment_id=builder.id,
            invocation_id=builder_invocation.id,
            envelope_digest=D,
        )
    verifier = _make(
        realm.id,
        project_id,
        work_id,
        role=AssignmentRole.VERIFIER,
        parent=coordinator.id,
        agent="verifier",
    )
    repository.create(verifier)
    repository.record_invocation(_invocation(verifier, "verify-exec"))
    repository.assert_verifier_gate(builder.id)
    repository.store_result(
        assignment_id=builder.id,
        invocation_id=builder_invocation.id,
        envelope_digest=D,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "update agents.assignment set status='completed',terminal_at=%s where id=%s",
            (NOW, builder.id),
        )
        with pytest.raises(Exception, match="terminal assignment"):
            cursor.execute(
                "update agents.assignment set status='failed' where id=%s", (builder.id,)
            )


def test_high_risk_verifier_must_have_distinct_agent_and_execution(scope) -> None:  # type: ignore[no-untyped-def]
    realm, connection, project_id, work_id = scope
    repository = AgentAssignmentRepository(connection, realm.id)
    coordinator = _make(realm.id, project_id, work_id)
    repository.create(coordinator)
    builder = _make(
        realm.id,
        project_id,
        work_id,
        role=AssignmentRole.BUILDER,
        parent=coordinator.id,
        agent="same-agent",
        risk="critical",
    )
    repository.create(builder)
    repository.record_invocation(_invocation(builder, "same-execution"))
    verifier = _make(
        realm.id,
        project_id,
        work_id,
        role=AssignmentRole.VERIFIER,
        parent=coordinator.id,
        agent="same-agent",
    )
    repository.create(verifier)
    repository.record_invocation(_invocation(verifier, "same-execution"))
    with pytest.raises(PolicyViolation, match="verifier"):
        repository.assert_verifier_gate(builder.id)


def test_cross_realm_rls_hides_assignments(scope, migrated_database) -> None:  # type: ignore[no-untyped-def]
    realm, connection, project_id, work_id = scope
    repository = AgentAssignmentRepository(connection, realm.id)
    repository.create(_make(realm.id, project_id, work_id))
    other = Realm.create(slug=f"other-{uuid4().hex[:8]}")
    with connect(migrated_database) as owner:
        configure_session(owner, role=None)
        RealmRepository(owner).create(other)
    with connect(migrated_database) as second:
        configure_session(second, realm_id=other.id)
        with second.cursor() as cursor:
            cursor.execute("select count(*) from agents.assignment where realm_id=%s", (realm.id,))
            assert cursor.fetchone()[0] == 0


def test_concurrent_builders_cannot_own_same_write_resource(scope, migrated_database) -> None:  # type: ignore[no-untyped-def]
    realm, connection, project_id, work_id = scope
    root_repository = AgentAssignmentRepository(connection, realm.id)
    coordinator = _make(realm.id, project_id, work_id)
    root_repository.create(coordinator)
    builders = tuple(
        _make(
            realm.id,
            project_id,
            work_id,
            role=AssignmentRole.BUILDER,
            parent=coordinator.id,
            agent=f"builder-{index}",
            writes=("path:repo:shared",),
        )
        for index in range(2)
    )

    def create_builder(builder: AgentAssignment) -> bool:
        try:
            with connect(migrated_database) as worker:
                configure_session(worker, realm_id=realm.id)
                AgentAssignmentRepository(worker, realm.id).create(builder)
            return True
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(create_builder, builders))
    assert sorted(outcomes) == [False, True]
