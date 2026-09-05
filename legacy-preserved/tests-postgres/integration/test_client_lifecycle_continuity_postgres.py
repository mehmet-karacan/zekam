"""Real PostgreSQL boundaries for governed Codex lifecycle admission."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

import pytest
from tests.integration.test_agent_residency_postgres import residency_scope as _residency_scope

from zekam.application.capability_profile import PROFILER_VERSION, CapabilityProfile
from zekam.application.client_lifecycle_bridge import (
    ClientLifecycleBridge,
    LifecycleClientContract,
    LifecycleRequest,
)
from zekam.application.client_lifecycle_composition import (
    _EVENT_NAMESPACE,
    _configure_active_memory_hook_runtime,
    compose_codex_lifecycle_handler,
    recover_committed_codex_delivery,
)
from zekam.application.client_lifecycle_continuity import (
    _HYDRATION_NAMESPACE,
    _SESSION_START_HYDRATION_TOKEN_BUDGET,
    PostgresLifecycleContinuityAdmission,
)
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.execution import ExecutionHost
from zekam.application.hook_runtime import HookRuntime
from zekam.application.memory_continuity import (
    HydrationPreparation,
    MemoryContinuityService,
)
from zekam.application.memory_hooks import memory_hook_bundle
from zekam.application.memory_upgrade import canonical_projection_source_digest
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.clients import ClientKind, ClientLifecycleEvent
from zekam.domain.context_continuity import JournalEntry
from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_environment import (
    AssignmentEnvironmentBinding,
    ExecutionEnvironmentSnapshot,
    TurnExecutionSnapshot,
    detect_environment_drift,
    reprobe_snapshot,
)
from zekam.domain.execution_run import (
    CheckpointDisposition,
    ExecutionEnvelope,
    ExecutionRun,
    ProviderBindingSnapshot,
)
from zekam.domain.model_routing import ExecutionTargetSnapshot, ProjectRoutingContext
from zekam.domain.project import SourceRevisionKind
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import Job, JobKind
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
)
from zekam.domain.security import (
    DataClassification as AuthorizationClassification,
)
from zekam.domain.session_continuity import (
    DataClassification,
    DigestReference,
    HydrationInventoryEntry,
    HydrationInventorySnapshot,
    ProjectionGenerationReceipt,
    SessionLifecycleEvent,
    TruthClass,
)
from zekam.domain.work import EffectKind, PlanStep
from zekam.infrastructure.clients.codex_lifecycle import (
    CODEX_EVENT_MAPPING,
    CODEX_REVIEWED_VERSION,
    codex_lifecycle_descriptor,
    load_codex_contract_evidence,
    parse_codex_hook_input,
)
from zekam.infrastructure.postgres.client_lifecycle_repository import ClientLifecycleRepository
from zekam.infrastructure.postgres.connection import (
    configure_session,
)
from zekam.infrastructure.postgres.connection import (
    session as realm_database_session,
)
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
from zekam.infrastructure.postgres.hook_runtime_repository import HookRuntimeRepository
from zekam.infrastructure.postgres.memory_continuity_repository import MemoryContinuityRepository
from zekam.infrastructure.postgres.memory_hook_installer import PostgresMemoryHookInstaller
from zekam.infrastructure.postgres.model_routing_repository import ModelRoutingRepository
from zekam.infrastructure.postgres.project_repository import (
    CapabilityProfileRepository,
    SourceBindingRepository,
)
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.UTC)


def _store_canonical_active_work_projection(
    *,
    realm: Any,
    connection: Any,
    repository: MemoryContinuityRepository,
    run: Any,
    source_digest: str,
    generated_at: dt.datetime,
) -> ProjectionGenerationReceipt:
    with connection.cursor() as cursor:
        cursor.execute(
            "select revision,state,record_digest from work.work_item"
            " where realm_id=%s and project_id=%s and id=%s",
            (realm.id, run.project_id, run.work_item_id),
        )
        work_revision, work_state, work_record_digest = cursor.fetchone()
        cursor.execute("select max(version) from core.schema_migrations")
        migration_head = int(cursor.fetchone()[0])
    database_revision_digest = digest(
        {
            "project_id": str(run.project_id),
            "work_item_id": str(run.work_item_id),
            "work_revision": int(work_revision),
            "work_state": str(work_state),
            "work_record_digest": str(work_record_digest),
        }
    )
    projection_source_digest = canonical_projection_source_digest(
        source_head=run.source_revision,
        source_tree_digest=source_digest,
        migration_head=migration_head,
        database_revision_digest=database_revision_digest,
    )
    projection_body = {
        "schema": "zekam-memory-continuity-public-projection/v1",
        "project_id": str(run.project_id),
        "work_item_id": str(run.work_item_id),
        "work_revision": int(work_revision),
        "work_state": str(work_state),
        "source_head": run.source_revision,
        "source_tree_digest": source_digest,
        "migration_head": migration_head,
        "database_revision_digest": database_revision_digest,
        "source_digest": projection_source_digest,
        "classification": "public",
        "public_filtered": True,
        "content_included": False,
        "fresh": True,
        "read_only": True,
        "grants_authority": False,
    }
    receipt = ProjectionGenerationReceipt(
        receipt_id=uuid4(),
        realm_id=realm.id,
        project_id=run.project_id,
        work_item_id=run.work_item_id,
        source_ref=f"work-item/{run.work_item_id}/revision/{work_revision}",
        source_digest=projection_source_digest,
        projection_ref="projection/active-work",
        projection_digest=digest(projection_body),
        generator_version="projection/v1",
        generated_at=generated_at,
    )
    assert repository.store_projection_receipt(
        receipt,
        idempotency_key=f"test:active-work-projection:{run.id}",
    )
    return receipt


def test_non_session_lifecycle_requires_fresh_hydration(
    realm_session: tuple[Any, Any],
    tmp_path: Path,
) -> None:
    scope = _residency_scope.__wrapped__(realm_session, tmp_path)  # type: ignore[attr-defined]
    realm, connection = realm_session
    run = scope["run"]
    repository = MemoryContinuityRepository(connection, realm.id)
    service = MemoryContinuityService(
        repository,
        AuthorizationRepository(connection, realm.id),
    )
    now = dt.datetime.now(dt.UTC)
    client_instance_id = "codex-hydration-gate"
    canonical_event = {
        "session_id": run.session_id,
        "client_id": client_instance_id,
    }

    bindings = SourceBindingRepository(connection, realm.id)
    binding = bindings.for_project(run.project_id)[0]
    source_revision = bindings.record_revision(
        binding_id=binding.id,
        kind=SourceRevisionKind.TREE_DIGEST,
        revision=run.source_revision,
        tree_digest=digest("lifecycle-hydration-source-tree"),
        now=now,
    )
    lifecycle_event = SessionLifecycleEvent(
        realm_id=realm.id,
        project_id=run.project_id,
        work_item_id=run.work_item_id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
        event_id=uuid4(),
        event_type="hydration_required",
        sequence=1,
        previous_digest=None,
        origin="client/opencode",
        causation_id="test/hydration-gate",
        correlation_id="test/hydration-gate",
        recursion_depth=0,
        source_revision=run.source_revision,
        plan_ref=f"work-plan:{run.plan_id}",
        checkpoint_ref=f"run:{run.id}:genesis",
        context_ref="context/current",
        payload_digest=digest("lifecycle-hydration-event-payload"),
        metadata=(),
        classification=DataClassification.INTERNAL,
        occurred_at=now,
        ingested_at=now,
    )
    repository.stage_lifecycle_delivery(
        lifecycle_event,
        idempotency_key=f"test:hydration-event:{run.id}",
        plan_digest=digest("lifecycle-hydration-event-plan"),
    )
    _store_canonical_active_work_projection(
        realm=realm,
        connection=connection,
        repository=repository,
        run=run,
        source_digest=source_revision.tree_digest,
        generated_at=now,
    )

    def admission(event_type: str) -> PostgresLifecycleContinuityAdmission:
        event = SimpleNamespace(
            event_type=event_type,
            project_id=run.project_id,
            work_item_id=run.work_item_id,
            run_id=run.id,
            session_id=run.session_id,
            client_id=run.client_id,
        )
        delivery = SimpleNamespace(
            plan=SimpleNamespace(event=event),
            client_instance_id=client_instance_id,
        )
        return PostgresLifecycleContinuityAdmission(
            connection=connection,
            realm_id=realm.id,
            bridge=cast(Any, SimpleNamespace()),
            memory_continuity=service,
            repository=cast(Any, SimpleNamespace()),
            delivery=cast(Any, delivery),
        )

    with pytest.raises(PolicyViolation, match="hydration-missing"):
        admission("pre_compaction")._assert_common_mutating_admission(canonical_event)

    # The same missing state is legal only for the bounded session-start
    # bootstrap, which produces hydration atomically in the production path.
    admission("session_start")._assert_common_mutating_admission(canonical_event)

    inventory = repository.read_hydration_inventory(
        project_id=run.project_id,
        work_item_id=run.work_item_id,
        run_id=run.id,
        session_id=run.session_id,
        client_id=run.client_id,
    )
    hydration_plan = service.prepare_from_inventory(
        HydrationPreparation(
            receipt_id=uuid4(),
            realm_id=realm.id,
            project_id=run.project_id,
            work_item_id=run.work_item_id,
            run_id=run.id,
            session_id=run.session_id,
            client_id=run.client_id,
            token_budget=5,
            idempotency_key=f"test:hydrate:{run.id}",
            created_at=now,
        ),
        inventory,
    )
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(
            realm=realm,
            kind=ActorKind.HUMAN,
            slug="lifecycle-hydration-authorizer",
            now=now,
        )
    )
    authorization = AuthorizationRepository(connection, realm.id).issue(
        Authorization.issue(
            realm_id=realm.id,
            actor_id=actor.id,
            work_item_id=run.work_item_id,
            plan_id=run.plan_id,
            plan_digest=hydration_plan.plan_digest,
            effect_digest=hydration_plan.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(hydration_plan.resource,),
                allowed_effects=("database-write",),
            ),
            risk="high",
            lifetime=dt.timedelta(minutes=5),
            now=now,
        )
    )
    applied = service.apply(
        hydration_plan,
        authorization_id=authorization.id,
        now=now,
    )
    assert applied.created
    admission("pre_compaction")._assert_common_mutating_admission(canonical_event)


def test_codex_guard_and_recovery_schema_are_installed(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from pg_trigger trigger_ join pg_class table_"
            " on table_.oid=trigger_.tgrelid join pg_namespace namespace_"
            " on namespace_.oid=table_.relnamespace"
            " where namespace_.nspname='client' and table_.relname='lifecycle_event'"
            " and trigger_.tgname='codex_lifecycle_admission_guard'"
            " and trigger_.tgconstraint<>0 and not trigger_.tgisinternal"
        )
        assert int(cursor.fetchone()[0]) == 1
        cursor.execute(
            "select count(*) from pg_trigger trigger_ join pg_class table_"
            " on table_.oid=trigger_.tgrelid join pg_namespace namespace_"
            " on namespace_.oid=table_.relnamespace join pg_constraint constraint_"
            " on constraint_.oid=trigger_.tgconstraint"
            " where namespace_.nspname='client'"
            " and table_.relname='codex_lifecycle_admission'"
            " and trigger_.tgname='codex_lifecycle_admission_row_guard'"
            " and constraint_.condeferrable and constraint_.condeferred"
            " and not trigger_.tgisinternal"
        )
        assert int(cursor.fetchone()[0]) == 1
        cursor.execute(
            "select count(*) from information_schema.columns"
            " where table_schema='client' and table_name='codex_lifecycle_admission'"
            " and column_name in ('entry_digest','work_plan_digest','effect_plan_digest',"
            " 'effect_plan_body',"
            " 'source_digest','policy_digest','migration_digest','envelope_digest',"
            " 'binding_digest','result_formula_digest')"
        )
        assert int(cursor.fetchone()[0]) == 10
        cursor.execute(
            "select count(*) from pg_constraint constraint_ join pg_class table_"
            " on table_.oid=constraint_.conrelid join pg_namespace namespace_"
            " on namespace_.oid=table_.relnamespace"
            " where namespace_.nspname='client'"
            " and table_.relname='codex_lifecycle_admission'"
            " and constraint_.contype='u'"
            " and pg_get_constraintdef(constraint_.oid)"
            " like 'UNIQUE (realm_id, entry_digest)%'"
        )
        assert int(cursor.fetchone()[0]) == 1
        cursor.execute(
            "select not exists(select 1 from pg_proc function_"
            " cross join lateral aclexplode(function_.proacl) acl"
            " where function_.oid='client.enforce_codex_lifecycle_admission()'::regprocedure"
            " and acl.grantee=0 and acl.privilege_type='EXECUTE')"
        )
        assert cursor.fetchone()[0] is True
        cursor.execute(
            "select function_.prosecdef,function_.proconfig"
            " from pg_proc function_"
            " where function_.oid="
            " 'client.enforce_codex_lifecycle_admission()'::regprocedure"
        )
        assert cursor.fetchone() == (True, ["search_path=pg_catalog"])
        cursor.execute(
            "select function_.prosecdef,function_.proconfig,"
            " has_function_privilege('zekam_app',function_.oid,'EXECUTE'),"
            " not exists(select 1 from aclexplode(function_.proacl) acl"
            " where acl.grantee=0 and acl.privilege_type='EXECUTE')"
            " from pg_proc function_ where function_.oid="
            " 'client.lock_codex_lifecycle_scope(uuid,uuid,uuid,uuid)'::regprocedure"
        )
        assert cursor.fetchone() == (True, ["search_path=pg_catalog"], True, True)
        cursor.execute(
            "select table_.relrowsecurity,table_.relforcerowsecurity,"
            " (select count(*) from pg_policy policy where policy.polrelid=table_.oid),"
            " has_table_privilege('zekam_app',table_.oid,'SELECT'),"
            " has_table_privilege('zekam_app',table_.oid,'INSERT'),"
            " has_table_privilege('zekam_app',table_.oid,'UPDATE'),"
            " has_table_privilege('zekam_app',table_.oid,'DELETE')"
            " from pg_class table_ join pg_namespace namespace_"
            " on namespace_.oid=table_.relnamespace"
            " where namespace_.nspname='client'"
            " and table_.relname='codex_lifecycle_admission'"
        )
        assert cursor.fetchone() == (True, True, 2, True, True, False, False)
        cursor.execute("select core.current_realm_id()")
        assert cursor.fetchone()[0] == realm.id


def test_generic_codex_ingest_bypass_rolls_back_at_deferred_guard(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    stream_id, event_id = uuid4(), uuid4()
    body = {
        "schema": "zekam-client-lifecycle-event/v1",
        "client_id": "codex-test-instance",
        "client_kind": "codex",
        "session_id": str(uuid4()),
        "sequence": 1,
        "previous_digest": None,
        "event_type": "session.created",
        "payload_digest": digest("content-free"),
        "occurred_at": NOW.isoformat(),
        "transcript_included": False,
        "grants_authority": False,
    }
    event_digest = digest(body)
    with pytest.raises(Exception, match="governed admission"):  # noqa: SIM117
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "insert into client.lifecycle_stream"
                " (id,realm_id,client_kind,client_instance_id,session_id,head_sequence,"
                " head_digest,created_at,updated_at) values(%s,%s,'codex',%s,%s,0,null,%s,%s)",
                (stream_id, realm.id, "codex-test-instance", body["session_id"], NOW, NOW),
            )
            cursor.execute(
                "insert into client.lifecycle_event"
                " (id,realm_id,stream_id,sequence,previous_digest,event_digest,payload,"
                " occurred_at,ingested_at,grants_authority)"
                " values(%s,%s,%s,1,null,%s,%s::jsonb,%s,%s,false)",
                (event_id, realm.id, stream_id, event_digest, canonical_json(body), NOW, NOW),
            )
            cursor.execute("set constraints client.codex_lifecycle_admission_guard immediate")
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from client.lifecycle_event where realm_id=%s and id=%s",
            (realm.id, event_id),
        )
        assert int(cursor.fetchone()[0]) == 0


def test_opencode_generic_ingest_regression_remains_admitted(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    event = ClientLifecycleEvent(
        client_id="opencode-regression",
        client_kind=ClientKind.OPENCODE,
        session_id="opencode-regression-session",
        sequence=1,
        previous_digest=None,
        event_type="session.created",
        payload_digest=digest("content-free-opencode-regression"),
        occurred_at=NOW,
    )
    acknowledgement = ClientLifecycleRepository(connection, realm.id).ingest(
        event.as_dict(),
        client_instance_id="opencode-regression",
        client_kind=ClientKind.OPENCODE,
        now=NOW,
    )
    assert acknowledgement.local_event_digest == event.event_digest


def test_admission_is_immutable_after_commit_boundary(
    realm_session: tuple[Any, Any],
) -> None:
    _, connection = realm_session
    with connection.cursor() as cursor:
        cursor.execute(
            "select action_timing,event_manipulation from information_schema.triggers"
            " where event_object_schema='client'"
            " and event_object_table='codex_lifecycle_admission'"
            " and trigger_name='codex_lifecycle_admission_no_mutation'"
            " order by event_manipulation"
        )
        assert cursor.fetchall() == [("BEFORE", "DELETE"), ("BEFORE", "UPDATE")]


def test_production_handler_commits_once_and_new_process_recovers_local_ack(
    realm_session: tuple[Any, Any],
    migrated_database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-commit local crash is lookup+ACK only, never an effect retry."""

    scope = _residency_scope.__wrapped__(realm_session, tmp_path)  # type: ignore[attr-defined]
    realm, connection = realm_session
    with connection.cursor() as cursor:
        cursor.execute(
            "select relation_name,"
            " has_table_privilege('zekam_app',relation_name,'SELECT'),"
            " has_table_privilege('zekam_app',relation_name,'UPDATE')"
            " from unnest(array['runtime.execution_envelope','runtime.effect_claim',"
            " 'work.task_plan']) relation_name order by relation_name"
        )
        assert cursor.fetchall() == [
            ("runtime.effect_claim", True, False),
            ("runtime.execution_envelope", True, False),
            ("work.task_plan", True, False),
        ]
    old_run = scope["run"]
    now = dt.datetime.now(dt.UTC)
    source_bindings = SourceBindingRepository(connection, realm.id)
    source_binding = source_bindings.for_project(old_run.project_id)[0]
    source_snapshot = source_bindings.record_revision(
        binding_id=source_binding.id,
        kind=SourceRevisionKind.TREE_DIGEST,
        revision=old_run.source_revision,
        tree_digest=digest("codex-production-source-tree"),
        now=now,
    )
    capability_profile = CapabilityProfile(
        generator_version=PROFILER_VERSION,
        languages=(),
        build_systems=(),
        frameworks=(),
        test_frameworks=(),
        databases=(),
        quality_tools=(),
        security_tools=(),
        continuous_integration=(),
        containers=(),
        modules=(),
        file_count=0,
        total_bytes=0,
    )
    CapabilityProfileRepository(connection, realm.id).store(
        project_id=old_run.project_id,
        source_revision_id=source_snapshot.id,
        profile=capability_profile,
        now=now,
    )
    session_id = str(uuid4())
    installation = PostgresMemoryHookInstaller(connection, realm.id).ensure(installed_at=now)
    turn_hook_store = HookRuntimeRepository(connection, realm.id)
    turn_hook_binding_id = turn_hook_store.start_session(session_ref=session_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "select config_effective_digest from hooks.session_binding where realm_id=%s and id=%s",
            (realm.id, turn_hook_binding_id),
        )
        turn_hook_config_digest = str(cursor.fetchone()[0])
    spool_home = tmp_path / "codex-production-home"
    spool = ClientLifecycleSpool(spool_home, client_id="codex")
    hook = parse_codex_hook_input(
        json.dumps(
            {
                "session_id": session_id,
                "hook_event_name": "SessionStart",
                "source": "startup",
                "permission_mode": "default",
            }
        )
    )
    entry = spool.stage(
        hook.observation_body(client_version=CODEX_REVIEWED_VERSION),
        delivery_id=hook.delivery_id(
            occurrence_id=str(uuid4()), client_version=CODEX_REVIEWED_VERSION
        ),
        occurred_at=now,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "select envelope.context_manifest_id,envelope.context_manifest_digest,"
            " envelope.context_packet_id,envelope.context_packet_digest,"
            " turn.exposed_tool_set_digest,environment.config_effective_digest,"
            " environment.snapshot_digest"
            " from runtime.execution_envelope envelope"
            " join runtime.turn_execution_snapshot turn"
            " on turn.realm_id=envelope.realm_id and turn.id=envelope.turn_execution_snapshot_id"
            " join runtime.execution_environment_snapshot environment"
            " on environment.realm_id=envelope.realm_id"
            " and environment.snapshot_digest=turn.execution_environment_snapshot_digest"
            " where envelope.realm_id=%s and envelope.run_id=%s"
            " order by envelope.request_ordinal desc limit 1",
            (realm.id, old_run.id),
        )
        context = cursor.fetchone()
        assert context is not None
        cursor.execute(
            "select revision,tree_digest from projects.source_revision revision"
            " join projects.source_binding binding on binding.realm_id=revision.realm_id"
            " and binding.id=revision.binding_id"
            " where binding.realm_id=%s and binding.project_id=%s"
            " order by revision.observed_at desc,revision.id desc limit 1",
            (realm.id, old_run.project_id),
        )
        source_revision, source_digest = cursor.fetchone()
        cursor.execute(
            "select version,models.capability_runtime_jsonb_digest(to_jsonb(checksum))"
            " from core.schema_migrations order by version desc limit 1"
        )
        migration_head, migration_digest = cursor.fetchone()
        migration_head = int(migration_head)
        migration_digest = str(migration_digest)

    resource = f"memory:{old_run.project_id}:session:{session_id}"
    graph = WorkGraphService(connection, realm)
    graph.set_intent(
        old_run.work_item_id,
        goal="Codex lifecycle deliverysini canonical checkpoint v2 ile tamamla",
    )
    plan = graph.create_plan(
        old_run.work_item_id,
        source_revision=str(source_revision),
        policy_digest=scope["policy_digest"],
        steps=(
            PlanStep(
                "build",
                "Governed Codex lifecycle",
                EffectKind.DATABASE_WRITE,
                logical_resources=(resource,),
                risk="high",
            ),
        ),
    )
    coordinator_assignment_id = uuid4()
    assignment_id = uuid4()
    verifier_assignment_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into agents.assignment"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,role,"
            " agent_ref,status,risk,instruction_digest,context_manifest_digest,"
            " assignment_digest,created_at) values"
            " (%s,%s,%s,%s,%s,'build',null,'coordinator',%s,'active','high',%s,%s,%s,%s)",
            (
                coordinator_assignment_id,
                realm.id,
                old_run.project_id,
                old_run.work_item_id,
                plan.id,
                "agent:codex-lifecycle-coordinator",
                digest("codex-lifecycle-coordinator-instruction"),
                str(context[1]),
                digest("codex-lifecycle-coordinator-assignment"),
                now,
            ),
        )
        cursor.execute(
            "insert into agents.assignment"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,role,"
            " agent_ref,status,risk,instruction_digest,context_manifest_digest,"
            " assignment_digest,created_at) values"
            " (%s,%s,%s,%s,%s,'build',%s,'builder',%s,'active','high',%s,%s,%s,%s)",
            (
                assignment_id,
                realm.id,
                old_run.project_id,
                old_run.work_item_id,
                plan.id,
                coordinator_assignment_id,
                "agent:codex-lifecycle",
                digest("codex-lifecycle-instruction"),
                str(context[1]),
                digest("codex-lifecycle-assignment"),
                now,
            ),
        )
        cursor.execute(
            "insert into agents.assignment"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,role,"
            " agent_ref,status,risk,instruction_digest,context_manifest_digest,"
            " assignment_digest,created_at) values"
            " (%s,%s,%s,%s,%s,'build',%s,'verifier',%s,'active','low',%s,%s,%s,%s)",
            (
                verifier_assignment_id,
                realm.id,
                old_run.project_id,
                old_run.work_item_id,
                plan.id,
                coordinator_assignment_id,
                "agent:codex-lifecycle-verifier",
                digest("codex-lifecycle-verifier-instruction"),
                str(context[1]),
                digest("codex-lifecycle-verifier-assignment"),
                now,
            ),
        )
        cursor.execute(
            "insert into agents.assignment_resource"
            " (realm_id,assignment_id,resource,mode) values(%s,%s,%s,'write')",
            (realm.id, assignment_id, resource),
        )
        cursor.execute(
            "insert into agents.assignment_resource"
            " (realm_id,assignment_id,resource,mode) values(%s,%s,%s,'read')",
            (realm.id, verifier_assignment_id, resource),
        )

    execution = ExecutionRunRepository(connection, realm.id)
    base_environment = scope["environment"]
    lifecycle_environment = ExecutionEnvironmentSnapshot.create(
        id=uuid4(),
        realm_id=realm.id,
        environment_id=f"{base_environment.environment_id}-codex-lifecycle",
        execution_identity=base_environment.execution_identity,
        provider=base_environment.provider,
        platform=base_environment.platform,
        executor_protocol_version=base_environment.executor_protocol_version,
        cwd_locator=base_environment.cwd_locator,
        workspace_roots=base_environment.workspace_roots,
        shell=base_environment.shell,
        permission_profile_id=base_environment.permission_profile_id,
        permission_profile_digest=base_environment.permission_profile_digest,
        filesystem_policy_digest=base_environment.filesystem_policy_digest,
        network_policy_digest=base_environment.network_policy_digest,
        tool_runtime_digest=base_environment.tool_runtime_digest,
        capability_digest=base_environment.capability_digest,
        config_effective_digest=turn_hook_config_digest,
        source_revision=str(source_revision),
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    execution.create_environment_snapshot(lifecycle_environment)
    probed_environment = reprobe_snapshot(
        lifecycle_environment,
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    execution.create_environment_snapshot(probed_environment)
    execution.record_environment_probe(
        detect_environment_drift(
            lifecycle_environment,
            probed_environment,
            checked_at=now,
        )
    )
    run = ExecutionRun.create(
        realm_id=realm.id,
        project_id=old_run.project_id,
        work_item_id=old_run.work_item_id,
        plan_id=plan.id,
        client_id="codex",
        session_id=session_id,
        source_revision=str(source_revision),
        policy_digest=scope["policy_digest"],
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        deadline=now + dt.timedelta(minutes=5),
        created_at=now,
    )
    execution.create_run(run)
    execution.activate_run(run.id, started_at=now)
    with connection.cursor() as cursor:
        cursor.execute(
            "select revision,state,record_digest from work.work_item"
            " where realm_id=%s and project_id=%s and id=%s",
            (realm.id, run.project_id, run.work_item_id),
        )
        work_revision, work_state, work_record_digest = cursor.fetchone()
    database_revision_digest = digest(
        {
            "project_id": str(run.project_id),
            "work_item_id": str(run.work_item_id),
            "work_revision": int(work_revision),
            "work_state": str(work_state),
            "work_record_digest": str(work_record_digest),
        }
    )
    projection_source_digest = canonical_projection_source_digest(
        source_head=str(source_revision),
        source_tree_digest=str(source_digest),
        migration_head=migration_head,
        database_revision_digest=database_revision_digest,
    )
    projection_body = {
        "schema": "zekam-memory-continuity-public-projection/v1",
        "project_id": str(run.project_id),
        "work_item_id": str(run.work_item_id),
        "work_revision": int(work_revision),
        "work_state": str(work_state),
        "source_head": str(source_revision),
        "source_tree_digest": str(source_digest),
        "migration_head": migration_head,
        "database_revision_digest": database_revision_digest,
        "source_digest": projection_source_digest,
        "classification": "public",
        "public_filtered": True,
        "content_included": False,
        "fresh": True,
        "read_only": True,
        "grants_authority": False,
    }
    projection_receipt = ProjectionGenerationReceipt(
        receipt_id=uuid4(),
        realm_id=realm.id,
        project_id=run.project_id,
        work_item_id=run.work_item_id,
        source_ref=f"work-item/{run.work_item_id}/revision/{work_revision}",
        source_digest=projection_source_digest,
        projection_ref="projection/active-work",
        projection_digest=digest(projection_body),
        generator_version="memory-continuity-shadow/v1",
        generated_at=now,
    )
    continuity_repository = MemoryContinuityRepository(connection, realm.id)
    assert continuity_repository.store_projection_receipt(
        projection_receipt,
        idempotency_key=f"session-start:{entry.delivery_id}:active-work-projection",
    )
    context_continuity = ContextContinuityRepository(
        connection,
        realm.id,
        run.project_id,
        run.work_item_id,
    )
    journal_entry = JournalEntry(
        1,
        str(run.work_item_id),
        "step-completed",
        digest("codex-lifecycle-ready-journal"),
        None,
        False,
        now,
    )
    context_continuity.append_journal(journal_entry, expected_head=None)
    target = ExecutionTargetSnapshot(
        client_id="codex",
        slot="lifecycle",
        execution_mode="native-sequential",
        model_selectable=True,
        structured_result=False,
        cancellation=False,
        max_concurrency=1,
        cost_evidence_digest=digest("codex-lifecycle-cost"),
        capability_digest=digest("codex-lifecycle-capability"),
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    routing = ModelRoutingRepository(connection, realm.id)
    routing.store_project_context(
        ProjectRoutingContext(
            project_id=run.project_id,
            source_revision_id=source_snapshot.id,
            source_revision=str(source_revision),
            tree_digest=str(source_digest),
            capability_profile_digest=capability_profile.digest,
            dependency_digest=digest("codex-lifecycle-dependencies"),
            framework_digest=digest("codex-lifecycle-frameworks"),
            technology_digest=digest("codex-lifecycle-technology"),
            architecture_digest=digest("codex-lifecycle-architecture"),
            rules_digest=digest("codex-lifecycle-rules"),
            suite_digest=digest("codex-lifecycle-suite"),
            inventory_digest=digest("codex-lifecycle-inventory"),
            policy_digest=scope["policy_digest"],
            captured_at=now,
            expires_at=now + dt.timedelta(minutes=10),
        )
    )
    target_id, _ = routing.store_execution_target(target)
    route_id, route_digest = uuid4(), digest("codex-lifecycle-route")
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into models.model_route_decision"
            " (id,realm_id,role_policy_id,execution_target_id,role,target_layer,"
            " inventory_digest,routing_policy_digest,policy_digest,execution_target_digest,"
            " status,primary_model_id,evidence_digest,decided_at)"
            " values(%s,%s,%s,%s,'implementer','general',%s,%s,%s,%s,'selected',%s,%s,%s)",
            (
                route_id,
                realm.id,
                scope["role_policy_id"],
                target_id,
                digest("codex-lifecycle-inventory"),
                scope["policy_digest"],
                scope["policy_digest"],
                target.snapshot_digest,
                scope["model"].model_id,
                route_digest,
                now,
            ),
        )
    provider = ProviderBindingSnapshot.create(
        realm_id=realm.id,
        model_id=scope["model"].model_id,
        provider_ref=f"model:{scope['model'].model_id}:codex-lifecycle",
        endpoint_ref=scope["model"].endpoint_ref,
        operation="invoke",
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=10),
    )
    execution.create_provider_binding(provider)

    base_job = Job.create(
        realm_id=realm.id,
        project_id=run.project_id,
        kind=JobKind.MUTATION,
        idempotency_key=f"codex-lifecycle:{entry.delivery_id}",
        resources=parse_requests(write=(resource,)),
        required_capabilities=("client.lifecycle.codex-drain",),
        max_attempts=1,
        work_item_id=run.work_item_id,
        plan_id=run.plan_id,
        step_id="build",
        assignment_id=assignment_id,
        run_id=run.id,
        payload={"schema": "pending-authorization"},
        now=now,
    )
    bundle = memory_hook_bundle(realm.id)
    preview_runtime = HookRuntime(max_workers=1)
    _configure_active_memory_hook_runtime(
        preview_runtime,
        bundle,
        ClientLifecycleRepository(connection, realm.id),
        now=now,
    )
    preview_session = preview_runtime.start_session()
    bridge = ClientLifecycleBridge(
        preview_runtime,
        MemoryContinuityRepository(connection, realm.id),
        AuthorizationRepository(connection, realm.id),
        HookRuntimeRepository(connection, realm.id),
    )
    evidence = load_codex_contract_evidence(
        Path(__file__).resolve().parents[2] / "config" / "client-lifecycle" / "codex-0.150.1.json"
    )
    contract = LifecycleClientContract.verified(
        descriptor=codex_lifecycle_descriptor("codex", installed_version=CODEX_REVIEWED_VERSION),
        installed_version=CODEX_REVIEWED_VERSION,
        event_mapping=CODEX_EVENT_MAPPING,
        contract_evidence_digest=str(evidence["file_digest"]),
    )
    request = LifecycleRequest(
        realm_id=realm.id,
        project_id=run.project_id,
        work_item_id=run.work_item_id,
        run_id=run.id,
        session_id=session_id,
        client_id="codex",
        event_id=uuid5(_EVENT_NAMESPACE, entry.entry_digest),
        external_event_type=entry.external_event_type,
        sequence=1,
        previous_digest=None,
        origin="client:codex",
        causation_id=f"delivery:{entry.delivery_id}",
        correlation_id=f"job:{base_job.id}",
        recursion_depth=0,
        max_recursion_depth=3,
        source_revision=str(source_revision),
        work_plan_ref=f"work-plan:{run.plan_id}",
        checkpoint_ref=None,
        context_ref=f"context-packet:{context[2]}",
        metadata=(),
        classification=DataClassification.INTERNAL,
        payload=entry.observation,
        idempotency_key=entry.delivery_id,
        occurred_at=entry.occurred_at,
        ingested_at=entry.occurred_at,
    )
    effect_plan = bridge.prepare(
        request,
        contract,
        preview_session,
        source_digest=str(source_digest),
        policy_digest=scope["policy_digest"],
        migration_digest=migration_digest,
    )
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="codex-lifecycle-authorizer", now=now)
    )
    authorization = Authorization.issue(
        realm_id=realm.id,
        actor_id=actor.id,
        work_item_id=run.work_item_id,
        plan_id=run.plan_id,
        plan_digest=effect_plan.plan_digest,
        effect_digest=effect_plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(resource,),
            allowed_effects=("database-write",),
            data_classifications=(AuthorizationClassification.INTERNAL,),
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=now,
    )
    AuthorizationRepository(connection, realm.id).issue(authorization)
    hydration_inventory = HydrationInventorySnapshot(
        realm_id=realm.id,
        project_id=run.project_id,
        work_item_id=run.work_item_id,
        run_id=run.id,
        session_id=session_id,
        client_id="codex",
        plan_ref=f"work-plan:{run.plan_id}",
        checkpoint_ref=f"run:{run.id}:genesis",
        source_digest=str(source_digest),
        policy_digest=scope["policy_digest"],
        migration_digest=migration_digest,
        context_digest=str(context[1]),
        entries=(
            HydrationInventoryEntry(
                ref="context/source",
                content_digest=digest("source"),
                token_count=5,
                truth_class=TruthClass.EXTERNAL_VERIFIED_FACT,
                classification=DataClassification.INTERNAL,
                required=True,
                source_ref="work/current",
                source_revision="revision-1",
            ),
        ),
        known_omissions=(),
        projection_refs=(
            DigestReference(
                "projection/active-work",
                projection_receipt.projection_digest,
                TruthClass.REPO_FACT,
            ),
        ),
        hydration_event_digest=effect_plan.event.event_digest,
    )
    continuity_service = MemoryContinuityService(
        continuity_repository,
        AuthorizationRepository(connection, realm.id),
    )
    hydration_plan = continuity_service.prepare_from_inventory(
        HydrationPreparation(
            receipt_id=uuid5(
                _HYDRATION_NAMESPACE,
                f"{realm.id}:{effect_plan.event.event_digest}",
            ),
            realm_id=realm.id,
            project_id=run.project_id,
            work_item_id=run.work_item_id,
            run_id=run.id,
            session_id=session_id,
            client_id="codex",
            token_budget=_SESSION_START_HYDRATION_TOKEN_BUDGET,
            idempotency_key=f"session-start:{effect_plan.event.event_id}:hydration",
            created_at=entry.occurred_at,
        ),
        hydration_inventory,
    )
    hydration_authorization = Authorization.issue(
        realm_id=realm.id,
        actor_id=actor.id,
        work_item_id=run.work_item_id,
        plan_id=run.plan_id,
        plan_digest=hydration_plan.plan_digest,
        effect_digest=hydration_plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(hydration_plan.resource,),
            allowed_effects=("database-write",),
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=now,
    )
    AuthorizationRepository(connection, realm.id).issue(hydration_authorization)
    job = replace(
        base_job,
        payload={
            "schema": "zekam-codex-lifecycle-job/v1",
            "authorization_id": str(authorization.id),
            "hydration_authorization_id": str(hydration_authorization.id),
        },
    )
    host = ExecutionHost(connection, realm.id, worker_label="codex-lifecycle-worker")
    host.jobs.enqueue(job)
    work = host.acquire_work(capabilities=("client.lifecycle.codex-drain",), now=now)
    assert work is not None and work.job.id == job.id
    execution.bind_assignment_environment(
        AssignmentEnvironmentBinding.create(
            realm_id=realm.id,
            assignment_id=assignment_id,
            execution_environment_snapshot_digest=lifecycle_environment.snapshot_digest,
            bound_at=now,
        )
    )
    turn = TurnExecutionSnapshot.create(
        realm_id=realm.id,
        assignment_id=assignment_id,
        run_id=run.id,
        attempt_id=work.attempt_id,
        client_session_id=session_id,
        turn_id="codex-lifecycle-turn",
        model_id=scope["model"].model_id,
        provider_id=provider.provider_ref,
        route_decision_digest=route_digest,
        reasoning_profile_digest=digest("codex-lifecycle-reasoning"),
        execution_environment_snapshot_digest=lifecycle_environment.snapshot_digest,
        context_manifest_digest=str(context[1]),
        exposed_tool_set_digest=str(context[4]),
        hook_set_digest=installation.hook_set_digest,
        config_effective_digest=turn_hook_config_digest,
        created_at=now,
    )
    execution.create_turn_snapshot(turn)
    envelope = ExecutionEnvelope.create(
        realm_id=realm.id,
        run_id=run.id,
        job_id=job.id,
        attempt_id=work.attempt_id,
        lease_id=work.lease.id,
        fencing_token=work.lease.fencing_token,
        request_ordinal=1,
        idempotency_key=f"codex-lifecycle-envelope:{entry.delivery_id}",
        assignment_id=assignment_id,
        role="builder",
        route_decision_id=route_id,
        route_decision_digest=route_digest,
        route_expires_at=target.expires_at,
        model_id=scope["model"].model_id,
        provider_binding_id=provider.id,
        provider_binding_digest=provider.binding_digest,
        provider_ref=provider.provider_ref,
        context_manifest_id=UUID(str(context[0])),
        context_manifest_digest=str(context[1]),
        context_packet_id=UUID(str(context[2])),
        context_packet_digest=str(context[3]),
        turn_execution_snapshot_id=turn.id,
        turn_execution_snapshot_digest=turn.turn_snapshot_digest,
        checkpoint_id=None,
        checkpoint_digest=None,
        checkpoint_disposition=CheckpointDisposition.NOT_APPLICABLE_GENESIS,
        source_revision=str(source_revision),
        policy_digest=scope["policy_digest"],
        authorization_scope_digest=digest(authorization.scope.body()),
        output_schema_digest=digest("codex-lifecycle-output"),
        payload_digest=entry.observation_digest,
        max_input_tokens=100,
        max_output_tokens=50,
        max_cost_micros=1_000,
        deadline=run.deadline,
        created_at=now,
    )
    execution.create_envelope(envelope)

    original_ack = ClientLifecycleSpool._acknowledge_verified_receipt

    def crash_after_commit(*args: Any, **kwargs: Any) -> Any:
        raise OSError("simulated local ACK crash")

    monkeypatch.setattr(ClientLifecycleSpool, "_acknowledge_verified_receipt", crash_after_commit)
    with pytest.raises(OSError, match="simulated local ACK crash"):
        compose_codex_lifecycle_handler(connection=connection, realm_id=realm.id, home=spool_home)(
            work
        )
    assert ClientLifecycleSpool(spool_home, client_id="codex").pending(limit=1) == (entry,)
    monkeypatch.setattr(ClientLifecycleSpool, "_acknowledge_verified_receipt", original_ack)
    with connection.cursor() as cursor:
        cursor.execute(
            "select (select count(*) from runtime.effect_claim where realm_id=%s and job_id=%s),"
            " (select count(*) from runtime.effect_receipt receipt join runtime.effect_claim claim"
            " on claim.realm_id=receipt.realm_id and claim.id=receipt.claim_id"
            " where claim.realm_id=%s and claim.job_id=%s),"
            " (select count(*) from client.codex_lifecycle_admission"
            " where realm_id=%s and job_id=%s),"
            " (select count(*) from continuity.session_hydration_receipt"
            " where realm_id=%s and run_id=%s and session_id=%s),"
            " (select count(*) from continuity.lifecycle_hydration_admission admission"
            " join client.codex_lifecycle_admission codex"
            " on codex.realm_id=admission.realm_id"
            " and codex.id=admission.codex_admission_id"
            " where admission.realm_id=%s and codex.job_id=%s)",
            (
                realm.id,
                job.id,
                realm.id,
                job.id,
                realm.id,
                job.id,
                realm.id,
                run.id,
                session_id,
                realm.id,
                job.id,
            ),
        )
        before = cursor.fetchone()
    assert before == (1, 1, 1, 1, 1)
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("reset role")
        cursor.execute(
            "update core.actor set status='suspended' where realm_id=%s and id=%s",
            (realm.id, actor.id),
        )
    configure_session(connection, realm_id=realm.id)
    with realm_database_session(migrated_database, realm_id=realm.id) as recovery_connection:
        recovered = recover_committed_codex_delivery(
            spool=ClientLifecycleSpool(spool_home, client_id="codex"),
            repository=ClientLifecycleRepository(recovery_connection, realm.id),
        )
        assert recovered is not None and recovered.outcome == "completed"
        assert ClientLifecycleSpool(spool_home, client_id="codex").pending(limit=1) == ()
        assert (
            recover_committed_codex_delivery(
                spool=ClientLifecycleSpool(spool_home, client_id="codex"),
                repository=ClientLifecycleRepository(recovery_connection, realm.id),
            )
            is None
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "select (select count(*) from runtime.effect_claim where realm_id=%s and job_id=%s),"
            " (select count(*) from runtime.effect_receipt receipt join runtime.effect_claim claim"
            " on claim.realm_id=receipt.realm_id and claim.id=receipt.claim_id"
            " where claim.realm_id=%s and claim.job_id=%s),"
            " (select count(*) from client.codex_lifecycle_admission"
            " where realm_id=%s and job_id=%s),"
            " (select count(*) from continuity.lifecycle_hydration_admission admission"
            " join client.codex_lifecycle_admission codex"
            " on codex.realm_id=admission.realm_id"
            " and codex.id=admission.codex_admission_id"
            " where admission.realm_id=%s and codex.job_id=%s),"
            " (select count(*) from runtime.lease where realm_id=%s and job_id=%s),"
            " (select count(*) from runtime.resource_lock where realm_id=%s and job_id=%s)",
            (
                realm.id,
                job.id,
                realm.id,
                job.id,
                realm.id,
                job.id,
                realm.id,
                job.id,
                realm.id,
                job.id,
                realm.id,
                job.id,
            ),
        )
        after = cursor.fetchone()
    assert after == (1, 1, 1, 1, 0, 0)
    with connection.cursor() as cursor:
        cursor.execute(
            "select plan_steps,completed_steps,pending_steps,step_results"
            " from work.checkpoint where realm_id=%s and job_id=%s",
            (realm.id, job.id),
        )
        checkpoint_partition = cursor.fetchone()
    assert checkpoint_partition[0:3] == (
        ["build"],
        ["build"],
        [],
    )
    assert set(checkpoint_partition[3]) == {"build"}
    assert str(checkpoint_partition[3]["build"]).startswith("sha256:")
    with connection.cursor() as cursor:
        cursor.execute(
            "select checkpoint.checkpoint_key,checkpoint.revision,checkpoint.plan_steps,"
            " checkpoint.completed_steps,checkpoint.pending_steps,"
            " work.validate_checkpoint_v2(checkpoint.realm_id,checkpoint.id),"
            " result.result_digest,result.assignment_id,step_receipt.receipt_id,"
            " verification.verifier_assignment_id,verification.verifier_invocation_id,"
            " verification.envelope_digest,verifier.agent_ref,builder.agent_ref,"
            " invocation.execution_identity,result_receipt.envelope_digest"
            " from work.checkpoint_v2 checkpoint"
            " join work.checkpoint_v2_step_result result"
            " on result.realm_id=checkpoint.realm_id and result.checkpoint_id=checkpoint.id"
            " and result.step_id='build'"
            " join work.checkpoint_v2_step_receipt step_receipt"
            " on step_receipt.realm_id=result.realm_id"
            " and step_receipt.checkpoint_id=result.checkpoint_id"
            " and step_receipt.step_id=result.step_id"
            " join work.checkpoint_v2_step_verification verification"
            " on verification.realm_id=result.realm_id"
            " and verification.checkpoint_id=result.checkpoint_id"
            " and verification.step_id=result.step_id"
            " join agents.assignment verifier on verifier.realm_id=verification.realm_id"
            " and verifier.id=verification.verifier_assignment_id"
            " join agents.assignment builder on builder.realm_id=result.realm_id"
            " and builder.id=result.assignment_id"
            " join agents.invocation invocation on invocation.realm_id=verification.realm_id"
            " and invocation.id=verification.verifier_invocation_id"
            " join agents.result_receipt result_receipt"
            " on result_receipt.realm_id=invocation.realm_id"
            " and result_receipt.invocation_id=invocation.id"
            " where checkpoint.realm_id=%s and checkpoint.job_id=%s",
            (realm.id, job.id),
        )
        checkpoint_v2 = cursor.fetchone()
    assert checkpoint_v2 is not None
    assert checkpoint_v2[0:6] == (
        f"client-lifecycle:{run.work_item_id}:{run.plan_id}",
        1,
        ["build"],
        ["build"],
        [],
        True,
    )
    assert checkpoint_v2[6] == checkpoint_partition[3]["build"]
    assert checkpoint_v2[7] == assignment_id
    assert checkpoint_v2[8] is not None
    assert checkpoint_v2[9] == verifier_assignment_id
    assert checkpoint_v2[10] is not None
    assert str(checkpoint_v2[11]).startswith("sha256:")
    assert checkpoint_v2[12] != checkpoint_v2[13]
    assert checkpoint_v2[14] != lifecycle_environment.execution_identity
    assert checkpoint_v2[15] == checkpoint_v2[11]
    with realm_database_session(migrated_database, realm_id=realm.id) as guard_connection:
        with guard_connection.transaction(), guard_connection.cursor() as guard_cursor:
            guard_cursor.execute(
                "select client.lock_codex_lifecycle_scope(%s,%s,%s,%s)",
                (realm.id, job.id, work.attempt_id, authorization.id),
            )
            with (
                realm_database_session(migrated_database, realm_id=realm.id) as child_connection,
                child_connection.cursor() as child_cursor,
            ):
                child_cursor.execute("set lock_timeout='100ms'")
                with pytest.raises(Exception) as child_blocked:
                    # FK child inserts take this KEY SHARE parent lock before an
                    # envelope/checkpoint can become a new latest row.
                    child_cursor.execute(
                        "select id from runtime.job where realm_id=%s and id=%s for key share",
                        (realm.id, job.id),
                    )
                assert getattr(child_blocked.value, "sqlstate", None) == "55P03"
        with guard_connection.cursor() as released_cursor:
            released_cursor.execute(
                "select id from runtime.job where realm_id=%s and id=%s for key share",
                (realm.id, job.id),
            )
            assert released_cursor.fetchone()[0] == job.id
    mutation_cases = (
        ("current-plan", "work_plan_digest"),
        ("authorization-plan", "effect_plan_digest"),
        ("claim-effect", "effect_digest"),
        ("source", "source_digest"),
        ("envelope", "envelope_digest"),
        ("receipt-terminal", "terminal_hook_receipt_digest"),
        ("formula", "result_formula_digest"),
        ("binding", "binding_digest"),
    )
    admission_fields = [
        "continuity_event_id",
        "delivery_outbox_id",
        "hook_receipt_id",
        "job_id",
        "attempt_id",
        "envelope_id",
        "authorization_id",
        "claim_id",
        "effect_receipt_id",
        "work_plan_digest",
        "effect_plan_digest",
        "effect_plan_body",
        "effect_digest",
        "source_digest",
        "policy_digest",
        "migration_digest",
        "envelope_digest",
        "terminal_hook_receipt_digest",
        "result_formula_digest",
        "binding_digest",
    ]
    for mutation_name, mutation_field in mutation_cases:
        selected_fields = ["%s" if field == mutation_field else field for field in admission_fields]
        configure_session(connection, realm_id=realm.id)
        with pytest.raises(Exception) as forged:  # noqa: SIM117
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("reset role")
                cursor.execute(
                    "create temporary table codex_admission_exact_source"
                    " on commit drop as select * from client.codex_lifecycle_admission"
                    " where realm_id=%s and job_id=%s",
                    (realm.id, job.id),
                )
                cursor.execute(
                    "create temporary table lifecycle_hydration_exact_source"
                    " on commit drop as select *"
                    " from continuity.lifecycle_hydration_admission"
                    " where realm_id=%s and codex_admission_id in"
                    " (select id from codex_admission_exact_source)",
                    (realm.id,),
                )
                cursor.execute(
                    "alter table continuity.lifecycle_hydration_admission"
                    " disable trigger lifecycle_hydration_no_mutation"
                )
                cursor.execute(
                    "delete from continuity.lifecycle_hydration_admission"
                    " where realm_id=%s and codex_admission_id in"
                    " (select id from codex_admission_exact_source)",
                    (realm.id,),
                )
                cursor.execute(
                    "alter table continuity.lifecycle_hydration_admission"
                    " enable trigger lifecycle_hydration_no_mutation"
                )
                cursor.execute(
                    "alter table client.codex_lifecycle_admission"
                    " disable trigger codex_lifecycle_admission_no_mutation"
                )
                cursor.execute(
                    "delete from client.codex_lifecycle_admission where realm_id=%s and job_id=%s",
                    (realm.id, job.id),
                )
                cursor.execute(
                    "alter table client.codex_lifecycle_admission"
                    " enable trigger codex_lifecycle_admission_no_mutation"
                )
                cursor.execute(
                    "insert into client.codex_lifecycle_admission"
                    " (id,realm_id,lifecycle_event_id,entry_digest,continuity_event_id,"
                    " delivery_outbox_id,hook_receipt_id,job_id,attempt_id,envelope_id,"
                    " authorization_id,claim_id,effect_receipt_id,work_plan_digest,"
                    " effect_plan_digest,effect_plan_body,effect_digest,source_digest,"
                    " policy_digest,migration_digest,envelope_digest,terminal_hook_receipt_digest,"
                    " result_formula_digest,binding_digest,created_at,grants_authority)"
                    " select id,realm_id,lifecycle_event_id,entry_digest,"
                    + ",".join(selected_fields)
                    + ",created_at,false"
                    " from codex_admission_exact_source"
                    " where realm_id=%s and job_id=%s",
                    (
                        digest(f"forged-field:{mutation_name}"),
                        realm.id,
                        job.id,
                    ),
                )
                cursor.execute(
                    "insert into continuity.lifecycle_hydration_admission"
                    " select * from lifecycle_hydration_exact_source"
                )
                cursor.execute(
                    "set constraints client.codex_lifecycle_admission_row_guard immediate"
                )
        assert getattr(forged.value, "sqlstate", None) == "23514"
        assert "governed admission" in str(forged.value)
    chain_mutations = {
        "future-admission": (
            "update codex_admission_chain_source"
            " set created_at=clock_timestamp()+interval '1 hour'",
            (),
        ),
        "wrong-other-step-digest": (
            "update work.checkpoint set step_results="
            "jsonb_set(step_results,'{prepare}',to_jsonb(%s::text))"
            " where realm_id=%s and job_id=%s",
            (digest("forged-prepare-result"), realm.id, job.id),
        ),
        "authorization-issued-after-claim": (
            "update security.authorization auth set issued_at=(select claimed_at"
            " + interval '1 second' from runtime.effect_claim claim"
            " where claim.realm_id=auth.realm_id"
            " and claim.authorization_id=auth.id)"
            " where auth.realm_id=%s and auth.id=%s",
            (realm.id, authorization.id),
        ),
        "authorization-risk": (
            "update security.authorization set risk='low' where realm_id=%s and id=%s",
            (realm.id, authorization.id),
        ),
        "authorization-actor": (
            "update core.actor set status='suspended' where realm_id=%s and id=%s",
            (realm.id, actor.id),
        ),
        "canonical-fence": (
            "update runtime.job set fencing_token=fencing_token+1 where realm_id=%s and id=%s",
            (realm.id, job.id),
        ),
        "compiler-enqueue-json-boolean": (
            "update hooks.result_receipt receipt set output_body=jsonb_set("
            " output_body,'{command,compiler_enqueue}','null'::jsonb),"
            " output_digest=models.capability_runtime_jsonb_digest(jsonb_set("
            " output_body,'{command,compiler_enqueue}','null'::jsonb))"
            " from client.codex_lifecycle_admission admission"
            " where admission.realm_id=%s and admission.job_id=%s"
            " and receipt.realm_id=admission.realm_id and receipt.id=admission.hook_receipt_id",
            (realm.id, job.id),
        ),
    }
    for mutation_name, (mutation_sql, mutation_params) in chain_mutations.items():
        configure_session(connection, realm_id=realm.id)
        with pytest.raises(Exception) as forged_chain:  # noqa: SIM117
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("reset role")
                cursor.execute(
                    "create temporary table codex_admission_chain_source"
                    " on commit drop as select * from client.codex_lifecycle_admission"
                    " where realm_id=%s and job_id=%s",
                    (realm.id, job.id),
                )
                cursor.execute(
                    "create temporary table lifecycle_hydration_chain_source"
                    " on commit drop as select *"
                    " from continuity.lifecycle_hydration_admission"
                    " where realm_id=%s and codex_admission_id in"
                    " (select id from codex_admission_chain_source)",
                    (realm.id,),
                )
                cursor.execute(
                    "alter table continuity.lifecycle_hydration_admission"
                    " disable trigger lifecycle_hydration_no_mutation"
                )
                cursor.execute(
                    "delete from continuity.lifecycle_hydration_admission"
                    " where realm_id=%s and codex_admission_id in"
                    " (select id from codex_admission_chain_source)",
                    (realm.id,),
                )
                cursor.execute(
                    "alter table continuity.lifecycle_hydration_admission"
                    " enable trigger lifecycle_hydration_no_mutation"
                )
                cursor.execute(
                    "alter table client.codex_lifecycle_admission"
                    " disable trigger codex_lifecycle_admission_no_mutation"
                )
                cursor.execute(
                    "delete from client.codex_lifecycle_admission where realm_id=%s and job_id=%s",
                    (realm.id, job.id),
                )
                cursor.execute(
                    "alter table client.codex_lifecycle_admission"
                    " enable trigger codex_lifecycle_admission_no_mutation"
                )
                cursor.execute(mutation_sql, mutation_params)
                cursor.execute(
                    "insert into client.codex_lifecycle_admission select id,realm_id,"
                    " lifecycle_event_id,entry_digest,continuity_event_id,delivery_outbox_id,"
                    " hook_receipt_id,job_id,attempt_id,envelope_id,authorization_id,claim_id,"
                    " effect_receipt_id,work_plan_digest,effect_plan_digest,effect_plan_body,"
                    " effect_digest,source_digest,policy_digest,migration_digest,envelope_digest,"
                    " terminal_hook_receipt_digest,result_formula_digest,binding_digest,"
                    " created_at,grants_authority from codex_admission_chain_source"
                )
                cursor.execute(
                    "insert into continuity.lifecycle_hydration_admission"
                    " select * from lifecycle_hydration_chain_source"
                )
                cursor.execute(
                    "set constraints client.codex_lifecycle_admission_row_guard immediate"
                )
        forged_chain_sqlstate = getattr(forged_chain.value, "sqlstate", None)
        assert forged_chain_sqlstate in {"23514", "42501"}, mutation_name
        if forged_chain_sqlstate == "23514":
            assert "governed admission" in str(forged_chain.value), mutation_name
        else:
            assert "append-only tablo" in str(forged_chain.value), mutation_name
    configure_session(connection, realm_id=realm.id)
    down_sql = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "0058_codex_lifecycle_admission.down.sql"
    ).read_text(encoding="utf-8")
    with pytest.raises(Exception) as refused:  # noqa: SIM117
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("reset role")
            cursor.execute(down_sql)
    assert getattr(refused.value, "sqlstate", None) == "55000"
    with connection.cursor() as cursor:
        cursor.execute(
            "select (select count(*) from client.codex_lifecycle_admission"
            " where realm_id=%s and job_id=%s),"
            " (select count(*) from pg_trigger trigger_ join pg_class table_"
            " on table_.oid=trigger_.tgrelid join pg_namespace namespace_"
            " on namespace_.oid=table_.relnamespace"
            " where namespace_.nspname='client'"
            " and trigger_.tgname in ('codex_lifecycle_admission_guard',"
            " 'codex_lifecycle_admission_row_guard') and not trigger_.tgisinternal),"
            " (select relforcerowsecurity from pg_class table_ join pg_namespace namespace_"
            " on namespace_.oid=table_.relnamespace"
            " where namespace_.nspname='client'"
            " and table_.relname='codex_lifecycle_admission')",
            (realm.id, job.id),
        )
        assert cursor.fetchone() == (1, 2, True)
