from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

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
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.session_continuity import (
    CompactionReceipt,
    CompactionStatus,
    ContextSelectionReference,
    DataClassification,
    FreshnessDimension,
    TruthClass,
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
    project, work, source_plan, run, actor = _scope(realm, connection, tmp_path)
    runtime, session, hook_repository, binding_id = _hook_runtime(realm.id, connection)
    repository = MemoryContinuityRepository(connection, realm.id)
    authorizations = AuthorizationRepository(connection, realm.id)
    bridge = ClientLifecycleBridge(runtime, repository, authorizations, hook_repository)
    descriptor = ClientDescriptor(
        ClientKind.OPENCODE,
        "opencode-local",
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
        "continuity-integration",
        "opencode-local",
        uuid4(),
        "session.compacting",
        1,
        None,
        "client:opencode-local",
        "client-event:one",
        "run:continuity-integration",
        0,
        3,
        "git:b8d970c",
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
        current_source_digest=compaction_plan.source_digest,
        current_policy_digest=compaction_plan.policy_digest,
        current_migration_digest=compaction_plan.migration_digest,
        current_context_digest=compaction_plan.context_digest,
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
    project, work, source_plan, run, actor = _scope(realm, connection, tmp_path)
    repository = MemoryContinuityRepository(connection, realm.id)
    authorizations = AuthorizationRepository(connection, realm.id)
    service = MemoryContinuityService(repository, authorizations)
    current = digest("current")
    request = HydrationPreparation(
        receipt_id=uuid4(),
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        run_id=run.id,
        session_id="continuity-integration",
        client_id="opencode-local",
        plan_ref=f"work-plan:{source_plan.id}",
        checkpoint_ref="checkpoint:one",
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
        inventory_digest=digest("inventory"),
        context_digest=digest("context"),
        required_candidates=(
            ContextSelectionReference(
                "context:required", digest("required"), 5, TruthClass.REPO_FACT
            ),
        ),
        optional_candidates=(),
        known_omissions=(),
        token_budget=8,
        freshness=(FreshnessDimension("source", current, current, True),),
        projection_refs=(),
        hydration_event_digest=digest("hydration-event"),
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
        current_source_digest=plan.source_digest,
        current_policy_digest=plan.policy_digest,
        current_migration_digest=plan.migration_digest,
        current_context_digest=plan.context_digest,
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
