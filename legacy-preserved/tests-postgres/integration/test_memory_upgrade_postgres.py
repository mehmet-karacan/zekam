from __future__ import annotations

import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import Error as PsycopgError

from zekam.application.continuity_projection import ACTIVE_WORK_PROJECTION_REF
from zekam.application.hook_runtime import HookRuntime, LoadedHookAdapter
from zekam.application.memory_hooks import (
    MEMORY_HOOK_EVENTS,
    MEMORY_HOOK_REVISION,
    memory_hook_bundle,
)
from zekam.application.memory_observability import MemoryDimensionStatus
from zekam.application.memory_upgrade import (
    FEATURE_RESOURCE,
    MemoryUpgradeService,
    UpgradeTarget,
)
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import canonical_json, digest
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
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.hook_runtime_repository import HookRuntimeRepository
from zekam.infrastructure.postgres.memory_hook_installer import PostgresMemoryHookInstaller
from zekam.infrastructure.postgres.memory_observability_repository import (
    PostgresMemoryHealthReader,
)
from zekam.infrastructure.postgres.memory_upgrade_repository import (
    PostgresMemoryUpgradeRepository,
)
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 26, 12, tzinfo=dt.UTC)
_DIGEST = "sha256:" + "0" * 64


def _insert_projection_admission_audit(connection: Any, realm_id: UUID) -> UUID:
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("set local session_replication_role='replica'")
        cursor.execute(
            "insert into work.completion_admission"
            " (id,realm_id,project_id,work_item_id,mode,expected_work_revision,"
            " expected_work_record_digest,plan_id,plan_digest,job_id,attempt_id,claim_id,"
            " authorization_id,run_id,close_receipt_id,projection_receipt_id,"
            " pre_close_outbox_id,checkpoint_id,effect_receipt_id,operation,admission_body,"
            " admission_digest,admitted_at) values"
            " (gen_random_uuid(),%s,gen_random_uuid(),gen_random_uuid(),'projection-aware',2,"
            " %s,gen_random_uuid(),%s,gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),"
            " gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),"
            " gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),'projection-aware-close',"
            " '{}'::jsonb,%s,statement_timestamp()) returning id",
            (realm_id, _DIGEST, _DIGEST, _DIGEST),
        )
        return cursor.fetchone()[0]


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
            "select distinct spec.revision"
            " from hooks.current_generation current_set"
            " join hooks.compiled_set_entry entry on entry.realm_id=current_set.realm_id"
            " and entry.compiled_set_id=current_set.compiled_set_id"
            " join hooks.spec_revision spec on spec.realm_id=entry.realm_id"
            " and spec.id=entry.spec_revision_id"
            " where current_set.realm_id=%s and spec.source_layer='memory-continuity'",
            (realm.id,),
        )
        assert {int(row[0]) for row in cursor.fetchall()} == {MEMORY_HOOK_REVISION}
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
            (realm.id, project.id, work.id, ACTIVE_WORK_PROJECTION_REF),
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


def test_concurrent_clean_realm_hook_install_is_single_generation_and_replay_safe(
    realm_session: tuple[Any, Any],
    migrated_database: Any,
) -> None:
    realm, connection = realm_session
    barrier = threading.Barrier(2)

    def install() -> Any:
        with connect(migrated_database) as worker:
            configure_session(worker, realm_id=realm.id)
            barrier.wait(timeout=10)
            return PostgresMemoryHookInstaller(worker, realm.id).ensure(installed_at=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(install), pool.submit(install))
        receipts = tuple(future.result(timeout=30) for future in futures)

    assert sorted(item.created for item in receipts) == [False, True]
    assert {item.generation for item in receipts} == {1}
    assert len({item.hook_set_digest for item in receipts}) == 1
    assert len({item.bundle_digest for item in receipts}) == 1

    with connection.cursor() as cursor:
        cursor.execute(
            "select generation,hook_set_digest from hooks.current_generation where realm_id=%s",
            (realm.id,),
        )
        assert cursor.fetchone() == (1, receipts[0].hook_set_digest)
        cursor.execute(
            "select"
            " (select count(*) from hooks.current_generation where realm_id=%s),"
            " (select count(*) from hooks.compiled_set where realm_id=%s),"
            " (select count(*) from hooks.spec_revision where realm_id=%s"
            "   and source_layer='memory-continuity' and revision=%s),"
            " (select count(*) from hooks.runtime_revision runtime"
            "   join hooks.spec_revision spec on spec.realm_id=runtime.realm_id"
            "   and spec.hook_id=runtime.hook_id and spec.revision=runtime.hook_revision"
            "   where runtime.realm_id=%s and spec.source_layer='memory-continuity'),"
            " (select count(*) from hooks.compiled_set_entry entry"
            "   join hooks.current_generation current_set"
            "   on current_set.realm_id=entry.realm_id"
            "   and current_set.compiled_set_id=entry.compiled_set_id"
            "   where entry.realm_id=%s),"
            " (select count(*) from security.permission_profile_revision"
            "   where realm_id=%s and name='memory-continuity-internal')",
            (
                realm.id,
                realm.id,
                realm.id,
                MEMORY_HOOK_REVISION,
                realm.id,
                realm.id,
                realm.id,
            ),
        )
        assert tuple(int(value) for value in cursor.fetchone()) == (
            1,
            1,
            len(MEMORY_HOOK_EVENTS),
            len(MEMORY_HOOK_EVENTS),
            len(MEMORY_HOOK_EVENTS),
            1,
        )


def test_memory_hook_bundle_uses_database_supported_schema_subset(
    realm_session: tuple[Any, Any],
) -> None:
    realm, connection = realm_session
    bundle = memory_hook_bundle(realm.id)

    with connection.cursor() as cursor:
        for spec in bundle.specs:
            cursor.execute(
                "select hooks.json_schema_supported(%s::jsonb),"
                " hooks.json_schema_supported(%s::jsonb)",
                (canonical_json(spec.input_schema), canonical_json(spec.output_schema)),
            )
            assert cursor.fetchone() == (True, True)


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


def test_0057_completion_admission_is_cross_realm_scoped_and_append_only(
    isolated_migrated_database: Any,
) -> None:
    """The immutable admission ledger never crosses the configured realm boundary."""

    realm_a, realm_b = uuid4(), uuid4()
    with connect(isolated_migrated_database) as owner:
        row_a = _insert_projection_admission_audit(owner, realm_a)
        row_b = _insert_projection_admission_audit(owner, realm_b)

    with connect(isolated_migrated_database) as realm_a_session:
        configure_session(realm_a_session, realm_id=realm_a)
        with realm_a_session.cursor() as cursor:
            cursor.execute(
                "select has_table_privilege(current_user,'work.completion_admission','select'),"
                " has_table_privilege(current_user,'work.completion_admission','insert'),"
                " has_table_privilege(current_user,'work.completion_admission','update'),"
                " has_table_privilege(current_user,'work.completion_admission','delete'),"
                " core.current_realm_id()"
            )
            assert cursor.fetchone() == (True, False, False, False, realm_a)
            cursor.execute("select id from work.completion_admission order by id")
            assert cursor.fetchall() == [(row_a,)]
        with (
            pytest.raises(PsycopgError) as update_denied,
            realm_a_session.transaction(),
            realm_a_session.cursor() as cursor,
        ):
            cursor.execute(
                "update work.completion_admission"
                " set admission_digest=admission_digest"
                " where id=%s",
                (row_a,),
            )
        assert update_denied.value.sqlstate == "42501"
        assert update_denied.value.diag.message_primary == (
            "permission denied for table completion_admission"
        )
        with (
            pytest.raises(PsycopgError) as delete_denied,
            realm_a_session.transaction(),
            realm_a_session.cursor() as cursor,
        ):
            cursor.execute("delete from work.completion_admission where id=%s", (row_a,))
        assert delete_denied.value.sqlstate == "42501"
        assert delete_denied.value.diag.message_primary == (
            "permission denied for table completion_admission"
        )

    with connect(isolated_migrated_database) as realm_b_session:
        configure_session(realm_b_session, realm_id=realm_b)
        with realm_b_session.cursor() as cursor:
            cursor.execute("select id from work.completion_admission order by id")
            assert cursor.fetchall() == [(row_b,)]

    with connect(isolated_migrated_database) as owner:
        configure_session(owner, realm_id=realm_a, role=None)
        with (
            pytest.raises(PsycopgError) as update_denied,
            owner.transaction(),
            owner.cursor() as cursor,
        ):
            cursor.execute(
                "update work.completion_admission set admission_digest=admission_digest"
                " where id=%s",
                (row_a,),
            )
        assert update_denied.value.sqlstate == "42501"
        assert update_denied.value.diag.message_primary == (
            "completion admission append-only contract"
        )
        with (
            pytest.raises(PsycopgError) as delete_denied,
            owner.transaction(),
            owner.cursor() as cursor,
        ):
            cursor.execute("delete from work.completion_admission where id=%s", (row_a,))
        assert delete_denied.value.sqlstate == "42501"
        assert delete_denied.value.diag.message_primary == (
            "append-only tablo: DELETE islemi reddedildi (work.completion_admission)"
        )
