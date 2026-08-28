from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import Error as PsycopgError

from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.execution_run import ExecutionRun
from zekam.domain.realm import Realm
from zekam.domain.work import EffectKind, EvidenceRef, PlanStep, WorkState, WorkType
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import RealmRepository
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
from zekam.infrastructure.postgres.work_repository import WorkItemRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


class _RollbackProbe(Exception):
    """Force a successful concurrency probe transaction to roll back."""


def _projection_lock_scope(
    connection: Any,
    realm: Realm,
    source: Path,
    *,
    label: str,
) -> tuple[Any, Any, Any, Any, Any]:
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(
        source_path=source,
        now=dt.datetime.now(dt.UTC),
    )
    graph = WorkGraphService(connection, realm)
    work = graph.create_item(
        project_id=project.id,
        type=WorkType.TASK,
        title=f"Projection close lock {label}",
        now=dt.datetime.now(dt.UTC),
    )
    policy_digest = digest(f"projection-close-lock-policy-{label}")
    plan = graph.create_plan(
        work.id,
        source_revision=f"git/projection-close-lock-{label}",
        policy_digest=policy_digest,
        steps=(PlanStep("close", "Close", EffectKind.DATABASE_WRITE),),
        now=dt.datetime.now(dt.UTC),
    )
    run = ExecutionRun.create(
        realm_id=realm.id,
        project_id=project.id,
        work_item_id=work.id,
        plan_id=plan.id,
        client_id="codex",
        session_id=f"session/projection-close-lock-{label}",
        source_revision=plan.source_revision,
        policy_digest=policy_digest,
        max_input_tokens=100,
        max_output_tokens=100,
        max_cost_micros=1000,
        deadline=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        created_at=dt.datetime.now(dt.UTC),
    )
    runs = ExecutionRunRepository(connection, realm.id)
    runs.create_run(run)
    runs.activate_run(run.id, started_at=dt.datetime.now(dt.UTC))
    job_id, attempt_id = uuid4(), uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,priority,"
            " attempt_count,max_attempts,fencing_token,idempotency_key,run_id)"
            " values (%s,%s,%s,%s,%s,'close','mutation','running',100,1,1,1,%s,%s)",
            (job_id, realm.id, project.id, work.id, plan.id, f"job-{job_id}", run.id),
        )
        cursor.execute(
            "insert into runtime.job_attempt"
            " (id,realm_id,job_id,attempt_number,fencing_token,worker_label,started_at)"
            " values (%s,%s,%s,1,1,'projection-close-worker',statement_timestamp())",
            (attempt_id, realm.id, job_id),
        )
    return project.id, work.id, run.id, job_id, attempt_id


def test_projection_closure_lock_is_work_scoped_and_blocks_target_rows_and_migration_writer(
    migrated_database,
    tmp_path,
) -> None:
    """Distinct Work scopes proceed while same-scope rows and the migration head stay stable."""

    realm = Realm.create(slug="projection-close-lock", display_name="Projection close lock")
    with connect(migrated_database) as setup:
        configure_session(setup, realm_id=realm.id, role=None)
        RealmRepository(setup).create(realm)
        configure_session(setup, realm_id=realm.id)
        scope_a = _projection_lock_scope(
            setup, realm, tmp_path / "projection-close-lock-source-a", label="a"
        )
        scope_b = _projection_lock_scope(
            setup, realm, tmp_path / "projection-close-lock-source-b", label="b"
        )

    with connect(migrated_database) as closer:
        configure_session(closer, realm_id=realm.id)
        with closer.transaction(), closer.cursor() as cursor:
            cursor.execute(
                "select continuity.lock_projection_closure_scope("
                "%s,%s,%s,%s,%s,%s),"
                " statement_timestamp(),clock_timestamp()",
                (realm.id, *scope_a),
            )
            locked_at, statement_now, wall_now = cursor.fetchone()
            assert locked_at == statement_now
            assert locked_at <= wall_now
            cursor.execute(
                "with expected(key) as (select hashtextextended("
                "%s::text||':'||%s::text||':'||%s::text||':'||%s::text,0))"
                " select count(*) from pg_locks held cross join expected"
                " where held.pid=pg_backend_pid() and held.locktype='advisory'"
                " and held.mode='ExclusiveLock' and held.granted and held.objsubid=1"
                " and held.classid::bigint=((expected.key>>32)&4294967295)"
                " and held.objid::bigint=(expected.key&4294967295)",
                (realm.id, scope_a[0], scope_a[1], scope_a[2]),
            )
            assert cursor.fetchone() == (1,)

            with connect(migrated_database) as different_scope:
                configure_session(different_scope, realm_id=realm.id, role=None)
                with pytest.raises(_RollbackProbe):
                    with different_scope.transaction(), different_scope.cursor() as other:
                        other.execute("set local lock_timeout='500ms'")
                        other.execute(
                            "select continuity.lock_projection_closure_scope("
                            "%s,%s,%s,%s,%s,%s)",
                            (realm.id, *scope_b),
                        )
                        other.execute(
                            "update work.work_item set title=title"
                            " where realm_id=%s and id=%s",
                            (realm.id, scope_b[1]),
                        )
                        assert other.rowcount == 1
                        other.execute(
                            "update projects.source_binding set updated_at=updated_at"
                            " where realm_id=%s and project_id=%s",
                            (realm.id, scope_b[0]),
                        )
                        assert other.rowcount == 1
                        other.execute(
                            "update runtime.job set priority=priority"
                            " where realm_id=%s and id=%s",
                            (realm.id, scope_b[3]),
                        )
                        assert other.rowcount == 1
                        raise _RollbackProbe

            with connect(migrated_database) as broad_probe:
                configure_session(broad_probe, realm_id=realm.id, role=None)
                with broad_probe.transaction(), broad_probe.cursor() as other:
                    other.execute("set local lock_timeout='500ms'")
                    other.execute(
                        "lock table projects.source_revision,"
                        " continuity.projection_generation_receipt in row exclusive mode"
                    )

            with connect(migrated_database) as targeted_writer:
                configure_session(targeted_writer, realm_id=realm.id, role=None)
                with pytest.raises(PsycopgError) as blocked_row:
                    with targeted_writer.transaction(), targeted_writer.cursor() as other:
                        other.execute("set local lock_timeout='100ms'")
                        other.execute(
                            "update work.work_item set title=title where realm_id=%s and id=%s",
                            (realm.id, scope_a[1]),
                        )
                assert blocked_row.value.sqlstate == "55P03"

            with connect(migrated_database) as same_scope:
                configure_session(same_scope, realm_id=realm.id)
                with pytest.raises(PsycopgError) as blocked_scope:
                    with same_scope.transaction(), same_scope.cursor() as other:
                        other.execute("set local lock_timeout='100ms'")
                        other.execute(
                            "select continuity.lock_projection_closure_scope("
                            "%s,%s,%s,%s,%s,%s)",
                            (realm.id, *scope_a),
                        )
                assert blocked_scope.value.sqlstate == "55P03"

            with connect(migrated_database) as migration_writer:
                configure_session(migration_writer, realm_id=realm.id, role=None)
                with pytest.raises(PsycopgError) as blocked_migration:
                    with migration_writer.transaction(), migration_writer.cursor() as other:
                        other.execute("set local lock_timeout='100ms'")
                        other.execute(
                            "lock table core.schema_migrations in row exclusive mode"
                        )
                assert blocked_migration.value.sqlstate == "55P03"


def test_closure_lock_helper_rejects_unbound_runtime_identity(
    migrated_database,
    tmp_path,
) -> None:
    realm = Realm.create(slug="projection-lock-unbound", display_name="Lock unbound")
    with connect(migrated_database) as setup:
        configure_session(setup, realm_id=realm.id, role=None)
        RealmRepository(setup).create(realm)
        configure_session(setup, realm_id=realm.id)
        source = tmp_path / "projection-lock-unbound-source"
        source.mkdir()
        project = ProjectIntegrationService(setup, realm).register(source_path=source)
        work = WorkGraphService(setup, realm).create_item(
            project_id=project.id,
            type=WorkType.TASK,
            title="Projection lock unbound",
        )
        with pytest.raises(PsycopgError):
            with setup.transaction(), setup.cursor() as cursor:
                cursor.execute(
                    "select continuity.lock_projection_closure_scope("
                    "%s,%s,%s,gen_random_uuid(),gen_random_uuid(),gen_random_uuid())",
                    (realm.id, project.id, work.id),
                )


def test_application_role_cannot_directly_lock_append_only_closure_tables(
    migrated_database,
) -> None:
    realm = Realm.create(slug="projection-lock-privilege", display_name="Lock privilege")
    with connect(migrated_database) as setup:
        configure_session(setup, realm_id=realm.id, role=None)
        RealmRepository(setup).create(realm)
    with connect(migrated_database) as connection:
        configure_session(connection, realm_id=realm.id)
        with pytest.raises(PsycopgError):
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("lock table work.task_plan in share mode")


@pytest.mark.parametrize("work_type", [WorkType.MAINTENANCE, WorkType.TASK])
def test_raw_completed_transition_is_rejected_without_same_transaction_admission(
    migrated_database,
    tmp_path,
    work_type: WorkType,
) -> None:
    realm = Realm.create(
        slug=f"raw-completed-{work_type.value}",
        display_name=f"Raw completed {work_type.value}",
    )
    with connect(migrated_database) as setup:
        configure_session(setup, realm_id=realm.id, role=None)
        RealmRepository(setup).create(realm)
        configure_session(setup, realm_id=realm.id)
        source = tmp_path / "raw-completed-source"
        source.mkdir()
        project = ProjectIntegrationService(setup, realm).register(
            source_path=source,
            now=dt.datetime.now(dt.UTC),
        )
        graph = WorkGraphService(setup, realm)
        work = graph.create_item(
            project_id=project.id,
            type=work_type,
            title="Raw completed guard",
            now=dt.datetime.now(dt.UTC),
        )
        plan = graph.create_plan(
            work.id,
            source_revision="git/raw-completed-guard",
            policy_digest=digest("raw-completed-policy"),
            steps=(PlanStep("verify", "Verify", EffectKind.NONE),),
            now=dt.datetime.now(dt.UTC),
        )
        graph.transition(work.id, WorkState.READY, now=dt.datetime.now(dt.UTC))
        graph.transition(work.id, WorkState.ACTIVE, now=dt.datetime.now(dt.UTC))
        current = graph.transition(
            work.id,
            WorkState.VERIFICATION,
            now=dt.datetime.now(dt.UTC),
        )
        if work_type is WorkType.TASK:
            run = ExecutionRun.create(
                realm_id=realm.id,
                project_id=project.id,
                work_item_id=work.id,
                plan_id=plan.id,
                client_id="codex",
                session_id="session/raw-completed-guard",
                source_revision=plan.source_revision,
                policy_digest=plan.policy_digest,
                max_input_tokens=100,
                max_output_tokens=100,
                max_cost_micros=1000,
                deadline=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
                created_at=dt.datetime.now(dt.UTC),
            )
            ExecutionRunRepository(setup, realm.id).create_run(run)
        completed = current.with_state(
            WorkState.COMPLETED,
            evidence=(EvidenceRef(kind="test", reference="raw-completion-guard"),),
            now=dt.datetime.now(dt.UTC),
        )
        with pytest.raises(PsycopgError) as rejected:
            with setup.transaction():
                WorkItemRepository(setup, realm.id).replace(
                    completed,
                    expected_revision=current.revision,
                )
        assert rejected.value.sqlstate == "42501"
        assert rejected.value.diag.message_primary == (
            "raw completed transition lacks exact completion admission"
        )
