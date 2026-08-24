from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import Error as PsycopgError

from zekam.application.context_materializer import FragmentMaterialization, materialize_fragments
from zekam.application.model_registry import load_inventory
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.resume_coordinator import ResumeCoordinator
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.checkpoint_v2 import (
    CheckpointV2,
    NextSafeActionV2,
    OpenEffect,
    OpenEffectState,
    Resumability,
    SandboxBindingV2,
    SandboxDisposition,
    StaleDigestBindings,
    StepResultV2,
)
from zekam.domain.context_continuity import (
    AuthorityLevel,
    ContextCandidate,
    JournalEntry,
    compile_context,
)
from zekam.domain.context_fragment import ContextContentKind, ContextRole, ContextVisibility
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
from zekam.domain.memory import (
    MemoryClass,
    MemoryEvidence,
    MemoryKey,
    MemoryRecord,
    MemoryScope,
    MemoryState,
)
from zekam.domain.model_invocation import GatewaySourceLabel, ModelRequestManifest
from zekam.domain.model_routing import (
    AgentRole,
    ExecutionTargetSnapshot,
    ProjectRoutingContext,
    RoleRoutingPolicy,
    RoutingLayer,
)
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.resume_apply import ResumeApplyEvent, ResumeApplyPhase, ResumeApplyState
from zekam.domain.runtime import EffectClaim, EffectReceipt
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.tool_registry import CompiledToolSet
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.checkpoint_v2_repository import CheckpointV2Repository
from zekam.infrastructure.postgres.context_continuity_repository import ContextContinuityRepository
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
from zekam.infrastructure.postgres.memory_repository import MemoryRepository
from zekam.infrastructure.postgres.memory_telemetry_repository import MemoryTelemetryRepository
from zekam.infrastructure.postgres.model_invocation_repository import ModelInvocationRepository
from zekam.infrastructure.postgres.model_repository import ModelInventoryRepository
from zekam.infrastructure.postgres.model_routing_repository import ModelRoutingRepository
from zekam.infrastructure.postgres.resume_apply_repository import ResumeApplyRepository
from zekam.infrastructure.postgres.resume_repository import ResumeRepository
from zekam.infrastructure.postgres.runtime_repository import EffectLedger
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository
from zekam.infrastructure.postgres.tool_registry_repository import ToolRegistryRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

E2E_FIXTURE_CONSUMER: Any | None = None


def test_checkpoint_v2_evidence_revision_and_terminal_gate(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    now = dt.datetime.now(dt.UTC)
    source = tmp_path / "checkpoint-v2-source"
    source.mkdir()
    project_service = ProjectIntegrationService(connection, realm)
    project = project_service.register(source_path=source)
    scan = project_service.scan(project.id, now=now)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(project_id=project.id, type=WorkType.TASK, title="Checkpoint v2")
    policy_digest = digest("checkpoint-policy")
    plan = graph.create_plan(
        work.id,
        source_revision=scan.revision.revision,
        policy_digest=policy_digest,
        steps=(
            PlanStep("research", "Research", EffectKind.NONE),
            PlanStep("build", "Build", EffectKind.FILE_WRITE, depends_on=("research",)),
        ),
    )
    manifest = compile_context(
        (
            ContextCandidate(
                "source",
                AuthorityLevel.VERIFIED,
                now,
                scan.revision.revision,
                digest("source"),
                1,
                True,
            ),
        ),
        token_budget=5,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=now,
    )
    continuity = ContextContinuityRepository(connection, realm.id, project.id, work.id)
    journal = JournalEntry(1, str(work.id), "checkpoint-start", digest("journal"), None, False, now)
    continuity.append_journal(journal, expected_head=None)
    manifest_id = continuity.store_manifest(manifest)
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
    execution = ExecutionRunRepository(connection, realm.id)
    execution.create_packet(packet)
    run = ExecutionRun.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        client_id="codex",
        session_id="checkpoint-test",
        source_revision=scan.revision.revision,
        policy_digest=policy_digest,
        max_input_tokens=10,
        max_output_tokens=10,
        max_cost_micros=100,
        deadline=now + dt.timedelta(minutes=10),
        created_at=now,
    )
    execution.create_run(run)
    execution.activate_run(run.id, started_at=now)

    model = next(record for record in load_inventory().records if record.enabled)
    ModelInventoryRepository(connection, realm.id).upsert(model)
    routing = ModelRoutingRepository(connection, realm.id)
    routing_context = ProjectRoutingContext(
        project_id=project.id,
        source_revision_id=scan.revision.id,
        source_revision=scan.revision.revision,
        tree_digest=scan.revision.tree_digest,
        capability_profile_digest=scan.profile.digest,
        dependency_digest=digest("dependencies"),
        framework_digest=digest("frameworks"),
        technology_digest=digest("technology"),
        architecture_digest=digest("architecture"),
        rules_digest=digest("rules"),
        suite_digest=digest("suite"),
        inventory_digest=digest("inventory"),
        policy_digest=policy_digest,
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    routing_context_id, _ = routing.store_project_context(routing_context)
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
        client_id="codex",
        slot="default",
        execution_mode="native-sequential",
        model_selectable=True,
        structured_result=True,
        cancellation=True,
        max_concurrency=1,
        cost_evidence_digest=digest("cost"),
        capability_digest=digest("capability"),
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    target_id, _ = routing.store_execution_target(target)
    (
        route_id,
        builder_id,
        verifier_id,
        builder_invocation,
        verifier_invocation,
        colliding_verifier_invocation,
        research_builder_id,
        research_job_id,
        research_attempt_id,
        research_lease_id,
        foreign_claim_id,
        foreign_receipt_id,
        job_id,
        attempt_id,
    ) = (uuid4() for _ in range(14))
    lease_id = uuid4()
    route_digest = digest("route")
    result_digest = digest("result")
    claim_one, claim_two, receipt_one, receipt_two = (uuid4() for _ in range(4))
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into models.model_route_decision"
            "(id,realm_id,role_policy_id,execution_target_id,role,target_layer,inventory_digest,"
            "routing_policy_digest,policy_digest,execution_target_digest,status,primary_model_id,"
            "evidence_digest,decided_at) values"
            "(%s,%s,%s,%s,'implementer','general',%s,%s,%s,%s,'selected',%s,%s,%s)",
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
        coordinator_id = uuid4()
        for assignment_id, parent_id, role, risk, agent, step_id in (
            (coordinator_id, None, "coordinator", "medium", "coordinator", "build"),
            (builder_id, coordinator_id, "builder", "high", "builder", "build"),
            (
                research_builder_id,
                coordinator_id,
                "builder",
                "medium",
                "research-builder",
                "research",
            ),
            (
                verifier_id,
                coordinator_id,
                "verifier",
                "medium",
                "independent-verifier",
                "build",
            ),
        ):
            instruction_digest = digest(f"instruction:{role}:{step_id}")
            assignment_digest = digest(
                {
                    "id": str(assignment_id),
                    "realm_id": str(realm.id),
                    "project_id": str(project.id),
                    "work_item_id": str(work.id),
                    "plan_id": str(plan.id),
                    "step_id": step_id,
                    "parent_assignment_id": str(parent_id) if parent_id else None,
                    "role": role,
                    "agent_ref": agent,
                    "risk": risk,
                    "instruction_digest": instruction_digest,
                    "context_manifest_digest": manifest.manifest_digest,
                    "read_resources": [],
                    "write_resources": [],
                }
            )
            cursor.execute(
                "insert into agents.assignment"
                "(id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,role,"
                "agent_ref,status,risk,instruction_digest,context_manifest_digest,"
                "assignment_digest,created_at) values"
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s,%s)",
                (
                    assignment_id,
                    realm.id,
                    project.id,
                    work.id,
                    plan.id,
                    step_id,
                    parent_id,
                    role,
                    agent,
                    risk,
                    instruction_digest,
                    manifest.manifest_digest,
                    assignment_digest,
                    now,
                ),
            )
        cursor.execute(
            "insert into runtime.job"
            "(id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,attempt_count,"
            "idempotency_key,assignment_id,run_id) values"
            "(%s,%s,%s,%s,%s,'research','read-only','running',1,%s,%s,%s)",
            (
                research_job_id,
                realm.id,
                project.id,
                work.id,
                plan.id,
                f"job-{research_job_id}",
                research_builder_id,
                run.id,
            ),
        )
        cursor.execute(
            "insert into runtime.job"
            "(id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,attempt_count,"
            "idempotency_key,assignment_id,run_id) values"
            "(%s,%s,%s,%s,%s,'build','mutation','running',1,%s,%s,%s)",
            (job_id, realm.id, project.id, work.id, plan.id, f"job-{job_id}", builder_id, run.id),
        )
        cursor.execute(
            "insert into runtime.job_attempt"
            "(id,realm_id,job_id,attempt_number,fencing_token,worker_label,started_at)"
            " values(%s,%s,%s,1,1,'research-test',%s)",
            (research_attempt_id, realm.id, research_job_id, now),
        )
        cursor.execute(
            "insert into runtime.job_attempt"
            "(id,realm_id,job_id,attempt_number,fencing_token,worker_label,started_at)"
            " values(%s,%s,%s,1,1,'checkpoint-test',%s)",
            (attempt_id, realm.id, job_id, now),
        )
        cursor.execute(
            "insert into runtime.lease"
            "(id,realm_id,job_id,attempt_id,owner_digest,fencing_token,worker_label,expires_at,"
            "heartbeat_at,created_at) values(%s,%s,%s,%s,%s,1,'research-test',%s,%s,%s)",
            (
                research_lease_id,
                realm.id,
                research_job_id,
                research_attempt_id,
                digest("research-owner"),
                now + dt.timedelta(minutes=5),
                now,
                now,
            ),
        )
        cursor.execute(
            "insert into runtime.lease"
            "(id,realm_id,job_id,attempt_id,owner_digest,fencing_token,worker_label,expires_at,"
            "heartbeat_at,created_at) values(%s,%s,%s,%s,%s,1,'checkpoint-test',%s,%s,%s)",
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
        for claim_id, key in ((claim_one, "one"), (claim_two, "two")):
            cursor.execute(
                "insert into runtime.effect_claim"
                "(id,realm_id,job_id,attempt_id,operation,effect_digest,authorization_digest,"
                "idempotency_key,resources,execution_identity,fencing_token,adapter_digest,"
                "claim_digest,claimed_at) values"
                "(%s,%s,%s,%s,'write',%s,%s,%s,'[]'::jsonb,'checkpoint-test',1,%s,%s,%s)",
                (
                    claim_id,
                    realm.id,
                    job_id,
                    attempt_id,
                    digest(f"effect:{key}"),
                    digest("authorization"),
                    f"claim-{claim_id}",
                    digest("adapter"),
                    digest(f"claim:{key}"),
                    now,
                ),
            )
        cursor.execute(
            "insert into runtime.effect_claim"
            "(id,realm_id,job_id,attempt_id,operation,effect_digest,authorization_digest,"
            "idempotency_key,resources,execution_identity,fencing_token,adapter_digest,"
            "claim_digest,claimed_at) values"
            "(%s,%s,%s,%s,'read',%s,%s,%s,'[]'::jsonb,'research-test',1,%s,%s,%s)",
            (
                foreign_claim_id,
                realm.id,
                research_job_id,
                research_attempt_id,
                digest("foreign-effect"),
                digest("research-authorization"),
                f"claim-{foreign_claim_id}",
                digest("research-adapter"),
                digest("foreign-claim"),
                now,
            ),
        )
        cursor.execute(
            "insert into runtime.effect_receipt"
            "(id,realm_id,claim_id,status,result_digest) values(%s,%s,%s,'completed',%s)",
            (foreign_receipt_id, realm.id, foreign_claim_id, result_digest),
        )
        cursor.execute(
            "insert into runtime.effect_receipt"
            "(id,realm_id,claim_id,status,result_digest) values(%s,%s,%s,'completed',%s)",
            (receipt_one, realm.id, claim_one, result_digest),
        )
        cursor.execute(
            "insert into agents.invocation"
            "(id,realm_id,assignment_id,client_id,execution_identity,invocation_digest,created_at)"
            " values(%s,%s,%s,'codex','shared-run',%s,%s)",
            (builder_invocation, realm.id, builder_id, digest("builder-invocation"), now),
        )
    connection.commit()

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
        environment_id="env-checkpoint-test",
        execution_identity="checkpoint-test",
        provider="local-process",
        platform="test-platform",
        executor_protocol_version="zekam-exec/v1",
        cwd_locator="workspace:checkpoint/root",
        workspace_roots=("workspace:checkpoint/root",),
        shell=ShellSnapshot("test-shell", digest("shell"), digest("profile")),
        permission_profile_id="test-profile",
        permission_profile_digest=digest("permission"),
        filesystem_policy_digest=digest("filesystem"),
        network_policy_digest=digest("network"),
        tool_runtime_digest=digest("tool-runtime"),
        capability_digest=digest("environment-capability"),
        config_effective_digest=digest("config"),
        source_revision=scan.revision.revision,
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    execution.create_environment_snapshot(environment)
    current_environment = reprobe_snapshot(
        environment,
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    execution.create_environment_snapshot(current_environment)
    execution.record_environment_probe(
        detect_environment_drift(environment, current_environment, checked_at=now)
    )
    compiled_tools = CompiledToolSet.create(
        realm_id=realm.id,
        role="builder",
        permission_profile_digest=environment.permission_profile_digest,
        entries=(),
        created_at=now,
    )
    ToolRegistryRepository(connection, realm.id).store_compiled_set(compiled_tools)
    for assignment_id in (research_builder_id, builder_id):
        execution.bind_assignment_environment(
            AssignmentEnvironmentBinding.create(
                realm_id=realm.id,
                assignment_id=assignment_id,
                execution_environment_snapshot_digest=environment.snapshot_digest,
                bound_at=now,
            )
        )
    research_turn = TurnExecutionSnapshot.create(
        realm_id=realm.id,
        assignment_id=research_builder_id,
        run_id=run.id,
        attempt_id=research_attempt_id,
        client_session_id="checkpoint-session",
        turn_id="research-turn",
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
    builder_turn = TurnExecutionSnapshot.create(
        realm_id=realm.id,
        assignment_id=builder_id,
        run_id=run.id,
        attempt_id=attempt_id,
        client_session_id="checkpoint-session",
        turn_id="builder-turn",
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
    execution.create_turn_snapshot(research_turn)
    execution.create_turn_snapshot(builder_turn)
    research_envelope = ExecutionEnvelope.create(
        realm_id=realm.id,
        run_id=run.id,
        job_id=research_job_id,
        attempt_id=research_attempt_id,
        lease_id=research_lease_id,
        fencing_token=1,
        request_ordinal=1,
        idempotency_key="research-call-1",
        assignment_id=research_builder_id,
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
        turn_execution_snapshot_id=research_turn.id,
        turn_execution_snapshot_digest=research_turn.turn_snapshot_digest,
        checkpoint_id=None,
        checkpoint_digest=None,
        checkpoint_disposition=CheckpointDisposition.NOT_APPLICABLE_GENESIS,
        source_revision=scan.revision.revision,
        policy_digest=policy_digest,
        authorization_scope_digest=digest("research-authorization"),
        output_schema_digest=digest("research-schema"),
        payload_digest=digest("research-payload"),
        max_input_tokens=10,
        max_output_tokens=10,
        max_cost_micros=100,
        deadline=run.deadline,
        created_at=now,
    )
    execution.create_envelope(research_envelope)
    envelope = ExecutionEnvelope.create(
        realm_id=realm.id,
        run_id=run.id,
        job_id=job_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        fencing_token=1,
        request_ordinal=1,
        idempotency_key="checkpoint-call-1",
        assignment_id=builder_id,
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
        turn_execution_snapshot_id=builder_turn.id,
        turn_execution_snapshot_digest=builder_turn.turn_snapshot_digest,
        checkpoint_id=None,
        checkpoint_digest=None,
        checkpoint_disposition=CheckpointDisposition.NOT_APPLICABLE_GENESIS,
        source_revision=scan.revision.revision,
        policy_digest=policy_digest,
        authorization_scope_digest=digest("authorization"),
        output_schema_digest=digest("schema"),
        payload_digest=digest("payload"),
        max_input_tokens=10,
        max_output_tokens=10,
        max_cost_micros=100,
        deadline=run.deadline,
        created_at=now,
    )
    execution.create_envelope(envelope)

    with connection.cursor() as cursor:
        cursor.execute("select checksum from core.schema_migrations order by version desc limit 1")
        migration_checksum = str(cursor.fetchone()[0])
    bindings = StaleDigestBindings(
        routing_context_snapshot_id=routing_context_id,
        source_revision=scan.revision.revision,
        policy_digest=policy_digest,
        capability_profile_digest=scan.profile.digest,
        dependency_snapshot_digest=digest("dependencies"),
        migration_head_digest=digest(migration_checksum),
        model_route_decision_digest=route_digest,
        context_manifest_digest=manifest.manifest_digest,
        context_packet_digest=packet.packet_digest,
        architecture_digest=digest("architecture"),
        rules_digest=digest("rules"),
        test_suite_digest=digest("suite"),
        model_inventory_digest=digest("inventory"),
        journal_head_digest=journal.entry_digest,
    )
    common = {
        "checkpoint_key": "build-progress",
        "realm_id": realm.id,
        "project_id": project.id,
        "work_item_id": work.id,
        "intent_digest": digest("intent"),
        "plan_id": plan.id,
        "plan_digest": plan.plan_digest,
        "step_id": "build",
        "run_id": run.id,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "assignment_id": builder_id,
        "execution_envelope_id": envelope.id,
        "execution_envelope_digest": envelope.envelope_digest,
        "route_decision_id": route_id,
        "context_manifest_id": manifest_id,
        "context_packet_id": packet.id,
        "bindings": bindings,
        "plan_steps": ("research", "build"),
        "logical_read_resources": (),
        "logical_write_resources": (),
        "sandbox": SandboxBindingV2(SandboxDisposition.NOT_APPLICABLE),
        "tokens_used": 0,
        "cost_micros_used": 0,
        "attempts_used": 1,
        "deadline": run.deadline,
        "rollback_or_recovery": (),
        "resumability": Resumability.RECONCILIATION_REQUIRED,
        "observed_lease_id": lease_id,
        "observed_fencing_token": 1,
        "created_at": now,
    }
    research_result = StepResultV2(
        "research",
        digest("research-result"),
        EffectKind.NONE,
        research_job_id,
        research_attempt_id,
        research_builder_id,
        research_envelope.id,
        research_envelope.envelope_digest,
    )
    research_checkpoint = CheckpointV2(
        checkpoint_id=uuid4(),
        revision=1,
        previous_checkpoint_id=None,
        previous_checkpoint_digest=None,
        completed_steps=("research",),
        pending_steps=("build",),
        step_results=(research_result,),
        open_effects=(),
        next_safe_action=NextSafeActionV2("dispatch", "build", "research tamamlandi"),
        **(
            common
            | {
                "step_id": "research",
                "job_id": research_job_id,
                "attempt_id": research_attempt_id,
                "assignment_id": research_builder_id,
                "execution_envelope_id": research_envelope.id,
                "execution_envelope_digest": research_envelope.envelope_digest,
                "resumability": Resumability.SAFE_CONTINUE,
                "observed_lease_id": research_lease_id,
            }
        ),
    )
    repository = CheckpointV2Repository(connection, realm.id)
    assert repository.store(research_checkpoint) == (research_checkpoint.checkpoint_id, True)
    with connection.cursor() as cursor:
        cursor.execute("update runtime.job set state='completed' where id=%s", (research_job_id,))
    connection.commit()
    if E2E_FIXTURE_CONSUMER is not None:
        E2E_FIXTURE_CONSUMER(locals())
        return

    first = CheckpointV2(
        checkpoint_id=uuid4(),
        revision=2,
        previous_checkpoint_id=research_checkpoint.checkpoint_id,
        previous_checkpoint_digest=research_checkpoint.checkpoint_digest,
        completed_steps=("research",),
        pending_steps=("build",),
        step_results=(research_result,),
        open_effects=(
            OpenEffect(
                claim_two, digest("effect:two"), OpenEffectState.STARTED_NO_TERMINAL_RECEIPT
            ),
        ),
        next_safe_action=NextSafeActionV2("reconcile", "build", "receipt pending"),
        **common,
    )
    assert repository.store(first) == (first.checkpoint_id, True)
    assert repository.store(first) == (first.checkpoint_id, False)
    assert repository.latest(first.checkpoint_key) == (
        first.checkpoint_id,
        2,
        first.checkpoint_digest,
    )
    assert repository.is_complete(first.checkpoint_id)
    before_counts: tuple[int, ...]
    with connection.cursor() as cursor:
        cursor.execute(
            "select (select count(*) from runtime.job),"
            " (select count(*) from security.authorization),"
            " (select count(*) from runtime.lease),"
            " (select count(*) from work.checkpoint_v2)"
        )
        before_counts = tuple(int(value) for value in cursor.fetchone())
    prepared = ResumeCoordinator(ResumeRepository(connection, realm.id)).prepare(
        work.id, client_id="claude", observed_at=now
    )
    assert prepared.disposition.value == "recovery-required"
    assert prepared.checkpoint_id == first.checkpoint_id
    assert prepared.sandbox == common["sandbox"]
    assert prepared.reconciliation_actions[0].claim_id == claim_two
    assert prepared.reacquire_resources == ()
    assert prepared.actions[0].kind == "reconcile-effect"
    assert not prepared.grants_authority
    with connection.cursor() as cursor:
        cursor.execute(
            "select (select count(*) from runtime.job),"
            " (select count(*) from security.authorization),"
            " (select count(*) from runtime.lease),"
            " (select count(*) from work.checkpoint_v2)"
        )
        after_counts = tuple(int(value) for value in cursor.fetchone())
    assert after_counts == before_counts
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="resume-actor", now=now)
    )
    apply_effect_digest = digest("effect:two")
    authorization = Authorization.issue(
        realm_id=realm.id,
        actor_id=actor.id,
        work_item_id=work.id,
        plan_id=plan.id,
        plan_digest=plan.plan_digest,
        effect_digest=apply_effect_digest,
        scope=AuthorizationScope(
            allowed_resources=("project:checkpoint",),
            allowed_effects=(EffectKind.DATABASE_WRITE.value,),
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=now,
    )
    AuthorizationRepository(connection, realm.id).issue(authorization)
    apply_repository = ResumeApplyRepository(connection, realm.id)
    apply_id, apply_created = apply_repository.create(
        prepared,
        actor_id=actor.id,
        authorization_id=authorization.id,
        effect_digest=apply_effect_digest,
        now=now,
    )
    assert apply_created
    apply_event = ResumeApplyEvent(
        apply_id=apply_id,
        sequence=1,
        phase=ResumeApplyPhase.CLAIM,
        state=ResumeApplyState.CLAIMED,
        reason_code="resume.test-claim",
        occurred_at=now,
        attempt_id=attempt_id,
        lease_id=lease_id,
        fencing_token=1,
        claim_id=claim_two,
    )
    assert apply_repository.append_event(apply_event)[1]
    connection.commit()
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.resume_apply_event"
            "(id,realm_id,resume_apply_id,sequence,phase,state,reason_code,attempt_id,lease_id,"
            "fencing_token,claim_id,previous_digest,event_digest,event_body,occurred_at) values"
            "(%s,%s,%s,2,'dispatch','dispatched','resume.forged',%s,%s,1,%s,%s,%s,%s::jsonb,%s)",
            (
                uuid4(),
                realm.id,
                apply_id,
                attempt_id,
                lease_id,
                claim_two,
                apply_event.event_digest,
                digest("forged-event"),
                '{"forged":true}',
                now,
            ),
        )
    connection.rollback()
    ambiguous = CheckpointV2(
        checkpoint_id=uuid4(),
        checkpoint_key="alternate-progress",
        revision=1,
        previous_checkpoint_id=None,
        previous_checkpoint_digest=None,
        completed_steps=("research",),
        pending_steps=("build",),
        step_results=(research_result,),
        open_effects=(
            OpenEffect(
                claim_two, digest("effect:two"), OpenEffectState.STARTED_NO_TERMINAL_RECEIPT
            ),
        ),
        next_safe_action=NextSafeActionV2("reconcile", "build", "receipt pending"),
        **{key: value for key, value in common.items() if key not in {"checkpoint_key"}},
    )
    assert repository.store(ambiguous) == (ambiguous.checkpoint_id, True)
    ambiguous_plan = ResumeCoordinator(ResumeRepository(connection, realm.id)).prepare(
        work.id, client_id="claude", observed_at=now
    )
    assert ambiguous_plan.disposition.value == "manual-review"
    assert ambiguous_plan.selected_checkpoint_reason == "ambiguous-or-invalid-v2-head"
    assert ambiguous_plan.reconciliation_actions == ()
    assert ambiguous_plan.actions == ()
    with pytest.raises(PsycopgError), connection.cursor() as cursor:
        cursor.execute(
            "update work.checkpoint_v2 set step_id='forged' where id=%s", (first.checkpoint_id,)
        )
    connection.rollback()

    incomplete = CheckpointV2(
        checkpoint_id=uuid4(),
        revision=3,
        previous_checkpoint_id=first.checkpoint_id,
        previous_checkpoint_digest=first.checkpoint_digest,
        completed_steps=("research", "build"),
        pending_steps=(),
        step_results=(
            research_result,
            StepResultV2(
                "build",
                result_digest,
                EffectKind.FILE_WRITE,
                job_id,
                attempt_id,
                builder_id,
                envelope.id,
                envelope.envelope_digest,
                (receipt_one,),
            ),
        ),
        open_effects=(
            OpenEffect(
                claim_two, digest("effect:two"), OpenEffectState.STARTED_NO_TERMINAL_RECEIPT
            ),
        ),
        next_safe_action=None,
        resumability=Resumability.SAFE_CONTINUE,
        **{key: value for key, value in common.items() if key != "resumability"},
    )
    with pytest.raises(PsycopgError):
        repository.store(incomplete)
    connection.rollback()

    with connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.effect_receipt"
            "(id,realm_id,claim_id,status,result_digest) values(%s,%s,%s,'completed',%s)",
            (receipt_two, realm.id, claim_two, result_digest),
        )
        cursor.execute(
            "insert into agents.invocation"
            "(id,realm_id,assignment_id,client_id,execution_identity,invocation_digest,created_at)"
            " values(%s,%s,%s,'codex','verifier-run',%s,%s)",
            (verifier_invocation, realm.id, verifier_id, digest("verifier-invocation"), now),
        )
        cursor.execute(
            "insert into agents.result_receipt"
            "(realm_id,assignment_id,invocation_id,envelope_digest) values(%s,%s,%s,%s)",
            (realm.id, verifier_id, verifier_invocation, digest("verifier-result")),
        )
        cursor.execute(
            "insert into agents.invocation"
            "(id,realm_id,assignment_id,client_id,execution_identity,invocation_digest,created_at)"
            " values(%s,%s,%s,'codex','shared-run',%s,%s)",
            (
                colliding_verifier_invocation,
                realm.id,
                verifier_id,
                digest("colliding-verifier-invocation"),
                now,
            ),
        )
        cursor.execute(
            "insert into agents.result_receipt"
            "(realm_id,assignment_id,invocation_id,envelope_digest) values(%s,%s,%s,%s)",
            (
                realm.id,
                verifier_id,
                colliding_verifier_invocation,
                digest("colliding-verifier-result"),
            ),
        )
    connection.commit()

    forged_effect = replace(
        incomplete,
        open_effects=(),
        step_results=(
            research_result,
            StepResultV2(
                "build",
                result_digest,
                EffectKind.NONE,
                job_id,
                attempt_id,
                builder_id,
                envelope.id,
                envelope.envelope_digest,
            ),
        ),
    )
    with pytest.raises(PsycopgError):
        repository.store(forged_effect)
    connection.rollback()

    mixed_receipt = replace(
        incomplete,
        open_effects=(),
        step_results=(
            research_result,
            StepResultV2(
                "build",
                result_digest,
                EffectKind.FILE_WRITE,
                job_id,
                attempt_id,
                builder_id,
                envelope.id,
                envelope.envelope_digest,
                (receipt_one, receipt_two, foreign_receipt_id),
                (verifier_invocation,),
                True,
            ),
        ),
    )
    with pytest.raises(PsycopgError):
        repository.store(mixed_receipt)
    connection.rollback()

    colliding_verifier = replace(
        incomplete,
        open_effects=(),
        step_results=(
            research_result,
            StepResultV2(
                "build",
                result_digest,
                EffectKind.FILE_WRITE,
                job_id,
                attempt_id,
                builder_id,
                envelope.id,
                envelope.envelope_digest,
                (receipt_one, receipt_two),
                (verifier_invocation, colliding_verifier_invocation),
                True,
            ),
        ),
    )
    with pytest.raises(PsycopgError):
        repository.store(colliding_verifier)
    connection.rollback()

    stale_context = replace(
        incomplete,
        open_effects=(),
        step_results=(
            research_result,
            StepResultV2(
                "build",
                result_digest,
                EffectKind.FILE_WRITE,
                job_id,
                attempt_id,
                builder_id,
                envelope.id,
                envelope.envelope_digest,
                (receipt_one, receipt_two),
                (verifier_invocation,),
                True,
            ),
        ),
        bindings=replace(bindings, dependency_snapshot_digest=digest("forged-dependencies")),
    )
    with pytest.raises(PsycopgError):
        repository.store(stale_context)
    connection.rollback()

    completed = replace(
        incomplete,
        open_effects=(),
        step_results=(
            research_result,
            StepResultV2(
                "build",
                result_digest,
                EffectKind.FILE_WRITE,
                job_id,
                attempt_id,
                builder_id,
                envelope.id,
                envelope.envelope_digest,
                (receipt_one, receipt_two),
                (verifier_invocation,),
                True,
            ),
        ),
    )
    assert repository.store(completed) == (completed.checkpoint_id, True)
    assert repository.is_complete(completed.checkpoint_id)
    with connection.cursor() as cursor:
        cursor.execute("update runtime.job set state='completed' where id=%s", (job_id,))
    connection.commit()


def test_checkpoint_v2_latest_returns_none_for_unknown_key(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    repository = CheckpointV2Repository(connection, realm.id)
    assert repository.latest("missing") is None


def test_memory_usage_correlates_only_after_canonical_independent_checkpoint_verification(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def consume(scope: dict[str, Any]) -> None:
        realm = scope["realm"]
        connection = scope["connection"]
        now = scope["now"]
        project = scope["project"]
        work = scope["work"]
        plan = scope["plan"]
        model = scope["model"]
        research_checkpoint = scope["research_checkpoint"]
        research_job_id = scope["research_job_id"]
        research_attempt_id = scope["research_attempt_id"]
        research_builder_id = scope["research_builder_id"]
        coordinator_id = scope["coordinator_id"]

        record = MemoryRecord(
            memory_id="checkpoint-outcome-memory",
            key=MemoryKey(MemoryScope.PROJECT, realm.slug, project_ref=project.slug),
            memory_class=MemoryClass.SEMANTIC,
            content="Bagimsiz checkpoint sonucu bellek yararini dogrular",
            state=MemoryState.ACTIVE,
            revision=1,
            created_at=now - dt.timedelta(minutes=1),
            evidence=(MemoryEvidence("test", "memory/outcome", digest("usage-evidence")),),
            reviewed_by="memory-reviewer",
            author_ref="memory-author",
            valid_from=now - dt.timedelta(minutes=1),
        )
        record_id = MemoryRepository(
            connection, realm.id, realm.slug, project.id, project.slug
        ).store_record(record)
        candidate = ContextCandidate(
            "verified-memory",
            AuthorityLevel.VERIFIED,
            now,
            record.record_digest,
            digest(record.content),
            6,
            True,
        )
        context_manifest = compile_context(
            (candidate,),
            token_budget=10,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=now,
        )
        fragment_set = materialize_fragments(
            context_manifest,
            (candidate,),
            (
                FragmentMaterialization(
                    "verified-memory",
                    ContextContentKind.MEMORY,
                    ContextRole.USER,
                    ContextVisibility.MODEL,
                    f"memory-record/{record_id}",
                    record.content,
                ),
            ),
        )
        continuity = ContextContinuityRepository(connection, realm.id, project.id, work.id)
        continuity.store_manifest(context_manifest)
        continuity.store_fragment_set(fragment_set, created_at=now)

        actor = ActorRepository(connection, realm.id).add(
            Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="usage-runtime", now=now)
        )
        effect_digest = digest("usage-provider-effect")
        authorization = Authorization.issue(
            realm_id=realm.id,
            actor_id=actor.id,
            work_item_id=work.id,
            plan_id=plan.id,
            plan_digest=plan.plan_digest,
            effect_digest=effect_digest,
            scope=AuthorizationScope(
                allowed_effects=("provider-call",), provider_refs=("provider:test",)
            ),
            risk="medium",
            lifetime=dt.timedelta(minutes=5),
            now=now,
        )
        AuthorizationRepository(connection, realm.id).issue(authorization)
        claim = EffectClaim.create(
            realm_id=realm.id,
            job_id=research_job_id,
            attempt_id=research_attempt_id,
            operation="provider.invoke",
            effect_digest=effect_digest,
            authorization_digest=authorization.authorization_digest,
            idempotency_key=f"memory-outcome-{uuid4()}",
            resources=(),
            execution_identity="memory-outcome:1",
            fencing_token=1,
            adapter_digest=digest("memory-outcome-adapter"),
            now=now,
        )
        ledger = EffectLedger(connection, realm.id)
        ledger.claim(claim, authorization_id=authorization.id)
        receipt = EffectReceipt.completed(
            realm_id=realm.id,
            claim=claim,
            result_digest=digest("memory-provider-result"),
            now=now,
        )
        ledger.receipt(receipt)
        assert (
            AuthorizationRepository(connection, realm.id)
            .consume(
                authorization.id,
                effect_digest=effect_digest,
                consumed_by="model-gateway",
                now=now,
            )
            .consumed
        )
        request = ModelRequestManifest.create(
            realm_id=realm.id,
            project_id=project.id,
            work_item_id=work.id,
            plan_id=plan.id,
            step_id="research",
            execution_envelope_id=None,
            execution_envelope_digest=None,
            run_id=scope["run"].id,
            job_id=research_job_id,
            attempt_id=research_attempt_id,
            assignment_id=research_builder_id,
            role="builder",
            risk="medium",
            route_decision_digest=scope["route_digest"],
            model_id=model.model_id,
            provider_ref="provider:test",
            context_manifest_digest=context_manifest.manifest_digest,
            context_fragment_set_digest=fragment_set.fragment_set_digest,
            model_visible_payload_digest=digest("memory-visible-payload"),
            context_packet_digest=scope["packet"].packet_digest,
            checkpoint_digest=None,
            source_revision=scope["scan"].revision.revision,
            policy_digest=scope["policy_digest"],
            payload_digest=digest("memory-visible-payload"),
            authorization_scope_digest=digest("memory-authorization-scope"),
            output_schema_digest=digest("memory-output-schema"),
            idempotency_key=f"memory-request-{uuid4()}",
            max_input_tokens=10,
            max_output_tokens=10,
            max_cost_micros=100,
            deadline=scope["run"].deadline,
            route_expires_at=scope["target"].expires_at,
            source_label=GatewaySourceLabel.MODEL_CAPABILITY,
            missing_bindings=(
                "checkpoint_digest",
                "execution_envelope_digest",
                "execution_envelope_id",
            ),
            created_at=now,
        )
        invocation_repository = ModelInvocationRepository(connection, realm.id)
        invocation_repository.store_manifest(request)
        invocation_attempt_id = invocation_repository.record_attempt(
            manifest_id=request.id,
            effect_claim_id=claim.id,
            authorization_id=authorization.id,
        )
        invocation_result_id = uuid4()
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into models.invocation_result"
                " (id,realm_id,manifest_id,attempt_id,effect_receipt_id,state,response_digest,"
                "created_at) values(%s,%s,%s,%s,%s,'verified',%s,%s)",
                (
                    invocation_result_id,
                    realm.id,
                    request.id,
                    invocation_attempt_id,
                    receipt.id,
                    receipt.result_digest,
                    now,
                ),
            )
            cursor.execute("select id from memory.usage_event where record_id=%s", (record_id,))
            usage_id = cursor.fetchone()[0]
            cursor.execute(
                "select count(*) from memory.usage_outcome where usage_event_id=%s", (usage_id,)
            )
            assert int(cursor.fetchone()[0]) == 0

        # Same work/step, but a different plan/run/job must never inherit this
        # checkpoint's verified outcome.
        foreign_plan_id, foreign_run_id, foreign_assignment_id = (uuid4() for _ in range(3))
        foreign_job_id, foreign_runtime_attempt_id = (uuid4() for _ in range(2))
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into work.task_plan"
                " (id,realm_id,project_id,work_item_id,revision,source_revision,policy_digest,"
                "steps,effect_digest,plan_digest,created_at) values"
                " (%s,%s,%s,%s,2,%s,%s,"
                ' \'[{"step_id":"research","effect":"provider-call"}]\'::jsonb,'
                "%s,%s,%s)",
                (
                    foreign_plan_id,
                    realm.id,
                    project.id,
                    work.id,
                    scope["scan"].revision.revision,
                    scope["policy_digest"],
                    digest("foreign-plan-effect"),
                    digest("foreign-plan"),
                    now,
                ),
            )
            cursor.execute(
                "insert into runtime.execution_run"
                " (id,realm_id,project_id,work_item_id,plan_id,client_id,source_revision,"
                "policy_digest,max_input_tokens,max_output_tokens,max_cost_micros,deadline,state,"
                "run_digest,created_at,started_at) values"
                " (%s,%s,%s,%s,%s,'codex',%s,%s,10,10,100,%s,'active',%s,%s,%s)",
                (
                    foreign_run_id,
                    realm.id,
                    project.id,
                    work.id,
                    foreign_plan_id,
                    scope["scan"].revision.revision,
                    scope["policy_digest"],
                    scope["run"].deadline,
                    digest("foreign-run"),
                    now,
                    now,
                ),
            )
            cursor.execute(
                "insert into agents.assignment"
                " (id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,"
                "role,agent_ref,status,risk,instruction_digest,context_manifest_digest,"
                "assignment_digest,created_at) values"
                " (%s,%s,%s,%s,%s,'research',%s,'builder','foreign-builder','active','medium',"
                "%s,%s,%s,%s)",
                (
                    foreign_assignment_id,
                    realm.id,
                    project.id,
                    work.id,
                    foreign_plan_id,
                    coordinator_id,
                    digest("foreign-instruction"),
                    context_manifest.manifest_digest,
                    digest("foreign-assignment"),
                    now,
                ),
            )
            cursor.execute(
                "insert into runtime.job"
                " (id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,attempt_count,"
                "idempotency_key,assignment_id,run_id) values"
                " (%s,%s,%s,%s,%s,'research','provider-call','running',1,%s,%s,%s)",
                (
                    foreign_job_id,
                    realm.id,
                    project.id,
                    work.id,
                    foreign_plan_id,
                    f"foreign-job-{foreign_job_id}",
                    foreign_assignment_id,
                    foreign_run_id,
                ),
            )
            cursor.execute(
                "insert into runtime.job_attempt"
                " (id,realm_id,job_id,attempt_number,fencing_token,worker_label,started_at)"
                " values(%s,%s,%s,1,1,'foreign-worker',%s)",
                (foreign_runtime_attempt_id, realm.id, foreign_job_id, now),
            )
        foreign_effect = digest("foreign-provider-effect")
        foreign_authorization = Authorization.issue(
            realm_id=realm.id,
            actor_id=actor.id,
            work_item_id=work.id,
            plan_id=foreign_plan_id,
            plan_digest=digest("foreign-plan"),
            effect_digest=foreign_effect,
            scope=AuthorizationScope(
                allowed_effects=("provider-call",), provider_refs=("provider:test",)
            ),
            risk="medium",
            lifetime=dt.timedelta(minutes=5),
            now=now,
        )
        AuthorizationRepository(connection, realm.id).issue(foreign_authorization)
        foreign_claim = EffectClaim.create(
            realm_id=realm.id,
            job_id=foreign_job_id,
            attempt_id=foreign_runtime_attempt_id,
            operation="provider.invoke",
            effect_digest=foreign_effect,
            authorization_digest=foreign_authorization.authorization_digest,
            idempotency_key=f"foreign-memory-{uuid4()}",
            resources=(),
            execution_identity="foreign-memory:1",
            fencing_token=1,
            adapter_digest=digest("foreign-adapter"),
            now=now,
        )
        ledger.claim(foreign_claim, authorization_id=foreign_authorization.id)
        foreign_receipt = EffectReceipt.completed(
            realm_id=realm.id,
            claim=foreign_claim,
            result_digest=digest("foreign-provider-result"),
            now=now,
        )
        ledger.receipt(foreign_receipt)
        assert (
            AuthorizationRepository(connection, realm.id)
            .consume(
                foreign_authorization.id,
                effect_digest=foreign_effect,
                consumed_by="model-gateway",
                now=now,
            )
            .consumed
        )
        foreign_request = ModelRequestManifest.create(
            **{
                name: getattr(request, name)
                for name in request.__dataclass_fields__
                if name not in {"id", "manifest_digest"}
            }
            | {
                "plan_id": foreign_plan_id,
                "run_id": foreign_run_id,
                "job_id": foreign_job_id,
                "attempt_id": foreign_runtime_attempt_id,
                "assignment_id": foreign_assignment_id,
                "idempotency_key": f"foreign-request-{uuid4()}",
            }
        )
        invocation_repository.store_manifest(foreign_request)
        foreign_invocation_attempt = invocation_repository.record_attempt(
            manifest_id=foreign_request.id,
            effect_claim_id=foreign_claim.id,
            authorization_id=foreign_authorization.id,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into models.invocation_result"
                " (id,realm_id,manifest_id,attempt_id,effect_receipt_id,state,response_digest,"
                "created_at) values(%s,%s,%s,%s,%s,'verified',%s,%s)",
                (
                    uuid4(),
                    realm.id,
                    foreign_request.id,
                    foreign_invocation_attempt,
                    foreign_receipt.id,
                    foreign_receipt.result_digest,
                    now,
                ),
            )
            cursor.execute(
                "select id from memory.usage_event where record_id=%s and task_plan_id=%s",
                (record_id, foreign_plan_id),
            )
            foreign_usage_id = cursor.fetchone()[0]

        verifier_assignment_id = uuid4()
        verifier_invocation_id = uuid4()
        verifier_envelope = digest("memory-outcome-verifier-envelope")
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into agents.assignment"
                " (id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,"
                "role,agent_ref,status,risk,instruction_digest,context_manifest_digest,"
                "assignment_digest,created_at) values"
                " (%s,%s,%s,%s,%s,'research',%s,'verifier','memory-outcome-verifier',"
                "'active','medium',%s,%s,%s,%s)",
                (
                    verifier_assignment_id,
                    realm.id,
                    project.id,
                    work.id,
                    plan.id,
                    coordinator_id,
                    digest("memory-verifier-instruction"),
                    context_manifest.manifest_digest,
                    digest("memory-verifier-assignment"),
                    now,
                ),
            )
            cursor.execute(
                "insert into agents.invocation"
                " (id,realm_id,assignment_id,client_id,execution_identity,invocation_digest,"
                "created_at) values(%s,%s,%s,'codex','memory-verifier-run',%s,%s)",
                (
                    verifier_invocation_id,
                    realm.id,
                    verifier_assignment_id,
                    digest("memory-verifier-invocation"),
                    now,
                ),
            )
            cursor.execute(
                "insert into agents.result_receipt"
                " (realm_id,assignment_id,invocation_id,envelope_digest) values(%s,%s,%s,%s)",
                (realm.id, verifier_assignment_id, verifier_invocation_id, verifier_envelope),
            )
            cursor.execute(
                "insert into work.checkpoint_v2_step_verification"
                " (realm_id,checkpoint_id,step_id,verifier_assignment_id,"
                "verifier_invocation_id,envelope_digest) values(%s,%s,'research',%s,%s,%s)",
                (
                    realm.id,
                    research_checkpoint.checkpoint_id,
                    verifier_assignment_id,
                    verifier_invocation_id,
                    verifier_envelope,
                ),
            )
        connection.commit()
        outcomes = MemoryTelemetryRepository(connection, realm.id).outcomes_for_record(record_id)
        assert len(outcomes) == 1
        assert outcomes[0].outcome_status == "verified-success"
        assert outcomes[0].usage_event_id == usage_id
        with connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from memory.usage_outcome where usage_event_id=%s",
                (foreign_usage_id,),
            )
            assert int(cursor.fetchone()[0]) == 0
        captured["done"] = True

    global E2E_FIXTURE_CONSUMER
    E2E_FIXTURE_CONSUMER = consume
    try:
        test_checkpoint_v2_evidence_revision_and_terminal_gate(realm_session, tmp_path)
    finally:
        E2E_FIXTURE_CONSUMER = None
    assert captured == {"done": True}
