from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from zekam.application.hook_runtime import HookRuntime, LoadedHookAdapter
from zekam.application.memory_hooks import MEMORY_HOOK_EVENTS
from zekam.application.memory_observability import MemoryDimensionStatus
from zekam.application.memory_upgrade import (
    FEATURE_RESOURCE,
    MemoryUpgradeService,
    UpgradeTarget,
)
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.config_provenance import PermissionProfileRevision
from zekam.domain.errors import PolicyViolation
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
from zekam.domain.work import WorkType
from zekam.infrastructure.postgres.config_provenance_repository import (
    ConfigProvenanceRepository,
)
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.hook_runtime_repository import HookRuntimeRepository
from zekam.infrastructure.postgres.memory_observability_repository import (
    PostgresMemoryHealthReader,
)
from zekam.infrastructure.postgres.memory_upgrade_repository import (
    PROJECTION_REF,
    PostgresMemoryUpgradeRepository,
)
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 26, 12, tzinfo=dt.UTC)


def _install_unrelated_custom_hook(
    connection: Any,
    realm_id: UUID,
    *,
    event_type: HookEventType = HookEventType.SESSION_START,
) -> None:
    hook_id = f"custom-{event_type.value.replace('_', '-').replace('.', '-')}"
    profile = PermissionProfileRevision.from_flags(
        realm_id=realm_id,
        name="custom-readonly",
        revision=1,
        permission_flags={
            "filesystem.read": False,
            "filesystem.write": False,
            "network.access": False,
            "process.run": False,
        },
        managed=False,
        created_at=NOW,
    )
    schema = {"type": "object"}
    spec = HookSpecRevision.create(
        realm_id=realm_id,
        hook_id=hook_id,
        revision=1,
        event_type=event_type,
        required=True,
        source_layer="custom",
        timeout_ms=1_000,
        execution_mode=HookExecutionMode.INTERNAL,
        input_schema=schema,
        output_schema=schema,
        permission_profile_name=profile.name,
        permission_profile_digest=profile.profile_digest,
        failure_policy=HookFailurePolicy.ABORT,
        created_at=NOW,
    )
    adapter = LoadedHookAdapter(
        f"{hook_id}-v1",
        digest({"custom-adapter": event_type.value}),
        HookExecutionMode.INTERNAL,
        lambda _payload: HookAdapterResult(HookResultKind.OBSERVATION, {}),
    )
    runtime_revision = HookRuntimeRevision.create(
        realm_id=realm_id,
        hook_id=spec.hook_id,
        hook_revision=1,
        adapter_ref=adapter.adapter_ref,
        adapter_digest=adapter.adapter_digest,
        permission_capabilities=(),
        load_state=HookLoadState.READY,
        captured_at=NOW,
        expires_at=NOW + dt.timedelta(days=365),
    )
    runtime = HookRuntime()
    compiled = runtime.reconfigure(
        realm_id=realm_id,
        config_effective_digest=digest({"custom-config": event_type.value}),
        specs=(spec,),
        runtimes=(runtime_revision,),
        profiles=(profile,),
        adapters=(adapter,),
        now=NOW,
        required_events=(event_type,),
    )
    repository = HookRuntimeRepository(connection, realm_id)
    profile_id, _ = ConfigProvenanceRepository(connection, realm_id).store_profile(profile)
    repository.store_spec(spec, permission_profile_revision_id=profile_id)
    repository.store_runtime(runtime_revision)
    compiled_id, _ = repository.store_compiled_set(compiled, created_at=NOW)
    repository.activate(compiled_id)
    runtime.shutdown(timeout_seconds=0)


def _authorization(
    repository: PostgresMemoryUpgradeRepository,
    authorizations: AuthorizationRepository,
    *,
    actor_id: UUID,
    work_item_id: UUID,
    plan: Any,
) -> Authorization:
    authorization = Authorization.issue(
        realm_id=repository.realm_id,
        actor_id=actor_id,
        work_item_id=work_item_id,
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(FEATURE_RESOURCE,),
            allowed_effects=("database-write",),
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )
    return authorizations.issue(authorization)


def test_shadow_bootstrap_installs_exact_hooks_and_public_projection_idempotently(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "memory-upgrade-source"
    source.mkdir()
    integration = ProjectIntegrationService(connection, realm)
    project = integration.register(source_path=source, now=NOW)
    source_revision = integration.scan(project.id, now=NOW).revision
    work = WorkGraphService(connection, realm).create_item(
        project_id=project.id,
        type=WorkType.TASK,
        title="Memory shadow bootstrap",
        now=NOW,
    )
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="memory-upgrade-reviewer", now=NOW)
    )
    _install_unrelated_custom_hook(connection, realm.id)
    repository = PostgresMemoryUpgradeRepository(
        connection,
        realm.id,
        project_id=project.id,
        work_item_id=work.id,
    )
    authorizations = AuthorizationRepository(connection, realm.id)
    service = MemoryUpgradeService(repository, authorizations)
    before = service.detect(now=NOW)
    assert before.required_hook_invalid_count == 17
    assert not before.projection_current
    assert before.source_head == source_revision.revision
    plan = service.check_plan(
        target=UpgradeTarget.SHADOW,
        rollback_ref="rollback/memory-shadow-bootstrap",
        rollback_digest=digest("rollback"),
        now=NOW,
    )
    authorization = _authorization(
        repository,
        authorizations,
        actor_id=actor.id,
        work_item_id=work.id,
        plan=plan,
    )

    applied = service.apply(plan, authorization_id=authorization.id, now=NOW)

    assert applied.created
    after = service.detect(now=NOW + dt.timedelta(seconds=1))
    assert after.required_hook_invalid_count == 0
    assert after.projection_current
    health = PostgresMemoryHealthReader(
        connection,
        source,
        tmp_path / "private-store",
        realm.id,
    ).collect(now=NOW + dt.timedelta(seconds=1))
    assert len(health.dimensions) == 15
    assert (
        next(
            item for item in health.dimensions if item.dimension_id == "required-hook-runtime"
        ).status
        is MemoryDimensionStatus.PASSED
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "select spec.event_type,count(*)"
            " from hooks.current_generation current_set"
            " join hooks.compiled_set_entry entry on entry.realm_id=current_set.realm_id"
            " and entry.compiled_set_id=current_set.compiled_set_id"
            " join hooks.spec_revision spec on spec.realm_id=entry.realm_id"
            " and spec.id=entry.spec_revision_id"
            " where current_set.realm_id=%s and spec.required"
            " and entry.runtime_revision_id is not null and entry.disabled_reason is null"
            " and spec.event_type=any(%s) group by spec.event_type",
            (realm.id, [item.value for item in MEMORY_HOOK_EVENTS]),
        )
        assert {str(row[0]): int(row[1]) for row in cursor.fetchall()} == {
            item.value: 1 for item in MEMORY_HOOK_EVENTS
        }
        cursor.execute(
            "select count(*) from hooks.current_generation current_set"
            " join hooks.compiled_set_entry entry on entry.realm_id=current_set.realm_id"
            " and entry.compiled_set_id=current_set.compiled_set_id"
            " join hooks.spec_revision spec on spec.realm_id=entry.realm_id"
            " and spec.id=entry.spec_revision_id"
            " where current_set.realm_id=%s and spec.hook_id='custom-session-start'",
            (realm.id,),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "select source_digest,projection_digest,classification,public_filtered,"
            " receipt_body,grants_authority"
            " from continuity.projection_generation_receipt"
            " where realm_id=%s and project_id=%s and work_item_id=%s and projection_ref=%s",
            (realm.id, project.id, work.id, PROJECTION_REF),
        )
        projection = cursor.fetchone()
    assert projection[0] == after.projection_source_digest
    assert projection[1] == after.latest_projection_digest
    assert projection[2:] == ("public", True, projection[4], False)
    assert projection[4]["public_filtered"] is True
    assert projection[4]["classification"] == "public"
    assert projection[4]["grants_authority"] is False

    replay_plan = service.check_plan(
        target=UpgradeTarget.SHADOW,
        rollback_ref="rollback/memory-shadow-bootstrap",
        rollback_digest=digest("rollback"),
        now=NOW + dt.timedelta(seconds=1),
    )
    replay = service.apply(replay_plan, authorization_id=UUID(int=0), now=NOW)
    assert not replay.created

    (source / "source-drift.txt").write_text("drift", encoding="utf-8")
    integration.scan(project.id, now=NOW + dt.timedelta(seconds=2))
    source_drift = service.detect(now=NOW + dt.timedelta(seconds=2))
    assert source_drift.source_head != after.source_head
    assert source_drift.projection_source_digest != after.projection_source_digest
    assert not source_drift.projection_current

    WorkGraphService(connection, realm).update_details(
        work.id,
        summary="Canonical DB revision drift",
        now=NOW + dt.timedelta(seconds=3),
    )
    database_drift = service.detect(now=NOW + dt.timedelta(seconds=3))
    assert database_drift.database_revision_digest != source_drift.database_revision_digest
    assert database_drift.projection_source_digest != source_drift.projection_source_digest
    assert not database_drift.projection_current


def test_shadow_bootstrap_fails_closed_on_required_event_conflict(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "memory-upgrade-conflict-source"
    source.mkdir()
    integration = ProjectIntegrationService(connection, realm)
    project = integration.register(source_path=source, now=NOW)
    integration.scan(project.id, now=NOW)
    work = WorkGraphService(connection, realm).create_item(
        project_id=project.id,
        type=WorkType.TASK,
        title="Memory shadow conflict",
        now=NOW,
    )
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="memory-conflict-reviewer", now=NOW)
    )
    _install_unrelated_custom_hook(
        connection,
        realm.id,
        event_type=HookEventType.PRE_TASK,
    )
    repository = PostgresMemoryUpgradeRepository(
        connection,
        realm.id,
        project_id=project.id,
        work_item_id=work.id,
    )
    authorizations = AuthorizationRepository(connection, realm.id)
    service = MemoryUpgradeService(repository, authorizations)
    plan = service.check_plan(
        target=UpgradeTarget.SHADOW,
        rollback_ref="rollback/memory-shadow-conflict",
        rollback_digest=digest("rollback-conflict"),
        now=NOW,
    )
    authorization = _authorization(
        repository,
        authorizations,
        actor_id=actor.id,
        work_item_id=work.id,
        plan=plan,
    )

    with pytest.raises(PolicyViolation, match="handler conflict"):
        service.apply(plan, authorization_id=authorization.id, now=NOW)

    with connection.cursor() as cursor:
        cursor.execute(
            "select state from security.authorization where realm_id=%s and id=%s",
            (realm.id, authorization.id),
        )
        assert cursor.fetchone()[0] == "issued"
        cursor.execute(
            "select count(*) from continuity.projection_generation_receipt"
            " where realm_id=%s and work_item_id=%s",
            (realm.id, work.id),
        )
        assert cursor.fetchone()[0] == 0
