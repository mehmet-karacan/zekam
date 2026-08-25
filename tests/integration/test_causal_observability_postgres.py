from __future__ import annotations

import datetime as dt
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.canonical import digest
from zekam.domain.realm import Realm
from zekam.infrastructure.postgres.connection import configure_session
from zekam.infrastructure.postgres.core_repository import RealmRepository
from zekam.infrastructure.postgres.observatory_projection import (
    PostgresObservatoryProjectionReader,
)
from zekam.interfaces.cli.scheduler import _causal_chain_rows, _orphan_rows

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
D = "sha256:" + "a" * 64


def test_correlation_chain_and_orphan_detector_use_canonical_rows(
    realm_session, migrated_database, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    source = tmp_path / "source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    (
        work_id,
        plan_id,
        assignment_id,
        verifier_assignment_id,
        job_id,
        second_job_id,
        completed_job_id,
        attempt_id,
        invocation_id,
        context_id,
        manifest_id,
        memory_id,
        loop_id,
        stale_loop_attempt_id,
        recent_loop_attempt_id,
        stale_claim_id,
        recent_claim_id,
        recent_invocation_id,
        recent_outbox_id,
        event_id,
        outbox_id,
    ) = (uuid4() for _ in range(21))
    stale = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
    forged_model_attempt_id = uuid4()
    forged_model_result_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into work.work_item"
            " (id,realm_id,project_id,type,state,title,record_digest,created_at,updated_at)"
            " values(%s,%s,%s,'task','active','causal test',%s,%s,%s)",
            (work_id, realm.id, project.id, D, stale, stale),
        )
        cursor.execute(
            "insert into work.task_plan"
            " (id,realm_id,project_id,work_item_id,revision,source_revision,policy_digest,"
            "steps,effect_digest,plan_digest,created_at) values"
            " (%s,%s,%s,%s,1,'causal-revision',%s,"
            ' \'[{"step_id":"observe","effect":"none"}]\'::jsonb,%s,%s,%s)',
            (plan_id, realm.id, project.id, work_id, D, digest("effect"), digest("plan"), stale),
        )
        cursor.execute(
            "insert into agents.assignment"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,role,agent_ref,status,risk,"
            "instruction_digest,context_manifest_digest,assignment_digest,created_at)"
            " values(%s,%s,%s,%s,%s,'observe','coordinator','causal-agent','active','medium',"
            "%s,%s,%s,%s)",
            (
                assignment_id,
                realm.id,
                project.id,
                work_id,
                plan_id,
                D,
                D,
                digest(str(assignment_id)),
                stale,
            ),
        )
        cursor.execute(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,idempotency_key,"
            "assignment_id,created_at,updated_at) values"
            " (%s,%s,%s,%s,%s,'observe','read-only','running',%s,%s,%s,%s)",
            (
                job_id,
                realm.id,
                project.id,
                work_id,
                plan_id,
                f"causal-{job_id}",
                assignment_id,
                stale,
                stale,
            ),
        )
        cursor.execute(
            "insert into agents.assignment"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,"
            "role,agent_ref,status,risk,instruction_digest,context_manifest_digest,"
            "assignment_digest,created_at) values"
            " (%s,%s,%s,%s,%s,'observe',%s,'verifier','causal-verifier','active','medium',"
            "%s,%s,%s,%s)",
            (
                verifier_assignment_id,
                realm.id,
                project.id,
                work_id,
                plan_id,
                assignment_id,
                digest("verifier-instruction"),
                D,
                digest(str(verifier_assignment_id)),
                stale,
            ),
        )
        cursor.execute(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,kind,state,idempotency_key,assignment_id,"
            "created_at,updated_at) values"
            " (%s,%s,%s,%s,'verification','completed',%s,%s,%s,%s)",
            (
                completed_job_id,
                realm.id,
                project.id,
                work_id,
                f"causal-{completed_job_id}",
                verifier_assignment_id,
                stale,
                stale,
            ),
        )
        cursor.execute(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,kind,state,idempotency_key,"
            "assignment_id,created_at,updated_at) values"
            " (%s,%s,%s,%s,%s,'observe','read-only','ready',%s,%s,%s,%s)",
            (
                second_job_id,
                realm.id,
                project.id,
                work_id,
                plan_id,
                f"causal-{second_job_id}",
                assignment_id,
                stale,
                stale,
            ),
        )
        cursor.execute(
            "insert into runtime.job_attempt"
            " (id,realm_id,job_id,attempt_number,fencing_token,worker_label,started_at)"
            " values(%s,%s,%s,1,1,'causal-worker',%s)",
            (attempt_id, realm.id, job_id, stale),
        )
        cursor.execute(
            "insert into agents.invocation"
            " (id,realm_id,assignment_id,client_id,execution_identity,invocation_digest,created_at)"
            " values(%s,%s,%s,'codex','causal-exec',%s,%s)",
            (invocation_id, realm.id, assignment_id, digest(str(invocation_id)), stale),
        )
        cursor.execute(
            "insert into agents.invocation"
            " (id,realm_id,assignment_id,client_id,execution_identity,invocation_digest,created_at)"
            " values(%s,%s,%s,'codex','recent-exec',%s,statement_timestamp())",
            (
                recent_invocation_id,
                realm.id,
                verifier_assignment_id,
                digest(str(recent_invocation_id)),
            ),
        )
        for claim_id, claimed_at in (
            (stale_claim_id, stale),
            (recent_claim_id, dt.datetime.now(dt.UTC)),
        ):
            cursor.execute(
                "insert into runtime.effect_claim"
                " (id,realm_id,job_id,attempt_id,operation,effect_digest,authorization_digest,"
                "idempotency_key,execution_identity,fencing_token,adapter_digest,claim_digest,"
                "claimed_at) values(%s,%s,%s,%s,'observe',%s,%s,%s,'causal-exec',1,%s,%s,%s)",
                (
                    claim_id,
                    realm.id,
                    job_id,
                    attempt_id,
                    digest(f"effect:{claim_id}"),
                    digest("authorization"),
                    f"claim-{claim_id}",
                    digest("adapter"),
                    digest(f"claim:{claim_id}"),
                    claimed_at,
                ),
            )
        cursor.execute(
            "insert into work.context_manifest"
            " (id,realm_id,project_id,work_item_id,token_budget,selected,omitted,"
            "candidate_fingerprint,manifest_digest,created_at)"
            " values(%s,%s,%s,%s,100,'[]'::jsonb,'[]'::jsonb,%s,%s,%s)",
            (context_id, realm.id, project.id, work_id, D, digest("context"), stale),
        )
        # Canonical loop rows are normally written through the guarded functions.
        # The acceptance fixture seeds both sides of the grace boundary directly
        # as the database owner so the read-only detector can be isolated.
        cursor.execute("reset role")
        cursor.execute(
            "insert into runtime.loop_policy"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,assignment_id,"
            "context_manifest_id,validator_assignment_id,max_attempts,max_tokens,"
            "max_cost_micros,deadline,validator_spec_digest,required_delta,forbidden_effects,"
            "terminal_states,source_revision,context_manifest_digest,plan_digest,"
            "policy_revision_digest,canonical_effect_kind,created_at,canonical_body,"
            "policy_digest) values"
            " (%s,%s,%s,%s,%s,'observe',%s,%s,%s,3,1000,1000,%s,%s,"
            "array['new-evidence']::text[],'{}'::text[],"
            "array['blocked','budget-exhausted','manual-review','passed']::text[],"
            "'causal-revision',%s,%s,%s,'none',%s,'{}'::jsonb,%s)",
            (
                loop_id,
                realm.id,
                project.id,
                work_id,
                plan_id,
                assignment_id,
                context_id,
                verifier_assignment_id,
                dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
                digest("verifier-instruction"),
                digest("context"),
                digest("plan"),
                digest("policy-revision"),
                stale,
                digest(f"loop:{loop_id}"),
            ),
        )
        for loop_attempt_id, predecessor, ordinal, admitted_at in (
            (stale_loop_attempt_id, None, 1, stale),
            (recent_loop_attempt_id, stale_loop_attempt_id, 2, dt.datetime.now(dt.UTC)),
        ):
            cursor.execute(
                "insert into runtime.loop_attempt"
                " (id,realm_id,loop_id,predecessor_attempt_id,ordinal,semantic_request_digest,"
                "prompt_digest,context_digest,action_digest,binding_digest,source_revision,"
                "plan_digest,policy_revision_digest,validator_spec_digest,canonical_effect_kind,"
                "effect_class,reserved_input_tokens,reserved_output_tokens,reserved_cost_micros,"
                "delta_digest,admitted_at) values"
                " (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'causal-revision',%s,%s,%s,'none',"
                "'read-only',10,10,10,%s,%s)",
                (
                    loop_attempt_id,
                    realm.id,
                    loop_id,
                    predecessor,
                    ordinal,
                    digest(f"semantic:{loop_attempt_id}"),
                    digest(f"prompt:{loop_attempt_id}"),
                    digest(f"context:{loop_attempt_id}"),
                    digest(f"action:{loop_attempt_id}"),
                    digest(f"binding:{loop_attempt_id}"),
                    digest("plan"),
                    digest("policy-revision"),
                    digest("verifier-instruction"),
                    digest(f"delta:{loop_attempt_id}"),
                    admitted_at,
                ),
            )
        cursor.execute("set role zekam_app")
        missing = [
            "authorization_scope_digest",
            "checkpoint_digest",
            "context_fragment_set_digest",
            "context_packet_digest",
            "execution_envelope_digest",
            "execution_envelope_id",
            "max_cost_micros",
            "max_input_tokens",
            "max_output_tokens",
            "model_visible_payload_digest",
            "output_schema_digest",
            "policy_digest",
            "route_expires_at",
            "run_id",
            "source_revision",
        ]
        cursor.execute(
            "insert into models.request_manifest"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,job_id,attempt_id,"
            "assignment_id,role,risk,route_decision_digest,model_id,provider_ref,"
            "context_manifest_digest,payload_digest,idempotency_key,deadline,source_label,"
            "missing_bindings,binding_status,created_at,manifest_digest) values"
            " (%s,%s,%s,%s,%s,'observe',%s,%s,%s,'implementer','low',%s,'model-x',"
            "'provider-x',%s,%s,%s,%s,'provider-contract',%s,'unbound',%s,%s)",
            (
                manifest_id,
                realm.id,
                project.id,
                work_id,
                plan_id,
                completed_job_id,
                attempt_id,
                verifier_assignment_id,
                digest("route"),
                digest("context"),
                digest("payload"),
                f"manifest-{manifest_id}",
                stale + dt.timedelta(hours=1),
                missing,
                stale,
                digest(str(manifest_id)),
            ),
        )
        cursor.execute(
            "insert into models.invocation_attempt"
            " (id,realm_id,manifest_id,ordinal,state,created_at)"
            " values(%s,%s,%s,1,'verified',%s)",
            (forged_model_attempt_id, realm.id, manifest_id, stale),
        )
        cursor.execute(
            "insert into models.invocation_result"
            " (id,realm_id,manifest_id,attempt_id,state,response_digest,created_at)"
            " values(%s,%s,%s,%s,'verified',%s,%s)",
            (
                forged_model_result_id,
                realm.id,
                manifest_id,
                forged_model_attempt_id,
                digest("unbound-verified-response"),
                stale,
            ),
        )
        cursor.execute(
            "insert into memory.candidate"
            " (id,realm_id,scope,project_id,work_item_id,logical_candidate_id,project_ref,"
            "work_ref,memory_class,content,author_ref,evidence,created_at) values"
            " (%s,%s,'work-item',%s,%s,%s,%s,%s,'working','causal memory','verifier',"
            "'[]'::jsonb,%s)",
            (
                memory_id,
                realm.id,
                project.id,
                work_id,
                f"candidate:{memory_id}",
                project.slug,
                f"work-digest:{D}",
                stale,
            ),
        )
        cursor.execute(
            "insert into runtime.execution_event"
            " (id,realm_id,job_id,event_type,occurred_at) values(%s,%s,%s,'job.started',%s)",
            (event_id, realm.id, job_id, stale),
        )
        cursor.execute(
            "insert into runtime.outbox_event"
            " (id,realm_id,job_id,event_type,created_at) values(%s,%s,%s,'job.started',%s)",
            (outbox_id, realm.id, job_id, stale),
        )
        cursor.execute(
            "insert into runtime.outbox_event"
            " (id,realm_id,job_id,event_type,created_at)"
            " values(%s,%s,%s,'job.heartbeat',statement_timestamp())",
            (recent_outbox_id, realm.id, job_id),
        )
        cursor.execute(
            "select record_type,node_id,source_node_id,target_node_id,kind "
            "from ops.causal_chain(%s,256)",
            (work_id,),
        )
        chain = cursor.fetchall()
        cursor.execute("select orphan_kind,node_id from ops.causal_orphan order by orphan_kind")
        orphans = cursor.fetchall()

    node_ids = {row[1] for row in chain if row[0] == "node"}
    edge_kinds = {row[4] for row in chain if row[0] == "edge"}
    assert {
        f"work:{work_id}",
        f"plan-step:{plan_id}:observe",
        f"job:{job_id}",
        f"assignment:{assignment_id}",
        f"context:{context_id}",
        f"route:{manifest_id}",
        f"memory-candidate:{memory_id}",
    } <= node_ids
    assert len([node for node in node_ids if node == f"assignment:{assignment_id}"]) == 1
    assert {
        "planned-step",
        "assigned-step",
        "scheduled-step-job",
        "scheduled-job",
        "assigned-job",
        "dispatched-agent",
        "compiled-context",
        "informed-route",
        "selected-invocation",
    } <= edge_kinds
    assert {"emitted-event", "queued-outbox-event"} <= edge_kinds
    assert ("running-job-without-live-lease", f"job:{job_id}") in orphans
    assert ("agent-invocation-without-result", f"invocation:{invocation_id}") in orphans
    assert ("outbox-event-not-published", f"outbox:{outbox_id}") in orphans
    assert ("claim-without-terminal-receipt", f"effect-claim:{stale_claim_id}") in orphans
    assert ("claim-without-terminal-receipt", f"effect-claim:{recent_claim_id}") not in orphans
    assert ("agent-invocation-without-result", f"invocation:{recent_invocation_id}") not in orphans
    assert ("outbox-event-not-published", f"outbox:{recent_outbox_id}") not in orphans
    assert ("loop-attempt-without-outcome", f"loop-attempt:{stale_loop_attempt_id}") in orphans
    assert (
        "loop-attempt-without-outcome",
        f"loop-attempt:{recent_loop_attempt_id}",
    ) not in orphans
    assert (
        "completed-agentic-job-without-checkpoint",
        f"job:{completed_job_id}",
    ) in orphans
    assert (
        "completed-agentic-job-without-verified-result",
        f"job:{completed_job_id}",
    ) in orphans

    projection = PostgresObservatoryProjectionReader(migrated_database, realm.id).read().causal
    assert projection.available is True
    assert {item.orphan_kind for item in projection.orphans} >= {
        "running-job-without-live-lease",
        "agent-invocation-without-result",
    }
    assert all(item.canonical_ref.startswith("db:") for item in projection.nodes)
    assert projection.grants_authority is False

    resolved_work_id, cli_chain, cli_truncated = _causal_chain_rows(connection, str(work_id))
    cli_orphans = _orphan_rows(connection)
    assert resolved_work_id == str(work_id)
    assert cli_truncated is False
    assert any(row["node_id"] == f"work:{work_id}" for row in cli_chain)
    assert any(
        row["record_type"] == "edge" and row["kind"] == "selected-invocation" for row in cli_chain
    )
    assert any(
        row["node_id"] == f"effect-claim:{stale_claim_id}"
        and row["orphan_kind"] == "claim-without-terminal-receipt"
        for row in cli_orphans
    )
    assert all(row["canonical_ref"].startswith("db:") for row in cli_orphans)


def test_causal_views_cannot_cross_realm_boundary(realm_session, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    visible_realm, connection = realm_session
    hidden_realm = Realm.create(slug=f"hidden-{uuid4().hex[:8]}", display_name="Hidden realm")
    configure_session(connection, realm_id=hidden_realm.id, role=None)
    RealmRepository(connection).create(hidden_realm)
    configure_session(connection, realm_id=hidden_realm.id)
    source = tmp_path / "hidden-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, hidden_realm).register(source_path=source)
    hidden_work = uuid4()
    hidden_job = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into work.work_item"
            " (id,realm_id,project_id,type,state,title,record_digest)"
            " values(%s,%s,%s,'task','active','hidden causal row',%s)",
            (hidden_work, hidden_realm.id, project.id, D),
        )
        cursor.execute(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,kind,state,idempotency_key)"
            " values(%s,%s,%s,%s,'read-only','running',%s)",
            (hidden_job, hidden_realm.id, project.id, hidden_work, f"hidden-{hidden_job}"),
        )
    configure_session(connection, realm_id=visible_realm.id)
    with connection.cursor() as cursor:
        cursor.execute(
            "select node_id from ops.causal_node where node_id=%s",
            (f"work:{hidden_work}",),
        )
        assert cursor.fetchall() == []
        cursor.execute("select * from ops.causal_chain(%s,256)", (hidden_work,))
        assert cursor.fetchall() == []
        cursor.execute(
            "select target_node_id from ops.causal_edge where target_node_id=%s",
            (f"job:{hidden_job}",),
        )
        assert cursor.fetchall() == []
        cursor.execute(
            "select node_id from ops.causal_orphan where node_id=%s",
            (f"job:{hidden_job}",),
        )
        assert cursor.fetchall() == []


def test_projection_filters_edges_to_selected_nodes_before_bounded_limit(
    realm_session, migrated_database, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    source = tmp_path / "dense-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    work_id = uuid4()
    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    focus_job = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into work.work_item"
            " (id,realm_id,project_id,type,state,title,record_digest)"
            " values(%s,%s,%s,'task','active','dense causal graph',%s)",
            (work_id, realm.id, project.id, D),
        )
        cursor.executemany(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,kind,state,idempotency_key,"
            "created_at,updated_at) values(%s,%s,%s,%s,'read-only','ready',%s,%s,%s)",
            (
                (
                    UUID(int=index + 1),
                    realm.id,
                    project.id,
                    work_id,
                    f"dense-{index}",
                    old,
                    old,
                )
                for index in range(520)
            ),
        )
        cursor.execute(
            "insert into runtime.job"
            " (id,realm_id,project_id,work_item_id,kind,state,idempotency_key)"
            " values(%s,%s,%s,%s,'read-only','ready','dense-focus')",
            (focus_job, realm.id, project.id, work_id),
        )

    causal = PostgresObservatoryProjectionReader(migrated_database, realm.id).read().causal
    assert any(item.node_id == f"job:{focus_job}" for item in causal.nodes)
    assert causal.truncated is True
    assert any(
        edge.source_node_id == f"work:{work_id}"
        and edge.target_node_id == f"job:{focus_job}"
        and edge.kind == "scheduled-job"
        for edge in causal.edges
    )

    resolved_work_id, cli_chain, cli_truncated = _causal_chain_rows(connection, str(work_id))
    cli_node_ids = {row["node_id"] for row in cli_chain if row["record_type"] == "node"}
    assert resolved_work_id == str(work_id)
    assert cli_truncated is True
    assert len(cli_chain) <= 256
    assert all(
        row["source_node_id"] in cli_node_ids and row["target_node_id"] in cli_node_ids
        for row in cli_chain
        if row["record_type"] == "edge"
    )
