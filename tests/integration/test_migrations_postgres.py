"""Gercek PostgreSQL uzerinde migration upgrade, head ve drift davranisi."""

from __future__ import annotations

import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import Error as PsycopgError
from psycopg.errors import InvalidParameterValue

from zekam.application.config import DatabaseSettings
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.domain.realm import Realm
from zekam.infrastructure.postgres import migrations
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import RealmRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

_DIGEST = "sha256:" + "0" * 64
_PROJECTION_ADMISSION_SIGNATURE = (
    "work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,"
    "uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text)"
)
_CONTROL_ADMISSION_SIGNATURE = (
    "work.admit_control_plane_completion(uuid,uuid,uuid,integer,text,uuid,text,"
    "uuid,uuid,uuid,uuid,uuid,uuid,text,uuid,text,uuid,text,uuid,text,text,text,"
    "text,text,text[],text[],text[],jsonb,text,text)"
)
_PROJECTION_LOCK_SIGNATURE = (
    "continuity.lock_projection_closure_scope(uuid,uuid,uuid,uuid,uuid,uuid)"
)
_CONTROL_LOCK_SIGNATURE = "work.lock_control_plane_completion_scope(uuid,uuid,uuid,uuid,uuid,uuid)"
_CODEX_LOCK_SIGNATURE = "client.lock_codex_lifecycle_scope(uuid,uuid,uuid,uuid)"
_CODEX_VALIDATOR_SIGNATURE = "client.enforce_codex_lifecycle_admission()"
_PLAN_ORDER_SIGNATURE = "work.task_plan_execution_order(jsonb)"


def _logical_catalog_snapshot(
    connection: Any,
    *,
    relations: tuple[str, ...],
    functions: tuple[str, ...],
    triggers: tuple[str, ...],
) -> dict[str, tuple[Any, ...]]:
    """Return OID-free catalogue evidence suitable for drop/recreate parity."""

    with connection.cursor() as cursor:
        cursor.execute(
            "select n.nspname||'.'||c.relname,c.relkind,c.relrowsecurity,"
            " c.relforcerowsecurity,coalesce(c.relacl::text,'')"
            " from pg_class c join pg_namespace n on n.oid=c.relnamespace"
            " where n.nspname||'.'||c.relname=any(%s) order by 1",
            (list(relations),),
        )
        relation_rows = tuple(cursor.fetchall())
        cursor.execute(
            "select n.nspname||'.'||p.proname,pg_get_function_identity_arguments(p.oid),"
            " p.pronargs,p.prosecdef,coalesce(p.proconfig::text,''),coalesce(p.proacl::text,''),"
            " regexp_replace(pg_get_functiondef(p.oid),'[[:space:]]+',' ','g'),"
            " regexp_replace(p.prosrc,'[[:space:]]+',' ','g')"
            " from pg_proc p join pg_namespace n on n.oid=p.pronamespace"
            " where n.nspname||'.'||p.proname=any(%s) order by 1,2",
            (list(functions),),
        )
        function_rows = tuple(cursor.fetchall())
        cursor.execute(
            "select n.nspname||'.'||c.relname,t.tgname,t.tgdeferrable,t.tginitdeferred,"
            " pg_get_triggerdef(t.oid) from pg_trigger t"
            " join pg_class c on c.oid=t.tgrelid"
            " join pg_namespace n on n.oid=c.relnamespace"
            " where not t.tgisinternal and t.tgname=any(%s) order by 1,2",
            (list(triggers),),
        )
        trigger_rows = tuple(cursor.fetchall())
        cursor.execute(
            "select schemaname||'.'||tablename,policyname,cmd,roles::text,"
            " coalesce(qual::text,''),coalesce(with_check::text,'') from pg_policies"
            " where schemaname||'.'||tablename=any(%s) order by 1,2",
            (list(relations),),
        )
        policy_rows = tuple(cursor.fetchall())
        cursor.execute(
            "select n.nspname||'.'||c.relname,con.conname,con.contype,"
            " pg_get_constraintdef(con.oid) from pg_constraint con"
            " join pg_class c on c.oid=con.conrelid"
            " join pg_namespace n on n.oid=c.relnamespace"
            " where n.nspname||'.'||c.relname=any(%s) order by 1,2",
            (list(relations),),
        )
        constraint_rows = tuple(cursor.fetchall())
        cursor.execute(
            "select n.nspname||'.'||c.relname,a.attnum,a.attname,"
            " format_type(a.atttypid,a.atttypmod),a.attnotnull,"
            " coalesce(pg_get_expr(default_value.adbin,default_value.adrelid),'')"
            " from pg_attribute a join pg_class c on c.oid=a.attrelid"
            " join pg_namespace n on n.oid=c.relnamespace"
            " left join pg_attrdef default_value on default_value.adrelid=a.attrelid"
            " and default_value.adnum=a.attnum"
            " where n.nspname||'.'||c.relname=any(%s)"
            " and a.attnum>0 and not a.attisdropped order by 1,2",
            (list(relations),),
        )
        column_rows = tuple(cursor.fetchall())
        cursor.execute(
            "select n.nspname||'.'||table_relation.relname,index_relation.relname,"
            " regexp_replace(pg_get_indexdef(indexes.indexrelid),'[[:space:]]+',' ','g')"
            " from pg_index indexes"
            " join pg_class table_relation on table_relation.oid=indexes.indrelid"
            " join pg_namespace n on n.oid=table_relation.relnamespace"
            " join pg_class index_relation on index_relation.oid=indexes.indexrelid"
            " where n.nspname||'.'||table_relation.relname=any(%s) order by 1,2",
            (list(relations),),
        )
        index_rows = tuple(cursor.fetchall())
    return {
        "relations": relation_rows,
        "functions": function_rows,
        "triggers": trigger_rows,
        "policies": policy_rows,
        "constraints": constraint_rows,
        "columns": column_rows,
        "indexes": index_rows,
    }


def _non_owner_execute_acl(
    connection: Any,
    signatures: tuple[str, ...],
) -> dict[str, set[tuple[str, bool]]]:
    """Resolve explicit/default ACLs so a NULL proacl cannot hide PUBLIC EXECUTE."""

    grants = {signature: set() for signature in signatures}
    with connection.cursor() as cursor:
        cursor.execute(
            "with requested(signature) as (select unnest(%s::text[]))"
            " select requested.signature,coalesce(grantee.rolname,'PUBLIC'),"
            " acl_entry.is_grantable"
            " from requested join pg_proc function_row"
            " on function_row.oid=to_regprocedure(requested.signature)"
            " cross join lateral aclexplode(coalesce(function_row.proacl,"
            " acldefault('f',function_row.proowner))) acl_entry"
            " left join pg_roles grantee on grantee.oid=acl_entry.grantee"
            " where acl_entry.privilege_type='EXECUTE'"
            " and acl_entry.grantee<>function_row.proowner order by 1,2",
            (list(signatures),),
        )
        for signature, grantee, is_grantable in cursor.fetchall():
            grants[str(signature)].add((str(grantee), bool(is_grantable)))
    return grants


def _non_owner_table_acl(
    connection: Any,
    relations: tuple[str, ...],
) -> dict[str, set[tuple[str, str, bool]]]:
    grants = {relation: set() for relation in relations}
    with connection.cursor() as cursor:
        cursor.execute(
            "with requested(relation_name) as (select unnest(%s::text[]))"
            " select requested.relation_name,coalesce(grantee.rolname,'PUBLIC'),"
            " acl_entry.privilege_type,acl_entry.is_grantable"
            " from requested join pg_class relation_row"
            " on relation_row.oid=to_regclass(requested.relation_name)"
            " cross join lateral aclexplode(coalesce(relation_row.relacl,"
            " acldefault('r',relation_row.relowner))) acl_entry"
            " left join pg_roles grantee on grantee.oid=acl_entry.grantee"
            " where acl_entry.grantee<>relation_row.relowner order by 1,2,3",
            (list(relations),),
        )
        for relation, grantee, privilege_type, is_grantable in cursor.fetchall():
            grants[str(relation)].add((str(grantee), str(privilege_type), bool(is_grantable)))
    return grants


def _non_owner_column_acl(
    connection: Any,
    relations: tuple[str, ...],
) -> dict[str, set[tuple[str, str, str, bool]]]:
    grants = {relation: set() for relation in relations}
    with connection.cursor() as cursor:
        cursor.execute(
            "with requested(relation_name) as (select unnest(%s::text[]))"
            " select requested.relation_name,column_row.attname,"
            " coalesce(grantee.rolname,'PUBLIC'),acl_entry.privilege_type,"
            " acl_entry.is_grantable"
            " from requested join pg_class relation_row"
            " on relation_row.oid=to_regclass(requested.relation_name)"
            " join pg_attribute column_row on column_row.attrelid=relation_row.oid"
            " cross join lateral aclexplode(coalesce(column_row.attacl,"
            " acldefault('c',relation_row.relowner)))"
            " acl_entry"
            " left join pg_roles grantee on grantee.oid=acl_entry.grantee"
            " where column_row.attnum>0 and not column_row.attisdropped"
            " and acl_entry.grantee<>relation_row.relowner order by 1,2,3,4",
            (list(relations),),
        )
        for relation, column, grantee, privilege_type, is_grantable in cursor.fetchall():
            grants[str(relation)].add(
                (
                    str(column),
                    str(grantee),
                    str(privilege_type),
                    bool(is_grantable),
                )
            )
    return grants


def _function_security_contract(
    connection: Any,
    signatures: tuple[str, ...],
) -> dict[str, tuple[str, int, bool, tuple[str, ...]]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "with requested(signature) as (select unnest(%s::text[]))"
            " select requested.signature,pg_get_function_identity_arguments(function_row.oid),"
            " function_row.pronargs,function_row.prosecdef,"
            " coalesce(function_row.proconfig,'{}'::text[])"
            " from requested join pg_proc function_row"
            " on function_row.oid=to_regprocedure(requested.signature) order by 1",
            (list(signatures),),
        )
        return {
            str(signature): (
                str(identity_arguments),
                int(argument_count),
                bool(security_definer),
                tuple(str(item) for item in configuration),
            )
            for (
                signature,
                identity_arguments,
                argument_count,
                security_definer,
                configuration,
            ) in cursor.fetchall()
        }


def _closure_catalog_snapshot(connection: Any) -> dict[str, tuple[Any, ...]]:
    return _logical_catalog_snapshot(
        connection,
        relations=("work.completion_admission",),
        functions=(
            "work.enforce_completion_admission_body",
            "work.enforce_completion_admission_update",
            "work.enforce_completed_admission",
            "work.reject_completed_insert",
            "work.task_plan_execution_order",
            "work.lock_control_plane_completion_scope",
            "work.admit_projection_completion",
            "work.admit_control_plane_completion",
            "continuity.enforce_exact_close_receipt",
            "continuity.enforce_hydration_authorization",
            "continuity.lock_projection_closure_scope",
            "runtime.reject_late_projection_close_claim",
        ),
        triggers=(
            "completion_admission_body_guard",
            "completion_admission_update_guard",
            "completion_admission_deny_delete",
            "close_exact_guard",
            "hydration_authorization_guard",
            "projection_close_claim_order_guard",
            "work_completed_admission_guard",
            "work_completed_insert_guard",
        ),
    )


def _codex_catalog_snapshot(connection: Any) -> dict[str, tuple[Any, ...]]:
    return _logical_catalog_snapshot(
        connection,
        relations=("client.codex_lifecycle_admission",),
        functions=(
            "client.lock_codex_lifecycle_scope",
            "client.enforce_codex_lifecycle_admission",
            "work.task_plan_execution_order",
        ),
        triggers=(
            "codex_lifecycle_admission_guard",
            "codex_lifecycle_admission_row_guard",
            "codex_lifecycle_admission_no_mutation",
        ),
    )


def _hook_revision_catalog_snapshot(connection: Any) -> dict[str, tuple[Any, ...]]:
    return _logical_catalog_snapshot(
        connection,
        relations=("hooks.spec_revision",),
        functions=("hooks.enforce_spec_revision",),
        triggers=("hook_spec_revision_guard",),
    )


def _insert_projection_admission_audit(connection: Any, realm_id: UUID) -> UUID:
    """Insert a CHECK-valid audit row while bypassing FK/user triggers in an isolated DB."""

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


def _insert_check_valid_codex_admission(cursor: Any, realm_id: UUID) -> UUID:
    cursor.execute(
        "insert into client.codex_lifecycle_admission"
        " (id,realm_id,lifecycle_event_id,entry_digest,continuity_event_id,"
        " delivery_outbox_id,hook_receipt_id,job_id,attempt_id,envelope_id,authorization_id,"
        " claim_id,effect_receipt_id,work_plan_digest,effect_plan_digest,effect_plan_body,"
        " effect_digest,source_digest,policy_digest,migration_digest,envelope_digest,"
        " terminal_hook_receipt_digest,result_formula_digest,binding_digest,created_at)"
        " values (gen_random_uuid(),%s,gen_random_uuid(),%s,gen_random_uuid(),"
        " gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),"
        " gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),gen_random_uuid(),%s,%s,"
        " '{}'::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,statement_timestamp()) returning id",
        (realm_id,) + (_DIGEST,) * 11,
    )
    return cursor.fetchone()[0]


def _insert_codex_admission_audit(connection: Any, realm_id: UUID) -> UUID:
    """Insert a CHECK-valid 0058 audit row without fabricating governed runtime evidence."""

    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("set local session_replication_role='replica'")
        return _insert_check_valid_codex_admission(cursor, realm_id)


def _insert_hydration_compat_audit(connection: Any) -> UUID:
    """Insert one CHECK-valid 0060 audit row without fabricating its FK graph."""

    row_id = uuid4()
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("set local session_replication_role='replica'")
        cursor.execute(
            "insert into continuity.lifecycle_hydration_admission"
            " (id,realm_id,codex_admission_id,continuity_event_id,delivery_outbox_id,"
            " hydration_receipt_id,hydration_authorization_id,hydration_plan_digest,"
            " hydration_effect_digest,hydration_apply_result_digest,hydration_created,"
            " hydration_applied_at,binding_digest,created_at,grants_authority) values"
            " (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,statement_timestamp(),%s,"
            " statement_timestamp(),false)",
            (row_id,) + tuple(uuid4() for _ in range(6)) + (_DIGEST,) * 4,
        )
    return row_id


def _insert_hook_revision_bootstrap_audit(connection: Any) -> UUID:
    """Insert CHECK-valid revision-2 history while bypassing FK and user triggers."""

    row_id = uuid4()
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("set local session_replication_role='replica'")
        cursor.execute(
            "insert into hooks.spec_revision"
            " (id,realm_id,hook_id,revision,event_type,required,source_layer,timeout_ms,"
            " execution_mode,input_schema,output_schema,input_schema_digest,"
            " output_schema_digest,permission_profile_revision_id,permission_profile_name,"
            " permission_profile_digest,failure_policy,created_at,hook_digest,hook_body,"
            " grants_authority) values"
            " (%s,%s,%s,2,'turn.start',false,'migration-test',1000,'internal',"
            " '{}'::jsonb,'{}'::jsonb,%s,%s,%s,%s,%s,'abort',statement_timestamp(),"
            " %s,'{}'::jsonb,false)",
            (
                row_id,
                uuid4(),
                "bootstrap-audit",
                _DIGEST,
                _DIGEST,
                uuid4(),
                "migration-audit",
                _DIGEST,
                _DIGEST,
            ),
        )
    return row_id


@pytest.fixture
def blank_database(postgres_settings: DatabaseSettings):  # type: ignore[no-untyped-def]
    """Bos, migration uygulanmamis gecici veritabani."""
    name = f"zekam_mig_{secrets.token_hex(6)}"
    with connect(postgres_settings) as connection, connection.cursor() as cursor:
        cursor.execute(f'create database "{name}"')
    scoped = DatabaseSettings(
        host=postgres_settings.host,
        port=postgres_settings.port,
        name=name,
        user=postgres_settings.user,
        sslmode=postgres_settings.sslmode,
    )
    try:
        yield scoped
    finally:
        with connect(postgres_settings) as connection, connection.cursor() as cursor:
            cursor.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (name,),
            )
            cursor.execute(f'drop database if exists "{name}"')


def test_clean_upgrade_applies_every_migration(blank_database: DatabaseSettings) -> None:
    available = migrations.discover_migrations()
    with connect(blank_database) as connection:
        applied = migrations.upgrade(connection)
        current = migrations.status(connection)
    assert [result.version for result in applied] == [m.version for m in available]
    assert current.head == available[-1].version
    assert current.pending == ()
    assert current.drift == ()
    assert current.is_current


def test_upgrade_is_idempotent(blank_database: DatabaseSettings) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection)
        second = migrations.upgrade(connection)
        current = migrations.status(connection)
    assert second == ()
    assert current.is_current


def _rollback_targets() -> list[int]:
    """Son uc migration'in surum numarasi.

    Sabit numara yazmak her yeni migration'da bu testleri kirdigi icin hedefler
    kesif sonucundan turetilir.
    """

    versions = [item.version for item in migrations.discover_migrations()]
    return versions[-3:]


@pytest.mark.parametrize("version", _rollback_targets())
def test_down_marks_pending_and_can_reapply(blank_database: DatabaseSettings, version: int) -> None:
    available = migrations.discover_migrations()
    later = [item.version for item in available if item.version >= version]
    with connect(blank_database) as connection:
        # Yalniz hedefe kadar yukselt: daha yuksek numarali migration uygulanmisken
        # aradan birini geri almak out-of-order duruma yol acar.
        migrations.upgrade(connection, target=version)
        rolled_back_result = migrations.downgrade(connection, target=version)
        assert rolled_back_result.version == version
        rolled_back = migrations.status(connection)
        assert rolled_back.head == version - 1
        assert [item.version for item in rolled_back.pending] == later
        with pytest.raises(ValidationFailed, match="mevcut migration head"):
            migrations.downgrade(connection, target=version)
        reapplied = migrations.upgrade(connection)
        current = migrations.status(connection)
    assert [item.version for item in reapplied] == later
    assert current.is_current


def test_0057_failed_down_restores_force_rls_acl_and_head(
    blank_database: DatabaseSettings,
) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=57)
        audit_id = _insert_projection_admission_audit(connection, uuid4())
        before = _closure_catalog_snapshot(connection)
        with pytest.raises(
            PsycopgError,
            match="completion admission audit data exists",
        ) as refused:
            migrations.downgrade(connection, target=57)
        assert refused.value.sqlstate == "55000"
        assert refused.value.diag.message_primary == (
            "0057 rollback refused: completion admission audit data exists"
        )
        assert migrations.status(connection).head == 57
        assert _closure_catalog_snapshot(connection) == before
        with connection.cursor() as cursor:
            cursor.execute(
                "select relrowsecurity,relforcerowsecurity"
                " from pg_class where oid='work.completion_admission'::regclass"
            )
            assert cursor.fetchone() == (True, True)
            cursor.execute(
                "select count(*) from work.completion_admission where id=%s", (audit_id,)
            )
            assert cursor.fetchone() == (1,)


def test_0058_failed_down_restores_force_rls_acl_and_head(
    blank_database: DatabaseSettings,
) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=58)
        audit_id = _insert_codex_admission_audit(connection, uuid4())
        before = _codex_catalog_snapshot(connection)
        with pytest.raises(
            PsycopgError,
            match="Codex lifecycle admission audit data exists",
        ) as refused:
            migrations.downgrade(connection, target=58)
        assert refused.value.sqlstate == "55000"
        assert refused.value.diag.message_primary == (
            "0058 rollback refused: Codex lifecycle admission audit data exists"
        )
        assert migrations.status(connection).head == 58
        assert _codex_catalog_snapshot(connection) == before
        with connection.cursor() as cursor:
            cursor.execute(
                "select relrowsecurity,relforcerowsecurity"
                " from pg_class where oid='client.codex_lifecycle_admission'::regclass"
            )
            assert cursor.fetchone() == (True, True)
            cursor.execute(
                "select count(*) from client.codex_lifecycle_admission where id=%s", (audit_id,)
            )
            assert cursor.fetchone() == (1,)


def test_0059_failed_down_preserves_head_and_catalog_when_history_starts_above_one(
    blank_database: DatabaseSettings,
) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=59)
        audit_id = _insert_hook_revision_bootstrap_audit(connection)
        before = _hook_revision_catalog_snapshot(connection)
        with pytest.raises(
            PsycopgError,
            match="hook revision history is not 0051-compatible",
        ) as refused:
            migrations.downgrade(connection, target=59)
        assert refused.value.sqlstate == "55000"
        assert refused.value.diag.message_primary == (
            "0059 rollback refused: hook revision history is not 0051-compatible"
        )
        assert migrations.status(connection).head == 59
        assert _hook_revision_catalog_snapshot(connection) == before
        with connection.cursor() as cursor:
            cursor.execute(
                "select relrowsecurity,relforcerowsecurity"
                " from pg_class where oid='hooks.spec_revision'::regclass"
            )
            assert cursor.fetchone() == (True, True)
            cursor.execute("select count(*) from hooks.spec_revision where id=%s", (audit_id,))
            assert cursor.fetchone() == (1,)


def test_0059_exact_upgrade_down_reapply_catalog_parity(
    blank_database: DatabaseSettings,
) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=58)
        snapshot_58 = _hook_revision_catalog_snapshot(connection)

        assert [item.version for item in migrations.upgrade(connection, target=59)] == [59]
        assert migrations.status(connection).head == 59
        snapshot_59 = _hook_revision_catalog_snapshot(connection)
        assert snapshot_59 != snapshot_58

        assert migrations.downgrade(connection, target=59).version == 59
        assert migrations.status(connection).head == 58
        assert _hook_revision_catalog_snapshot(connection) == snapshot_58

        assert [item.version for item in migrations.upgrade(connection, target=59)] == [59]
        assert _hook_revision_catalog_snapshot(connection) == snapshot_59
        final_status = migrations.status(connection)
        assert final_status.head == 59
        assert final_status.drift == ()


def test_0061_projection_and_codex_guards_use_canonical_hydration(
    blank_database: DatabaseSettings,
) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=60)
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure)",
                (_PROJECTION_ADMISSION_SIGNATURE,),
            )
            projection_baseline = str(cursor.fetchone()[0])
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure)",
                (_CODEX_VALIDATOR_SIGNATURE,),
            )
            codex_baseline = str(cursor.fetchone()[0])

        assert [item.version for item in migrations.upgrade(connection, target=61)] == [61]
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure)",
                (_PROJECTION_ADMISSION_SIGNATURE,),
            )
            projection_revised = str(cursor.fetchone()[0])
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure)",
                (_CODEX_VALIDATOR_SIGNATURE,),
            )
            codex_revised = str(cursor.fetchone()[0])
            cursor.execute(
                "select trigger_.tgname,trigger_.tgdeferrable,trigger_.tginitdeferred"
                " from pg_trigger trigger_ where not trigger_.tgisinternal"
                " and trigger_.tgfoid=%s::regprocedure"
                " and trigger_.tgname in"
                " ('codex_lifecycle_admission_guard','codex_lifecycle_admission_row_guard')"
                " order by trigger_.tgname",
                (_CODEX_VALIDATOR_SIGNATURE,),
            )
            deferred_triggers = cursor.fetchall()
        assert projection_revised != projection_baseline
        assert "hydration.receipt_body->>'source_digest'=source_tree_digest_" in projection_revised
        assert (
            projection_revised.count("freshness_dimension->>'observed_digest'=source_tree_digest_")
            == 1
        )
        assert (
            projection_revised.count("freshness_dimension->>'expected_digest'=source_tree_digest_")
            == 1
        )
        assert "projection_ref->>'digest'=prior_projection.projection_digest" in projection_revised
        assert (
            "hydration_event.event_type in ('session_start','hydration_required')"
            in projection_revised
        )
        assert "continuity.lifecycle_hydration_admission admission" in projection_revised
        assert "projection_ref='projection/active-work'" in projection_revised
        assert "and ((hydration_event.event_type='hydration_required'" in projection_revised
        assert (
            projection_revised.count("and ((hydration_event.event_type='hydration_required'") == 2
        )
        outer_checkpoint_precursor = (
            "and hydration.receipt_body->>'checkpoint_ref' in\n"
            "      (checkpoint.checkpoint_key,'db:work.checkpoint/'||checkpoint.id::text,\n"
            "       'run:'||run.id::text||':genesis')"
        )
        assert projection_revised.count(outer_checkpoint_precursor) == 1
        assert (
            "and hydration.receipt_body->>'checkpoint_ref' in\n"
            "      (checkpoint.checkpoint_key,'db:work.checkpoint/'||checkpoint.id::text)"
            not in projection_revised
        )
        assert (
            projection_revised.count(
                "hydration.receipt_body->>'checkpoint_ref'=\n"
                "              'run:'||run.id::text||':genesis'"
            )
            == 1
        )
        assert projection_revised.count("'run:'||run.id::text||':genesis'") == 2
        assert (
            "and hydration_event.ingested_at<=hydration.created_at\n"
            "        and hydration_outbox.completed_at<=hydration.created_at"
            not in projection_revised
        )
        assert (
            "hydration.receipt_body->>'source_digest'=current_projection_source_digest_"
            not in projection_revised
        )
        assert (
            "freshness_dimension->>'observed_digest'=current_projection_source_digest_"
            not in projection_revised
        )
        assert (
            "freshness_dimension->>'expected_digest'=current_projection_source_digest_"
            not in projection_revised
        )
        assert codex_revised != codex_baseline
        assert "job.payload->>'schema'='zekam-codex-lifecycle-job/v1'" in codex_revised
        assert "job.payload->>'authorization_id'=admission.authorization_id::text" in codex_revised
        assert codex_revised.count("(select count(*) from jsonb_object_keys(job.payload))=3") == 1
        assert codex_revised.count("(select count(*) from jsonb_object_keys(job.payload))=2") == 1
        assert "job.payload ? 'hydration_authorization_id'" in codex_revised
        assert "continuity.lifecycle_hydration_admission hydration_admission" in codex_revised
        assert "hydration_admission.codex_admission_id=admission.id" in codex_revised
        assert "hydration_admission.continuity_event_id=continuity_event.id" in codex_revised
        assert "hydration_admission.delivery_outbox_id=outbox.id" in codex_revised
        assert "hydration_admission.hydration_authorization_id::text" in codex_revised
        assert "=job.payload->>'hydration_authorization_id'" in codex_revised
        assert "continuity_event.event_type<>'session_start'" in codex_revised
        assert "not (job.payload ? 'hydration_authorization_id')" in codex_revised
        assert deferred_triggers == [
            ("codex_lifecycle_admission_guard", True, True),
            ("codex_lifecycle_admission_row_guard", True, True),
        ]

        assert migrations.downgrade(connection, target=61).version == 61
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure)",
                (_PROJECTION_ADMISSION_SIGNATURE,),
            )
            assert str(cursor.fetchone()[0]) == projection_baseline
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure)",
                (_CODEX_VALIDATOR_SIGNATURE,),
            )
            assert str(cursor.fetchone()[0]) == codex_baseline
        assert [item.version for item in migrations.upgrade(connection, target=61)] == [61]
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure)",
                (_PROJECTION_ADMISSION_SIGNATURE,),
            )
            assert str(cursor.fetchone()[0]) == projection_revised
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure)",
                (_CODEX_VALIDATOR_SIGNATURE,),
            )
            assert str(cursor.fetchone()[0]) == codex_revised


def test_0061_codex_guard_still_rejects_bypass_at_deferred_commit(
    blank_database: DatabaseSettings,
) -> None:
    realm = Realm.create(slug=f"migration-{secrets.token_hex(6)}")
    stream_id, event_id = uuid4(), uuid4()
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=61)
        configure_session(connection, realm_id=realm.id, role=None)
        RealmRepository(connection).create(realm)
        configure_session(connection, realm_id=realm.id)

        with pytest.raises(PsycopgError) as rejected:  # noqa: SIM117
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "insert into client.lifecycle_stream"
                    " (id,realm_id,client_kind,client_instance_id,session_id,head_sequence,"
                    " head_digest,created_at,updated_at) values"
                    " (%s,%s,'codex','migration-codex','migration-session',0,null,"
                    " statement_timestamp(),statement_timestamp())",
                    (stream_id, realm.id),
                )
                cursor.execute(
                    "insert into client.lifecycle_event"
                    " (id,realm_id,stream_id,sequence,previous_digest,event_digest,payload,"
                    " occurred_at,ingested_at,grants_authority) values"
                    " (%s,%s,%s,1,null,%s,'{}'::jsonb,statement_timestamp(),"
                    " statement_timestamp(),false)",
                    (event_id, realm.id, stream_id, _DIGEST),
                )
                cursor.execute(
                    "select count(*) from client.lifecycle_event where realm_id=%s and id=%s",
                    (realm.id, event_id),
                )
                assert cursor.fetchone() == (1,)

        assert rejected.value.sqlstate == "23514"
        rejection_message = rejected.value.diag.message_primary or ""
        assert (
            rejection_message
            == "pre-compact exact active execution binding requires one run; found 0"
            or rejection_message
            == "Codex lifecycle generic ingest governed admission olmadan commit edilemez"
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from client.lifecycle_event where realm_id=%s and id=%s",
                (realm.id, event_id),
            )
            assert cursor.fetchone() == (0,)


def test_0061_down_refuses_lifecycle_hydration_audit_history(
    blank_database: DatabaseSettings,
) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=61)
        audit_id = _insert_hydration_compat_audit(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure)",
                (_PROJECTION_ADMISSION_SIGNATURE,),
            )
            before = str(cursor.fetchone()[0])
        with pytest.raises(
            PsycopgError,
            match="lifecycle hydration admission audit data exists",
        ) as refused:
            migrations.downgrade(connection, target=61)
        assert refused.value.sqlstate == "55000"
        assert refused.value.diag.message_primary == (
            "0061 rollback refused: lifecycle hydration admission audit data exists"
        )
        assert migrations.status(connection).head == 61
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure)",
                (_PROJECTION_ADMISSION_SIGNATURE,),
            )
            assert str(cursor.fetchone()[0]) == before
            cursor.execute(
                "select count(*) from continuity.lifecycle_hydration_admission where id=%s",
                (audit_id,),
            )
            assert cursor.fetchone() == (1,)
            cursor.execute(
                "select relforcerowsecurity from pg_class"
                " where oid='continuity.lifecycle_hydration_admission'::regclass"
            )
            assert cursor.fetchone() == (True,)


def test_0061_down_refuses_projection_completion_audit_history(
    blank_database: DatabaseSettings,
) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=61)
        audit_id = _insert_projection_admission_audit(connection, uuid4())
        with pytest.raises(
            PsycopgError,
            match="projection completion admission audit data exists",
        ) as refused:
            migrations.downgrade(connection, target=61)
        assert refused.value.sqlstate == "55000"
        assert refused.value.diag.message_primary == (
            "0061 rollback refused: projection completion admission audit data exists"
        )
        assert migrations.status(connection).head == 61
        with connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from work.completion_admission where id=%s",
                (audit_id,),
            )
            assert cursor.fetchone() == (1,)
            cursor.execute(
                "select relforcerowsecurity from pg_class"
                " where oid='work.completion_admission'::regclass"
            )
            assert cursor.fetchone() == (True,)


def test_0062_lifecycle_resource_scope_exact_roundtrip(
    blank_database: DatabaseSettings,
) -> None:
    previous_scope = "array['continuity:session:'||event.session_id]"
    revised_scope = "array['memory:'||event.project_id::text||':session:'||event.session_id]"
    canonical_markers = (
        "hydration.receipt_body->>'source_digest'=source_tree_digest_",
        "freshness_dimension->>'observed_digest'=source_tree_digest_",
        "freshness_dimension->>'expected_digest'=source_tree_digest_",
        "projection_ref->>'digest'=prior_projection.projection_digest",
        "hydration_event.event_type in ('session_start','hydration_required')",
        "continuity.lifecycle_hydration_admission admission",
    )
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=61)
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure),"
                " obj_description(%s::regprocedure,'pg_proc')",
                (_PROJECTION_ADMISSION_SIGNATURE, _PROJECTION_ADMISSION_SIGNATURE),
            )
            baseline, baseline_comment = cursor.fetchone()
        baseline = str(baseline)
        assert baseline.count(previous_scope) == 1
        assert revised_scope not in baseline
        assert baseline_comment == "0061 canonical inventory hydration compatibility"

        assert [item.version for item in migrations.upgrade(connection, target=62)] == [62]
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure),"
                " obj_description(%s::regprocedure,'pg_proc')",
                (_PROJECTION_ADMISSION_SIGNATURE, _PROJECTION_ADMISSION_SIGNATURE),
            )
            revised, revised_comment = cursor.fetchone()
        revised = str(revised)
        assert revised != baseline
        assert previous_scope not in revised
        assert revised.count(revised_scope) == 1
        assert all(marker in revised for marker in canonical_markers)
        assert revised.count("'run:'||run.id::text||':genesis'") == 2
        assert revised_comment == "0062 project-scoped lifecycle resource authorization"

        assert migrations.downgrade(connection, target=62).version == 62
        assert migrations.status(connection).head == 61
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure),"
                " obj_description(%s::regprocedure,'pg_proc')",
                (_PROJECTION_ADMISSION_SIGNATURE, _PROJECTION_ADMISSION_SIGNATURE),
            )
            restored, restored_comment = cursor.fetchone()
        assert str(restored) == baseline
        assert restored_comment == "0061 canonical inventory hydration compatibility"

        assert [item.version for item in migrations.upgrade(connection, target=62)] == [62]
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure),"
                " obj_description(%s::regprocedure,'pg_proc')",
                (_PROJECTION_ADMISSION_SIGNATURE, _PROJECTION_ADMISSION_SIGNATURE),
            )
            reapplied, reapplied_comment = cursor.fetchone()
        assert str(reapplied) == revised
        assert reapplied_comment == revised_comment


def test_0062_down_refuses_projection_completion_audit_history(
    blank_database: DatabaseSettings,
) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=62)
        audit_id = _insert_projection_admission_audit(connection, uuid4())
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure)",
                (_PROJECTION_ADMISSION_SIGNATURE,),
            )
            before = str(cursor.fetchone()[0])

        with pytest.raises(
            PsycopgError,
            match="projection completion admission audit data exists",
        ) as refused:
            migrations.downgrade(connection, target=62)
        assert refused.value.sqlstate == "55000"
        assert refused.value.diag.message_primary == (
            "0062 rollback refused: projection completion admission audit data exists"
        )
        assert migrations.status(connection).head == 62
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_get_functiondef(%s::regprocedure),"
                " obj_description(%s::regprocedure,'pg_proc')",
                (_PROJECTION_ADMISSION_SIGNATURE, _PROJECTION_ADMISSION_SIGNATURE),
            )
            after, function_comment = cursor.fetchone()
            assert str(after) == before
            assert function_comment == ("0062 project-scoped lifecycle resource authorization")
            cursor.execute(
                "select count(*) from work.completion_admission where id=%s",
                (audit_id,),
            )
            assert cursor.fetchone() == (1,)
            cursor.execute(
                "select relforcerowsecurity from pg_class"
                " where oid='work.completion_admission'::regclass"
            )
            assert cursor.fetchone() == (True,)


def test_0056_0057_0058_exact_upgrade_down_reapply_catalog_parity(
    blank_database: DatabaseSettings,
) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=56)
        snapshot_56 = (_closure_catalog_snapshot(connection), _codex_catalog_snapshot(connection))

        applied_57 = migrations.upgrade(connection, target=57)
        assert [item.version for item in applied_57] == [57]
        assert migrations.status(connection).head == 57
        snapshot_57 = (_closure_catalog_snapshot(connection), _codex_catalog_snapshot(connection))

        applied_58 = migrations.upgrade(connection, target=58)
        assert [item.version for item in applied_58] == [58]
        assert migrations.status(connection).head == 58
        snapshot_58 = (_closure_catalog_snapshot(connection), _codex_catalog_snapshot(connection))

        assert migrations.downgrade(connection, target=58).version == 58
        assert migrations.status(connection).head == 57
        assert (_closure_catalog_snapshot(connection), _codex_catalog_snapshot(connection)) == (
            snapshot_57
        )

        assert migrations.downgrade(connection, target=57).version == 57
        assert migrations.status(connection).head == 56
        assert (_closure_catalog_snapshot(connection), _codex_catalog_snapshot(connection)) == (
            snapshot_56
        )

        assert [item.version for item in migrations.upgrade(connection, target=57)] == [57]
        assert (_closure_catalog_snapshot(connection), _codex_catalog_snapshot(connection)) == (
            snapshot_57
        )
        assert [item.version for item in migrations.upgrade(connection, target=58)] == [58]
        assert (_closure_catalog_snapshot(connection), _codex_catalog_snapshot(connection)) == (
            snapshot_58
        )
        final_status = migrations.status(connection)
        assert final_status.head == 58
        assert final_status.drift == ()
        assert {migration.version for migration in final_status.pending}.isdisjoint({57, 58})


def test_concurrent_downgrade_rechecks_head_under_lock(
    blank_database: DatabaseSettings,
) -> None:
    target = migrations.discover_migrations()[-1].version
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=target)

    barrier = threading.Barrier(2)

    def attempt() -> str:
        with connect(blank_database) as connection:
            barrier.wait(timeout=5)
            try:
                migrations.downgrade(connection, target=target)
            except ValidationFailed:
                return "stale-head-rejected"
            return "rolled-back"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(lambda _: attempt(), range(2)))

    assert outcomes == ["rolled-back", "stale-head-rejected"]
    with connect(blank_database) as connection:
        assert migrations.status(connection).head == target - 1


def test_partial_upgrade_to_target(blank_database: DatabaseSettings) -> None:
    with connect(blank_database) as connection:
        applied = migrations.upgrade(connection, target=1)
        current = migrations.status(connection)
    available = migrations.discover_migrations()
    assert [result.version for result in applied] == [1]
    assert current.head == 1
    assert [migration.version for migration in current.pending] == [
        migration.version for migration in available[1:]
    ]


def test_checksum_drift_blocks_upgrade(blank_database: DatabaseSettings, tmp_path: Path) -> None:
    source = migrations.discover_migrations()
    for migration in source:
        (tmp_path / migration.path.name).write_text(migration.read_sql(), encoding="utf-8")
        (tmp_path / migration.down_path.name).write_text(
            migration.down_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    with connect(blank_database) as connection:
        migrations.upgrade(connection, tmp_path)
        target = tmp_path / source[0].path.name
        target.write_text(
            target.read_text(encoding="utf-8") + "\n-- sonradan eklendi\ncreate table core.x ();\n",
            encoding="utf-8",
        )
        current = migrations.status(connection, tmp_path)
        assert [finding.kind.value for finding in current.drift] == ["checksum-mismatch"]
        with pytest.raises(ConfigurationError, match="drift"):
            migrations.upgrade(connection, tmp_path)


def test_required_extensions_are_created_by_migration(blank_database: DatabaseSettings) -> None:
    """Temiz kurulumda migration eklentileri kendisi kurar."""
    with connect(blank_database) as connection:
        migrations.upgrade(connection)
        with connection.cursor() as cursor:
            cursor.execute("select extname from pg_extension")
            present = {row[0] for row in cursor.fetchall()}
    assert {"vector", "pg_trgm", "btree_gin", "pgcrypto"} <= present


def test_all_declared_schemas_exist_after_upgrade(blank_database: DatabaseSettings) -> None:
    expected = {
        "core",
        "projects",
        "work",
        "runtime",
        "models",
        "research",
        "knowledge",
        "memory",
        "skills",
        "security",
        "ops",
    }
    with connect(blank_database) as connection:
        migrations.upgrade(connection)
        with connection.cursor() as cursor:
            cursor.execute("select nspname from pg_namespace")
            present = {row[0] for row in cursor.fetchall()}
    assert expected <= present


def test_application_role_exists_and_is_not_superuser(blank_database: DatabaseSettings) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "select rolsuper, rolbypassrls, rolcanlogin from pg_roles"
                " where rolname = 'zekam_app'"
            )
            row = cursor.fetchone()
    assert row is not None, "zekam_app rolu olusturulmali"
    assert row[0] is False, "Uygulama rolu superuser olmamali"
    assert row[1] is False, "Uygulama rolu RLS'i atlamamali"


def test_0057_closure_function_signatures_and_privileges_are_exact(
    blank_database: DatabaseSettings,
) -> None:
    """The app receives only the exact 18/30-argument admission entry points."""
    signatures = (
        _PROJECTION_LOCK_SIGNATURE,
        _CONTROL_LOCK_SIGNATURE,
        _PROJECTION_ADMISSION_SIGNATURE,
        _CONTROL_ADMISSION_SIGNATURE,
    )
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=58)
        with connection.cursor() as cursor:
            cursor.execute(
                "select has_table_privilege('zekam_app','work.completion_admission','select'),"
                " has_table_privilege('zekam_app','work.completion_admission','insert'),"
                " has_table_privilege('zekam_app','work.completion_admission','update'),"
                " has_table_privilege('zekam_app','work.completion_admission','delete'),"
                " has_table_privilege('zekam_app','work.task_plan','update'),"
                " has_function_privilege('zekam_app',"
                " 'continuity.lock_projection_closure_scope(uuid,uuid,uuid,uuid,uuid,uuid)',"
                " 'execute'),"
                " has_function_privilege('zekam_app',"
                " 'work.lock_control_plane_completion_scope(uuid,uuid,uuid,uuid,uuid,uuid)',"
                " 'execute'),"
                " has_function_privilege('zekam_app',%s,'execute'),"
                " has_function_privilege('zekam_app',%s,'execute')",
                (_PROJECTION_ADMISSION_SIGNATURE, _CONTROL_ADMISSION_SIGNATURE),
            )
            privileges = cursor.fetchone()
            cursor.execute(
                "select coalesce(relacl::text,'') from pg_class"
                " where oid='work.completion_admission'::regclass"
            )
            completion_relation_acl = str(cursor.fetchone()[0])
        contracts = _function_security_contract(connection, signatures)
        execute_acl = _non_owner_execute_acl(connection, signatures)
        table_acl = _non_owner_table_acl(connection, ("work.completion_admission",))
        column_acl = _non_owner_column_acl(connection, ("work.completion_admission",))
    assert privileges == (True, False, False, False, False, True, False, True, True)
    assert completion_relation_acl
    assert table_acl == {
        "work.completion_admission": {("zekam_app", "SELECT", False)},
    }
    assert column_acl == {"work.completion_admission": set()}
    assert contracts[_PROJECTION_ADMISSION_SIGNATURE][1:3] == (18, True)
    assert contracts[_CONTROL_ADMISSION_SIGNATURE][1:3] == (30, True)
    assert contracts[_PROJECTION_ADMISSION_SIGNATURE][0].count(",") + 1 == 18
    assert contracts[_CONTROL_ADMISSION_SIGNATURE][0].count(",") + 1 == 30
    lock_search_path = (
        "search_path=pg_catalog, core, projects, work, runtime, security, continuity, memory",
    )
    admission_search_path = ("search_path=pg_catalog, core, work, runtime, security, continuity",)
    assert contracts[_PROJECTION_LOCK_SIGNATURE][3] == lock_search_path
    assert contracts[_CONTROL_LOCK_SIGNATURE][3] == lock_search_path
    assert contracts[_PROJECTION_ADMISSION_SIGNATURE][3] == admission_search_path
    assert contracts[_CONTROL_ADMISSION_SIGNATURE][3] == admission_search_path
    assert execute_acl == {
        _PROJECTION_LOCK_SIGNATURE: {("zekam_app", False)},
        _CONTROL_LOCK_SIGNATURE: set(),
        _PROJECTION_ADMISSION_SIGNATURE: {("zekam_app", False)},
        _CONTROL_ADMISSION_SIGNATURE: {("zekam_app", False)},
    }


def test_0058_codex_admission_catalogue_acl_rls_and_dual_trigger_contract(
    blank_database: DatabaseSettings,
) -> None:
    signatures = (_CODEX_LOCK_SIGNATURE, _CODEX_VALIDATOR_SIGNATURE, _PLAN_ORDER_SIGNATURE)
    with connect(blank_database) as connection:
        migrations.upgrade(connection, target=58)
        with connection.cursor() as cursor:
            cursor.execute(
                "select relrowsecurity,relforcerowsecurity,coalesce(relacl::text,'')"
                " from pg_class where oid='client.codex_lifecycle_admission'::regclass"
            )
            relation = cursor.fetchone()
            cursor.execute(
                "select schemaname,tablename,policyname,cmd,roles::text,qual,with_check"
                " from pg_policies where schemaname='client'"
                " and tablename='codex_lifecycle_admission' order by policyname"
            )
            policies = cursor.fetchall()
            cursor.execute(
                "select t.tgname,t.tgdeferrable,t.tginitdeferred,c.relname,t.tgtype,"
                " trigger_function.oid::regprocedure::text,"
                " regexp_replace(pg_get_triggerdef(t.oid),'[[:space:]]+',' ','g')"
                " from pg_trigger t join pg_class c on c.oid=t.tgrelid"
                " join pg_namespace n on n.oid=c.relnamespace"
                " join pg_proc trigger_function on trigger_function.oid=t.tgfoid"
                " where not t.tgisinternal and t.tgname in"
                " ('codex_lifecycle_admission_guard','codex_lifecycle_admission_row_guard',"
                " 'codex_lifecycle_admission_no_mutation') order by t.tgname"
            )
            triggers = cursor.fetchall()
            cursor.execute(
                "select has_table_privilege("
                " 'zekam_app','client.codex_lifecycle_admission','select'),"
                " has_table_privilege('zekam_app','client.codex_lifecycle_admission','insert'),"
                " has_table_privilege('zekam_app','client.codex_lifecycle_admission','update'),"
                " has_table_privilege('zekam_app','client.codex_lifecycle_admission','delete'),"
                " has_function_privilege('zekam_app',"
                " 'client.lock_codex_lifecycle_scope(uuid,uuid,uuid,uuid)','execute'),"
                " has_function_privilege('zekam_app',"
                " 'client.enforce_codex_lifecycle_admission()','execute'),"
                " has_function_privilege('zekam_app',"
                " 'work.task_plan_execution_order(jsonb)','execute')"
            )
            privileges = cursor.fetchone()
            cursor.execute(
                "select count(*) from information_schema.role_table_grants"
                " where table_schema='client' and table_name='codex_lifecycle_admission'"
                " and grantee='PUBLIC'"
            )
            public_grants = cursor.fetchone()[0]
        contracts = _function_security_contract(connection, signatures)
        execute_acl = _non_owner_execute_acl(connection, signatures)
        table_acl = _non_owner_table_acl(connection, ("client.codex_lifecycle_admission",))
        column_acl = _non_owner_column_acl(
            connection,
            ("client.codex_lifecycle_admission",),
        )
    assert relation[:2] == (True, True)
    assert relation[2]
    assert table_acl == {
        "client.codex_lifecycle_admission": {
            ("zekam_app", "INSERT", False),
            ("zekam_app", "SELECT", False),
        }
    }
    assert column_acl == {"client.codex_lifecycle_admission": set()}
    assert policies == [
        (
            "client",
            "codex_lifecycle_admission",
            "scope_insert",
            "INSERT",
            "{public}",
            None,
            "(realm_id = core.current_realm_id())",
        ),
        (
            "client",
            "codex_lifecycle_admission",
            "scope_select",
            "SELECT",
            "{public}",
            "(realm_id = core.current_realm_id())",
            None,
        ),
    ]
    assert execute_acl == {
        _CODEX_LOCK_SIGNATURE: {("zekam_app", False)},
        _CODEX_VALIDATOR_SIGNATURE: {("zekam_app", False)},
        _PLAN_ORDER_SIGNATURE: {("zekam_app", False)},
    }
    assert triggers == [
        (
            "codex_lifecycle_admission_guard",
            True,
            True,
            "lifecycle_event",
            5,
            "client.enforce_codex_lifecycle_admission()",
            "CREATE CONSTRAINT TRIGGER codex_lifecycle_admission_guard AFTER INSERT ON "
            "client.lifecycle_event DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE "
            "FUNCTION client.enforce_codex_lifecycle_admission()",
        ),
        (
            "codex_lifecycle_admission_no_mutation",
            False,
            False,
            "codex_lifecycle_admission",
            26,
            "core.deny_mutation()",
            "CREATE TRIGGER codex_lifecycle_admission_no_mutation BEFORE DELETE OR UPDATE ON "
            "client.codex_lifecycle_admission FOR EACH STATEMENT EXECUTE FUNCTION "
            "core.deny_mutation()",
        ),
        (
            "codex_lifecycle_admission_row_guard",
            True,
            True,
            "codex_lifecycle_admission",
            5,
            "client.enforce_codex_lifecycle_admission()",
            "CREATE CONSTRAINT TRIGGER codex_lifecycle_admission_row_guard AFTER INSERT ON "
            "client.codex_lifecycle_admission DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION client.enforce_codex_lifecycle_admission()",
        ),
    ]
    assert privileges == (True, True, False, False, True, True, True)
    assert public_grants == 0
    assert contracts[_CODEX_VALIDATOR_SIGNATURE] == (
        "",
        0,
        True,
        ("search_path=pg_catalog",),
    )
    assert contracts[_CODEX_LOCK_SIGNATURE] == (
        "realm_id_ uuid, job_id_ uuid, attempt_id_ uuid, authorization_id_ uuid",
        4,
        True,
        ("search_path=pg_catalog",),
    )
    assert contracts[_PLAN_ORDER_SIGNATURE] == (
        "steps_ jsonb",
        1,
        False,
        ("search_path=pg_catalog",),
    )


def test_0058_codex_admission_is_cross_realm_scoped_and_append_only(
    blank_database: DatabaseSettings,
) -> None:
    realm_a, realm_b = uuid4(), uuid4()
    with connect(blank_database) as owner:
        migrations.upgrade(owner, target=58)
        row_a = _insert_codex_admission_audit(owner, realm_a)
        row_b = _insert_codex_admission_audit(owner, realm_b)

    with connect(blank_database) as realm_a_session:
        configure_session(realm_a_session, realm_id=realm_a)
        with realm_a_session.cursor() as cursor:
            cursor.execute("select id from client.codex_lifecycle_admission order by id")
            assert cursor.fetchall() == [(row_a,)]
        with pytest.raises(PsycopgError) as update_denied:  # noqa: SIM117
            with realm_a_session.transaction(), realm_a_session.cursor() as cursor:
                cursor.execute(
                    "update client.codex_lifecycle_admission set created_at=created_at where id=%s",
                    (row_a,),
                )
        assert update_denied.value.sqlstate == "42501"
        assert update_denied.value.diag.message_primary == (
            "permission denied for table codex_lifecycle_admission"
        )
        with pytest.raises(PsycopgError) as delete_denied:  # noqa: SIM117
            with realm_a_session.transaction(), realm_a_session.cursor() as cursor:
                cursor.execute("delete from client.codex_lifecycle_admission where id=%s", (row_a,))
        assert delete_denied.value.sqlstate == "42501"
        assert delete_denied.value.diag.message_primary == (
            "permission denied for table codex_lifecycle_admission"
        )

    with connect(blank_database) as realm_b_session:
        configure_session(realm_b_session, realm_id=realm_b)
        with realm_b_session.cursor() as cursor:
            cursor.execute("select id from client.codex_lifecycle_admission order by id")
            assert cursor.fetchall() == [(row_b,)]

    with connect(blank_database) as owner:
        configure_session(owner, realm_id=realm_a, role=None)
        with pytest.raises(PsycopgError) as update_denied:  # noqa: SIM117
            with owner.transaction(), owner.cursor() as cursor:
                cursor.execute(
                    "update client.codex_lifecycle_admission set created_at=created_at where id=%s",
                    (row_a,),
                )
        assert update_denied.value.sqlstate == "42501"
        assert update_denied.value.diag.message_primary == (
            "append-only tablo: UPDATE islemi reddedildi (client.codex_lifecycle_admission)"
        )
        with pytest.raises(PsycopgError) as delete_denied:  # noqa: SIM117
            with owner.transaction(), owner.cursor() as cursor:
                cursor.execute("delete from client.codex_lifecycle_admission where id=%s", (row_a,))
        assert delete_denied.value.sqlstate == "42501"
        assert delete_denied.value.diag.message_primary == (
            "append-only tablo: DELETE islemi reddedildi (client.codex_lifecycle_admission)"
        )


def test_0058_check_valid_cross_realm_app_insert_is_rejected_by_rls(
    blank_database: DatabaseSettings,
) -> None:
    realm_a, realm_b = uuid4(), uuid4()
    with connect(blank_database) as owner:
        migrations.upgrade(owner, target=58)
    with connect(blank_database) as app_connection:
        configure_session(app_connection, realm_id=realm_a)
        with pytest.raises(PsycopgError) as rejected:  # noqa: SIM117
            with app_connection.transaction(), app_connection.cursor() as cursor:
                _insert_check_valid_codex_admission(cursor, realm_b)
    assert rejected.value.sqlstate == "42501"
    assert rejected.value.diag.message_primary == (
        'new row violates row-level security policy for table "codex_lifecycle_admission"'
    )


def test_memory_continuity_base_and_exact_close_guards_coexist(
    blank_database: DatabaseSettings,
) -> None:
    """0057, 0055 identity/digest guardini kaldirmaz veya degistirmez."""
    with connect(blank_database) as connection:
        migrations.upgrade(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "select tgname from pg_trigger"
                " where tgrelid='continuity.session_close_receipt'::regclass"
                " and not tgisinternal order by tgname"
            )
            names = {str(row[0]) for row in cursor.fetchall()}
    assert {"close_identity", "close_exact_guard", "deny_update", "deny_delete"} <= names


def test_ledger_records_checksum_and_duration(blank_database: DatabaseSettings) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "select version, name, checksum, duration_ms, applied_by"
                " from core.schema_migrations order by version"
            )
            rows = cursor.fetchall()
    available = {m.version: m for m in migrations.discover_migrations()}
    for version, name, checksum, duration_ms, applied_by in rows:
        assert checksum == available[version].checksum
        assert name == available[version].name
        assert duration_ms >= 0
        assert applied_by


def test_causal_observability_objects_are_read_only_and_bounded(
    blank_database: DatabaseSettings,
) -> None:
    with connect(blank_database) as connection:
        migrations.upgrade(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "select relname,reloptions from pg_class c "
                "join pg_namespace n on n.oid=c.relnamespace "
                "where n.nspname='ops' and relname in ('causal_node','causal_edge','causal_orphan')"
            )
            views = {row[0]: row[1] or [] for row in cursor.fetchall()}
            cursor.execute("select count(*) from ops.causal_chain(gen_random_uuid(),256)")
            assert cursor.fetchone() == (0,)
            with pytest.raises(InvalidParameterValue):
                cursor.execute("select count(*) from ops.causal_chain(gen_random_uuid(),513)")
    assert set(views) == {"causal_node", "causal_edge", "causal_orphan"}
    assert all("security_invoker=true" in options for options in views.values())
