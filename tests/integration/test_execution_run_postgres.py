from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from psycopg import Error as PsycopgError

from zekam.application.model_registry import load_inventory
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import AuthorityLevel, ContextCandidate, compile_context
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
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
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
    manifest_id = ContextContinuityRepository(
        connection, realm.id, project.id, work.id
    ).store_manifest(manifest)
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
