from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from tests.integration.test_agent_residency_postgres import residency_scope as _residency_scope
from tests.integration.test_memory_continuity_postgres import _canonical_projection

from zekam.application.client_lifecycle_bridge import (
    ClientLifecycleBridge,
    LifecycleClientContract,
    LifecycleRequest,
)
from zekam.application.hook_runtime import HookRuntime, LoadedHookAdapter
from zekam.application.memory_continuity import (
    HydrationPreparation,
    MemoryContinuityService,
)
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.clients import ClientDescriptor, ClientKind
from zekam.domain.config_provenance import PermissionProfileRevision
from zekam.domain.execution_run import ExecutionRun
from zekam.domain.hook_runtime import (
    HookAdapterResult,
    HookEventType,
    HookExecutionMode,
    HookFailurePolicy,
    HookLoadState,
    HookResultKind,
    HookRuntimeRevision,
    HookSpecRevision,
)
from zekam.domain.project import SourceRevisionKind
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.session_continuity import (
    CompactionReceipt,
    CompactionStatus,
    DataClassification,
    SessionLifecycleEvent,
)
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.config_provenance_repository import (
    ConfigProvenanceRepository,
)
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
from zekam.infrastructure.postgres.hook_runtime_repository import HookRuntimeRepository
from zekam.infrastructure.postgres.memory_continuity_repository import (
    MemoryContinuityRepository,
)
from zekam.infrastructure.postgres.project_repository import SourceBindingRepository
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime.now(dt.UTC)


def _scope(realm: Any, connection: Any, tmp_path: Path):  # type: ignore[no-untyped-def]
    source = tmp_path / "continuity-application-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(
        project_id=project.id,
        type=WorkType.TASK,
        title="Continuity application integration",
    )
    policy_digest = digest("continuity-policy")
    plan = graph.create_plan(
        work.id,
        source_revision="revision-1",
        policy_digest=policy_digest,
        steps=(PlanStep("continuity", "Continuity", EffectKind.DATABASE_WRITE),),
    )
    run = ExecutionRun.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        client_id="opencode-local",
        session_id="continuity-integration",
        source_revision="revision-1",
        policy_digest=policy_digest,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        deadline=NOW + dt.timedelta(minutes=10),
        created_at=NOW,
    )
    executions = ExecutionRunRepository(connection, realm.id)
    executions.create_run(run)
    executions.activate_run(run.id, started_at=NOW)
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="continuity-reviewer", now=NOW)
    )
    return project, work, plan, run, actor


def _hook_runtime(realm_id, connection):  # type: ignore[no-untyped-def]
    input_schema = {
        "type": "object",
        "properties": {
            "lifecycle": {"type": "object"},
            "data": {
                "type": "object",
                "properties": {"checkpoint_digest": {"type": "string"}},
                "required": ["checkpoint_digest"],
                "additionalProperties": False,
            },
        },
        "required": ["lifecycle", "data"],
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {"ack_digest": {"type": "string"}},
        "required": ["ack_digest"],
        "additionalProperties": False,
    }
    profile = PermissionProfileRevision.from_flags(
        realm_id=realm_id,
        name="continuity-hook-readonly",
        revision=1,
        permission_flags={
            "filesystem.read": True,
            "filesystem.write": False,
            "network.access": False,
            "process.run": False,
        },
        managed=True,
        created_at=NOW,
    )
    spec = HookSpecRevision.create(
        realm_id=realm_id,
        hook_id="opencode-pre-compaction",
        revision=1,
        event_type=HookEventType.PRE_COMPACTION,
        required=True,
        source_layer="managed-policy",
        timeout_ms=1_000,
        execution_mode=HookExecutionMode.INTERNAL,
        input_schema=input_schema,
        output_schema=output_schema,
        permission_profile_name=profile.name,
        permission_profile_digest=profile.profile_digest,
        failure_policy=HookFailurePolicy.ABORT,
        created_at=NOW,
    )
    revision = HookRuntimeRevision.create(
        realm_id=realm_id,
        hook_id=spec.hook_id,
        hook_revision=spec.revision,
        adapter_ref="continuity-hook-v1",
        adapter_digest=digest("continuity-hook-v1"),
        permission_capabilities=("filesystem.read",),
        load_state=HookLoadState.READY,
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(hours=1),
    )
    adapter = LoadedHookAdapter(
        revision.adapter_ref,
        revision.adapter_digest,
        HookExecutionMode.INTERNAL,
        lambda payload: HookAdapterResult(
            HookResultKind.OBSERVATION,
            {"ack_digest": digest(payload["data"]["checkpoint_digest"])},
        ),
    )
    runtime = HookRuntime()
    compiled = runtime.reconfigure(
        realm_id=realm_id,
        config_effective_digest=digest("continuity-hook-config"),
        specs=(spec,),
        runtimes=(revision,),
        profiles=(profile,),
        adapters=(adapter,),
        now=NOW,
        required_events=(HookEventType.PRE_COMPACTION,),
    )
    hook_repository = HookRuntimeRepository(connection, realm_id)
    profile_id, _ = ConfigProvenanceRepository(connection, realm_id).store_profile(profile)
    hook_repository.store_spec(spec, permission_profile_revision_id=profile_id)
    hook_repository.store_runtime(revision)
    compiled_id, _ = hook_repository.store_compiled_set(compiled, created_at=NOW)
    hook_repository.activate(compiled_id)
    binding_id = hook_repository.start_session(session_ref="continuity-integration")
    return runtime, runtime.start_session(), hook_repository, binding_id


def _authorize(
    authorizations: AuthorizationRepository,
    *,
    realm_id,
    actor,
    work,
    source_plan,
    mutation_plan,
):  # type: ignore[no-untyped-def]
    authorization = Authorization.issue(
        realm_id=realm_id,
        actor_id=actor.id,
        work_item_id=work.id,
        plan_id=source_plan.id,
        plan_digest=mutation_plan.plan_digest,
        effect_digest=mutation_plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(mutation_plan.resource,),
            allowed_effects=("database-write",),
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )
    return authorizations.issue(authorization)


def test_bridge_stages_outbox_records_hook_receipt_and_requires_terminal_finalize(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    scope = _residency_scope.__wrapped__(realm_session, tmp_path)  # type: ignore[attr-defined]
    run = scope["run"]
    project = SimpleNamespace(id=run.project_id)
    work = SimpleNamespace(id=run.work_item_id)
    source_plan = SimpleNamespace(id=run.plan_id)
    bindings = SourceBindingRepository(connection, realm.id)
    binding = bindings.for_project(project.id)[0]
    bindings.record_revision(
        binding_id=binding.id,
        kind=SourceRevisionKind.TREE_DIGEST,
        revision=run.source_revision,
        tree_digest=digest("bridge-source-tree"),
        now=NOW,
    )
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(
            realm=realm,
            kind=ActorKind.HUMAN,
            slug="bridge-continuity-authorizer",
            now=NOW,
        )
    )
    runtime, session, hook_repository, binding_id = _hook_runtime(realm.id, connection)
    repository = MemoryContinuityRepository(connection, realm.id)
    authorizations = AuthorizationRepository(connection, realm.id)
    bridge = ClientLifecycleBridge(runtime, repository, authorizations, hook_repository)
    descriptor = ClientDescriptor(
        ClientKind.OPENCODE,
        run.client_id,
        "opencode.exe",
        frozenset({"chat", "structured-result", "lifecycle-events-v2"}),
        version="1.0.0-reviewed",
    )
    contract = LifecycleClientContract.verified(
        descriptor=descriptor,
        installed_version="1.0.0-reviewed",
        event_mapping=(("session.compacting", HookEventType.PRE_COMPACTION),),
        contract_evidence_digest=digest("official-opencode-contract"),
    )
    request = LifecycleRequest(
        realm.id,
        project.id,
        work.id,
        run.id,
        run.session_id,
        run.client_id,
        uuid4(),
        "session.compacting",
        1,
        None,
        f"client:{run.client_id}",
        "client-event:one",
        "run:continuity-integration",
        0,
        3,
        run.source_revision,
        f"work-plan:{source_plan.id}",
        "checkpoint:draft-1",
        "context:bounded-1",
        (),
        DataClassification.INTERNAL,
        {"checkpoint_digest": digest("checkpoint")},
        "opencode:continuity-integration:pre-compaction:1",
        NOW,
        NOW,
    )
    plan = bridge.prepare(
        request,
        contract,
        session,
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
    )
    authorization = _authorize(
        authorizations,
        realm_id=realm.id,
        actor=actor,
        work=work,
        source_plan=source_plan,
        mutation_plan=plan,
    )

    applied = bridge.apply(
        plan,
        session,
        session_binding_id=binding_id,
        authorization_id=authorization.id,
        current_source_digest=plan.source_digest,
        current_policy_digest=plan.policy_digest,
        current_migration_digest=plan.migration_digest,
        now=NOW,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "select state,terminal_receipt_digest from continuity.lifecycle_delivery_outbox"
            " where realm_id=%s and id=%s",
            (realm.id, applied.outbox_id),
        )
        assert cursor.fetchone() == ("pending", None)
        cursor.execute(
            "select count(*) from hooks.result_receipt where realm_id=%s",
            (realm.id,),
        )
        assert cursor.fetchone()[0] == 1

    continuity_service = MemoryContinuityService(repository, authorizations)
    compaction_receipt = CompactionReceipt(
        receipt_id=uuid4(),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=request.session_id,
        client_id=request.client_id,
        pre_compaction_event_digest=applied.event_digest,
        checkpoint_draft_digest=digest("checkpoint-draft"),
        outbox_ref=f"continuity-outbox:{applied.outbox_id}",
        outbox_payload_digest=digest("outbox-payload"),
        worker_result_digest=None,
        checkpoint_ref=None,
        checkpoint_digest=None,
        post_compaction_event_digest=None,
        rehydration_receipt_digest=None,
        status=CompactionStatus.PREPARED,
        created_at=NOW,
        completed_at=None,
    )
    compaction_plan = continuity_service.prepare_compaction(
        compaction_receipt,
        idempotency_key="compaction:continuity-integration:1",
        source_digest=plan.source_digest,
        policy_digest=plan.policy_digest,
        migration_digest=plan.migration_digest,
        context_digest=digest("context"),
    )
    compaction_authorization = _authorize(
        authorizations,
        realm_id=realm.id,
        actor=actor,
        work=work,
        source_plan=source_plan,
        mutation_plan=compaction_plan,
    )
    continuity_service.apply(
        compaction_plan,
        authorization_id=compaction_authorization.id,
        now=NOW,
    )
    hydration_event = SessionLifecycleEvent(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
        event_id=uuid4(),
        event_type="hydration_required",
        sequence=2,
        previous_digest=applied.event_digest,
        origin="client/opencode",
        causation_id="cause/bridge-hydration",
        correlation_id="correlation/bridge-hydration",
        recursion_depth=0,
        source_revision=run.source_revision,
        plan_ref=f"work-plan:{run.plan_id}",
        checkpoint_ref=f"run:{run.id}:genesis",
        context_ref="context/current",
        payload_digest=digest("bridge-hydration-event"),
        metadata=(),
        classification=DataClassification.INTERNAL,
        occurred_at=NOW,
        ingested_at=NOW,
    )
    repository.stage_lifecycle_delivery(
        hydration_event,
        idempotency_key="bridge-hydration-lifecycle",
        plan_digest=digest("bridge-hydration-lifecycle-plan"),
    )
    _canonical_projection(realm, connection, repository, project, work)
    hydration_plan = continuity_service.prepare_hydration(
        HydrationPreparation(
            receipt_id=uuid4(),
            realm_id=realm.id,
            project_id=project.id,
            work_item_id=work.id,
            run_id=run.id,
            session_id=run.session_id,
            client_id=run.client_id,
            token_budget=8,
            idempotency_key="bridge-hydration-receipt",
            created_at=NOW,
        )
    )
    hydration_authorization = _authorize(
        authorizations,
        realm_id=realm.id,
        actor=actor,
        work=work,
        source_plan=source_plan,
        mutation_plan=hydration_plan,
    )
    continuity_service.apply(
        hydration_plan,
        authorization_id=hydration_authorization.id,
        now=NOW,
    )
    bridge.finalize(
        applied,
        receipt_digest=compaction_receipt.receipt_digest,
        status="completed",
        completed_at=NOW,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "select state,terminal_receipt_digest from continuity.lifecycle_delivery_outbox"
            " where realm_id=%s and id=%s",
            (realm.id, applied.outbox_id),
        )
        assert cursor.fetchone() == ("completed", compaction_receipt.receipt_digest)
    hook_repository.close_session(binding_id)
    runtime.close_session(session)
    runtime.shutdown(timeout_seconds=0)


def test_hydration_prepare_apply_uses_concrete_repository_and_snapshot(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    scope = _residency_scope.__wrapped__(realm_session, tmp_path)  # type: ignore[attr-defined]
    run = scope["run"]
    project = SimpleNamespace(id=run.project_id)
    work = SimpleNamespace(id=run.work_item_id)
    source_plan = SimpleNamespace(id=run.plan_id)
    bindings = SourceBindingRepository(connection, realm.id)
    binding = bindings.for_project(project.id)[0]
    bindings.record_revision(
        binding_id=binding.id,
        kind=SourceRevisionKind.TREE_DIGEST,
        revision=run.source_revision,
        tree_digest=digest("application-hydration-source-tree"),
        now=NOW,
    )
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(
            realm=realm,
            kind=ActorKind.HUMAN,
            slug="application-hydration-authorizer",
            now=NOW,
        )
    )
    repository = MemoryContinuityRepository(connection, realm.id)
    authorizations = AuthorizationRepository(connection, realm.id)
    service = MemoryContinuityService(repository, authorizations)
    event = SessionLifecycleEvent(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
        event_id=uuid4(),
        event_type="hydration_required",
        sequence=1,
        previous_digest=None,
        origin="client/opencode",
        causation_id="cause/application-hydration",
        correlation_id="correlation/application-hydration",
        recursion_depth=0,
        source_revision=run.source_revision,
        plan_ref=f"work-plan:{run.plan_id}",
        checkpoint_ref=f"run:{run.id}:genesis",
        context_ref="context/current",
        payload_digest=digest("application-hydration-event"),
        metadata=(),
        classification=DataClassification.INTERNAL,
        occurred_at=NOW,
        ingested_at=NOW,
    )
    repository.stage_lifecycle_delivery(
        event,
        idempotency_key="application-hydration-lifecycle",
        plan_digest=digest("application-hydration-lifecycle-plan"),
    )
    _canonical_projection(realm, connection, repository, project, work)
    request = HydrationPreparation(
        receipt_id=uuid4(),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
        token_budget=8,
        idempotency_key="hydration:continuity-integration:1",
        created_at=NOW,
    )
    plan = service.prepare_hydration(request)
    authorization = _authorize(
        authorizations,
        realm_id=realm.id,
        actor=actor,
        work=work,
        source_plan=source_plan,
        mutation_plan=plan,
    )
    applied = service.apply(
        plan,
        authorization_id=authorization.id,
        now=NOW,
    )
    snapshot = repository.read_session_snapshot(
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id=request.session_id,
        client_id=request.client_id,
    )
    assert applied.created and applied.receipt_digest == snapshot.hydration_receipt_digest
    assert snapshot.ready_for_mutation
