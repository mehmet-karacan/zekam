from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import Error as PsycopgError

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
from zekam.domain.model_invocation import GatewaySourceLabel, ModelRequestManifest
from zekam.domain.model_routing import (
    AgentRole,
    ExecutionTargetSnapshot,
    RoleRoutingPolicy,
    RoutingLayer,
)
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
from zekam.infrastructure.postgres.model_invocation_repository import ModelInvocationRepository
from zekam.infrastructure.postgres.model_repository import ModelInventoryRepository
from zekam.infrastructure.postgres.model_routing_repository import ModelRoutingRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_execution_run_and_exact_context_packet_roundtrip(
    realm_session: tuple[Any, Any], tmp_path: Path
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
            "status,primary_model_id,evidence_digest,decided_at) values"
            "(%s,%s,%s,%s,'implementer','general',%s,%s,%s,%s,'selected',%s,%s,%s)",
            (
                decision_id,
                realm.id,
                policy_id,
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
        exposed_tool_set_digest=digest("tool-set"),
        hook_set_digest=digest("hooks"),
        config_effective_digest=environment.config_effective_digest,
        created_at=now,
    )
    repository.create_turn_snapshot(turn)
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
        exposed_tool_set_digest=digest("tool-set"),
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
    invocation.activate_enforce(policy_digest)
    invocation.assert_current_envelope(request_manifest)
    invocation.assert_current_context_fragment_set(request_manifest)
    bound_gateway = ModelGateway.from_execution_envelope(
        invocation, GatewaySourceLabel.PROVIDER_CONTRACT, bound_envelope.id
    )
    assert bound_gateway.bindings.execution_envelope_digest == bound_envelope.envelope_digest
    assert bound_gateway.bindings.max_cost_micros == bound_envelope.max_cost_micros
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
        exposed_tool_set_digest=digest("tool-set"),
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
