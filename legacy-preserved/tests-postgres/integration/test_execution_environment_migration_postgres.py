from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.application.config import DatabaseSettings
from zekam.domain.canonical import digest
from zekam.infrastructure.postgres import migrations
from zekam.infrastructure.postgres.connection import connect

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_migration_36_accepts_legacy_envelopes_but_leaves_them_explicitly_unbound(
    postgres_settings: DatabaseSettings,
) -> None:
    database_name = f"zekam_env_upgrade_{uuid4().hex[:12]}"
    scoped = DatabaseSettings(
        host=postgres_settings.host,
        port=postgres_settings.port,
        name=database_name,
        user=postgres_settings.user,
        sslmode=postgres_settings.sslmode,
    )
    with connect(postgres_settings) as connection, connection.cursor() as cursor:
        cursor.execute(f'create database "{database_name}"')
    try:
        with connect(scoped) as connection:
            migrations.upgrade(connection, target=35)
            legacy_id = uuid4()
            now = dt.datetime.now(dt.UTC)
            route_expiry = now + dt.timedelta(minutes=10)
            with connection.cursor() as cursor:
                cursor.execute("set session_replication_role=replica")
                cursor.execute(
                    "insert into runtime.execution_envelope"
                    "(id,realm_id,run_id,job_id,attempt_id,lease_id,fencing_token,"
                    "request_ordinal,idempotency_key,assignment_id,role,route_decision_id,"
                    "route_decision_digest,route_expires_at,model_id,provider_binding_id,"
                    "provider_binding_digest,provider_ref,context_manifest_id,"
                    "context_manifest_digest,context_packet_id,context_packet_digest,"
                    "checkpoint_id,checkpoint_digest,checkpoint_disposition,source_revision,"
                    "policy_digest,authorization_scope_digest,output_schema_digest,payload_digest,"
                    "max_input_tokens,max_output_tokens,max_cost_micros,deadline,envelope_digest,"
                    "checkpoint_v2_id,checkpoint_v2_digest,grants_authority,created_at) values"
                    "(%s,%s,%s,%s,%s,%s,1,1,'legacy-envelope',%s,'builder',%s,%s,%s,"
                    "'model/legacy',%s,%s,'provider:legacy',%s,%s,%s,%s,null,null,"
                    "'not-applicable-genesis','legacy-revision',%s,%s,%s,%s,10,10,100,%s,%s,"
                    "null,null,false,%s)",
                    (
                        legacy_id,
                        uuid4(),
                        uuid4(),
                        uuid4(),
                        uuid4(),
                        uuid4(),
                        uuid4(),
                        uuid4(),
                        digest("route"),
                        route_expiry,
                        uuid4(),
                        digest("provider"),
                        uuid4(),
                        digest("context"),
                        uuid4(),
                        digest("packet"),
                        digest("policy"),
                        digest("authorization"),
                        digest("schema"),
                        digest("payload"),
                        now + dt.timedelta(minutes=5),
                        digest("legacy-envelope"),
                        now,
                    ),
                )
                cursor.execute("set session_replication_role=origin")
            connection.commit()
            migrations.upgrade(connection, target=36)
            with connection.cursor() as cursor:
                cursor.execute(
                    "select turn_execution_snapshot_id,turn_execution_snapshot_digest"
                    " from runtime.execution_envelope where id=%s",
                    (legacy_id,),
                )
                assert tuple(cursor.fetchone()) == (None, None)
                cursor.execute(
                    "select convalidated from pg_constraint"
                    " where conname='execution_envelope_turn_snapshot_required'"
                )
                assert cursor.fetchone()[0] is False
    finally:
        with connect(postgres_settings) as connection, connection.cursor() as cursor:
            cursor.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname=%s",
                (database_name,),
            )
            cursor.execute(f'drop database if exists "{database_name}"')
