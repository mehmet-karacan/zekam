from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.agent_residency import AgentResidencyManager
from zekam.application.model_registry import load_inventory
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.agent_graph import AgentGraphRoot, SpawnEdge
from zekam.domain.agent_residency import AssignmentRuntimeSnapshot, ReloadRequest
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import AuthorityLevel, ContextCandidate, compile_context
from zekam.domain.context_fragment import (
    ContextContentKind,
    ContextFragment,
    ContextFragmentSet,
    ContextRole,
    ContextVisibility,
)
from zekam.domain.execution_environment import (
    AssignmentEnvironmentBinding,
    ExecutionEnvironmentSnapshot,
    ShellSnapshot,
    TurnExecutionSnapshot,
    detect_environment_drift,
    reprobe_snapshot,
)
from zekam.domain.execution_run import (
    CheckpointDisposition,
    ContextPacket,
    ContextPacketSection,
    ExecutionEnvelope,
    ExecutionRun,
    ProviderBindingSnapshot,
)
from zekam.domain.model_routing import (
    AgentRole,
    ExecutionTargetSnapshot,
    RoleRoutingPolicy,
    RoutingLayer,
)
from zekam.domain.realm import Realm
from zekam.domain.tool_registry import CompiledToolSet
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.agent_graph_repository import AgentGraphRepository
from zekam.infrastructure.postgres.agent_residency_repository import AgentResidencyRepository
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.core_repository import RealmRepository
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
from zekam.infrastructure.postgres.model_repository import ModelInventoryRepository
from zekam.infrastructure.postgres.model_routing_repository import ModelRoutingRepository
from zekam.infrastructure.postgres.tool_registry_repository import ToolRegistryRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
def residency_scope(realm_session: tuple[Any, Any], tmp_path: Path):  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    source = tmp_path / "residency-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    work_graph = WorkGraphService(connection, realm)
    work = work_graph.create_item(project_id=project.id, type=WorkType.TASK, title="Residency")
    policy_digest = digest("residency-policy")
    plan = work_graph.create_plan(
        work.id,
        source_revision="revision-1",
        policy_digest=policy_digest,
        steps=(PlanStep("build", "Build", EffectKind.FILE_WRITE),),
    )
    now = dt.datetime.now(dt.UTC)
    manifest = compile_context(
        (
            ContextCandidate(
                "source",
                AuthorityLevel.VERIFIED,
                now,
                "revision-1",
                digest("source"),
                5,
                True,
            ),
        ),
        token_budget=10,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=now,
    )
    continuity = ContextContinuityRepository(connection, realm.id, project.id, work.id)
    manifest_id = continuity.store_manifest(manifest)
    continuity.store_fragment_set(
        ContextFragmentSet(
            manifest.manifest_digest,
            (
                ContextFragment(
                    fragment_id="fragment/source",
                    candidate_id="source",
                    content_kind=ContextContentKind.WORK_CONTEXT,
                    role=ContextRole.USER,
                    order=0,
                    visibility=ContextVisibility.MODEL,
                    authority=AuthorityLevel.VERIFIED,
                    source_ref="work/current",
                    source_revision="revision-1",
                    content_digest=digest("source"),
                    token_count=5,
                    required=True,
                ),
            ),
        ),
        created_at=now,
    )
    execution = ExecutionRunRepository(connection, realm.id)
    run = ExecutionRun.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        client_id="opencode",
        session_id="residency-session",
        source_revision="revision-1",
        policy_digest=policy_digest,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        deadline=now + dt.timedelta(minutes=10),
        created_at=now,
    )
    execution.create_run(run)
    execution.activate_run(run.id, started_at=now)
    selected = manifest.selected[0]
    packet = ContextPacket.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        manifest_id=manifest_id,
        manifest_digest=manifest.manifest_digest,
        sections=(ContextPacketSection(selected.candidate_id, selected.content_digest, 1),),
        created_at=now,
    )
    execution.create_packet(packet)

    model = next(item for item in load_inventory().records if item.enabled)
    ModelInventoryRepository(connection, realm.id).upsert(model)
    routing = ModelRoutingRepository(connection, realm.id)
    role_policy = RoleRoutingPolicy(
        role=AgentRole.IMPLEMENTER,
        target_layer=RoutingLayer.GENERAL,
        required_layers=(RoutingLayer.GENERAL,),
        top_k=1,
        fallback_model_ids=(),
        max_cost=10,
        max_latency_ms=30_000,
        independent_from_roles=(),
        policy_digest=policy_digest,
    )
    role_policy_id = routing.store_role_policy(role_policy, effective_from=now)
    target = ExecutionTargetSnapshot(
        client_id="opencode",
        slot="default",
        execution_mode="native-sequential",
        model_selectable=True,
        structured_result=False,
        cancellation=False,
        max_concurrency=1,
        cost_evidence_digest=digest("cost"),
        capability_digest=digest("capability"),
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    target_id, _ = routing.store_execution_target(target)
    route_id = uuid4()
    route_digest = digest("residency-route")
    coordinator_id, child_id = uuid4(), uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into models.model_route_decision"
            " (id,realm_id,role_policy_id,execution_target_id,role,target_layer,inventory_digest,"
            " routing_policy_digest,policy_digest,execution_target_digest,status,primary_model_id,"
            " evidence_digest,decided_at) values"
            " (%s,%s,%s,%s,'implementer','general',%s,%s,%s,%s,'selected',%s,%s,%s)",
            (
                route_id,
                realm.id,
                role_policy_id,
                target_id,
                digest("inventory"),
                policy_digest,
                policy_digest,
                target.snapshot_digest,
                model.model_id,
                route_digest,
                now,
            ),
        )
        for assignment_id, parent, role in (
            (coordinator_id, None, "coordinator"),
            (child_id, coordinator_id, "builder"),
        ):
            cursor.execute(
                "insert into agents.assignment"
                " (id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,role,"
                " agent_ref,status,risk,instruction_digest,context_manifest_digest,"
                " assignment_digest,created_at) values"
                " (%s,%s,%s,%s,%s,'build',%s,%s,%s,'active','medium',%s,%s,%s,%s)",
                (
                    assignment_id,
                    realm.id,
                    project.id,
                    work.id,
                    plan.id,
                    parent,
                    role,
                    f"agent:{role}",
                    digest(f"instruction:{role}"),
                    manifest.manifest_digest,
                    digest(f"assignment:{role}"),
                    now,
                ),
            )
    root = AgentGraphRoot.create(
        realm_id=realm.id,
        run_id=run.id,
        coordinator_assignment_id=coordinator_id,
        max_concurrency=1,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        created_at=now,
    )
    graph_repository = AgentGraphRepository(connection, realm.id)
    graph_repository.create_root(root)
    edge = SpawnEdge.create(
        realm_id=realm.id,
        root_id=root.id,
        parent_assignment_id=coordinator_id,
        child_assignment_id=child_id,
        reserved_input_tokens=50,
        reserved_output_tokens=25,
        reserved_cost_micros=500,
        created_at=now,
    )
    graph_repository.reserve_spawn(edge)

    provider = ProviderBindingSnapshot.create(
        realm_id=realm.id,
        model_id=model.model_id,
        provider_ref=f"model:{model.model_id}",
        endpoint_ref=model.endpoint_ref,
        operation="invoke",
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    execution.create_provider_binding(provider)
    environment = ExecutionEnvironmentSnapshot.create(
        realm_id=realm.id,
        environment_id="residency-env",
        execution_identity="residency-executor",
        provider="local-process",
        platform="test-platform",
        executor_protocol_version="zekam-exec/v1",
        cwd_locator="workspace:residency/root",
        workspace_roots=("workspace:residency/root",),
        shell=ShellSnapshot("test-shell", digest("shell"), digest("profile")),
        permission_profile_id="residency-profile",
        permission_profile_digest=digest("permission"),
        filesystem_policy_digest=digest("filesystem"),
        network_policy_digest=digest("network"),
        tool_runtime_digest=digest("tools"),
        capability_digest=digest("environment-capability"),
        config_effective_digest=digest("config"),
        source_revision="revision-1",
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    execution.create_environment_snapshot(environment)
    current_environment = reprobe_snapshot(
        environment, captured_at=now, expires_at=now + dt.timedelta(minutes=10)
    )
    execution.create_environment_snapshot(current_environment)
    execution.record_environment_probe(
        detect_environment_drift(environment, current_environment, checked_at=now)
    )
    execution.bind_assignment_environment(
        AssignmentEnvironmentBinding.create(
            realm_id=realm.id,
            assignment_id=child_id,
            execution_environment_snapshot_digest=environment.snapshot_digest,
            bound_at=now,
        )
    )
    compiled_tools = CompiledToolSet.create(
        realm_id=realm.id,
        role="builder",
        permission_profile_digest=environment.permission_profile_digest,
        entries=(),
        created_at=now,
    )
    ToolRegistryRepository(connection, realm.id).store_compiled_set(compiled_tools)
    job_id, attempt_id, lease_id = uuid4(), uuid4(), uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,idempotency_key,"
            " assignment_id,run_id) values"
            " (%s,%s,%s,%s,%s,'build','provider-call','running',%s,%s,%s)",
            (job_id, realm.id, project.id, work.id, plan.id, f"job-{job_id}", child_id, run.id),
        )
        cursor.execute(
            "insert into runtime.job_attempt"
            " (id,realm_id,job_id,attempt_number,fencing_token,worker_label,started_at)"
            " values(%s,%s,%s,1,1,'residency-worker',%s)",
            (attempt_id, realm.id, job_id, now),
        )
        cursor.execute(
            "insert into runtime.lease"
            " (id,realm_id,job_id,attempt_id,owner_digest,fencing_token,worker_label,expires_at,"
            " heartbeat_at,created_at) values"
            " (%s,%s,%s,%s,%s,1,'residency-worker',%s,%s,%s)",
            (
                lease_id,
                realm.id,
                job_id,
                attempt_id,
                digest("owner"),
                now + dt.timedelta(minutes=5),
                now,
                now,
            ),
        )
    turn = TurnExecutionSnapshot.create(
        realm_id=realm.id,
        assignment_id=child_id,
        run_id=run.id,
        attempt_id=attempt_id,
        client_session_id="residency-session",
        turn_id="turn-1",
        model_id=model.model_id,
        provider_id=provider.provider_ref,
        route_decision_digest=route_digest,
        reasoning_profile_digest=digest("reasoning"),
        execution_environment_snapshot_digest=environment.snapshot_digest,
        context_manifest_digest=manifest.manifest_digest,
        exposed_tool_set_digest=compiled_tools.tool_set_digest,
        hook_set_digest=digest("hooks"),
        config_effective_digest=environment.config_effective_digest,
        created_at=now,
    )
    execution.create_turn_snapshot(turn)
    envelope = ExecutionEnvelope.create(
        realm_id=realm.id,
        run_id=run.id,
        job_id=job_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        fencing_token=1,
        request_ordinal=1,
        idempotency_key="residency-call-1",
        assignment_id=child_id,
        role="builder",
        route_decision_id=route_id,
        route_decision_digest=route_digest,
        route_expires_at=target.expires_at,
        model_id=model.model_id,
        provider_binding_id=provider.id,
        provider_binding_digest=provider.binding_digest,
        provider_ref=provider.provider_ref,
        context_manifest_id=manifest_id,
        context_manifest_digest=manifest.manifest_digest,
        context_packet_id=packet.id,
        context_packet_digest=packet.packet_digest,
        checkpoint_id=None,
        checkpoint_digest=None,
        checkpoint_disposition=CheckpointDisposition.NOT_APPLICABLE_GENESIS,
        source_revision="revision-1",
        policy_digest=policy_digest,
        authorization_scope_digest=digest("authorization"),
        output_schema_digest=digest("schema"),
        payload_digest=digest("payload"),
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        deadline=run.deadline,
        created_at=dt.datetime.now(dt.UTC),
        turn_execution_snapshot_id=turn.id,
        turn_execution_snapshot_digest=turn.turn_snapshot_digest,
    )
    execution.create_envelope(envelope)
    snapshot = AssignmentRuntimeSnapshot.create(
        realm_id=realm.id,
        edge_id=edge.id,
        assignment_id=child_id,
        execution_envelope_id=envelope.id,
        role="builder",
        model_id=model.model_id,
        provider_binding_id=provider.id,
        provider_binding_digest=provider.binding_digest,
        route_decision_id=route_id,
        route_decision_digest=route_digest,
        environment_snapshot_digest=environment.snapshot_digest,
        permission_profile_digest=environment.permission_profile_digest,
        config_effective_digest=environment.config_effective_digest,
        source_revision="revision-1",
        policy_digest=policy_digest,
        created_at=envelope.created_at,
    )
    return {
        "realm": realm,
        "connection": connection,
        "manager": AgentResidencyManager(realm.id, AgentResidencyRepository(connection, realm.id)),
        "snapshot": snapshot,
        "edge": edge,
        "child_id": child_id,
        "current_environment": current_environment,
        "environment": environment,
        "route_id": route_id,
        "provider_id": provider.id,
        "provider": provider,
        "model": model,
        "role_policy_id": role_policy_id,
        "target_id": target_id,
        "target_digest": target.snapshot_digest,
        "policy_digest": policy_digest,
        "now": snapshot.created_at,
        "execution": execution,
        "run": run,
    }


def test_evict_reload_and_dead_cleanup_are_canonical(residency_scope) -> None:  # type: ignore[no-untyped-def]
    scope = residency_scope
    manager = scope["manager"]
    snapshot = scope["snapshot"]
    edge = scope["edge"]
    now = scope["now"]
    residency_id, created = manager.register(snapshot, runtime_session_ref="runtime:child-1")
    assert created and residency_id
    assert manager.evict(edge.id, occurred_at=now + dt.timedelta(microseconds=1))
    evicted = manager.status(edge.id)
    assert evicted["state"] == "evicted"
    assert evicted["runtime_session_ref"] is None
    assert evicted["grants_authority"] is False
    with scope["connection"].cursor() as cursor:
        cursor.execute("select status from agents.spawn_edge where id=%s", (edge.id,))
        assert cursor.fetchone()[0] == "reserved"
        cursor.execute("select status from agents.assignment where id=%s", (scope["child_id"],))
        assert cursor.fetchone()[0] == "active"
        cursor.execute("select active_count from agents.graph_root where id=%s", (edge.root_id,))
        assert cursor.fetchone()[0] == 1
    request = ReloadRequest.create(
        realm_id=scope["realm"].id,
        edge_id=edge.id,
        current_environment_snapshot_digest=scope["current_environment"].snapshot_digest,
        current_route_decision_id=scope["route_id"],
        current_provider_binding_id=scope["provider_id"],
        runtime_session_ref="runtime:child-2",
        requested_at=now + dt.timedelta(microseconds=2),
    )
    result = manager.reload(request)
    assert result.loaded and result.generation == 2 and result.reason is None
    assert manager.status(edge.id)["runtime_session_ref"] == "runtime:child-2"
    assert manager.evict(edge.id, occurred_at=now + dt.timedelta(microseconds=3))

    drift_time = dt.datetime.now(dt.UTC)
    drifted = reprobe_snapshot(
        scope["environment"],
        captured_at=drift_time,
        expires_at=drift_time + dt.timedelta(minutes=10),
        capability_digest=digest("drifted-capability"),
    )
    scope["execution"].create_environment_snapshot(drifted)
    scope["execution"].record_environment_probe(
        detect_environment_drift(scope["environment"], drifted, checked_at=drift_time)
    )
    rejected = manager.reload(
        ReloadRequest.create(
            realm_id=scope["realm"].id,
            edge_id=edge.id,
            current_environment_snapshot_digest=drifted.snapshot_digest,
            current_route_decision_id=scope["route_id"],
            current_provider_binding_id=scope["provider_id"],
            runtime_session_ref="runtime:unsafe",
            requested_at=drift_time,
        )
    )
    assert not rejected.loaded and rejected.reason == "environment-drift"
    assert manager.status(edge.id)["state"] == "evicted"
    assert manager.mark_dead(edge.id, occurred_at=drift_time, reason="runtime-lost")
    dead = manager.status(edge.id)
    assert dead["state"] == "dead" and dead["dead_reason"] == "runtime-lost"
    with scope["connection"].cursor() as cursor:
        cursor.execute("select status from agents.spawn_edge where id=%s", (edge.id,))
        assert cursor.fetchone()[0] == "reserved"
        cursor.execute("select status from agents.assignment where id=%s", (scope["child_id"],))
        assert cursor.fetchone()[0] == "recovery-required"
        cursor.execute(
            "select count(*) from app_server.notification_event"
            " where event_type='agent.residency.changed'"
        )
        assert cursor.fetchone()[0] == 5
        cursor.execute(
            "select active_count,reserved_input_tokens,reserved_output_tokens,"
            " reserved_cost_micros from agents.graph_root where id=%s",
            (edge.root_id,),
        )
        assert cursor.fetchone() == (1, 50, 25, 500)
        cursor.execute("select accepted,reason from agents.reload_attempt order by attempted_at")
        assert cursor.fetchall() == [(True, None), (False, "environment-drift")]


def test_concurrent_reload_loads_only_one_runtime(residency_scope, migrated_database) -> None:  # type: ignore[no-untyped-def]
    scope = residency_scope
    manager = scope["manager"]
    snapshot = scope["snapshot"]
    edge = scope["edge"]
    now = scope["now"]
    manager.register(snapshot, runtime_session_ref="runtime:child-1")
    manager.evict(edge.id, occurred_at=now + dt.timedelta(microseconds=1))
    requests = tuple(
        ReloadRequest.create(
            realm_id=scope["realm"].id,
            edge_id=edge.id,
            current_environment_snapshot_digest=scope["current_environment"].snapshot_digest,
            current_route_decision_id=scope["route_id"],
            current_provider_binding_id=scope["provider_id"],
            runtime_session_ref=f"runtime:concurrent-{index}",
            requested_at=now + dt.timedelta(microseconds=2),
        )
        for index in range(2)
    )

    def reload(request: ReloadRequest) -> bool:
        with connect(migrated_database) as worker:
            configure_session(worker, realm_id=scope["realm"].id)
            return AgentResidencyRepository(worker, scope["realm"].id).reload(request).loaded

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(reload, requests))
    assert sorted(outcomes) == [False, True]
    status = manager.status(edge.id)
    assert status["state"] == "loaded" and status["generation"] == 2
    assert [event["state"] for event in status["events"]] == ["loaded", "evicted", "loaded"]


def test_backdated_reload_request_cannot_reuse_old_health_evidence(residency_scope) -> None:  # type: ignore[no-untyped-def]
    scope = residency_scope
    manager = scope["manager"]
    edge = scope["edge"]
    now = scope["now"]
    manager.register(scope["snapshot"], runtime_session_ref="runtime:child-1")
    manager.evict(edge.id, occurred_at=now + dt.timedelta(microseconds=1))
    with pytest.raises(Exception, match="temporal provenance"):
        manager.reload(
            ReloadRequest.create(
                realm_id=scope["realm"].id,
                edge_id=edge.id,
                current_environment_snapshot_digest=scope["current_environment"].snapshot_digest,
                current_route_decision_id=scope["route_id"],
                current_provider_binding_id=scope["provider_id"],
                runtime_session_ref="runtime:backdated",
                requested_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=31),
            )
        )


def test_reload_rejects_superseded_route_observation(residency_scope) -> None:  # type: ignore[no-untyped-def]
    scope = residency_scope
    manager = scope["manager"]
    edge = scope["edge"]
    now = scope["now"]
    manager.register(scope["snapshot"], runtime_session_ref="runtime:child-1")
    manager.evict(edge.id, occurred_at=now + dt.timedelta(microseconds=1))

    newer_route_id = uuid4()
    observed_at = dt.datetime.now(dt.UTC)
    with scope["connection"].cursor() as cursor:
        cursor.execute(
            "insert into models.model_route_decision"
            " (id,realm_id,role_policy_id,execution_target_id,role,target_layer,inventory_digest,"
            " routing_policy_digest,policy_digest,execution_target_digest,status,primary_model_id,"
            " evidence_digest,decided_at) values"
            " (%s,%s,%s,%s,'implementer','general',%s,%s,%s,%s,'selected',%s,%s,%s)",
            (
                newer_route_id,
                scope["realm"].id,
                scope["role_policy_id"],
                scope["target_id"],
                digest("inventory:newer"),
                scope["policy_digest"],
                scope["policy_digest"],
                scope["target_digest"],
                scope["model"].model_id,
                digest("residency-route:newer"),
                observed_at,
            ),
        )
    rejected_route = manager.reload(
        ReloadRequest.create(
            realm_id=scope["realm"].id,
            edge_id=edge.id,
            current_environment_snapshot_digest=scope["current_environment"].snapshot_digest,
            current_route_decision_id=scope["route_id"],
            current_provider_binding_id=scope["provider_id"],
            runtime_session_ref="runtime:old-route",
            requested_at=observed_at,
        )
    )
    assert not rejected_route.loaded and rejected_route.reason == "route-drift"


def test_reload_rejects_superseded_provider_observation(residency_scope) -> None:  # type: ignore[no-untyped-def]
    scope = residency_scope
    manager = scope["manager"]
    edge = scope["edge"]
    now = scope["now"]
    manager.register(scope["snapshot"], runtime_session_ref="runtime:child-1")
    manager.evict(edge.id, occurred_at=now + dt.timedelta(microseconds=1))
    observed_at = dt.datetime.now(dt.UTC)
    newer_provider = ProviderBindingSnapshot.create(
        realm_id=scope["realm"].id,
        model_id=scope["provider"].model_id,
        provider_ref=scope["provider"].provider_ref,
        endpoint_ref=scope["provider"].endpoint_ref,
        operation=scope["provider"].operation,
        captured_at=observed_at,
        expires_at=observed_at + dt.timedelta(minutes=10),
    )
    scope["execution"].create_provider_binding(newer_provider)
    rejected_provider = manager.reload(
        ReloadRequest.create(
            realm_id=scope["realm"].id,
            edge_id=edge.id,
            current_environment_snapshot_digest=scope["current_environment"].snapshot_digest,
            current_route_decision_id=scope["route_id"],
            current_provider_binding_id=scope["provider_id"],
            runtime_session_ref="runtime:old-provider",
            requested_at=dt.datetime.now(dt.UTC),
        )
    )
    assert not rejected_provider.loaded and rejected_provider.reason == "provider-drift"


def test_terminal_run_blocks_reload_and_closing_dead_is_visible(residency_scope) -> None:  # type: ignore[no-untyped-def]
    scope = residency_scope
    manager = scope["manager"]
    snapshot = scope["snapshot"]
    edge = scope["edge"]
    now = scope["now"]
    manager.register(snapshot, runtime_session_ref="runtime:child-1")
    manager.evict(edge.id, occurred_at=now + dt.timedelta(microseconds=1))
    terminal_at = dt.datetime.now(dt.UTC)
    scope["execution"].finish_run(scope["run"].id, state="cancelled", terminal_at=terminal_at)
    result = manager.reload(
        ReloadRequest.create(
            realm_id=scope["realm"].id,
            edge_id=edge.id,
            current_environment_snapshot_digest=scope["current_environment"].snapshot_digest,
            current_route_decision_id=scope["route_id"],
            current_provider_binding_id=scope["provider_id"],
            runtime_session_ref="runtime:blocked",
            requested_at=terminal_at,
        )
    )
    assert not result.loaded and result.reason == "source-policy-drift"
    assert manager.mark_dead(edge.id, occurred_at=terminal_at, reason="run-terminal")
    assert manager.status(edge.id)["state"] == "dead"


def test_idle_closing_dead_state_chain_is_stable(residency_scope) -> None:  # type: ignore[no-untyped-def]
    scope = residency_scope
    manager = scope["manager"]
    edge = scope["edge"]
    now = scope["now"]
    manager.register(scope["snapshot"], runtime_session_ref="runtime:child-1")
    assert manager.mark_idle(edge.id, occurred_at=now + dt.timedelta(microseconds=1))
    assert manager.begin_close(edge.id, occurred_at=now + dt.timedelta(microseconds=2))
    assert manager.mark_dead(
        edge.id, occurred_at=now + dt.timedelta(microseconds=3), reason="cleaner-timeout"
    )
    status = manager.status(edge.id)
    assert [event["state"] for event in status["events"]] == [
        "loaded",
        "idle",
        "closing",
        "dead",
    ]


def test_runtime_snapshot_forgery_and_direct_residency_writes_fail(residency_scope) -> None:  # type: ignore[no-untyped-def]
    scope = residency_scope
    snapshot = scope["snapshot"]
    manager = scope["manager"]
    manager.register(snapshot, runtime_session_ref="runtime:child-1")
    connection = scope["connection"]
    with connection.cursor() as cursor:
        cursor.execute(
            "select has_table_privilege('zekam_app','agents.runtime_residency','insert'),"
            " has_table_privilege('zekam_app','agents.runtime_residency','update'),"
            " has_table_privilege('zekam_app','agents.residency_event','insert')"
        )
        assert cursor.fetchone() == (False, False, False)
    with connection.cursor() as cursor, pytest.raises(Exception, match="permission denied"):
        cursor.execute(
            "update agents.runtime_residency set state='dead' where edge_id=%s",
            (snapshot.edge_id,),
        )
    forged = AssignmentRuntimeSnapshot.create(
        **{
            **{field: getattr(snapshot, field) for field in snapshot.__dataclass_fields__},
            "id": uuid4(),
            "edge_id": uuid4(),
            "snapshot_digest": "",
        }
    )
    with pytest.raises(Exception, match=r"foreign key|binding drift"):
        AgentResidencyRepository(connection, scope["realm"].id).register_loaded(
            forged, runtime_session_ref="runtime:forged"
        )


def test_residency_is_realm_isolated(residency_scope, migrated_database) -> None:  # type: ignore[no-untyped-def]
    scope = residency_scope
    manager = scope["manager"]
    manager.register(scope["snapshot"], runtime_session_ref="runtime:child-1")
    other = Realm.create(slug=f"residency-other-{uuid4().hex[:8]}")
    with connect(migrated_database) as owner:
        configure_session(owner, role=None)
        RealmRepository(owner).create(other)
    with connect(migrated_database) as worker:
        configure_session(worker, realm_id=other.id)
        with worker.cursor() as cursor:
            cursor.execute(
                "select count(*) from agents.runtime_residency where edge_id=%s",
                (scope["edge"].id,),
            )
            assert cursor.fetchone()[0] == 0
        with worker.cursor() as cursor, pytest.raises(Exception, match="cross-realm"):
            cursor.execute(
                "select agents.transition_runtime_residency(%s,%s,'evicted',%s,null)",
                (scope["realm"].id, scope["edge"].id, dt.datetime.now(dt.UTC)),
            )
