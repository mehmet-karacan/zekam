from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.agent_graph import (
    AgentGraphRoot,
    AgentMessage,
    ChildRuntimeStatus,
    SpawnEdge,
)
from zekam.domain.agents import AgentAssignment, AssignmentRole
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_run import ExecutionRun
from zekam.domain.realm import Realm
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.agent_assignment_repository import AgentAssignmentRepository
from zekam.infrastructure.postgres.agent_graph_repository import AgentGraphRepository
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import RealmRepository
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def _assignment(
    realm_id: UUID,
    project_id: UUID,
    work_id: UUID,
    plan_id: UUID,
    *,
    role: AssignmentRole,
    parent: UUID | None,
    agent_ref: str,
    now: dt.datetime,
) -> AgentAssignment:
    draft = AgentAssignment(
        id=uuid4(),
        realm_id=realm_id,
        project_id=project_id,
        work_item_id=work_id,
        plan_id=plan_id,
        role=role,
        parent_assignment_id=parent,
        agent_ref=agent_ref,
        instruction_digest=digest(f"instruction:{agent_ref}"),
        context_manifest_digest=digest(f"context:{agent_ref}"),
        assignment_digest=digest("draft"),
        created_at=now,
    )
    values = {field: getattr(draft, field) for field in draft.__dataclass_fields__}
    return AgentAssignment(**{**values, "assignment_digest": digest(draft.identity_body())})


@pytest.fixture
def graph_scope(realm_session: tuple[Any, Any], tmp_path: Path):  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    source = tmp_path / "agent-graph-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title="Agent graph")
    policy_digest = digest("agent-graph-policy")
    plan = graph.create_plan(
        work.id,
        source_revision="revision-1",
        policy_digest=policy_digest,
        steps=(PlanStep("delegate", "Delegate", EffectKind.NONE),),
    )
    now = dt.datetime.now(dt.UTC)
    run = ExecutionRun.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        client_id="opencode",
        session_id="agent-graph-test",
        source_revision="revision-1",
        policy_digest=policy_digest,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        deadline=now + dt.timedelta(minutes=10),
        created_at=now,
    )
    runs = ExecutionRunRepository(connection, realm.id)
    runs.create_run(run)
    runs.activate_run(run.id, started_at=now)
    assignments = AgentAssignmentRepository(connection, realm.id)
    coordinator = _assignment(
        realm.id,
        project.id,
        work.id,
        plan.id,
        role=AssignmentRole.COORDINATOR,
        parent=None,
        agent_ref="coordinator",
        now=now,
    )
    assignments.create(coordinator)
    children = tuple(
        _assignment(
            realm.id,
            project.id,
            work.id,
            plan.id,
            role=AssignmentRole.RESEARCHER,
            parent=coordinator.id,
            agent_ref=f"researcher-{index}",
            now=now,
        )
        for index in range(2)
    )
    for child in children:
        assignments.create(child)
    root = AgentGraphRoot.create(
        realm_id=realm.id,
        run_id=run.id,
        coordinator_assignment_id=coordinator.id,
        max_concurrency=1,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        created_at=now,
    )
    AgentGraphRepository(connection, realm.id).create_root(root)
    return realm, connection, root, coordinator, children, now, run


def _edge(root: AgentGraphRoot, child: AgentAssignment, now: dt.datetime) -> SpawnEdge:
    return SpawnEdge.create(
        realm_id=root.realm_id,
        root_id=root.id,
        parent_assignment_id=root.coordinator_assignment_id,
        child_assignment_id=child.id,
        reserved_input_tokens=40,
        reserved_output_tokens=20,
        reserved_cost_micros=400,
        created_at=now,
    )


def test_root_budget_status_chain_and_typed_message(graph_scope) -> None:  # type: ignore[no-untyped-def]
    realm, connection, root, coordinator, children, now, _ = graph_scope
    repository = AgentGraphRepository(connection, realm.id)
    edge = _edge(root, children[0], now)
    assert repository.reserve_spawn(edge) == (edge.id, True)
    assert repository.reserve_spawn(edge) == (edge.id, False)
    assert repository.transition_child(
        edge.id, status=ChildRuntimeStatus.ACTIVE, occurred_at=now + dt.timedelta(seconds=1)
    )
    message = AgentMessage.create(
        realm_id=realm.id,
        root_id=root.id,
        sender_assignment_id=coordinator.id,
        recipient_assignment_id=children[0].id,
        context_type="assignment-context-manifest",
        context_ref=str(children[0].id),
        context_digest=children[0].context_manifest_digest,
        payload_schema="zekam-task-request/v1",
        payload={"task": "inspect"},
        created_at=now + dt.timedelta(seconds=2),
    )
    assert repository.send_message(message) == (message.id, True)
    assert repository.send_message(message) == (message.id, False)
    assert repository.transition_child(
        edge.id,
        status=ChildRuntimeStatus.COMPLETED,
        occurred_at=now + dt.timedelta(seconds=3),
        input_tokens_used=30,
        output_tokens_used=10,
        cost_micros_used=250,
    )
    assert not repository.transition_child(
        edge.id,
        status=ChildRuntimeStatus.COMPLETED,
        occurred_at=now + dt.timedelta(seconds=3),
        input_tokens_used=30,
        output_tokens_used=10,
        cost_micros_used=250,
    )
    snapshot = repository.snapshot(root.id)
    assert snapshot["usage"] == {
        "active": 0,
        "reserved_input_tokens": 0,
        "used_input_tokens": 30,
        "reserved_output_tokens": 0,
        "used_output_tokens": 10,
        "reserved_cost_micros": 0,
        "used_cost_micros": 250,
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "select sequence,previous_digest,event_digest,status"
            " from agents.child_status_event where edge_id=%s order by sequence",
            (edge.id,),
        )
        events = cursor.fetchall()
    assert [row[0] for row in events] == [1, 2, 3]
    assert events[0][1] is None and events[1][1] == events[0][2]
    assert [row[3] for row in events] == ["reserved", "active", "completed"]


def test_concurrent_spawn_has_one_parent_and_shared_slot(graph_scope, migrated_database) -> None:  # type: ignore[no-untyped-def]
    realm, connection, root, _, children, now, _ = graph_scope
    edges = tuple(
        _edge(root, child, now + dt.timedelta(microseconds=index))
        for index, child in enumerate(children)
    )

    def reserve(edge: SpawnEdge) -> bool:
        try:
            with connect(migrated_database) as worker:
                configure_session(worker, realm_id=realm.id)
                AgentGraphRepository(worker, realm.id).reserve_spawn(edge)
            return True
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(reserve, edges))
    assert sorted(outcomes) == [False, True]
    snapshot = AgentGraphRepository(connection, realm.id).snapshot(root.id)
    assert snapshot["usage"]["active"] == 1  # type: ignore[index]
    assert len(snapshot["edges"]) == 1  # type: ignore[arg-type]
    surviving_edge = UUID(str(snapshot["edges"][0]["id"]))  # type: ignore[index]
    AgentGraphRepository(connection, realm.id).transition_child(
        surviving_edge,
        status=ChildRuntimeStatus.CANCELLED,
        occurred_at=now + dt.timedelta(seconds=1),
    )
    loser = edges[outcomes.index(False)]
    assert AgentGraphRepository(connection, realm.id).reserve_spawn(loser)[1]
    duplicate = SpawnEdge.create(
        realm_id=loser.realm_id,
        root_id=loser.root_id,
        parent_assignment_id=loser.parent_assignment_id,
        child_assignment_id=loser.child_assignment_id,
        reserved_input_tokens=10,
        reserved_output_tokens=5,
        reserved_cost_micros=50,
        created_at=now + dt.timedelta(seconds=2),
    )
    with pytest.raises(Exception, match=r"unique|duplicate"):
        AgentGraphRepository(connection, realm.id).reserve_spawn(duplicate)


def test_budget_and_root_membership_are_fail_closed(graph_scope) -> None:  # type: ignore[no-untyped-def]
    realm, connection, root, _, children, now, _ = graph_scope
    repository = AgentGraphRepository(connection, realm.id)
    too_large = SpawnEdge.create(
        realm_id=realm.id,
        root_id=root.id,
        parent_assignment_id=root.coordinator_assignment_id,
        child_assignment_id=children[0].id,
        reserved_input_tokens=101,
        reserved_output_tokens=1,
        reserved_cost_micros=1,
        created_at=now,
    )
    with pytest.raises(PolicyViolation, match="butcesi"):
        repository.reserve_spawn(too_large)
    outsider = uuid4()
    with connection.cursor() as cursor, pytest.raises(Exception, match=r"foreign key|membership"):
        cursor.execute(
            "insert into agents.message"
            " (id,realm_id,root_id,sender_assignment_id,recipient_assignment_id,context_type,"
            " context_ref,context_digest,payload_schema,payload,message_body,message_digest,"
            " created_at) values (%s,%s,%s,%s,%s,'assignment-context-manifest',%s,%s,"
            " 'schema/v1','{\"task\":\"inspect\"}','{}',%s,%s)",
            (
                uuid4(),
                realm.id,
                root.id,
                root.coordinator_assignment_id,
                outsider,
                str(outsider),
                digest("context"),
                digest("message"),
                now,
            ),
        )


def test_inactive_run_and_mismatched_plan_fail_closed(graph_scope) -> None:  # type: ignore[no-untyped-def]
    realm, connection, root, coordinator, _, now, run = graph_scope
    inactive = ExecutionRun.create(
        realm_id=realm.id,
        project_id=run.project_id,
        work_item_id=run.work_item_id,
        plan_id=run.plan_id,
        client_id="opencode",
        session_id="inactive-agent-graph",
        source_revision=run.source_revision,
        policy_digest=run.policy_digest,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        deadline=now + dt.timedelta(minutes=20),
        created_at=now + dt.timedelta(seconds=1),
    )
    ExecutionRunRepository(connection, realm.id).create_run(inactive)
    inactive_root = AgentGraphRoot.create(
        realm_id=realm.id,
        run_id=inactive.id,
        coordinator_assignment_id=coordinator.id,
        max_concurrency=1,
        max_input_tokens=10,
        max_output_tokens=10,
        max_cost_micros=10,
        created_at=now + dt.timedelta(seconds=1),
    )
    with pytest.raises(Exception, match="active execution run"):
        AgentGraphRepository(connection, realm.id).create_root(inactive_root)
    alternate_plan = WorkGraphService(connection, realm).create_plan(
        run.work_item_id,
        source_revision="revision-2",
        policy_digest=run.policy_digest,
        steps=(PlanStep("alternate", "Alternate", EffectKind.NONE),),
    )
    mismatched = _assignment(
        realm.id,
        run.project_id,
        run.work_item_id,
        alternate_plan.id,
        role=AssignmentRole.RESEARCHER,
        parent=coordinator.id,
        agent_ref="wrong-plan",
        now=now,
    )
    AgentAssignmentRepository(connection, realm.id).create(mismatched)
    with pytest.raises(Exception, match="binding gecersiz"):
        AgentGraphRepository(connection, realm.id).reserve_spawn(_edge(root, mismatched, now))


def test_status_and_message_provenance_cannot_be_forged(graph_scope) -> None:  # type: ignore[no-untyped-def]
    realm, connection, root, coordinator, children, now, _ = graph_scope
    repository = AgentGraphRepository(connection, realm.id)
    edge = _edge(root, children[0], now)
    repository.reserve_spawn(edge)
    with connection.cursor() as cursor:
        cursor.execute(
            "select has_table_privilege('zekam_app','agents.graph_root','update'),"
            " has_table_privilege('zekam_app','agents.spawn_edge','insert'),"
            " has_table_privilege('zekam_app','agents.spawn_edge','update'),"
            " has_table_privilege('zekam_app','agents.child_status_event','insert')"
        )
        assert cursor.fetchone() == (False, False, False, False)
    with connection.cursor() as cursor, pytest.raises(Exception, match="permission denied"):
        cursor.execute(
            "update agents.spawn_edge set status='completed',terminal_at=%s where id=%s",
            (now + dt.timedelta(seconds=1), edge.id),
        )
    with connection.cursor() as cursor, pytest.raises(Exception, match="permission denied"):
        cursor.execute(
            "insert into agents.child_status_event"
            " (id,realm_id,root_id,edge_id,sequence,previous_digest,status,event_digest,event_body,"
            " occurred_at) values (%s,%s,%s,%s,2,%s,'completed',%s,'{}',%s)",
            (
                uuid4(),
                realm.id,
                root.id,
                edge.id,
                digest("fake-previous"),
                digest("fake-event"),
                now + dt.timedelta(seconds=1),
            ),
        )
    message_id = uuid4()
    fake_context = digest("fabricated-context")
    body = {
        "schema": "zekam-agent-message/v1",
        "id": str(message_id),
        "realm_id": str(realm.id),
        "root_id": str(root.id),
        "sender_assignment_id": str(coordinator.id),
        "recipient_assignment_id": str(children[0].id),
        "context": {
            "type": "assignment-context-manifest",
            "ref": str(children[0].id),
            "digest": fake_context,
        },
        "payload_schema": "zekam-task-request/v1",
        "payload": {"task": "inspect"},
        "created_at": now,
        "grants_authority": False,
    }
    with connection.cursor() as cursor, pytest.raises(Exception, match="canonical assignment"):
        cursor.execute(
            "insert into agents.message"
            " (id,realm_id,root_id,sender_assignment_id,recipient_assignment_id,context_type,"
            " context_ref,context_digest,payload_schema,payload,message_body,message_digest,"
            " created_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)",
            (
                message_id,
                realm.id,
                root.id,
                coordinator.id,
                children[0].id,
                "assignment-context-manifest",
                str(children[0].id),
                fake_context,
                "zekam-task-request/v1",
                canonical_json(body["payload"]),
                canonical_json(body),
                digest(body),
                now,
            ),
        )


def test_agent_graph_is_realm_isolated(graph_scope, migrated_database) -> None:  # type: ignore[no-untyped-def]
    realm, _, root, _, _, now, _ = graph_scope
    other = Realm.create(slug=f"graph-other-{uuid4().hex[:8]}")
    with connect(migrated_database) as owner:
        configure_session(owner, role=None)
        RealmRepository(owner).create(other)
    with connect(migrated_database) as worker:
        configure_session(worker, realm_id=other.id)
        with worker.cursor() as cursor:
            cursor.execute("select count(*) from agents.graph_root where id=%s", (root.id,))
            assert cursor.fetchone()[0] == 0
        with worker.cursor() as cursor, pytest.raises(Exception, match="cross-realm"):
            cursor.execute(
                "select agents.transition_graph_child(%s,%s,'active',%s,0,0,0)",
                (realm.id, uuid4(), now),
            )
