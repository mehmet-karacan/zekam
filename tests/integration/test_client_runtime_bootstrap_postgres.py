"""Client runtime bootstrap canonical control-plane integration."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.integration.test_agent_residency_postgres import residency_scope as _residency_scope

from zekam.application.capability_profile import PROFILER_VERSION, CapabilityProfile
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.client_runtime_bootstrap import (
    ClaimedLifecycleBootstrapService,
    ClientRuntimeBootstrapService,
)
from zekam.application.execution import ExecutionHost
from zekam.application.governance import GovernanceService
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.application.worker import (
    WorkerSettings,
    run_codex_lifecycle_bootstrap_once,
    run_codex_lifecycle_once,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_environment import (
    ExecutionEnvironmentSnapshot,
    detect_environment_drift,
    reprobe_snapshot,
)
from zekam.domain.model_routing import (
    AgentRole,
    ExecutionTargetSnapshot,
    ProjectRoutingContext,
    RoleRoutingPolicy,
    RoutingLayer,
)
from zekam.domain.project import SourceRevisionKind
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.runtime import JobState
from zekam.domain.work import AcceptanceCriterion, WorkState, WorkType
from zekam.infrastructure.clients.codex_lifecycle import (
    CODEX_REVIEWED_VERSION,
    parse_codex_hook_input,
)
from zekam.infrastructure.postgres.agent_assignment_repository import AgentAssignmentRepository
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
from zekam.infrastructure.postgres.memory_hook_installer import PostgresMemoryHookInstaller
from zekam.infrastructure.postgres.model_routing_repository import ModelRoutingRepository
from zekam.infrastructure.postgres.project_repository import (
    CapabilityProfileRepository,
    SourceBindingRepository,
)
from zekam.infrastructure.postgres.runtime_repository import JobRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_bootstrap_is_atomic_exact_and_leaves_effect_for_worker(
    realm_session: tuple[Any, Any], tmp_path: Any
) -> None:
    realm, connection = realm_session
    governance = GovernanceService(connection, realm)
    governance.ensure_default_policy()
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="runtime-bootstrap-reviewer")
    )
    source = tmp_path / "source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(
        source_path=source, slug="runtime-bootstrap"
    )
    graph = WorkGraphService(connection, realm, actor_id=actor.id)
    work = graph.create_item(
        project_id=project.id,
        type=WorkType.TASK,
        title="Client runtime bootstrap",
        acceptance_criteria=(AcceptanceCriterion("terminal worker receipt"),),
    )
    service = ClientRuntimeBootstrapService(connection, realm)
    prepared = service.prepare(
        project_id=project.id,
        work_item_id=work.id,
        actor_id=actor.id,
        client_id="codex",
        session_id="session-bootstrap",
        entry_digest=digest("pending-session-start"),
        source_revision="git:reviewed-source",
    )

    with pytest.raises(PolicyViolation, match="exact plan digest"):
        service.apply(
            prepared,
            supplied_plan_digest=digest("wrong"),
            current_entry_digest=prepared.entry_digest,
            current_source_revision=prepared.source_revision,
        )
    assert graph.items.get(work.id).state is WorkState.PROPOSED
    assert graph.snapshot(work.id).plan is None

    result = service.apply(
        prepared,
        supplied_plan_digest=prepared.plan_digest,
        current_entry_digest=prepared.entry_digest,
        current_source_revision=prepared.source_revision,
        now=dt.datetime.now(dt.UTC),
    )
    snapshot = graph.snapshot(work.id)
    assert snapshot.item.state is WorkState.ACTIVE
    assert snapshot.intent is not None and snapshot.plan is not None
    assert tuple(step.step_id for step in snapshot.plan.steps) == (
        "client-lifecycle-bootstrap",
        "client-lifecycle-drain",
    )
    assert snapshot.plan.steps[0].effect.value == "database-write"
    assert snapshot.plan.steps[1].effect.value == "database-write"
    job = JobRepository(connection, realm.id).get(result.job_id)
    assert job.max_attempts == 1
    assert job.run_id == result.run_id
    assert job.assignment_id == result.bootstrap_assignment_id
    assert job.payload["schema"] == "zekam-codex-lifecycle-bootstrap-job/v1"
    assert job.payload["entry_digest"] == prepared.entry_digest
    assert job.payload["child_assignment_id"] == str(result.builder_assignment_id)
    assert set(job.payload) == {
        "schema",
        "entry_digest",
        "authorization_id",
        "effect_digest",
        "child_assignment_id",
        "context_created_at",
        "context_manifest_digest",
    }
    builder = AgentAssignmentRepository(connection, realm.id).get(result.builder_assignment_id)
    verifier = AgentAssignmentRepository(connection, realm.id).get(result.verifier_assignment_id)
    bootstrap = AgentAssignmentRepository(connection, realm.id).get(result.bootstrap_assignment_id)
    assert bootstrap.step_id == "client-lifecycle-bootstrap"
    assert bootstrap.write_resources == (prepared.bootstrap_resource,)
    assert builder.step_id == "client-lifecycle-drain"
    assert builder.write_resources == (prepared.resource,)
    assert verifier.read_resources == (prepared.resource,)

    with pytest.raises(PolicyViolation, match="exact Work state"):
        service.prepare(
            project_id=project.id,
            work_item_id=work.id,
            actor_id=actor.id,
            client_id="codex",
            session_id="session-bootstrap",
            entry_digest=prepared.entry_digest,
            source_revision=prepared.source_revision,
        )


def test_missing_runtime_template_rejects_before_parent_claim(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    GovernanceService(connection, realm).ensure_default_policy()
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="template-preclaim-reviewer")
    )
    source = tmp_path / "missing-template-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(
        source_path=source, slug="missing-template-bootstrap"
    )
    graph = WorkGraphService(connection, realm, actor_id=actor.id)
    work = graph.create_item(
        project_id=project.id,
        type=WorkType.TASK,
        title="Missing template must fail before claim",
        acceptance_criteria=(AcceptanceCriterion("no parent claim"),),
    )
    now = dt.datetime.now(dt.UTC)
    home = tmp_path / "missing-template-home"
    spool = ClientLifecycleSpool(home, client_id="codex")
    hook = parse_codex_hook_input(
        json.dumps(
            {
                "session_id": str(uuid4()),
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
    service = ClientRuntimeBootstrapService(connection, realm)
    plan = service.prepare(
        project_id=project.id,
        work_item_id=work.id,
        actor_id=actor.id,
        client_id="codex",
        session_id=entry.session_id,
        entry_digest=entry.entry_digest,
        source_revision="git:missing-template",
        now=now,
    )
    applied = service.apply(
        plan,
        supplied_plan_digest=plan.plan_digest,
        current_entry_digest=entry.entry_digest,
        current_source_revision=plan.source_revision,
        now=now,
    )

    with pytest.raises(PolicyViolation, match="template eksik"):
        run_codex_lifecycle_bootstrap_once(
            connection,
            realm.id,
            home=home,
            settings=WorkerSettings(
                worker_label="template-preclaim-worker",
                capabilities=("client.lifecycle.codex-bootstrap",),
                max_iterations=1,
            ),
        )
    parent = JobRepository(connection, realm.id).get(applied.job_id)
    assert parent.state is JobState.READY
    assert parent.attempt_count == 0
    assert (
        ExecutionHost(connection, realm.id, worker_label="verify").ledger.claims_for_job(parent.id)
        == ()
    )


def test_claimed_bootstrap_materializes_exact_child_on_real_postgres(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    scope = _residency_scope.__wrapped__(realm_session, tmp_path)  # type: ignore[attr-defined]
    realm, connection = realm_session
    GovernanceService(connection, realm).ensure_default_policy()
    base_run = scope["run"]
    now = dt.datetime.now(dt.UTC)
    bindings = SourceBindingRepository(connection, realm.id)
    binding = bindings.for_project(base_run.project_id)[0]
    source_snapshot = bindings.record_revision(
        binding_id=binding.id,
        kind=SourceRevisionKind.TREE_DIGEST,
        revision=base_run.source_revision,
        tree_digest=digest("bootstrap-materializer-source-tree"),
        now=now,
    )
    profile = CapabilityProfile(
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
        project_id=base_run.project_id,
        source_revision_id=source_snapshot.id,
        profile=profile,
        now=now,
    )
    target = ExecutionTargetSnapshot(
        client_id="codex",
        slot="lifecycle",
        execution_mode="native-sequential",
        model_selectable=True,
        structured_result=False,
        cancellation=False,
        max_concurrency=1,
        cost_evidence_digest=digest("bootstrap-lifecycle-cost"),
        capability_digest=digest("bootstrap-lifecycle-capability"),
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=30),
    )
    routing = ModelRoutingRepository(connection, realm.id)
    target_id, _ = routing.store_execution_target(target)
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into models.model_route_decision"
            " (id,realm_id,role_policy_id,execution_target_id,role,target_layer,"
            " inventory_digest,routing_policy_digest,policy_digest,execution_target_digest,"
            " status,primary_model_id,evidence_digest,decided_at) values"
            " (%s,%s,%s,%s,'implementer','general',%s,%s,%s,%s,'selected',%s,%s,%s)",
            (
                uuid4(),
                realm.id,
                scope["role_policy_id"],
                target_id,
                digest("inventory"),
                scope["policy_digest"],
                scope["policy_digest"],
                target.snapshot_digest,
                scope["model"].model_id,
                digest("bootstrap-lifecycle-route"),
                now,
            ),
        )
    PostgresMemoryHookInstaller(connection, realm.id).ensure(installed_at=now)
    with connection.cursor() as cursor:
        cursor.execute(
            "select compiled.config_effective_digest from hooks.current_generation current"
            " join hooks.compiled_set compiled on compiled.realm_id=current.realm_id"
            " and compiled.id=current.compiled_set_id where current.realm_id=%s",
            (realm.id,),
        )
        hook_config_digest = str(cursor.fetchone()[0])
    sticky = scope["environment"]
    lifecycle_environment = ExecutionEnvironmentSnapshot.create(
        realm_id=realm.id,
        environment_id="bootstrap-lifecycle-env",
        execution_identity=sticky.execution_identity,
        provider=sticky.provider,
        platform=sticky.platform,
        executor_protocol_version=sticky.executor_protocol_version,
        cwd_locator=sticky.cwd_locator,
        workspace_roots=sticky.workspace_roots,
        shell=sticky.shell,
        permission_profile_id=sticky.permission_profile_id,
        permission_profile_digest=sticky.permission_profile_digest,
        filesystem_policy_digest=sticky.filesystem_policy_digest,
        network_policy_digest=sticky.network_policy_digest,
        tool_runtime_digest=sticky.tool_runtime_digest,
        capability_digest=sticky.capability_digest,
        config_effective_digest=hook_config_digest,
        source_revision=base_run.source_revision,
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=30),
    )
    execution = ExecutionRunRepository(connection, realm.id)
    execution.create_environment_snapshot(lifecycle_environment)
    current_environment = reprobe_snapshot(
        lifecycle_environment,
        captured_at=now,
        expires_at=now + dt.timedelta(minutes=30),
    )
    execution.create_environment_snapshot(current_environment)
    execution.record_environment_probe(
        detect_environment_drift(lifecycle_environment, current_environment, checked_at=now)
    )
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="bootstrap-materializer-reviewer")
    )
    graph = WorkGraphService(connection, realm, actor_id=actor.id)
    work_item = graph.create_item(
        project_id=base_run.project_id,
        type=WorkType.TASK,
        title="Claimed lifecycle bootstrap",
        acceptance_criteria=(AcceptanceCriterion("terminal child receipt"),),
    )
    home = tmp_path / "bootstrap-home"
    spool = ClientLifecycleSpool(home, client_id="codex")
    hook = parse_codex_hook_input(
        json.dumps(
            {
                "session_id": str(uuid4()),
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
    service = ClientRuntimeBootstrapService(connection, realm)
    plan = service.prepare(
        project_id=base_run.project_id,
        work_item_id=work_item.id,
        actor_id=actor.id,
        client_id="codex",
        session_id=entry.session_id,
        entry_digest=entry.entry_digest,
        source_revision=base_run.source_revision,
        now=now,
    )
    inventory_digest = digest("bootstrap-materializer-inventory")
    current_role_policy_id = routing.store_role_policy(
        RoleRoutingPolicy(
            role=AgentRole.IMPLEMENTER,
            target_layer=RoutingLayer.GENERAL,
            required_layers=(RoutingLayer.GENERAL,),
            top_k=1,
            fallback_model_ids=(),
            max_cost=10,
            max_latency_ms=30_000,
            independent_from_roles=(),
            policy_digest=plan.policy_digest,
        ),
        effective_from=now,
    )
    routing.store_project_context(
        ProjectRoutingContext(
            project_id=base_run.project_id,
            source_revision_id=source_snapshot.id,
            source_revision=base_run.source_revision,
            tree_digest=source_snapshot.tree_digest,
            capability_profile_digest=profile.digest,
            dependency_digest=digest("bootstrap-dependencies"),
            framework_digest=digest("bootstrap-frameworks"),
            technology_digest=digest("bootstrap-technology"),
            architecture_digest=digest("bootstrap-architecture"),
            rules_digest=digest("bootstrap-rules"),
            suite_digest=digest("bootstrap-suite"),
            inventory_digest=inventory_digest,
            policy_digest=plan.policy_digest,
            captured_at=now,
            expires_at=now + dt.timedelta(minutes=30),
        )
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into models.model_route_decision"
            " (id,realm_id,role_policy_id,execution_target_id,role,target_layer,"
            " inventory_digest,routing_policy_digest,policy_digest,execution_target_digest,"
            " status,primary_model_id,evidence_digest,decided_at) values"
            " (%s,%s,%s,%s,'implementer','general',%s,%s,%s,%s,'selected',%s,%s,%s)",
            (
                uuid4(),
                realm.id,
                current_role_policy_id,
                target_id,
                inventory_digest,
                plan.policy_digest,
                plan.policy_digest,
                target.snapshot_digest,
                scope["model"].model_id,
                digest("bootstrap-current-lifecycle-route"),
                now,
            ),
        )
    applied = service.apply(
        plan,
        supplied_plan_digest=plan.plan_digest,
        current_entry_digest=entry.entry_digest,
        current_source_revision=base_run.source_revision,
        now=now,
    )
    host = ExecutionHost(connection, realm.id, worker_label="bootstrap-worker")
    claimed = host.acquire_work(capabilities=("client.lifecycle.codex-bootstrap",), now=now)
    assert claimed is not None and claimed.job.id == applied.job_id
    result_digest = ClaimedLifecycleBootstrapService(connection, realm.id).materialize(
        claimed, home, now=now
    )
    assert result_digest.startswith("sha256:")
    children = JobRepository(connection, realm.id).list_by_state(JobState.READY)
    child = next(item for item in children if item.step_id == "client-lifecycle-drain")
    assert set(child.payload) == {
        "schema",
        "authorization_id",
        "hydration_authorization_id",
        "lifecycle_plan_body",
    }
    child_result = run_codex_lifecycle_once(
        connection,
        realm.id,
        home=home,
        settings=WorkerSettings(
            worker_label="codex-lifecycle-worker",
            capabilities=("client.lifecycle.codex-drain",),
            max_iterations=1,
        ),
    )
    assert child_result is not None
    assert spool.pending(limit=1) == ()
    assert JobRepository(connection, realm.id).get(child.id).state is JobState.COMPLETED
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from runtime.job where realm_id=%s and run_id=%s"
                " and state in ('ready','running','recovery-required')",
            (realm.id, child.run_id),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "select count(*) from runtime.claim_without_receipt claim"
            " join runtime.job job on job.realm_id=claim.realm_id and job.id=claim.job_id"
            " where claim.realm_id=%s and job.run_id=%s",
            (realm.id, child.run_id),
        )
        assert cursor.fetchone()[0] == 0

    rebootstrap = service.prepare(
        project_id=base_run.project_id,
        work_item_id=work_item.id,
        actor_id=actor.id,
        client_id="codex",
        session_id=entry.session_id,
        entry_digest=entry.entry_digest,
        source_revision=base_run.source_revision,
        rebootstrap=True,
        now=now + dt.timedelta(seconds=1),
    )
    assert rebootstrap.rebootstrap is True
    reapplied = service.apply(
        rebootstrap,
        supplied_plan_digest=rebootstrap.plan_digest,
        current_entry_digest=entry.entry_digest,
        current_source_revision=base_run.source_revision,
        now=now + dt.timedelta(seconds=1),
    )
    assert reapplied.task_plan_id != applied.task_plan_id
    assert JobRepository(connection, realm.id).get(reapplied.job_id).state is JobState.READY
    assert graph.snapshot(work_item.id).plan is not None
    assert graph.snapshot(work_item.id).plan.revision == 2


def test_lifecycle_currentness_accepts_dirty_aware_run_source_sql_contract() -> None:
    source = Path(
        "src/zekam/infrastructure/postgres/client_lifecycle_repository.py"
    ).read_text(encoding="utf-8")

    assert "s.revision=(case when e.source_revision" in source
    assert "~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'" in source
    assert "then substring(e.source_revision from 5 for 40)" in source
    assert source.count("source.revision=(case when envelope.source_revision") == 3
    assert source.count("then substring(envelope.source_revision from 5 for 40)") == 3
    assert "context.source_revision=(case when plan.source_revision" in source
    assert "then substring(plan.source_revision from 5 for 40)" in source

    migration = Path("migrations/0070_checkpoint_v2_dirty_source_revision.sql").read_text(
        encoding="utf-8"
    )
    assert "substring(new.source_revision from 5 for 40)" in migration

    admission_migration = Path(
        "migrations/0071_codex_lifecycle_plan_body_admission.sql"
    ).read_text(encoding="utf-8")
    assert "payload_key not in ('schema','authorization_id'" in admission_migration
    assert "'hydration_authorization_id','lifecycle_plan_body')" in admission_migration
    assert "job.payload->'lifecycle_plan_body'=admission.effect_plan_body" in (
        admission_migration
    )
    assert "continuity_event.idempotency_key||'':job:''||job.id::text" in (
        admission_migration
    )

    dirty_admission_migration = Path(
        "migrations/0072_codex_lifecycle_dirty_source_admission.sql"
    ).read_text(encoding="utf-8")
    assert "when envelope.source_revision ~ '^git:[0-9a-f]{40};state:" in (
        dirty_admission_migration
    )
    assert "then substring(envelope.source_revision from 5 for 40)" in (
        dirty_admission_migration
    )

    bootstrap = Path("src/zekam/application/client_runtime_bootstrap.py").read_text(
        encoding="utf-8"
    )
    assert "reuse_existing=True" in bootstrap
    assert 'f"codex-lifecycle:{entry.delivery_id}:parent:{job.id}"' in bootstrap
    assert "Lifecycle child job replay reddedildi" in bootstrap

    composition = Path("src/zekam/application/client_lifecycle_composition.py").read_text(
        encoding="utf-8"
    )
    assert 'idempotency_key=f"{entry.delivery_id}:job:{work.job.id}"' in composition
    assert 'f"{entry.delivery_id}:job:{terminal[\'job_id\']}"' in composition
