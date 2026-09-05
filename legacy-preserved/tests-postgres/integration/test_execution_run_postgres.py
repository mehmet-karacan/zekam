from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import Error as PsycopgError

from zekam.application.execution import ExecutionHost
from zekam.application.model_catalog import catalog_refresh_plan_digest
from zekam.application.model_gateway import ModelGateway
from zekam.application.model_registry import load_inventory
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    Checkpoint,
    ContextCandidate,
    compile_context,
)
from zekam.domain.context_fragment import (
    ContextContentKind,
    ContextFragment,
    ContextFragmentSet,
    ContextRole,
    ContextVisibility,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_environment import (
    AssignmentEnvironmentBinding,
    EnvironmentDriftReport,
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
from zekam.domain.model_catalog import (
    CatalogFetchProvenance,
    CatalogFetchStatus,
    CatalogReceiptStatus,
    CatalogRefreshStrategy,
    CatalogSource,
    CatalogVisibility,
    ModelCatalogEntry,
    ModelCatalogSnapshot,
    catalog_fetch_response_digest,
)
from zekam.domain.model_invocation import GatewaySourceLabel, ModelRequestManifest
from zekam.domain.model_routing import (
    AgentRole,
    ExecutionTargetSnapshot,
    RoleRoutingPolicy,
    RoutingLayer,
)
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.runtime import EffectClaim, EffectReceipt
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.tool_registry import (
    CompiledToolSet,
    ToolDispatchBinding,
    ToolExposure,
    ToolRuntimeRevision,
    ToolSetEntry,
    ToolSpecRevision,
)
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
from zekam.infrastructure.postgres.model_catalog_repository import ModelCatalogRepository
from zekam.infrastructure.postgres.model_invocation_repository import ModelInvocationRepository
from zekam.infrastructure.postgres.model_repository import ModelInventoryRepository
from zekam.infrastructure.postgres.model_routing_repository import ModelRoutingRepository
from zekam.infrastructure.postgres.runtime_repository import EffectLedger
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository
from zekam.infrastructure.postgres.tool_registry_repository import ToolRegistryRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_execution_run_and_exact_context_packet_roundtrip(
    realm_session: tuple[Any, Any], migrated_database: Any, tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "execution-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title="Execution")
    policy_digest = digest("execution-policy")
    plan = graph.create_plan(
        work.id,
        source_revision="revision-1",
        policy_digest=policy_digest,
        steps=(PlanStep("build", "Build", EffectKind.FILE_WRITE),),
    )
    now = dt.datetime.now(dt.UTC)
    manifest = compile_context(
        (
            ContextCandidate(
                "required-source",
                AuthorityLevel.VERIFIED,
                now,
                "revision-1",
                digest("source-content"),
                10,
                True,
            ),
        ),
        token_budget=20,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=now,
    )
    continuity_repository = ContextContinuityRepository(connection, realm.id, project.id, work.id)
    manifest_id = continuity_repository.store_manifest(manifest)
    fragment_set = ContextFragmentSet(
        manifest.manifest_digest,
        (
            ContextFragment(
                fragment_id="fragment/required-source",
                candidate_id="required-source",
                content_kind=ContextContentKind.WORK_CONTEXT,
                role=ContextRole.USER,
                order=0,
                visibility=ContextVisibility.MODEL,
                authority=AuthorityLevel.VERIFIED,
                source_ref="work/current",
                source_revision="revision-1",
                content_digest=digest("source-content"),
                token_count=10,
                required=True,
            ),
        ),
    )
    continuity_repository.store_fragment_set(fragment_set, created_at=now)
    repository = ExecutionRunRepository(connection, realm.id)
    run = ExecutionRun.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        client_id="opencode",
        session_id="session-1",
        source_revision="revision-1",
        policy_digest=policy_digest,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        deadline=now + dt.timedelta(minutes=10),
        created_at=now,
    )
    assert repository.create_run(run) == (run.id, True)
    assert repository.create_run(run) == (run.id, False)
    repository.activate_run(run.id, started_at=now)
    repository.record_usage(
        run.id, input_tokens_used=10, output_tokens_used=5, cost_micros_used=100
    )
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "update runtime.execution_run set started_at=%s where id=%s",
            (now + dt.timedelta(seconds=2), run.id),
        )
    connection.rollback()

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
    assert repository.create_packet(packet) == (packet.id, True)
    assert repository.create_packet(packet) == (packet.id, False)
    with pytest.raises(PsycopgError):
        repository.create_packet(
            replace(
                packet,
                id=type(packet.id)(int=packet.id.int + 1),
                sections=(ContextPacketSection("forged-source", digest("forged"), 1),),
                packet_digest="",
            )
        )
    connection.rollback()
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into work.context_packet"
            "(id,realm_id,project_id,work_item_id,manifest_id,manifest_digest,"
            "ordered_sections,packet_digest,created_at) values"
            "(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
            (
                UUID(int=packet.id.int + 2),
                realm.id,
                project.id,
                work.id,
                manifest_id,
                manifest.manifest_digest,
                '[{"candidate_id":"required-source","content_digest":"'
                + selected.content_digest
                + '","ordinal":1,"forged":true}]',
                digest("extra-key-packet"),
                now,
            ),
        )
    connection.rollback()

    second = ExecutionRun.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        client_id="opencode",
        session_id="session-2",
        source_revision="revision-1",
        policy_digest=policy_digest,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        deadline=now + dt.timedelta(minutes=10),
        created_at=now + dt.timedelta(microseconds=1),
    )
    repository.create_run(second)
    job_id = UUID(int=run.id.int + 3)
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.job"
            "(id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,"
            "idempotency_key,run_id) values(%s,%s,%s,%s,%s,'build','provider-call','ready',%s,%s)",
            (job_id, realm.id, project.id, work.id, plan.id, f"run-job-{job_id}", run.id),
        )
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute("update runtime.job set run_id=%s where id=%s", (second.id, job_id))
    connection.rollback()

    model = next(record for record in load_inventory().records if record.enabled)
    ModelInventoryRepository(connection, realm.id).upsert(model)
    routing = ModelRoutingRepository(connection, realm.id)
    policy = RoleRoutingPolicy(
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
    policy_id = routing.store_role_policy(policy, effective_from=now)
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
    provider_binding = ProviderBindingSnapshot.create(
        realm_id=realm.id,
        model_id=model.model_id,
        provider_ref=f"model:{model.model_id}",
        endpoint_ref=model.endpoint_ref,
        operation="invoke",
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    repository.create_provider_binding(provider_binding)
    catalog = ModelCatalogSnapshot(
        id=uuid4(),
        realm_id=realm.id,
        provider_id="aihub",
        entries=(
            ModelCatalogEntry(
                model.model_id,
                CatalogVisibility.AUTHENTICATED,
                True,
                "chat-completions",
                ("text",),
            ),
        ),
        etag=None,
        fetched_at=now,
        expires_at=now + dt.timedelta(minutes=10),
        client_version="zekam-test/1",
        source=CatalogSource.PACKAGE,
        fetch_status=CatalogFetchStatus.FETCHED,
        error_category=None,
    )
    ModelCatalogRepository(connection, realm.id).store(catalog)
    decision_id = UUID(int=run.id.int + 4)
    route_digest = digest("selected-route")
    coordinator_id = UUID(int=run.id.int + 5)
    builder_id = UUID(int=run.id.int + 6)
    attempt_id = UUID(int=run.id.int + 7)
    lease_id = UUID(int=run.id.int + 8)
    envelope_job_id = UUID(int=run.id.int + 9)
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into models.model_route_decision"
            "(id,realm_id,role_policy_id,execution_target_id,role,target_layer,"
            "inventory_digest,routing_policy_digest,policy_digest,execution_target_digest,"
            "catalog_provider_id,catalog_digest,catalog_snapshot_digest,catalog_snapshot_id,"
            "status,primary_model_id,evidence_digest,decided_at) values"
            "(%s,%s,%s,%s,'implementer','general',%s,%s,%s,%s,%s,%s,%s,%s,"
            "'selected',%s,%s,%s)",
            (
                decision_id,
                realm.id,
                policy_id,
                target_id,
                digest("inventory"),
                policy_digest,
                policy_digest,
                target.snapshot_digest,
                catalog.provider_id,
                catalog.catalog_digest,
                catalog.snapshot_digest,
                catalog.id,
                model.model_id,
                route_digest,
                now,
            ),
        )
        for assignment_id, parent, role in (
            (coordinator_id, None, "coordinator"),
            (builder_id, coordinator_id, "builder"),
        ):
            cursor.execute(
                "insert into agents.assignment"
                "(id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,"
                "role,agent_ref,status,risk,instruction_digest,context_manifest_digest,"
                "assignment_digest,created_at) values"
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,'active','medium',%s,%s,%s,%s)",
                (
                    assignment_id,
                    realm.id,
                    project.id,
                    work.id,
                    plan.id,
                    "build",
                    parent,
                    role,
                    f"agent:{role}",
                    digest(f"instruction:{role}"),
                    manifest.manifest_digest,
                    digest(f"assignment:{role}"),
                    now,
                ),
            )
        cursor.execute(
            "insert into runtime.job"
            "(id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,idempotency_key,"
            "assignment_id,run_id) values"
            "(%s,%s,%s,%s,%s,'build','provider-call','running',%s,%s,%s)",
            (
                envelope_job_id,
                realm.id,
                project.id,
                work.id,
                plan.id,
                f"envelope-job-{envelope_job_id}",
                builder_id,
                run.id,
            ),
        )
        cursor.execute(
            "insert into runtime.job_attempt"
            "(id,realm_id,job_id,attempt_number,fencing_token,worker_label,started_at)"
            " values(%s,%s,%s,1,1,'execution-test',%s)",
            (attempt_id, realm.id, envelope_job_id, now + dt.timedelta(seconds=1)),
        )
        cursor.execute(
            "insert into runtime.lease"
            "(id,realm_id,job_id,attempt_id,owner_digest,fencing_token,worker_label,"
            "expires_at,heartbeat_at,created_at) values"
            "(%s,%s,%s,%s,%s,1,'execution-test',%s,%s,%s)",
            (
                lease_id,
                realm.id,
                envelope_job_id,
                attempt_id,
                digest("owner"),
                now + dt.timedelta(minutes=5),
                now,
                now,
            ),
        )
    common = {
        "realm_id": realm.id,
        "run_id": run.id,
        "job_id": envelope_job_id,
        "attempt_id": attempt_id,
        "lease_id": lease_id,
        "fencing_token": 1,
        "assignment_id": builder_id,
        "role": "builder",
        "route_decision_id": decision_id,
        "route_decision_digest": route_digest,
        "route_expires_at": target.expires_at,
        "model_id": model.model_id,
        "provider_binding_id": provider_binding.id,
        "provider_binding_digest": provider_binding.binding_digest,
        "provider_ref": provider_binding.provider_ref,
        "context_manifest_id": manifest_id,
        "context_manifest_digest": manifest.manifest_digest,
        "context_packet_id": packet.id,
        "context_packet_digest": packet.packet_digest,
        "checkpoint_id": None,
        "checkpoint_digest": None,
        "checkpoint_disposition": CheckpointDisposition.NOT_APPLICABLE_GENESIS,
        "source_revision": "revision-1",
        "policy_digest": policy_digest,
        "authorization_scope_digest": digest("authorization"),
        "output_schema_digest": digest("schema"),
        "max_input_tokens": 100,
        "max_output_tokens": 50,
        "max_cost_micros": 1_000,
        "deadline": run.deadline,
        "created_at": dt.datetime.now(dt.UTC),
    }
    environment = ExecutionEnvironmentSnapshot.create(
        realm_id=realm.id,
        environment_id="env-execution-test",
        execution_identity="execution-test",
        provider="local-process",
        platform="test-platform",
        executor_protocol_version="zekam-exec/v1",
        cwd_locator="workspace:execution/root",
        workspace_roots=("workspace:execution/root",),
        shell=ShellSnapshot("test-shell", digest("shell"), digest("profile")),
        permission_profile_id="test-profile",
        permission_profile_digest=digest("permission"),
        filesystem_policy_digest=digest("filesystem"),
        network_policy_digest=digest("network"),
        tool_runtime_digest=digest("tool-runtime"),
        capability_digest=digest("environment-capability"),
        config_effective_digest=digest("config"),
        source_revision="revision-1",
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    repository.create_environment_snapshot(environment)
    current_environment = reprobe_snapshot(
        environment,
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    repository.create_environment_snapshot(current_environment)
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.execution_environment_snapshot"
            "(id,realm_id,environment_id,execution_identity,provider,platform,"
            "executor_protocol_version,cwd_locator,workspace_roots,shell,permission_profile_id,"
            "permission_profile_digest,filesystem_policy_digest,network_policy_digest,"
            "tool_runtime_digest,capability_digest,config_effective_digest,source_revision,"
            "captured_at,expires_at,grants_authority,snapshot_digest)"
            " select %s,realm_id,environment_id,execution_identity,provider,platform,"
            "executor_protocol_version,cwd_locator,workspace_roots,shell,permission_profile_id,"
            "permission_profile_digest,filesystem_policy_digest,network_policy_digest,"
            "tool_runtime_digest,capability_digest,config_effective_digest,source_revision,"
            "captured_at,expires_at,false,%s from runtime.execution_environment_snapshot"
            " where id=%s",
            (uuid4(), digest("forged-snapshot"), environment.id),
        )
    connection.rollback()
    repository.record_environment_probe(
        detect_environment_drift(environment, current_environment, checked_at=now)
    )
    tool_spec = ToolSpecRevision.create(
        realm_id=realm.id,
        tool_id="test.read",
        revision=1,
        name="Test read",
        description="Read fixture data",
        input_schema_digest=digest("tool-input"),
        output_schema_digest=digest("tool-output"),
        created_at=now,
    )
    tool_runtime = ToolRuntimeRevision.create(
        realm_id=realm.id,
        tool_id=tool_spec.tool_id,
        revision=1,
        adapter_ref="test:read",
        executable_revision="test-read@1",
        executable_digest=digest("tool-binary"),
        permission_capabilities=("filesystem.read",),
        parallel_supported=True,
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    tool_registry = ToolRegistryRepository(connection, realm.id)
    tool_registry.store_spec(tool_spec)
    tool_registry.store_runtime(tool_runtime)
    compiled_tools = CompiledToolSet.create(
        realm_id=realm.id,
        role="builder",
        permission_profile_digest=environment.permission_profile_digest,
        entries=(
            ToolSetEntry(
                tool_spec.tool_id,
                1,
                ToolExposure.DIRECT,
                tool_spec.spec_digest,
                tool_runtime.runtime_digest,
            ),
        ),
        created_at=now,
    )
    tool_registry.store_compiled_set(compiled_tools)
    equal_time_drift = reprobe_snapshot(
        environment,
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
        capability_digest=digest("equal-time-drift"),
    )
    repository.create_environment_snapshot(equal_time_drift)
    with pytest.raises(PsycopgError):
        repository.record_environment_probe(
            detect_environment_drift(environment, equal_time_drift, checked_at=now)
        )
    connection.rollback()
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.environment_probe_evidence"
            "(id,realm_id,execution_identity,sticky_snapshot_digest,current_snapshot_digest,"
            "drift_dimensions,checked_at,evidence_digest)"
            " select %s,realm_id,execution_identity,sticky_snapshot_digest,"
            "current_snapshot_digest,drift_dimensions,checked_at,%s"
            " from runtime.environment_probe_evidence where realm_id=%s limit 1",
            (uuid4(), digest("forged-evidence"), realm.id),
        )
    connection.rollback()
    with pytest.raises(PsycopgError):
        repository.record_environment_probe(
            EnvironmentDriftReport(
                environment.snapshot_digest,
                current_environment.snapshot_digest,
                (),
                now + dt.timedelta(minutes=11),
            )
        )
    connection.rollback()
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.environment_probe_evidence"
            "(id,realm_id,execution_identity,sticky_snapshot_digest,current_snapshot_digest,"
            "drift_dimensions,checked_at,evidence_digest) values"
            "(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                UUID(int=environment.id.int + 1),
                realm.id,
                environment.execution_identity,
                current_environment.snapshot_digest,
                environment.snapshot_digest,
                ["environment.capability-drift"],
                now,
                digest("forged-probe"),
            ),
        )
    connection.rollback()
    assignment_environment = AssignmentEnvironmentBinding.create(
        realm_id=realm.id,
        assignment_id=builder_id,
        execution_environment_snapshot_digest=environment.snapshot_digest,
        bound_at=now,
    )
    repository.bind_assignment_environment(assignment_environment)
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into agents.assignment_environment_binding"
            "(id,realm_id,assignment_id,execution_environment_snapshot_digest,bound_at,"
            "grants_authority,binding_digest) select %s,realm_id,assignment_id,"
            "execution_environment_snapshot_digest,bound_at,false,%s"
            " from agents.assignment_environment_binding where id=%s",
            (uuid4(), digest("forged-binding"), assignment_environment.id),
        )
    connection.rollback()
    turn = TurnExecutionSnapshot.create(
        realm_id=realm.id,
        assignment_id=builder_id,
        run_id=run.id,
        attempt_id=attempt_id,
        client_session_id="session-1",
        turn_id="turn-1",
        model_id=model.model_id,
        provider_id=provider_binding.provider_ref,
        route_decision_digest=route_digest,
        reasoning_profile_digest=digest("reasoning"),
        execution_environment_snapshot_digest=environment.snapshot_digest,
        context_manifest_digest=manifest.manifest_digest,
        exposed_tool_set_digest=compiled_tools.tool_set_digest,
        hook_set_digest=digest("hooks"),
        config_effective_digest=environment.config_effective_digest,
        created_at=now,
    )
    repository.create_turn_snapshot(turn)
    wrong_permission_tools = CompiledToolSet.create(
        realm_id=realm.id,
        role="builder",
        permission_profile_digest=digest("wrong-permission"),
        entries=(),
        created_at=now,
    )
    tool_registry.store_compiled_set(wrong_permission_tools)
    wrong_tool_turn = TurnExecutionSnapshot.create(
        realm_id=realm.id,
        assignment_id=builder_id,
        run_id=run.id,
        attempt_id=attempt_id,
        client_session_id="session-1",
        turn_id="wrong-tool-permission-turn",
        model_id=model.model_id,
        provider_id=provider_binding.provider_ref,
        route_decision_digest=route_digest,
        reasoning_profile_digest=digest("reasoning"),
        execution_environment_snapshot_digest=environment.snapshot_digest,
        context_manifest_digest=manifest.manifest_digest,
        exposed_tool_set_digest=wrong_permission_tools.tool_set_digest,
        hook_set_digest=digest("hooks"),
        config_effective_digest=environment.config_effective_digest,
        created_at=now,
    )
    with pytest.raises(PsycopgError):
        repository.create_turn_snapshot(wrong_tool_turn)
    connection.rollback()
    foreign_job_id, foreign_attempt_id = uuid4(), uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.job"
            "(id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,"
            "idempotency_key,assignment_id,run_id) values"
            "(%s,%s,%s,%s,%s,'build','provider-call','running',%s,%s,%s)",
            (
                foreign_job_id,
                realm.id,
                project.id,
                work.id,
                plan.id,
                f"foreign-job-{foreign_job_id}",
                coordinator_id,
                run.id,
            ),
        )
        cursor.execute(
            "insert into runtime.job_attempt"
            "(id,realm_id,job_id,attempt_number,fencing_token,worker_label,started_at)"
            " values(%s,%s,%s,1,1,'foreign-worker',%s)",
            (foreign_attempt_id, realm.id, foreign_job_id, now),
        )
    connection.commit()
    foreign_turn = TurnExecutionSnapshot.create(
        realm_id=realm.id,
        assignment_id=builder_id,
        run_id=run.id,
        attempt_id=foreign_attempt_id,
        client_session_id="session-1",
        turn_id="foreign-attempt-turn",
        model_id=model.model_id,
        provider_id=provider_binding.provider_ref,
        route_decision_digest=route_digest,
        reasoning_profile_digest=digest("reasoning"),
        execution_environment_snapshot_digest=environment.snapshot_digest,
        context_manifest_digest=manifest.manifest_digest,
        exposed_tool_set_digest=compiled_tools.tool_set_digest,
        hook_set_digest=digest("hooks"),
        config_effective_digest=environment.config_effective_digest,
        created_at=now,
    )
    with pytest.raises(PsycopgError):
        repository.create_turn_snapshot(foreign_turn)
    connection.rollback()
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.turn_execution_snapshot"
            "(id,realm_id,assignment_id,run_id,attempt_id,client_session_id,turn_id,model_id,"
            "provider_id,route_decision_digest,reasoning_profile_digest,"
            "execution_environment_snapshot_digest,context_manifest_digest,"
            "exposed_tool_set_digest,hook_set_digest,config_effective_digest,trace_id,"
            "grants_authority,created_at,turn_snapshot_digest)"
            " select %s,realm_id,assignment_id,run_id,attempt_id,client_session_id,"
            "'forged-turn',model_id,provider_id,route_decision_digest,reasoning_profile_digest,"
            "execution_environment_snapshot_digest,context_manifest_digest,"
            "exposed_tool_set_digest,hook_set_digest,config_effective_digest,trace_id,false,"
            "created_at,%s from runtime.turn_execution_snapshot where id=%s",
            (uuid4(), digest("forged-turn"), turn.id),
        )
    connection.rollback()
    common.update(
        turn_execution_snapshot_id=turn.id,
        turn_execution_snapshot_digest=turn.turn_snapshot_digest,
    )
    first_envelope = ExecutionEnvelope.create(
        request_ordinal=1,
        idempotency_key="call-1",
        payload_digest=digest("payload-1"),
        **common,
    )
    second_envelope = ExecutionEnvelope.create(
        request_ordinal=2,
        idempotency_key="call-2",
        payload_digest=digest("payload-2"),
        **common,
    )
    assert repository.create_envelope(first_envelope)[1] is True
    assert repository.create_envelope(second_envelope)[1] is True
    checkpoint = Checkpoint(
        "gateway-bound-checkpoint",
        str(project.id),
        str(work.id),
        str(plan.id),
        "revision-1",
        ("build",),
        (),
        ("build",),
        (),
        manifest.manifest_digest,
        digest("journal-head"),
        "invoke-model",
        dt.datetime.now(dt.UTC),
    )
    checkpoint_id = ContextContinuityRepository(
        connection, realm.id, project.id, work.id
    ).store_checkpoint(checkpoint, task_plan_id=plan.id, job_id=envelope_job_id)
    bound_envelope = ExecutionEnvelope.create(
        request_ordinal=3,
        idempotency_key="call-3",
        payload_digest=digest("payload-3"),
        checkpoint_id=checkpoint_id,
        checkpoint_digest=checkpoint.checkpoint_digest,
        checkpoint_disposition=CheckpointDisposition.BOUND,
        **{
            key: value
            for key, value in common.items()
            if key not in {"checkpoint_id", "checkpoint_digest", "checkpoint_disposition"}
        },
    )
    repository.create_envelope(bound_envelope)
    request_manifest = ModelRequestManifest.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        step_id="build",
        execution_envelope_id=bound_envelope.id,
        execution_envelope_digest=bound_envelope.envelope_digest,
        run_id=run.id,
        job_id=envelope_job_id,
        attempt_id=attempt_id,
        assignment_id=builder_id,
        role="builder",
        risk="medium",
        route_decision_digest=route_digest,
        catalog_provider_id=catalog.provider_id,
        catalog_digest=catalog.catalog_digest,
        catalog_snapshot_digest=catalog.snapshot_digest,
        catalog_snapshot_id=catalog.id,
        model_id=model.model_id,
        provider_ref=provider_binding.provider_ref,
        context_manifest_digest=manifest.manifest_digest,
        context_fragment_set_digest=fragment_set.fragment_set_digest,
        model_visible_payload_digest=digest("payload-3"),
        context_packet_digest=packet.packet_digest,
        checkpoint_digest=checkpoint.checkpoint_digest,
        source_revision="revision-1",
        policy_digest=policy_digest,
        payload_digest=digest("payload-3"),
        authorization_scope_digest=digest("authorization"),
        output_schema_digest=digest("schema"),
        idempotency_key="call-3",
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        deadline=run.deadline,
        route_expires_at=target.expires_at,
        source_label=GatewaySourceLabel.PROVIDER_CONTRACT,
        created_at=dt.datetime.now(dt.UTC),
        turn_execution_snapshot_digest=turn.turn_snapshot_digest,
        environment_digest=environment.snapshot_digest,
        permission_profile_digest=environment.permission_profile_digest,
        tool_set_digest=turn.exposed_tool_set_digest,
        tool_visible_payload_digest=compiled_tools.compile_model_payload().serialized_tools_digest,
        tool_visible_payload_mode="direct",
        config_effective_digest=turn.config_effective_digest,
        hook_set_digest=turn.hook_set_digest,
    )
    invocation = ModelInvocationRepository(connection, realm.id)
    assert invocation.store_manifest(request_manifest)[1] is True
    forged_values = {
        name: getattr(request_manifest, name)
        for name in request_manifest.__dataclass_fields__
        if name not in {"id", "manifest_digest"}
    }
    forged_values["permission_profile_digest"] = digest("forged-permission")
    forged_manifest = ModelRequestManifest.create(**forged_values)
    with pytest.raises(PsycopgError):
        invocation.store_manifest(forged_manifest)
    connection.rollback()
    forged_values["permission_profile_digest"] = request_manifest.permission_profile_digest
    forged_values["tool_visible_payload_digest"] = digest("forged-tool-payload")
    forged_values["idempotency_key"] = "call-3-forged-tool-payload"
    forged_tool_payload = ModelRequestManifest.create(**forged_values)
    with pytest.raises(PsycopgError):
        invocation.store_manifest(forged_tool_payload)
    connection.rollback()
    invocation.activate_enforce(policy_digest)
    invocation.assert_current_envelope(request_manifest)
    invocation.assert_current_context_fragment_set(request_manifest)
    invocation.assert_current_catalog(request_manifest)
    catalog_forged_values = {
        name: getattr(request_manifest, name)
        for name in request_manifest.__dataclass_fields__
        if name not in {"id", "manifest_digest"}
    }
    catalog_forged_values["catalog_digest"] = digest("forged-catalog")
    catalog_forged_values["idempotency_key"] = "call-3-forged-catalog"
    with pytest.raises(PolicyViolation, match="catalog stale"):
        invocation.assert_current_catalog(ModelRequestManifest.create(**catalog_forged_values))
    bound_gateway = ModelGateway.from_execution_envelope(
        invocation, GatewaySourceLabel.PROVIDER_CONTRACT, bound_envelope.id
    )
    assert bound_gateway.bindings.execution_envelope_digest == bound_envelope.envelope_digest
    assert bound_gateway.bindings.max_cost_micros == bound_envelope.max_cost_micros
    assert bound_gateway.bindings.catalog_snapshot_id == catalog.id
    tool_claim = EffectClaim.create(
        realm_id=realm.id,
        job_id=envelope_job_id,
        attempt_id=attempt_id,
        operation="tool.execute:test.read",
        effect_digest=digest("tool-effect"),
        authorization_digest=digest("authorization"),
        idempotency_key="tool-effect-1",
        resources=(),
        execution_identity="execution-test:1",
        fencing_token=1,
        adapter_digest=digest("tool-adapter"),
        now=dt.datetime.now(dt.UTC),
    )
    EffectLedger(connection, realm.id).claim(tool_claim)
    tool_binding = ToolDispatchBinding(
        tool_claim.id,
        turn.turn_snapshot_digest,
        compiled_tools.tool_set_digest,
        tool_spec.tool_id,
        1,
        tool_spec.spec_digest,
        tool_runtime.runtime_digest,
        digest("tool-input-value"),
    )

    runtime_v2 = ToolRuntimeRevision.create(
        realm_id=realm.id,
        tool_id=tool_spec.tool_id,
        revision=2,
        adapter_ref="test:read",
        executable_revision="test-read@2",
        executable_digest=digest("tool-binary-v2"),
        permission_capabilities=("filesystem.read",),
        parallel_supported=True,
        captured_at=dt.datetime.now(dt.UTC),
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10),
    )

    class ToolAdapter:
        calls = 0
        concurrent_update_blocked = False

        def runtime_binding(self):  # type: ignore[no-untyped-def]
            return tool_spec.tool_id, 1, tool_runtime.runtime_digest

        def execute(self, binding, *, permit):  # type: ignore[no-untyped-def]
            permit.assert_for(binding)
            with connect(migrated_database) as concurrent:
                configure_session(concurrent, realm_id=realm.id)
                with concurrent.cursor() as cursor:
                    cursor.execute(
                        "select pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                        (f"{realm.id}:{tool_spec.tool_id}",),
                    )
                    self.concurrent_update_blocked = cursor.fetchone()[0] is False
            self.calls += 1
            return digest("tool-result")

    tool_adapter = ToolAdapter()
    execution_host = ExecutionHost(connection, realm.id, worker_label="execution-test")
    assert execution_host.dispatch_tool(tool_binding, tool_adapter) == digest("tool-result")
    assert tool_adapter.calls == 1
    assert tool_adapter.concurrent_update_blocked is True
    tool_registry.store_runtime(runtime_v2)
    with pytest.raises(PolicyViolation, match="runtime revision mismatch"):
        execution_host.dispatch_tool(tool_binding, tool_adapter)
    with pytest.raises(PolicyViolation, match="runtime drift"):
        invocation.assert_current_tool_set(request_manifest)
    assert tool_adapter.calls == 1
    drift_moment = dt.datetime.now(dt.UTC)
    drifted_environment = reprobe_snapshot(
        environment,
        captured_at=drift_moment,
        expires_at=drift_moment + dt.timedelta(minutes=10),
        capability_digest=digest("drifted-capability"),
    )
    repository.create_environment_snapshot(drifted_environment)
    repository.record_environment_probe(
        detect_environment_drift(environment, drifted_environment, checked_at=drift_moment)
    )
    with pytest.raises(PolicyViolation, match="stale"):
        invocation.assert_current_envelope(request_manifest)
    stale_turn = TurnExecutionSnapshot.create(
        realm_id=realm.id,
        assignment_id=builder_id,
        run_id=run.id,
        attempt_id=attempt_id,
        client_session_id="session-1",
        turn_id="stale-probe-turn",
        model_id=model.model_id,
        provider_id=provider_binding.provider_ref,
        route_decision_digest=route_digest,
        reasoning_profile_digest=digest("reasoning"),
        execution_environment_snapshot_digest=environment.snapshot_digest,
        context_manifest_digest=manifest.manifest_digest,
        exposed_tool_set_digest=compiled_tools.tool_set_digest,
        hook_set_digest=digest("hooks"),
        config_effective_digest=environment.config_effective_digest,
        created_at=drift_moment,
    )
    with pytest.raises(PsycopgError):
        repository.create_turn_snapshot(stale_turn)
    connection.rollback()
    with pytest.raises(PsycopgError):
        repository.create_envelope(
            ExecutionEnvelope.create(
                request_ordinal=3,
                idempotency_key="call-3",
                payload_digest=digest("payload-3"),
                provider_ref="forged-provider",
                **{key: value for key, value in common.items() if key != "provider_ref"},
            )
        )
    connection.rollback()
    newer_catalog = ModelCatalogSnapshot(
        id=uuid4(),
        realm_id=realm.id,
        provider_id=catalog.provider_id,
        entries=catalog.entries,
        etag=None,
        fetched_at=now + dt.timedelta(seconds=1),
        expires_at=now + dt.timedelta(minutes=10),
        client_version="zekam-test/2",
        source=CatalogSource.PACKAGE,
        fetch_status=CatalogFetchStatus.FETCHED,
        error_category=None,
        prior_snapshot_id=catalog.id,
    )
    ModelCatalogRepository(connection, realm.id).store(newer_catalog)
    superseded_values = {
        name: getattr(request_manifest, name)
        for name in request_manifest.__dataclass_fields__
        if name not in {"id", "manifest_digest"}
    }
    superseded_values["idempotency_key"] = "call-3-superseded-catalog"
    with pytest.raises(PsycopgError):
        invocation.store_manifest(ModelRequestManifest.create(**superseded_values))
    connection.rollback()
    with pytest.raises(PolicyViolation, match="catalog stale"):
        invocation.assert_current_catalog(request_manifest)
    remote_plan_digest = catalog_refresh_plan_digest(
        provider_id=catalog.provider_id,
        strategy=CatalogRefreshStrategy.FORCE_PROBE,
        client_version="zekam-test/remote",
        ttl_seconds=600,
        prior_snapshot_digest=newer_catalog.snapshot_digest,
    )
    actor = Actor.create(
        realm=realm,
        kind=ActorKind.SERVICE,
        slug="catalog-fetcher",
        display_name="Catalog fetcher",
        now=now,
    )
    ActorRepository(connection, realm.id).add(actor)
    authorization = Authorization.issue(
        realm_id=realm.id,
        actor_id=actor.id,
        plan_digest=remote_plan_digest,
        effect_digest=remote_plan_digest,
        scope=AuthorizationScope(
            allowed_resources=(f"provider.catalog:{catalog.provider_id}",),
            allowed_effects=("model-catalog-refresh",),
            provider_refs=(catalog.provider_id,),
        ),
        risk="low",
        lifetime=dt.timedelta(minutes=5),
        now=now,
    )
    authorizations = AuthorizationRepository(connection, realm.id)
    authorizations.issue(authorization)
    consumed = authorizations.consume(
        authorization.id,
        effect_digest=remote_plan_digest,
        consumed_by="catalog-fetcher",
        now=now + dt.timedelta(milliseconds=1),
    )
    assert consumed.consumed is True
    remote_claim = EffectClaim.create(
        realm_id=realm.id,
        job_id=envelope_job_id,
        attempt_id=attempt_id,
        operation="model-catalog-refresh",
        effect_digest=remote_plan_digest,
        authorization_digest=authorization.authorization_digest,
        idempotency_key="remote-catalog-refresh",
        resources=(),
        execution_identity="catalog-fetcher:1",
        fencing_token=1,
        adapter_digest=digest("catalog-adapter"),
        now=now + dt.timedelta(milliseconds=2),
    )
    ledger = EffectLedger(connection, realm.id)
    ledger.claim(remote_claim, authorization_id=authorization.id)
    remote_response_digest = catalog_fetch_response_digest(
        status_code=200,
        entries=catalog.entries,
        etag="remote-etag",
        error_category=None,
    )
    remote_receipt = EffectReceipt.completed(
        realm_id=realm.id,
        claim=remote_claim,
        result_digest=remote_response_digest,
        adapter_evidence_digest=digest("catalog-adapter-evidence"),
        now=now + dt.timedelta(milliseconds=3),
    )
    ledger.receipt(remote_receipt)
    remote_catalog = ModelCatalogSnapshot(
        id=uuid4(),
        realm_id=realm.id,
        provider_id=catalog.provider_id,
        entries=catalog.entries,
        etag="remote-etag",
        fetched_at=now + dt.timedelta(seconds=2),
        expires_at=now + dt.timedelta(minutes=10, seconds=2),
        client_version="zekam-test/remote",
        source=CatalogSource.REMOTE,
        fetch_status=CatalogFetchStatus.FETCHED,
        error_category=None,
        prior_snapshot_id=newer_catalog.id,
        fetch_provenance=CatalogFetchProvenance(
            plan_digest=remote_plan_digest,
            strategy=CatalogRefreshStrategy.FORCE_PROBE,
            ttl_seconds=600,
            prior_snapshot_digest=newer_catalog.snapshot_digest,
            authorization_id=authorization.id,
            authorization_digest=authorization.authorization_digest,
            claim_id=remote_claim.id,
            claim_digest=remote_claim.claim_digest,
            receipt_id=remote_receipt.id,
            receipt_status=CatalogReceiptStatus.COMPLETED,
            status_code=200,
            response_etag="remote-etag",
            response_digest=remote_response_digest,
            adapter_evidence_digest=remote_receipt.adapter_evidence_digest or "",
        ),
    )
    assert ModelCatalogRepository(connection, realm.id).store(remote_catalog) == (
        remote_catalog.id,
        True,
    )
    repository.finish_run(run.id, state="completed", terminal_at=now + dt.timedelta(seconds=2))


def test_execution_run_rejects_plan_scope_drift(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "execution-drift-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title="Drift")
    plan = graph.create_plan(
        work.id,
        source_revision="revision-1",
        policy_digest=digest("policy"),
        steps=(PlanStep("read", "Read", EffectKind.NONE),),
    )
    now = dt.datetime.now(dt.UTC)
    run = ExecutionRun.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        client_id="codex",
        session_id=None,
        source_revision="forged-revision",
        policy_digest=digest("policy"),
        max_input_tokens=10,
        max_output_tokens=10,
        max_cost_micros=10,
        deadline=now + dt.timedelta(minutes=1),
        created_at=now,
    )
    with pytest.raises(PsycopgError):
        ExecutionRunRepository(connection, realm.id).create_run(run)
    connection.rollback()
