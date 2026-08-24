"""PostgreSQL acceptance checks for the capability calibration runtime ledger."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.config import DatabaseSettings
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.model_capability_runtime import (
    MAX_PROVIDER_CALLS,
    CapabilityRuntimeApprovalManifest,
    CapabilityRuntimeOutcome,
    CapabilityRuntimeStatus,
)
from zekam.infrastructure.postgres import migrations
from zekam.infrastructure.postgres.connection import connect

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

TABLES = (
    "capability_runtime_approval_manifest",
    "capability_runtime_approval_slot",
    "capability_runtime_continuity_state",
    "capability_runtime_turn_checkpoint",
    "capability_runtime_slot_authorization",
    "capability_runtime_call_outcome",
    "capability_runtime_episode_outcome",
    "capability_runtime_skipped_slot",
    "capability_runtime_outcome",
)


def _sha(label: str) -> str:
    return digest({"label": label})


def _seed_adversarial_completed_candidate(
    connection: Any, *, mode: str
) -> tuple[Any, Any, tuple[str, ...]]:
    realm_id, project_id, work_id, plan_id, cohort_id, manifest_id = (uuid4() for _ in range(6))
    coordinator_id = uuid4()
    models = tuple(f"adversarial-model-{index}" for index in range(7))
    tasks = tuple(_sha(f"adversarial-task-{index}") for index in range(3))
    evidence: list[str] = []
    provider_steps: list[dict[str, Any]] = []
    episode_steps: list[dict[str, Any]] = []
    with connection.cursor() as cursor:
        cursor.execute("set session_replication_role=replica")
        try:
            cursor.execute(
                "insert into runtime.job"
                " (id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,max_attempts,"
                " write_resources,idempotency_key)"
                " values (%s,%s,%s,%s,%s,'capability-finalize','verification',%s,1,%s,%s)",
                (
                    coordinator_id,
                    realm_id,
                    project_id,
                    work_id,
                    plan_id,
                    "failed" if mode == "coordinator-failed" else "completed",
                    [f"model-benchmark:capability-cohort:{cohort_id}"],
                    f"adversarial-coordinator-{manifest_id}",
                ),
            )
            cursor.execute(
                "insert into models.capability_runtime_approval_manifest"
                " (id,realm_id,cohort_id,work_item_id,task_plan_id,coordinator_job_id,"
                " source_revision,model_ids,task_digests,episode_count,slots_per_episode,"
                " max_provider_calls,max_retries,approval_evidence_digest,manifest_digest)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,21,8,168,0,%s,%s)",
                (
                    manifest_id,
                    realm_id,
                    cohort_id,
                    work_id,
                    plan_id,
                    coordinator_id,
                    "adversarial-revision",
                    list(models),
                    list(tasks),
                    _sha("adversarial-approval"),
                    _sha(f"adversarial-manifest-{manifest_id}"),
                ),
            )
            ordinal = 0
            for model_id in models:
                for task_digest in tasks:
                    episode_job_id = uuid4()
                    episode_step_id = f"episode-{episode_job_id}"
                    episode_resource = f"model-benchmark:{model_id}:{task_digest}"
                    episode_failed = mode == "failed-call" and ordinal == 0
                    cursor.execute(
                        "insert into runtime.job"
                        " (id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,"
                        " max_attempts,write_resources,idempotency_key)"
                        " values (%s,%s,%s,%s,%s,%s,'provider-call',%s,1,%s,%s)",
                        (
                            episode_job_id,
                            realm_id,
                            project_id,
                            work_id,
                            plan_id,
                            episode_step_id,
                            "failed" if episode_failed else "completed",
                            [episode_resource],
                            f"adversarial-episode-{episode_job_id}",
                        ),
                    )
                    for turn in range(1, 9):
                        ordinal += 1
                        slot_id, claim_id, receipt_id, checkpoint_id = (uuid4() for _ in range(4))
                        result = _sha(f"adversarial-result-{manifest_id}-{ordinal}")
                        call_evidence = _sha(f"adversarial-call-{manifest_id}-{ordinal}")
                        evidence.append(call_evidence)
                        failed = mode == "failed-call" and ordinal == 1
                        call_id = f"adversarial-call-{ordinal}"
                        call_resource = f"provider:{model_id}:chat-completions:{call_id}"
                        template = {
                            "schema": "zekam-capability-request-template/v1",
                            "model": model_id,
                            "system": "public",
                            "prompt_prefix": "public ",
                            "max_tokens": 256,
                        }
                        cursor.execute(
                            "insert into models.capability_runtime_approval_slot"
                            " (id,realm_id,manifest_id,cohort_id,model_id,task_digest,"
                            " turn_number,ordinal,job_id,provider_ref,backend_model,"
                            " endpoint_resource,call_resource,endpoint_identity_digest,operation,"
                            " call_id,fixture_digest,fixture_identity_digest,max_output_tokens,"
                            " request_template,request_template_digest,"
                            " derivation_rule_digest,chain_seed_digest,slot_digest)"
                            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'provider-x',%s,"
                            " 'provider:x',%s,%s,'chat-completions',"
                            " %s,%s,%s,256,%s::jsonb,"
                            " %s,%s,%s,%s)",
                            (
                                slot_id,
                                realm_id,
                                manifest_id,
                                cohort_id,
                                model_id,
                                task_digest,
                                turn,
                                ordinal,
                                episode_job_id,
                                model_id,
                                call_resource,
                                _sha("endpoint"),
                                call_id,
                                _sha("fixture"),
                                _sha("fixture-identity"),
                                json.dumps(template, sort_keys=True),
                                digest(template),
                                _sha("derivation"),
                                _sha("seed"),
                                _sha(f"slot-{manifest_id}-{ordinal}"),
                            ),
                        )
                        provider_steps.append(
                            {
                                "step_id": call_id,
                                "effect": "provider-call",
                                "logical_resources": [call_resource],
                                "depends_on": []
                                if turn == 1
                                else [f"adversarial-call-{ordinal - 1}"],
                            }
                        )
                        if mode != "missing-receipts":
                            cursor.execute(
                                "insert into runtime.effect_receipt"
                                " (id,realm_id,claim_id,status,result_digest,failure_category)"
                                " values (%s,%s,%s,%s,%s,%s)",
                                (
                                    receipt_id,
                                    realm_id,
                                    claim_id,
                                    "failed" if failed else "completed",
                                    None if failed else result,
                                    "adversarial-failure" if failed else None,
                                ),
                            )
                        cursor.execute(
                            "insert into models.capability_runtime_call_outcome"
                            " (id,realm_id,slot_id,claim_id,receipt_id,checkpoint_id,status,"
                            " result_digest,failure_category,evidence_digest,completed_at)"
                            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())",
                            (
                                uuid4(),
                                realm_id,
                                slot_id,
                                claim_id,
                                receipt_id,
                                checkpoint_id,
                                "failed" if failed else "completed",
                                None if failed else result,
                                "adversarial-failure" if failed else None,
                                call_evidence,
                            ),
                        )
                    episode_steps.append(
                        {
                            "step_id": episode_step_id,
                            "effect": "database-write",
                            "logical_resources": [episode_resource],
                            "depends_on": sorted(step["step_id"] for step in provider_steps[-8:]),
                        }
                    )
            plan_steps = [
                *provider_steps,
                *episode_steps,
                {
                    "step_id": "capability-finalize",
                    "effect": "database-write",
                    "logical_resources": [f"model-benchmark:capability-cohort:{cohort_id}"],
                    "depends_on": sorted(step["step_id"] for step in episode_steps),
                },
            ]
            cursor.execute(
                "insert into work.task_plan"
                " (id,realm_id,project_id,work_item_id,revision,source_revision,policy_digest,"
                " steps,effect_digest,plan_digest) values (%s,%s,%s,%s,1,"
                " 'adversarial-revision',%s,%s::jsonb,%s,%s)",
                (
                    plan_id,
                    realm_id,
                    project_id,
                    work_id,
                    _sha("policy"),
                    json.dumps(plan_steps, sort_keys=True),
                    _sha("plan-effect"),
                    _sha("plan"),
                ),
            )
        finally:
            cursor.execute("set session_replication_role=origin")
    return realm_id, manifest_id, tuple(evidence)


def _seed_authorization_derivation_candidate(connection: Any) -> dict[str, Any]:
    ids = {
        name: uuid4()
        for name in (
            "realm",
            "project",
            "work",
            "plan",
            "cohort",
            "manifest",
            "coordinator",
            "episode",
            "slot",
            "actor",
            "authorization",
        )
    }
    values = {
        **ids,
        "task": _sha("derived-task"),
        "seed": _sha("derived-seed"),
        "plan_digest": _sha("derived-plan"),
        "effect": _sha("derived-effect"),
        "authorization_digest": _sha("derived-authorization"),
        "request": _sha("derived-request"),
    }
    with connection.cursor() as cursor:
        cursor.execute("set session_replication_role=replica")
        try:
            cursor.execute(
                "insert into core.realm (id,slug,display_name) values (%s,%s,'Derived realm')",
                (ids["realm"], f"derived-{str(ids['realm'])[:8]}"),
            )
            for job_id, kind, state, key in (
                (ids["coordinator"], "verification", "ready", "coordinator"),
                (ids["episode"], "provider-call", "running", "episode"),
            ):
                cursor.execute(
                    "insert into runtime.job"
                    " (id,realm_id,project_id,work_item_id,plan_id,kind,state,max_attempts,"
                    " idempotency_key) values (%s,%s,%s,%s,%s,%s,%s,1,%s)",
                    (
                        job_id,
                        ids["realm"],
                        ids["project"],
                        ids["work"],
                        ids["plan"],
                        kind,
                        state,
                        f"derived-{key}-{ids['manifest']}",
                    ),
                )
            cursor.execute(
                "insert into models.capability_runtime_approval_manifest"
                " (id,realm_id,cohort_id,work_item_id,task_plan_id,coordinator_job_id,"
                " source_revision,model_ids,task_digests,episode_count,slots_per_episode,"
                " max_provider_calls,max_retries,approval_evidence_digest,manifest_digest)"
                " values (%s,%s,%s,%s,%s,%s,'derived-revision',%s,%s,21,8,168,0,%s,%s)",
                (
                    ids["manifest"],
                    ids["realm"],
                    ids["cohort"],
                    ids["work"],
                    ids["plan"],
                    ids["coordinator"],
                    [f"derived-model-{i}" for i in range(7)],
                    [values["task"], _sha("derived-task-2"), _sha("derived-task-3")],
                    _sha("derived-approval"),
                    _sha("derived-manifest"),
                ),
            )
            cursor.execute(
                "insert into models.capability_runtime_approval_slot"
                " (id,realm_id,manifest_id,cohort_id,model_id,task_digest,turn_number,ordinal,"
                " job_id,provider_ref,backend_model,endpoint_resource,call_resource,"
                " endpoint_identity_digest,operation,call_id,fixture_digest,"
                " fixture_identity_digest,max_output_tokens,request_template,"
                " request_template_digest,derivation_rule_digest,chain_seed_digest,slot_digest)"
                " values (%s,%s,%s,%s,'derived-model-0',%s,1,1,%s,'provider-x',"
                " 'backend-x','provider:x:endpoint','provider:x:call',%s,"
                " 'chat-completions','call-1',"
                " %s,%s,256,%s::jsonb,%s,%s,%s,%s)",
                (
                    ids["slot"],
                    ids["realm"],
                    ids["manifest"],
                    ids["cohort"],
                    values["task"],
                    ids["episode"],
                    _sha("derived-endpoint"),
                    _sha("fixture"),
                    _sha("fixture-identity"),
                    json.dumps(
                        {
                            "schema": "zekam-capability-request-template/v1",
                            "model": "backend-x",
                            "system": "public",
                            "prompt_prefix": "public ",
                            "max_tokens": 256,
                        },
                        sort_keys=True,
                    ),
                    digest(
                        {
                            "schema": "zekam-capability-request-template/v1",
                            "model": "backend-x",
                            "system": "public",
                            "prompt_prefix": "public ",
                            "max_tokens": 256,
                        }
                    ),
                    _sha("derived-rule"),
                    values["seed"],
                    _sha("derived-slot"),
                ),
            )
            continuity = {
                "facts": [],
                "open_questions": [],
                "risks": [],
                "next_action": "start",
            }
            cursor.execute(
                "insert into models.capability_runtime_continuity_state"
                " (id,realm_id,manifest_id,slot_id,continuity_state,continuity_state_digest,"
                " prior_result_digest,derivation_attestation_digest,event_digest)"
                " values (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)",
                (
                    uuid4(),
                    ids["realm"],
                    ids["manifest"],
                    ids["slot"],
                    json.dumps(continuity, sort_keys=True),
                    digest(continuity),
                    values["seed"],
                    _sha("derived-attestation"),
                    _sha("derived-event"),
                ),
            )
            cursor.execute(
                "select request_body_digest,authorization_plan_digest,effect_digest"
                " from models.capability_runtime_derived_digests(%s,%s)",
                (ids["realm"], ids["slot"]),
            )
            derived = cursor.fetchone()
            values["request"], values["plan_digest"], values["effect"] = map(str, derived)
            cursor.execute(
                "insert into security.authorization"
                " (id,realm_id,actor_id,work_item_id,plan_id,plan_digest,effect_digest,scope,"
                " allowed_resources,allowed_effects,provider_refs,secret_ref_ids,risk,state,"
                " expires_at,authorization_digest)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,'critical','issued',"
                " now()+interval '1 hour',%s)",
                (
                    ids["authorization"],
                    ids["realm"],
                    ids["actor"],
                    ids["work"],
                    ids["plan"],
                    values["plan_digest"],
                    values["effect"],
                    '{"data_classifications":["public"]}',
                    ["provider:x:endpoint", "provider:x:call"],
                    ["provider-call"],
                    ["provider-x"],
                    [uuid4()],
                    values["authorization_digest"],
                ),
            )
        finally:
            cursor.execute("set session_replication_role=origin")
    return values


def test_current_migration_head_and_runtime_tables_are_rls_append_only(
    migrated_database: DatabaseSettings,
) -> None:
    assert migrations.discover_migrations()[-1].version == 36
    assert migrations.discover_migrations()[-1].has_down
    with connect(migrated_database) as connection, connection.cursor() as cursor:
        cursor.execute("select max(version) from core.schema_migrations")
        assert cursor.fetchone()[0] == 36
        cursor.execute(
            "select relname,relrowsecurity,relforcerowsecurity from pg_class"
            " join pg_namespace on pg_namespace.oid=pg_class.relnamespace"
            " where nspname='models' and relname=any(%s) order by relname",
            (list(TABLES),),
        )
        rows = cursor.fetchall()
        assert [str(row[0]) for row in rows] == sorted(TABLES)
        assert all(bool(row[1]) and bool(row[2]) for row in rows)
        cursor.execute(
            "select event_object_table,trigger_name from information_schema.triggers"
            " where event_object_schema='models' and event_object_table=any(%s)"
            " and trigger_name in ('deny_update','deny_delete')",
            (list(TABLES),),
        )
        triggers = {(str(row[0]), str(row[1])) for row in cursor.fetchall()}
    assert triggers == {
        (table, trigger) for table in TABLES for trigger in ("deny_update", "deny_delete")
    }


def test_migration_25_derivation_golden_down_and_reapply(
    postgres_settings: DatabaseSettings,
) -> None:
    database_name = f"zekam_test_{uuid4().hex[:12]}"
    scoped = DatabaseSettings(
        host=postgres_settings.host,
        port=postgres_settings.port,
        name=database_name,
        user=postgres_settings.user,
        sslmode=postgres_settings.sslmode,
    )
    template = {
        "schema": "zekam-capability-request-template/v1",
        "model": "model/test",
        "system": "system",
        "prompt_prefix": "prefix\n",
        "max_tokens": 17,
    }
    state = {
        "facts": ["alpha", "gamma"],
        "open_questions": ["beta?"],
        "risks": [],
        "next_action": "verify",
    }

    def request_digest(cursor: Any) -> str:
        cursor.execute(
            "select models.capability_runtime_jsonb_digest("
            "models.derive_capability_request_body(%s::jsonb,%s::jsonb))",
            (json.dumps(template, sort_keys=True), json.dumps(state, sort_keys=True)),
        )
        return str(cursor.fetchone()[0])

    with connect(postgres_settings) as connection, connection.cursor() as cursor:
        cursor.execute(f'create database "{database_name}"')
    try:
        with connect(scoped) as connection:
            migrations.upgrade(connection, target=24)
            with connection.cursor() as cursor:
                assert request_digest(cursor) == (
                    "sha256:4a5fd7d23042bb9429634a19d280bfd9e567284d4cb39b16ac7fe41adfcbc5b9"
                )
            migrations.upgrade(connection, target=25)
            with connection.cursor() as cursor:
                assert request_digest(cursor) == (
                    "sha256:09221c94678df030360a65799081883ef9b14549c97b4cfa7b3c55cffb9aa4f8"
                )
            migrations.downgrade(connection, target=25)
            restored = migrations.status(connection)
            assert restored.head == 24
            assert restored.drift == ()
            with connection.cursor() as cursor:
                assert request_digest(cursor) == (
                    "sha256:4a5fd7d23042bb9429634a19d280bfd9e567284d4cb39b16ac7fe41adfcbc5b9"
                )
                cursor.execute(
                    "select pg_get_functiondef("
                    "'models.enforce_capability_runtime_continuity()'::regprocedure)"
                )
                assert "zekam-capability-continuity-derive/v3" in str(cursor.fetchone()[0])
            migrations.upgrade(connection, target=25)
            reapplied = migrations.status(connection)
            assert reapplied.head == 25
            assert tuple(item.version for item in reapplied.pending) == tuple(range(26, 37))
    finally:
        with connect(postgres_settings) as connection, connection.cursor() as cursor:
            cursor.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname=%s",
                (database_name,),
            )
            cursor.execute(f'drop database if exists "{database_name}"')


def test_migration_24_down_restores_executable_head_23_functions(
    postgres_settings: DatabaseSettings,
) -> None:
    database_name = f"zekam_test_{uuid4().hex[:12]}"
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
            migrations.upgrade(connection, target=24)
            migrations.downgrade(connection, target=24)
            status = migrations.status(connection)
            assert status.head == 23
            assert status.drift == ()
            with connection.cursor() as cursor:
                cursor.execute(
                    "select pg_get_functiondef('models.enforce_capability_runtime_outcome()'"
                    "::regprocedure)"
                )
                restored_outcome = str(cursor.fetchone()[0])
                assert "successful_count<>168" in restored_outcome.replace(" ", "")
                assert "capability_runtime_episode_outcome" not in restored_outcome
                cursor.execute(
                    "create temp table restored_score_probe("
                    "realm_id uuid,cohort_id uuid,model_id text)"
                )
                cursor.execute(
                    "create trigger restored_score before insert on restored_score_probe"
                    " for each row execute function "
                    "models.enforce_capability_runtime_scorecard_gate()"
                )
                cursor.execute(
                    "insert into restored_score_probe values (%s,%s,'model')",
                    (uuid4(), uuid4()),
                )
                cursor.execute(
                    "create temp table restored_outcome_probe("
                    "realm_id uuid,manifest_id uuid,status text,actual_provider_calls integer,"
                    "actual_retries integer,call_evidence_digests text[])"
                )
                cursor.execute(
                    "create trigger restored_outcome before insert on restored_outcome_probe"
                    " for each row execute function models.enforce_capability_runtime_outcome()"
                )
                cursor.execute(
                    "insert into restored_outcome_probe values (%s,%s,'partial',0,0,'{}')",
                    (uuid4(), uuid4()),
                )
                with pytest.raises(Exception, match=r"aggregate count/status/evidence mismatch"):
                    cursor.execute(
                        "insert into restored_outcome_probe values (%s,%s,'completed',161,0,%s)",
                        (uuid4(), uuid4(), [_sha(f"restored-{index}") for index in range(161)]),
                    )
                cursor.execute(
                    "create temp table restored_call_probe("
                    "realm_id uuid,slot_id uuid,claim_id uuid,receipt_id uuid,"
                    "checkpoint_id uuid,status text,result_digest text,failure_category text)"
                )
                cursor.execute(
                    "create trigger restored_call before insert on restored_call_probe"
                    " for each row execute function "
                    "models.enforce_capability_runtime_call_outcome()"
                )
                with pytest.raises(
                    Exception, match=r"claim/receipt/checkpoint/job binding mismatch"
                ):
                    cursor.execute(
                        "insert into restored_call_probe values (%s,%s,%s,%s,%s,'failed',null,'x')",
                        (uuid4(), uuid4(), uuid4(), uuid4(), uuid4()),
                    )
            migrations.upgrade(connection, target=24)
            restored_24 = migrations.status(connection)
            assert restored_24.head == 24
            assert restored_24.drift == ()
    finally:
        with connect(postgres_settings) as connection, connection.cursor() as cursor:
            cursor.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname=%s",
                (database_name,),
            )
            cursor.execute(f'drop database if exists "{database_name}"')


def test_runtime_tables_reject_updates_even_when_no_row_matches(
    realm_session: tuple[Any, Any],
) -> None:
    _, connection = realm_session
    for table in TABLES:
        with (
            pytest.raises(
                Exception,
                match=r"append-only|degistirilemez|silinemez|permission denied",
            ),
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            cursor.execute(f"update models.{table} set id=id where false")


def test_runtime_schema_has_exact_slot_and_score_gates(
    migrated_database: DatabaseSettings,
) -> None:
    with connect(migrated_database) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select conname from pg_constraint c join pg_class t on t.oid=c.conrelid"
            " join pg_namespace n on n.oid=t.relnamespace"
            " where n.nspname='models' and t.relname in"
            " ('capability_runtime_approval_slot','capability_runtime_slot_authorization')"
        )
        constraints = {str(row[0]) for row in cursor.fetchall()}
        cursor.execute(
            "select tgname from pg_trigger t join pg_class c on c.oid=t.tgrelid"
            " join pg_namespace n on n.oid=c.relnamespace"
            " where n.nspname='models' and not t.tgisinternal"
        )
        triggers = {str(row[0]) for row in cursor.fetchall()}
        cursor.execute(
            "select column_name,is_generated,generation_expression"
            " from information_schema.columns where table_schema='models'"
            " and table_name='capability_runtime_outcome'"
            " and column_name in ('score_eligible','routing_eligible')"
        )
        generated = {str(row[0]): (str(row[1]), str(row[2])) for row in cursor.fetchall()}
        cursor.execute(
            "select pg_get_functiondef('models.enforce_capability_runtime_scorecard_gate()'"
            "::regprocedure)"
        )
        scorecard_gate = str(cursor.fetchone()[0])
    assert "capability_runtime_slot_exact_unique" in constraints
    assert "capability_runtime_slot_auth_authorization_unique" in constraints
    assert "capability_runtime_manifest_exact_slots" in triggers
    assert "capability_runtime_scorecard_gate" in triggers
    assert set(generated) == {"score_eligible", "routing_eligible"}
    assert all(value[0] == "ALWAYS" for value in generated.values())
    assert "completed" in generated["score_eligible"][1]
    assert "false" in generated["routing_eligible"][1]
    compact_gate = scorecard_gate.replace(" ", "")
    assert "episode.status<>'not-comparable'" in compact_gate
    assert "episode.noop_ratio<>1" in compact_gate


def test_sql_request_derivation_matches_python_golden_vector(
    migrated_database: DatabaseSettings,
) -> None:
    template = {
        "schema": "zekam-capability-request-template/v1",
        "model": "model/test",
        "system": "system",
        "prompt_prefix": "prefix\n",
        "max_tokens": 17,
    }
    state = {
        "facts": ["alpha", "gamma"],
        "open_questions": ["beta?"],
        "risks": [],
        "next_action": "verify",
    }
    with connect(migrated_database) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select models.capability_runtime_jsonb_digest(%s::jsonb),"
            " models.capability_runtime_jsonb_digest(%s::jsonb),"
            " models.capability_runtime_jsonb_digest("
            " models.derive_capability_request_body(%s::jsonb,%s::jsonb))",
            tuple(
                json.dumps(value, sort_keys=True) for value in (template, state, template, state)
            ),
        )
        row = cursor.fetchone()
        cursor.execute(
            "select models.capability_runtime_jsonb_digest(jsonb_build_object("
            " 'schema','zekam-capability-request-derivation/v1',"
            " 'algorithm','zekam-capability-continuity-derive/v4',"
            " 'template_digest',models.capability_runtime_jsonb_digest(%s::jsonb),"
            " 'continuity_state_digest',models.capability_runtime_jsonb_digest(%s::jsonb),"
            " 'request_body_digest',models.capability_runtime_jsonb_digest("
            " models.derive_capability_request_body(%s::jsonb,%s::jsonb))))",
            tuple(
                json.dumps(value, sort_keys=True) for value in (template, state, template, state)
            ),
        )
        attestation_digest = str(cursor.fetchone()[0])
    assert row == (
        "sha256:7cc1613f26ad1aa39ee75ae98680a4927ae19131f1d78efa349228b21c95502c",
        "sha256:95908edc8b10ee95b025cda83d35f49fd8e8986caddff5b1c325d42f87beb3ba",
        "sha256:09221c94678df030360a65799081883ef9b14549c97b4cfa7b3c55cffb9aa4f8",
    )
    assert attestation_digest == (
        "sha256:905c0875d749d492362d3a4c9037905188abeab8860aeba9d80d48e334a6b5a3"
    )


@pytest.mark.parametrize("mode", ["missing-receipts", "failed-call", "coordinator-failed"])
def test_completed_outcome_rejects_unsuccessful_or_nonterminal_runtime(
    migrated_database: DatabaseSettings, mode: str
) -> None:
    with connect(migrated_database) as connection:
        realm_id, manifest_id, evidence = _seed_adversarial_completed_candidate(
            connection, mode=mode
        )
        with (
            pytest.raises(Exception, match=r"aggregate count/status/evidence mismatch"),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "insert into models.capability_runtime_outcome"
                " (id,realm_id,manifest_id,status,actual_provider_calls,actual_retries,"
                " call_evidence_digests,evidence_digest,completed_at)"
                " values (%s,%s,%s,'completed',168,0,%s,%s,now())",
                (uuid4(), realm_id, manifest_id, list(evidence), _sha(f"outcome-{mode}")),
            )


@pytest.mark.parametrize(
    "mutation",
    ["189-steps", "191-steps", "wrong-dependency", "wrong-resource", "wrong-effect"],
)
def test_task_plan_190_gate_rejects_structure_tamper(
    migrated_database: DatabaseSettings, mutation: str
) -> None:
    with connect(migrated_database) as connection:
        realm_id, manifest_id, _ = _seed_adversarial_completed_candidate(
            connection, mode="missing-receipts"
        )
        with connection.cursor() as cursor:
            cursor.execute("set session_replication_role=replica")
            try:
                if mutation == "189-steps":
                    expression = "steps - 189"
                elif mutation == "191-steps":
                    expression = "steps || '[{}]'::jsonb"
                elif mutation == "wrong-dependency":
                    expression = "jsonb_set(steps,'{0,depends_on}','[\"tamper\"]')"
                elif mutation == "wrong-resource":
                    expression = "jsonb_set(steps,'{0,logical_resources}','[\"tamper\"]')"
                else:
                    expression = "jsonb_set(steps,'{0,effect}','\"database-write\"')"
                cursor.execute(
                    f"update work.task_plan set steps={expression} where id=(select task_plan_id"
                    " from models.capability_runtime_approval_manifest where id=%s)",
                    (manifest_id,),
                )
            finally:
                cursor.execute("set session_replication_role=origin")
            cursor.execute(
                "create temp table capability_plan_probe(manifest_id uuid,realm_id uuid)"
            )
            cursor.execute(
                "create trigger validate_probe after insert on capability_plan_probe"
                " for each row execute function models.validate_capability_runtime_slot_set()"
            )
        with (
            pytest.raises(Exception, match=r"exact 168-slot cartesian set"),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "insert into capability_plan_probe(manifest_id,realm_id) values (%s,%s)",
                (manifest_id, realm_id),
            )


def test_derived_authorization_rejects_prior_chain_tamper_and_double_issue(
    migrated_database: DatabaseSettings,
) -> None:
    with connect(migrated_database) as connection:
        values = _seed_authorization_derivation_candidate(connection)
        sql = (
            "insert into models.capability_runtime_slot_authorization"
            " (id,realm_id,manifest_id,slot_id,authorization_id,authorization_plan_digest,"
            " authorization_digest,request_body_digest,effect_digest,"
            " prior_response_chain_digest,binding_digest)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        base = (
            uuid4(),
            values["realm"],
            values["manifest"],
            values["slot"],
            values["authorization"],
            values["plan_digest"],
            values["authorization_digest"],
            values["request"],
            values["effect"],
            values["seed"],
            _sha("binding-valid"),
        )
        with (
            pytest.raises(Exception, match=r"tamper/scope/chain mismatch"),
            connection.cursor() as cursor,
        ):
            cursor.execute(sql, (*base[:-2], _sha("wrong-prior"), base[-1]))
        with connection.cursor() as cursor:
            cursor.execute(sql, base)
        with (
            pytest.raises(Exception, match=r"unique|duplicate"),
            connection.cursor() as cursor,
        ):
            cursor.execute(sql, (uuid4(), *base[1:-1], _sha("binding-replay")))


@pytest.mark.parametrize("mutation", ["call-resource", "endpoint-operation"])
def test_slot_rejects_provider_resource_or_operation_mismatch(
    migrated_database: DatabaseSettings, mutation: str
) -> None:
    with connect(migrated_database) as connection:
        values = _seed_authorization_derivation_candidate(connection)
        call_id = f"call-tamper-{mutation}"
        call_resource = f"provider:derived-model-0:chat-completions:{call_id}"
        endpoint_resource = "provider-x:endpoint:chat-completions"
        if mutation == "call-resource":
            call_resource = f"provider:derived-model-0:code-completions:{call_id}"
        else:
            endpoint_resource = "provider-x:endpoint:code-completions"
        template = {
            "schema": "zekam-capability-request-template/v1",
            "model": "backend-x",
            "system": "public",
            "prompt_prefix": "public ",
            "max_tokens": 256,
        }
        with (
            pytest.raises(Exception, match=r"slot template/job binding mismatch"),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "insert into models.capability_runtime_approval_slot"
                " (id,realm_id,manifest_id,cohort_id,model_id,task_digest,turn_number,ordinal,"
                " job_id,provider_ref,backend_model,endpoint_resource,call_resource,"
                " endpoint_identity_digest,operation,call_id,fixture_digest,"
                " fixture_identity_digest,max_output_tokens,request_template,"
                " request_template_digest,derivation_rule_digest,chain_seed_digest,slot_digest)"
                " values (%s,%s,%s,%s,'derived-model-0',%s,2,2,%s,'provider-x','backend-x',"
                " %s,%s,%s,'chat-completions',%s,%s,%s,256,%s::jsonb,%s,%s,%s,%s)",
                (
                    uuid4(),
                    values["realm"],
                    values["manifest"],
                    values["cohort"],
                    values["task"],
                    values["episode"],
                    endpoint_resource,
                    call_resource,
                    _sha("tampered-endpoint"),
                    call_id,
                    _sha("tampered-fixture"),
                    _sha("tampered-fixture-identity"),
                    json.dumps(template, sort_keys=True),
                    digest(template),
                    _sha("tampered-rule"),
                    _sha("tampered-seed"),
                    _sha(f"tampered-slot-{mutation}"),
                ),
            )


def test_call_outcome_rejects_claim_operation_tamper(
    migrated_database: DatabaseSettings,
) -> None:
    with connect(migrated_database) as connection:
        values = _seed_authorization_derivation_candidate(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "select effect_action,claim_operation"
                " from models.capability_runtime_derived_digests(%s,%s)",
                (values["realm"], values["slot"]),
            )
            effect_action, claim_operation = map(str, cursor.fetchone())
            assert effect_action == "provider-contract-call-" + digest(
                {
                    "request_identity": "call-1",
                    "payload_digest": values["request"],
                    "plan_digest": values["plan_digest"],
                }
            ).removeprefix("sha256:")
            assert claim_operation == "provider-contract:call-1"
            cursor.execute(
                "insert into models.capability_runtime_slot_authorization"
                " (id,realm_id,manifest_id,slot_id,authorization_id,authorization_plan_digest,"
                " authorization_digest,request_body_digest,effect_digest,"
                " prior_response_chain_digest,binding_digest)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    uuid4(),
                    values["realm"],
                    values["manifest"],
                    values["slot"],
                    values["authorization"],
                    values["plan_digest"],
                    values["authorization_digest"],
                    values["request"],
                    values["effect"],
                    values["seed"],
                    _sha("operation-binding"),
                ),
            )
            cursor.execute(
                "select id from models.capability_runtime_continuity_state where slot_id=%s",
                (values["slot"],),
            )
            continuity_id = cursor.fetchone()[0]
            checkpoint_id, claim_id, receipt_id = uuid4(), uuid4(), uuid4()
            result_digest = _sha("operation-result")
            cursor.execute("set session_replication_role=replica")
            try:
                cursor.execute(
                    "insert into models.capability_runtime_turn_checkpoint"
                    " (id,realm_id,manifest_id,slot_id,continuity_state_id,job_id,"
                    " completed_turns,pending_turns,result_digest,checkpoint_digest)"
                    " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        checkpoint_id,
                        values["realm"],
                        values["manifest"],
                        values["slot"],
                        continuity_id,
                        values["episode"],
                        [1],
                        list(range(2, 9)),
                        result_digest,
                        _sha("operation-checkpoint"),
                    ),
                )
                cursor.execute(
                    "insert into runtime.effect_claim"
                    " (id,realm_id,job_id,attempt_id,operation,effect_digest,"
                    " authorization_digest,authorization_id,idempotency_key,resources,"
                    " execution_identity,fencing_token,adapter_digest,claim_digest)"
                    " values (%s,%s,%s,%s,'provider-contract:tampered',%s,%s,%s,%s,"
                    " '[]'::jsonb,'test',1,%s,%s)",
                    (
                        claim_id,
                        values["realm"],
                        values["episode"],
                        uuid4(),
                        values["effect"],
                        values["authorization_digest"],
                        values["authorization"],
                        _sha("operation-idempotency"),
                        _sha("operation-adapter"),
                        _sha("operation-claim"),
                    ),
                )
                cursor.execute(
                    "insert into runtime.effect_receipt"
                    " (id,realm_id,claim_id,status,result_digest)"
                    " values (%s,%s,%s,'completed',%s)",
                    (receipt_id, values["realm"], claim_id, result_digest),
                )
            finally:
                cursor.execute("set session_replication_role=origin")
        with (
            pytest.raises(Exception, match=r"claim/receipt/checkpoint/job binding mismatch"),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "insert into models.capability_runtime_call_outcome"
                " (id,realm_id,slot_id,claim_id,receipt_id,checkpoint_id,status,"
                " result_digest,evidence_digest,completed_at)"
                " values (%s,%s,%s,%s,%s,%s,'completed',%s,%s,now())",
                (
                    uuid4(),
                    values["realm"],
                    values["slot"],
                    claim_id,
                    receipt_id,
                    checkpoint_id,
                    result_digest,
                    _sha("operation-outcome"),
                ),
            )


def test_terminal_episode_rejects_attempted_skipped_partition_tamper(
    migrated_database: DatabaseSettings,
) -> None:
    with connect(migrated_database) as connection:
        realm_id, manifest_id, _ = _seed_adversarial_completed_candidate(
            connection, mode="missing-receipts"
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into core.realm(id,slug,display_name) values (%s,%s,'Terminal realm')",
                (realm_id, f"terminal-{str(realm_id)[:8]}"),
            )
            cursor.execute(
                "select model_id,task_digest,job_id from models.capability_runtime_approval_slot"
                " where realm_id=%s and manifest_id=%s order by ordinal limit 1",
                (realm_id, manifest_id),
            )
            model_id, task_digest, job_id = cursor.fetchone()
        with (
            pytest.raises(Exception, match=r"episode terminal evidence mismatch"),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "insert into models.capability_runtime_episode_outcome"
                " (id,realm_id,manifest_id,model_id,task_digest,job_id,status,"
                " attempted_calls,successful_calls,failure_turn,reason_code,evidence_digest,"
                " completed_at) values (%s,%s,%s,%s,%s,%s,'model-contract-failed',"
                " 1,1,1,'model-contract-failure',%s,now())",
                (
                    uuid4(),
                    realm_id,
                    manifest_id,
                    model_id,
                    task_digest,
                    job_id,
                    _sha("tampered-terminal-episode"),
                ),
            )


def test_scorecard_rejects_missing_terminal_episode_correspondence(
    migrated_database: DatabaseSettings,
) -> None:
    with connect(migrated_database) as connection:
        realm_id, manifest_id, evidence = _seed_adversarial_completed_candidate(
            connection, mode="missing-receipts"
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "select cohort_id,model_ids[1] from models.capability_runtime_approval_manifest"
                " where id=%s",
                (manifest_id,),
            )
            cohort_id, model_id = cursor.fetchone()
            cursor.execute("set session_replication_role=replica")
            try:
                cursor.execute(
                    "insert into models.capability_runtime_outcome"
                    " (id,realm_id,manifest_id,status,actual_provider_calls,actual_retries,"
                    " call_evidence_digests,evidence_digest,completed_at)"
                    " values (%s,%s,%s,'completed',168,0,%s,%s,now())",
                    (uuid4(), realm_id, manifest_id, list(evidence), _sha("score-ready")),
                )
            finally:
                cursor.execute("set session_replication_role=origin")
            cursor.execute(
                "create temp table capability_scorecard_probe("
                "realm_id uuid,cohort_id uuid,model_id text)"
            )
            cursor.execute(
                "create trigger scorecard_probe before insert on capability_scorecard_probe"
                " for each row execute function models.enforce_capability_runtime_scorecard_gate()"
            )
        with (
            pytest.raises(Exception, match=r"exact terminal episode correspondence"),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "insert into capability_scorecard_probe values (%s,%s,%s)",
                (realm_id, cohort_id, model_id),
            )


def test_domain_rejects_scope_or_budget_drift_and_partial_is_not_eligible() -> None:
    manifest = CapabilityRuntimeApprovalManifest(
        cohort_id=uuid4(),
        work_item_id=uuid4(),
        task_plan_id=uuid4(),
        coordinator_job_id=uuid4(),
        source_revision="revision-23",
        model_ids=tuple(f"model-{index}" for index in range(7)),
        task_digests=tuple(_sha(f"task-{index}") for index in range(3)),
        approval_evidence_digest=_sha("approval"),
    )
    assert manifest.max_provider_calls == MAX_PROVIDER_CALLS == 168
    assert manifest.max_retries == 0
    partial = CapabilityRuntimeOutcome(
        status=CapabilityRuntimeStatus.PARTIAL,
        actual_provider_calls=1,
        actual_retries=0,
        call_evidence_digests=(_sha("call-1"),),
        evidence_digest=_sha("partial"),
        completed_at=dt.datetime.now(dt.UTC),
    )
    assert partial.score_eligible is False
    assert partial.routing_eligible is False
    complete = CapabilityRuntimeOutcome(
        status=CapabilityRuntimeStatus.COMPLETED,
        actual_provider_calls=168,
        actual_retries=0,
        call_evidence_digests=tuple(_sha(f"complete-{index}") for index in range(168)),
        evidence_digest=_sha("complete"),
        completed_at=dt.datetime.now(dt.UTC),
    )
    assert complete.score_eligible is True
    assert complete.routing_eligible is False
    contract_terminal = CapabilityRuntimeOutcome(
        status=CapabilityRuntimeStatus.COMPLETED,
        actual_provider_calls=161,
        actual_retries=0,
        call_evidence_digests=tuple(_sha(f"contract-{index}") for index in range(161)),
        evidence_digest=_sha("contract-terminal"),
        completed_at=dt.datetime.now(dt.UTC),
        successful_episode_count=20,
        contract_failed_episode_count=1,
        skipped_slot_count=7,
    )
    assert contract_terminal.score_eligible is True
    assert contract_terminal.routing_eligible is False
    with pytest.raises(ValidationFailed, match="retry"):
        CapabilityRuntimeOutcome(
            status=CapabilityRuntimeStatus.PARTIAL,
            actual_provider_calls=1,
            actual_retries=1,
            call_evidence_digests=(_sha("call-1"),),
            evidence_digest=_sha("retry"),
            completed_at=dt.datetime.now(dt.UTC),
        )
    with pytest.raises(ValidationFailed, match="terminal 21x8 partition"):
        CapabilityRuntimeOutcome(
            status=CapabilityRuntimeStatus.COMPLETED,
            actual_provider_calls=167,
            actual_retries=0,
            call_evidence_digests=tuple(_sha(f"call-{index}") for index in range(167)),
            evidence_digest=_sha("early-complete"),
            completed_at=dt.datetime.now(dt.UTC),
        )
