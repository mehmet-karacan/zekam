"""Canonical OpenCode lifecycle ingest and acknowledgement repository."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.clients import ClientKind
from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7


@dataclass(frozen=True, slots=True)
class LifecycleAck:
    event_id: UUID
    local_event_digest: str
    canonical_digest: str
    acknowledged_at: dt.datetime
    compaction_outbox_id: UUID | None = None
    compaction_payload_digest: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {
            "event_id": str(self.event_id),
            "local_event_digest": self.local_event_digest,
            "canonical_digest": self.canonical_digest,
            "acknowledged_at": self.acknowledged_at.isoformat(),
        }
        if self.compaction_outbox_id is not None:
            result["compaction_outbox_id"] = str(self.compaction_outbox_id)
            result["compaction_payload_digest"] = str(self.compaction_payload_digest)
        return result


@dataclass(frozen=True, slots=True)
class ActiveLifecycleExecution:
    """Read-only proof that one delivery is attached to the live worker envelope."""

    project_id: UUID
    work_item_id: UUID
    plan_id: UUID
    run_id: UUID
    envelope_id: UUID
    envelope_digest: str
    source_revision: str
    source_digest: str
    policy_digest: str
    migration_digest: str
    context_manifest_digest: str
    journal_head_digest: str
    work_plan_digest: str


@dataclass(frozen=True, slots=True)
class LifecycleTerminalRecord:
    """Exact immutable rows used to rebuild a local spool acknowledgement."""

    continuity_event_id: UUID
    continuity_event_digest: str
    delivery_outbox_id: UUID
    terminal_receipt_digest: str
    compiler_enqueue: bool
    effect_receipt_id: UUID
    effect_result_digest: str
    checkpoint_id: UUID
    checkpoint_digest: str
    adapter_evidence_digest: str


@dataclass(frozen=True, slots=True)
class HookTerminalOutput:
    receipt_id: UUID
    output_digest: str
    compiler_enqueue: bool


@dataclass(frozen=True, slots=True)
class ClientLifecycleRepository:
    connection: Any
    realm_id: UUID

    def next_codex_lifecycle_job_id(self) -> UUID | None:
        """Select only the dedicated immutable Codex lifecycle queue contract."""

        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select id from runtime.job where realm_id=%s and state='ready'"
                " and kind='mutation' and max_attempts=1"
                " and payload->>'schema'='zekam-codex-lifecycle-job/v1'"
                " and jsonb_typeof(payload->'authorization_id')='string'"
                " and payload->>'authorization_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
                " and (select count(*) from jsonb_object_keys(payload))=2"
                " and required_capabilities=array['client.lifecycle.codex-drain']::text[]"
                " and read_resources='{}'::text[] and cardinality(write_resources)=1"
                " and work_item_id is not null and plan_id is not null and step_id is not null"
                " and assignment_id is not null and run_id is not null"
                " and available_at<=clock_timestamp()"
                " and exists(select 1 from security.authorization authorization"
                " where authorization.realm_id=runtime.job.realm_id"
                " and authorization.id::text=payload->>'authorization_id'"
                " and authorization.state='issued' and authorization.expires_at>clock_timestamp())"
                " and not exists(select 1 from runtime.effect_claim claim"
                " where claim.realm_id=runtime.job.realm_id and claim.job_id=runtime.job.id)"
                " order by priority,available_at,created_at,id limit 1",
                (self.realm_id,),
            )
            row = cursor.fetchone()
        return None if row is None else UUID(str(row[0]))

    def committed_admission_exists(self, entry_digest: str) -> bool:
        parse_digest(entry_digest)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from client.codex_lifecycle_admission"
                " where realm_id=%s and entry_digest=%s",
                (self.realm_id, entry_digest),
            )
            count = int(cursor.fetchone()[0])
        if count > 1:
            raise PolicyViolation("Codex lifecycle entry duplicate governed admission tasiyor")
        return count == 1

    def resolve_committed_delivery(
        self,
        *,
        entry_digest: str,
        idempotency_key: str,
        canonical_event_digest: str,
    ) -> dict[str, Any]:
        """Resolve a committed Codex chain without a live lease or owner token."""

        parse_digest(entry_digest)
        parse_digest(canonical_event_digest)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select job_id,attempt_id,authorization_id"
                " from client.codex_lifecycle_admission"
                " where realm_id=%s and entry_digest=%s",
                (self.realm_id, entry_digest),
            )
            locked_identity = cursor.fetchall()
            if len(locked_identity) != 1:
                raise PolicyViolation("Committed Codex admission identity exact degil")
            cursor.execute(
                "select client.lock_codex_lifecycle_scope(%s,%s,%s,%s)",
                (
                    self.realm_id,
                    locked_identity[0][0],
                    locked_identity[0][1],
                    None,
                ),
            )
            if cursor.fetchone() is None:
                raise PolicyViolation("Committed Codex runtime parent lock alinamadi")
            cursor.execute(
                "select admission.lifecycle_event_id,admission.continuity_event_id,"
                " admission.delivery_outbox_id,admission.hook_receipt_id,admission.job_id,"
                " admission.attempt_id,admission.envelope_id,admission.authorization_id,"
                " admission.claim_id,admission.effect_receipt_id,admission.work_plan_digest,"
                " admission.effect_plan_digest,admission.effect_plan_body,admission.effect_digest,admission.source_digest,"
                " admission.policy_digest,admission.migration_digest,admission.envelope_digest,"
                " admission.terminal_hook_receipt_digest,admission.result_formula_digest,"
                " admission.binding_digest,admission.created_at,attempt.worker_label,"
                " continuity_event.event_digest,"
                " continuity_event.event_type,continuity_event.project_id,"
                " continuity_event.work_item_id,continuity_event.run_id,"
                " continuity_event.session_id,continuity_event.client_id,"
                " hook_receipt.output_digest,"
                " hook_receipt.output_body->'command'->>'compiler_enqueue',"
                " effect_receipt.result_digest,effect_receipt.adapter_evidence_digest,"
                " effect_receipt.completed_at,effect_receipt.status,"
                " effect_receipt.failure_category,effect_receipt.failure_digest,"
                " effect_receipt.token_count,effect_receipt.cost_micros,effect_receipt.latency_ms,"
                " claim.authorization_digest,claim.claim_digest,"
                " claim.operation,claim.adapter_digest,claim.fencing_token,claim.idempotency_key,claim.resources,"
                " claim.execution_identity,claim.claimed_at,authorization.scope,"
                " authorization.consumed_at,authorization.consumed_by,"
                " envelope.envelope_digest,task_plan.plan_digest,checkpoint.id,"
                " checkpoint.checkpoint_digest"
                " from client.codex_lifecycle_admission admission"
                " join client.lifecycle_event lifecycle_event"
                " on lifecycle_event.realm_id=admission.realm_id"
                " and lifecycle_event.id=admission.lifecycle_event_id"
                " join continuity.session_lifecycle_event continuity_event"
                " on continuity_event.realm_id=admission.realm_id"
                " and continuity_event.id=admission.continuity_event_id"
                " join continuity.lifecycle_delivery_outbox outbox"
                " on outbox.realm_id=admission.realm_id and outbox.id=admission.delivery_outbox_id"
                " and outbox.event_id=continuity_event.id"
                " join hooks.result_receipt hook_receipt"
                " on hook_receipt.realm_id=admission.realm_id and hook_receipt.id=admission.hook_receipt_id"
                " join hooks.invocation hook_invocation"
                " on hook_invocation.realm_id=admission.realm_id"
                " and hook_invocation.id=hook_receipt.invocation_id"
                " join hooks.session_binding hook_session"
                " on hook_session.realm_id=admission.realm_id"
                " and hook_session.id=hook_invocation.session_binding_id"
                " join runtime.effect_claim claim"
                " on claim.realm_id=admission.realm_id and claim.id=admission.claim_id"
                " join runtime.effect_receipt effect_receipt"
                " on effect_receipt.realm_id=admission.realm_id"
                " and effect_receipt.id=admission.effect_receipt_id"
                " and effect_receipt.claim_id=claim.id"
                " join runtime.job job on job.realm_id=admission.realm_id and job.id=admission.job_id"
                " join runtime.job_attempt attempt on attempt.realm_id=admission.realm_id"
                " and attempt.id=admission.attempt_id and attempt.job_id=job.id"
                " join runtime.execution_envelope envelope on envelope.realm_id=admission.realm_id"
                " and envelope.id=admission.envelope_id and envelope.job_id=job.id"
                " and envelope.attempt_id=attempt.id"
                " join security.authorization authorization"
                " on authorization.realm_id=admission.realm_id"
                " and authorization.id=admission.authorization_id"
                " join work.task_plan task_plan on task_plan.realm_id=admission.realm_id"
                " and task_plan.id=job.plan_id"
                " join lateral(select step.value as body"
                " from jsonb_array_elements(task_plan.steps) step"
                " where step.value->>'step_id'=job.step_id) plan_step on true"
                " join runtime.execution_run run on run.realm_id=admission.realm_id"
                " and run.id=job.run_id and run.id=envelope.run_id"
                " join projects.source_binding source_binding"
                " on source_binding.realm_id=admission.realm_id"
                " and source_binding.project_id=job.project_id"
                " join projects.source_revision source"
                " on source.realm_id=source_binding.realm_id"
                " and source.binding_id=source_binding.id"
                " and source.revision=envelope.source_revision"
                " and source.tree_digest=admission.source_digest"
                " join lateral(select migration.checksum from core.schema_migrations migration"
                " where models.capability_runtime_jsonb_digest(to_jsonb(migration.checksum))"
                " =admission.migration_digest order by migration.version desc limit 1) migration on true"
                " join work.checkpoint checkpoint on checkpoint.realm_id=admission.realm_id"
                " and checkpoint.job_id=job.id"
                " where admission.realm_id=%s and admission.entry_digest=%s"
                " and lifecycle_event.event_digest=%s"
                " and continuity_event.idempotency_key=%s"
                " and admission.lifecycle_event_id=lifecycle_event.id"
                " and admission.effect_plan_digest=outbox.plan_digest"
                " and admission.effect_plan_digest="
                " models.capability_runtime_jsonb_digest(admission.effect_plan_body)"
                " and admission.effect_plan_body->>'schema'='zekam-lifecycle-bridge-plan/v1'"
                " and (select count(*) from jsonb_object_keys(admission.effect_plan_body))=14"
                " and admission.effect_plan_body->>'event_digest'=continuity_event.event_digest"
                " and admission.effect_plan_body->>'hook_payload_digest'=hook_invocation.input_digest"
                " and admission.effect_plan_body->>'client_contract_digest'="
                " 'sha256:e688a17271134e25ef233bfda7095308311afc48a7bee825bd720e3e93571147'"
                " and (admission.effect_plan_body->>'hook_generation')::integer"
                " =hook_invocation.generation"
                " and admission.effect_plan_body->>'hook_set_digest'=hook_session.hook_set_digest"
                " and admission.effect_plan_body->'hook_ids'=jsonb_build_array((select spec.hook_id"
                " from hooks.spec_revision spec where spec.realm_id=hook_invocation.realm_id"
                " and spec.id=hook_invocation.spec_revision_id))"
                " and admission.effect_plan_body->>'idempotency_key'=continuity_event.idempotency_key"
                " and admission.effect_plan_body->>'source_digest'=admission.source_digest"
                " and admission.effect_plan_body->>'policy_digest'=admission.policy_digest"
                " and admission.effect_plan_body->>'migration_digest'=admission.migration_digest"
                " and admission.effect_plan_body->>'effect_digest'=admission.effect_digest"
                " and admission.effect_plan_digest=authorization.plan_digest"
                " and admission.work_plan_digest=task_plan.plan_digest"
                " and task_plan.plan_digest=models.capability_runtime_jsonb_digest("
                " jsonb_build_object('work_item_id',task_plan.work_item_id::text,"
                " 'project_id',task_plan.project_id::text,'revision',task_plan.revision,"
                " 'source_revision',task_plan.source_revision,"
                " 'policy_digest',task_plan.policy_digest,'steps',task_plan.steps,"
                " 'effect_digest',task_plan.effect_digest,'grants_authority',false))"
                " and admission.source_digest=source.tree_digest"
                " and envelope.source_revision=source.revision"
                " and envelope.source_revision=task_plan.source_revision"
                " and admission.policy_digest=envelope.policy_digest"
                " and envelope.policy_digest=task_plan.policy_digest"
                " and admission.migration_digest="
                " models.capability_runtime_jsonb_digest(to_jsonb(migration.checksum))"
                " and admission.effect_digest=claim.effect_digest"
                " and admission.effect_digest=authorization.effect_digest"
                " and admission.envelope_digest=envelope.envelope_digest"
                " and envelope.id=(select latest.id from runtime.execution_envelope latest"
                " where latest.realm_id=envelope.realm_id and latest.job_id=envelope.job_id"
                " and latest.attempt_id=envelope.attempt_id"
                " order by latest.request_ordinal desc,latest.created_at desc,latest.id desc limit 1)"
                " and outbox.state='completed'"
                " and outbox.terminal_receipt_digest=admission.terminal_hook_receipt_digest"
                " and hook_receipt.status='completed'"
                " and hook_receipt.effect_performed=false"
                " and hook_receipt.grants_authority=false"
                " and hook_receipt.output_digest=admission.terminal_hook_receipt_digest"
                " and hook_receipt.output_digest="
                " models.capability_runtime_jsonb_digest(hook_receipt.output_body)"
                " and jsonb_typeof(hook_receipt.output_body->'command'->'compiler_enqueue')"
                " ='boolean'"
                " and hook_invocation.event_type=continuity_event.event_type"
                " and hook_invocation.input_body->'lifecycle'=continuity_event.event_body"
                " and models.capability_runtime_jsonb_digest(hook_invocation.input_body->'data')"
                " =continuity_event.event_body->>'payload_digest'"
                " and hook_session.session_ref='codex:'||continuity_event.session_id||':'||admission.entry_digest"
                " and effect_receipt.status='completed'"
                " and effect_receipt.result_digest=admission.result_formula_digest"
                " and effect_receipt.failure_category is null"
                " and effect_receipt.failure_digest is null"
                " and effect_receipt.token_count=0 and effect_receipt.cost_micros=0"
                " and effect_receipt.latency_ms>=0"
                " and authorization.state='consumed'"
                " and authorization.consumed_by='client-lifecycle-bridge/v1'"
                " and authorization.authorization_digest=claim.authorization_digest"
                " and claim.idempotency_key=continuity_event.idempotency_key"
                " and claim.operation='client-lifecycle-drain'"
                " and claim.adapter_digest=models.capability_runtime_jsonb_digest("
                " jsonb_build_object('adapter','claimedwork-codex-lifecycle','version',1))"
                " and claim.resources=jsonb_build_array("
                " jsonb_build_object('resource',job.write_resources[1],'mode','write'))"
                " and claim.effect_digest=admission.effect_digest"
                " and claim.authorization_id=admission.authorization_id"
                " and claim.claim_digest=models.capability_runtime_jsonb_digest("
                " jsonb_build_object('job_id',claim.job_id::text,'operation',claim.operation,"
                " 'effect_digest',claim.effect_digest,"
                " 'authorization_digest',claim.authorization_digest,"
                " 'idempotency_key',claim.idempotency_key,'resources',claim.resources,"
                " 'execution_identity',claim.execution_identity,"
                " 'fencing_token',claim.fencing_token,'adapter_digest',claim.adapter_digest))"
                " and authorization.scope=jsonb_build_object("
                " 'allowed_resources',to_jsonb(job.write_resources),"
                " 'allowed_effects',jsonb_build_array('database-write'),"
                " 'provider_refs','[]'::jsonb,'secret_ref_ids','[]'::jsonb,"
                " 'data_classifications',jsonb_build_array('internal'))"
                " and authorization.allowed_resources=job.write_resources"
                " and authorization.allowed_effects=array['database-write']::text[]"
                " and cardinality(authorization.provider_refs)=0"
                " and cardinality(authorization.secret_ref_ids)=0"
                " and authorization.risk=plan_step.body->>'risk'"
                " and authorization.risk='high'"
                " and plan_step.body->>'effect'='database-write'"
                " and plan_step.body->'logical_resources'=to_jsonb(job.write_resources)"
                " and job.state='completed' and attempt.outcome='succeeded'"
                " and job.kind='mutation' and job.max_attempts=1 and job.attempt_count=1"
                " and job.required_capabilities=array['client.lifecycle.codex-drain']::text[]"
                " and job.read_resources='{}'::text[] and cardinality(job.write_resources)=1"
                " and job.payload->>'schema'='zekam-codex-lifecycle-job/v1'"
                " and job.payload->>'authorization_id'=admission.authorization_id::text"
                " and (select count(*) from jsonb_object_keys(job.payload))=2"
                " and attempt.result_digest=effect_receipt.result_digest"
                " and attempt.fencing_token=claim.fencing_token"
                " and job.fencing_token=claim.fencing_token"
                " and claim.execution_identity=attempt.worker_label||':'||attempt.fencing_token::text"
                " and envelope.fencing_token=claim.fencing_token"
                " and authorization.issued_at<=claim.claimed_at"
                " and claim.claimed_at<=authorization.consumed_at"
                " and authorization.expires_at>=authorization.consumed_at"
                " and authorization.consumed_at<=hook_invocation.created_at"
                " and hook_invocation.created_at<=hook_receipt.completed_at"
                " and hook_receipt.completed_at<=effect_receipt.completed_at"
                " and checkpoint.task_plan_id=job.plan_id"
                " and checkpoint.project_id=job.project_id"
                " and checkpoint.work_item_id=job.work_item_id"
                " and checkpoint.source_revision=task_plan.source_revision"
                " and checkpoint.plan_steps=work.task_plan_execution_order(task_plan.steps)"
                " and checkpoint.completed_steps||checkpoint.pending_steps=checkpoint.plan_steps"
                " and job.step_id=any(checkpoint.completed_steps)"
                " and (select array_agg(result.key order by result.key)"
                " from jsonb_each_text(checkpoint.step_results) result)"
                " =(select array_agg(step order by step)"
                " from unnest(checkpoint.completed_steps) step)"
                " and not exists(select 1 from jsonb_each_text(checkpoint.step_results) result"
                " where result.value !~ '^sha256:[0-9a-f]{64}$')"
                " and checkpoint.step_results->>job.step_id=effect_receipt.result_digest"
                " and effect_receipt.completed_at<=checkpoint.created_at"
                " and checkpoint.created_at<=attempt.finished_at"
                " and attempt.finished_at<=admission.created_at"
                " and admission.created_at<=clock_timestamp()"
                " and effect_receipt.adapter_evidence_digest="
                " models.capability_runtime_jsonb_digest(jsonb_build_object("
                " 'adapter','claimedwork-codex-lifecycle/v1',"
                " 'entry_digest',admission.entry_digest,"
                " 'plan_digest',admission.effect_plan_digest,"
                " 'terminal_hook_receipt_digest',admission.terminal_hook_receipt_digest))"
                " and not exists(select 1 from runtime.lease lease"
                "   where lease.realm_id=job.realm_id and lease.job_id=job.id)"
                " and not exists(select 1 from runtime.resource_lock lock"
                "   where lock.realm_id=job.realm_id and lock.job_id=job.id)"
                " and not exists(select 1 from runtime.effect_claim orphan"
                "   where orphan.realm_id=job.realm_id and orphan.job_id=job.id"
                "   and not exists(select 1 from runtime.effect_receipt receipt"
                "     where receipt.realm_id=orphan.realm_id and receipt.claim_id=orphan.id))"
                " for share of job,attempt,envelope,authorization,checkpoint",
                (self.realm_id, entry_digest, canonical_event_digest, idempotency_key),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Committed Codex lifecycle exact terminal chain bulunamadi")
        row = rows[0]
        keys = (
            "lifecycle_event_id","continuity_event_id","delivery_outbox_id","hook_receipt_id",
            "job_id","attempt_id","envelope_id","authorization_id","claim_id",
            "effect_receipt_id","work_plan_digest","effect_plan_digest","effect_plan_body","effect_digest",
            "source_digest","policy_digest","migration_digest","admission_envelope_digest",
            "terminal_hook_receipt_digest","result_formula_digest","binding_digest","created_at",
            "worker_label",
            "continuity_event_digest","event_type","project_id","work_item_id","run_id",
            "session_id","client_id","hook_output_digest","compiler_enqueue","effect_result_digest",
            "adapter_evidence_digest","effect_completed_at","effect_status", "failure_category",
            "failure_digest","token_count","cost_micros","latency_ms", "authorization_digest","claim_digest",
            "operation","adapter_digest","fencing_token","claim_idempotency_key","resources","execution_identity",
            "claimed_at","authorization_scope","consumed_at","consumed_by","envelope_digest",
            "stored_work_plan_digest","checkpoint_id","checkpoint_digest",
        )
        document = dict(zip(keys, row, strict=True))
        compiler_text = str(document["compiler_enqueue"]).lower()
        if compiler_text not in {"true", "false"}:
            raise PolicyViolation("Committed Codex compiler enqueue boolean degil")
        document["compiler_enqueue"] = compiler_text == "true"
        admission_body = {
            "schema": "zekam-codex-lifecycle-governed-admission/v1",
            **{
                key: str(document[key])
                for key in (
                    "lifecycle_event_id","continuity_event_id","delivery_outbox_id",
                    "hook_receipt_id","job_id","attempt_id","envelope_id","authorization_id",
                    "claim_id","effect_receipt_id",
                )
            },
            "entry_digest": entry_digest,
            "work_plan_digest": str(document["work_plan_digest"]),
            "effect_plan_digest": str(document["effect_plan_digest"]),
            "effect_plan_body": dict(document["effect_plan_body"]),
            "effect_digest": str(document["effect_digest"]),
            "source_digest": str(document["source_digest"]),
            "policy_digest": str(document["policy_digest"]),
            "migration_digest": str(document["migration_digest"]),
            "envelope_digest": str(document["admission_envelope_digest"]),
            "terminal_hook_receipt_digest": str(document["terminal_hook_receipt_digest"]),
            "result_formula_digest": str(document["result_formula_digest"]),
            "grants_authority": False,
        }
        if digest(admission_body) != str(document["binding_digest"]):
            raise PolicyViolation("Committed Codex governed admission binding digest drift")
        return document

    def record_governed_admission(
        self,
        *,
        lifecycle_event_id: UUID,
        entry_digest: str,
        continuity_event_id: UUID,
        delivery_outbox_id: UUID,
        hook_receipt_id: UUID,
        job_id: UUID,
        attempt_id: UUID,
        envelope_id: UUID,
        authorization_id: UUID,
        claim_id: UUID,
        effect_receipt_id: UUID,
        work_plan_digest: str,
        effect_plan_digest: str,
        effect_plan_body: Mapping[str, Any],
        effect_digest: str,
        source_digest: str,
        policy_digest: str,
        migration_digest: str,
        envelope_digest: str,
        terminal_hook_receipt_digest: str,
        result_formula_digest: str,
        now: dt.datetime,
    ) -> str:
        body = {
            "schema": "zekam-codex-lifecycle-governed-admission/v1",
            "lifecycle_event_id": str(lifecycle_event_id),
            "entry_digest": entry_digest,
            "continuity_event_id": str(continuity_event_id),
            "delivery_outbox_id": str(delivery_outbox_id),
            "hook_receipt_id": str(hook_receipt_id),
            "job_id": str(job_id),
            "attempt_id": str(attempt_id),
            "envelope_id": str(envelope_id),
            "authorization_id": str(authorization_id),
            "claim_id": str(claim_id),
            "effect_receipt_id": str(effect_receipt_id),
            "work_plan_digest": work_plan_digest,
            "effect_plan_digest": effect_plan_digest,
            "effect_plan_body": dict(effect_plan_body),
            "effect_digest": effect_digest,
            "source_digest": source_digest,
            "policy_digest": policy_digest,
            "migration_digest": migration_digest,
            "envelope_digest": envelope_digest,
            "terminal_hook_receipt_digest": terminal_hook_receipt_digest,
            "result_formula_digest": result_formula_digest,
            "grants_authority": False,
        }
        binding_digest = digest(body)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into client.codex_lifecycle_admission"
                " (id,realm_id,lifecycle_event_id,entry_digest,continuity_event_id,delivery_outbox_id,"
                " hook_receipt_id,job_id,attempt_id,envelope_id,authorization_id,claim_id,"
                " effect_receipt_id,work_plan_digest,effect_plan_digest,effect_plan_body,effect_digest,"
                " source_digest,policy_digest,migration_digest,envelope_digest,"
                " terminal_hook_receipt_digest,result_formula_digest,binding_digest,created_at,"
                " grants_authority) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                " %s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,false)",
                (
                    new_uuid7(now=now),self.realm_id,lifecycle_event_id,entry_digest,continuity_event_id,
                    delivery_outbox_id,hook_receipt_id,job_id,attempt_id,envelope_id,
                    authorization_id,claim_id,effect_receipt_id,work_plan_digest,
                    effect_plan_digest,canonical_json(effect_plan_body),effect_digest,source_digest,policy_digest,
                    migration_digest,envelope_digest,terminal_hook_receipt_digest,
                    result_formula_digest,binding_digest,now,
                ),
            )
        return binding_digest

    def current_work_plan_digest(self, *, work_item_id: UUID, plan_id: UUID) -> str:
        """Recompute the current TaskPlan and compare it with its stored digest."""

        from zekam.infrastructure.postgres.work_repository import TaskPlanRepository

        plan = TaskPlanRepository(self.connection, self.realm_id).current(work_item_id)
        if plan is None or plan.id != plan_id:
            raise PolicyViolation("Lifecycle job current canonical TaskPlan'a bagli degil")
        computed = plan.plan_digest
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select plan_digest,effect_digest from work.task_plan"
                " where realm_id=%s and id=%s and work_item_id=%s",
                (self.realm_id, plan_id, work_item_id),
            )
            row = cursor.fetchone()
        if row is None or str(row[0]) != computed or str(row[1]) != plan.effect_digest:
            raise PolicyViolation("Stored TaskPlan digest/effect recomputation drift")
        return computed

    def claimed_plan_inputs(
        self,
        *,
        job_id: UUID,
        attempt_id: UUID,
        lease_id: UUID,
        owner_digest: str,
        fencing_token: int,
        session_id: str,
        now: dt.datetime,
    ) -> dict[str, str | None]:
        """Derive lifecycle plan provenance only from the current exact envelope."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select envelope.source_revision,source.tree_digest,envelope.policy_digest,"
                " models.capability_runtime_jsonb_digest(to_jsonb(migration.checksum)),"
                " task_plan.plan_digest,envelope.checkpoint_disposition,envelope.checkpoint_id,"
                " envelope.checkpoint_v2_id,envelope.context_packet_id,envelope.envelope_digest"
                " from runtime.job job"
                " join runtime.job_attempt attempt on attempt.realm_id=job.realm_id"
                " and attempt.id=%s and attempt.job_id=job.id and attempt.outcome is null"
                " join runtime.lease lease on lease.realm_id=job.realm_id"
                " and lease.id=%s and lease.job_id=job.id and lease.attempt_id=attempt.id"
                " join runtime.execution_envelope envelope on envelope.realm_id=job.realm_id"
                " and envelope.job_id=job.id and envelope.attempt_id=attempt.id"
                " and envelope.lease_id=lease.id"
                " join runtime.execution_run run on run.realm_id=job.realm_id"
                " and run.id=job.run_id and run.id=envelope.run_id"
                " join work.task_plan task_plan on task_plan.realm_id=job.realm_id"
                " and task_plan.id=job.plan_id"
                " join projects.project project on project.realm_id=job.realm_id"
                " and project.id=job.project_id"
                " join projects.source_binding binding on binding.realm_id=project.realm_id"
                " and binding.project_id=project.id"
                " join lateral(select revision.revision,revision.tree_digest"
                " from projects.source_revision revision where revision.realm_id=binding.realm_id"
                " and revision.binding_id=binding.id order by revision.observed_at desc,"
                " revision.id desc limit 1) source on true"
                " join lateral(select checksum from core.schema_migrations"
                " order by version desc limit 1) migration on true"
                " where job.realm_id=%s and job.id=%s and job.state='running'"
                " and attempt.fencing_token=%s and lease.fencing_token=%s"
                " and lease.owner_digest=%s and lease.expires_at>%s"
                " and run.state='active' and run.client_id='codex'"
                " and run.session_id=%s and run.deadline>%s"
                " and envelope.fencing_token=%s"
                " and envelope.id=(select latest.id from runtime.execution_envelope latest"
                " where latest.realm_id=envelope.realm_id and latest.job_id=envelope.job_id"
                " and latest.attempt_id=envelope.attempt_id"
                " order by latest.request_ordinal desc,latest.created_at desc,latest.id desc limit 1)"
                " and envelope.source_revision=source.revision"
                " and envelope.source_revision=task_plan.source_revision"
                " and envelope.policy_digest=task_plan.policy_digest"
                " and task_plan.id=(select current_plan.id from work.task_plan current_plan"
                " where current_plan.realm_id=task_plan.realm_id"
                " and current_plan.work_item_id=task_plan.work_item_id"
                " order by current_plan.revision desc,current_plan.id desc limit 1)",
                (
                    attempt_id,
                    lease_id,
                    self.realm_id,
                    job_id,
                    fencing_token,
                    fencing_token,
                    owner_digest,
                    now,
                    session_id,
                    now,
                    fencing_token,
                ),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Codex lifecycle plan provenance exact envelope'dan cozulmedi")
        row = rows[0]
        work_plan_digest = self.current_work_plan_digest(
            work_item_id=self._required_job_work_item(job_id),
            plan_id=self._required_job_plan(job_id),
        )
        if work_plan_digest != str(row[4]):
            raise PolicyViolation("Codex lifecycle envelope current Work plan digest drift")
        checkpoint_ref: str | None
        if str(row[5]) == "bound":
            checkpoint_ref = f"checkpoint:{row[6]}"
        elif str(row[5]) == "bound-v2":
            checkpoint_ref = f"checkpoint-v2:{row[7]}"
        elif str(row[5]) == "not-applicable-genesis":
            checkpoint_ref = None
        else:
            raise PolicyViolation("Codex lifecycle checkpoint disposition gecersiz")
        result: dict[str, str | None] = {
            "source_revision": str(row[0]),
            "source_digest": str(row[1]),
            "policy_digest": str(row[2]),
            "migration_digest": str(row[3]),
            "work_plan_digest": work_plan_digest,
            "checkpoint_ref": checkpoint_ref,
            "context_ref": f"context-packet:{row[8]}",
            "envelope_digest": str(row[9]),
        }
        for key in (
            "source_digest",
            "policy_digest",
            "migration_digest",
            "work_plan_digest",
            "envelope_digest",
        ):
            parse_digest(str(result[key]))
        return result

    def _required_job_work_item(self, job_id: UUID) -> UUID:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select work_item_id from runtime.job where realm_id=%s and id=%s",
                (self.realm_id, job_id),
            )
            row = cursor.fetchone()
        if row is None or row[0] is None:
            raise PolicyViolation("Codex lifecycle job work item identity eksik")
        return UUID(str(row[0]))

    def _required_job_plan(self, job_id: UUID) -> UUID:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select plan_id from runtime.job where realm_id=%s and id=%s",
                (self.realm_id, job_id),
            )
            row = cursor.fetchone()
        if row is None or row[0] is None:
            raise PolicyViolation("Codex lifecycle job plan identity eksik")
        return UUID(str(row[0]))

    def lookup(self, event_digest: str) -> LifecycleAck:
        """Read an existing canonical ACK without invoking the ingest mutation path."""

        parse_digest(event_digest)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select e.id,a.canonical_digest,a.acknowledged_at,o.id,o.payload_digest"
                " from client.lifecycle_event e join client.lifecycle_ack a"
                " on a.realm_id=e.realm_id and a.event_id=e.id"
                " left join work.compaction_checkpoint_outbox o"
                " on o.realm_id=e.realm_id and o.lifecycle_event_id=e.id"
                " where e.realm_id=%s and e.event_digest=%s",
                (self.realm_id, event_digest),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Lifecycle canonical terminal ACK bulunamadi")
        return LifecycleAck(
            UUID(str(row[0])),
            event_digest,
            str(row[1]),
            row[2],
            None if row[3] is None else UUID(str(row[3])),
            None if row[4] is None else str(row[4]),
        )

    def current_execution(
        self,
        *,
        job_id: UUID,
        attempt_id: UUID,
        lease_id: UUID,
        owner_digest: str,
        fencing_token: int,
        claim_id: UUID,
        authorization_id: UUID,
        effect_plan_digest: str,
        work_plan_digest: str,
        effect_digest: str,
        operation: str,
        adapter_digest: str,
        claim_digest: str,
        authorization_digest: str,
        source_digest: str,
        policy_digest: str,
        migration_digest: str,
        resource: str,
        session_id: str,
        now: dt.datetime,
        allow_consumed: bool = False,
    ) -> ActiveLifecycleExecution:
        """Re-read every mutable execution gate; caller may hold a transaction lock."""

        for value in (
            owner_digest,
            effect_plan_digest,
            work_plan_digest,
            effect_digest,
            adapter_digest,
            claim_digest,
            authorization_digest,
            source_digest,
            policy_digest,
            migration_digest,
        ):
            parse_digest(value)
        authorization_states = ("issued", "consumed") if allow_consumed else ("issued",)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select client.lock_codex_lifecycle_scope(%s,%s,%s,%s)",
                (self.realm_id, job_id, attempt_id, authorization_id),
            )
            if cursor.fetchone() is None:
                raise PolicyViolation("Lifecycle DB currentness lock alinamadi")
            cursor.execute(
                "select j.project_id,j.work_item_id,j.plan_id,j.run_id,e.id,e.envelope_digest,"
                " e.source_revision,s.tree_digest,e.policy_digest,"
                " models.capability_runtime_jsonb_digest(to_jsonb(m.checksum)),"
                " e.context_manifest_digest,w.entry_digest,t.plan_digest"
                " from runtime.job j"
                " join runtime.job_attempt a on a.realm_id=j.realm_id and a.job_id=j.id"
                " join runtime.lease l on l.realm_id=j.realm_id and l.job_id=j.id"
                " join runtime.execution_envelope e on e.realm_id=j.realm_id"
                "  and e.job_id=j.id and e.attempt_id=a.id and e.lease_id=l.id"
                " join runtime.execution_run r on r.realm_id=j.realm_id and r.id=e.run_id"
                " join work.task_plan t on t.realm_id=j.realm_id and t.id=j.plan_id"
                " join runtime.effect_claim c on c.realm_id=j.realm_id and c.job_id=j.id"
                " join security.authorization z on z.realm_id=j.realm_id and z.id=c.authorization_id"
                " join core.actor za on za.realm_id=z.realm_id and za.id=z.actor_id"
                " join lateral(select step.value as body from jsonb_array_elements(t.steps) step"
                " where step.value->>'step_id'=j.step_id) plan_step on true"
                " join projects.project p on p.realm_id=j.realm_id and p.id=j.project_id"
                " join projects.source_binding b on b.realm_id=p.realm_id and b.project_id=p.id"
                " join lateral (select revision.revision,revision.tree_digest"
                "   from projects.source_revision revision"
                "   where revision.realm_id=b.realm_id and revision.binding_id=b.id"
                "   order by revision.observed_at desc,revision.id desc limit 1) s on true"
                " join lateral (select version,checksum from core.schema_migrations"
                "   order by version desc limit 1) m on true"
                " join lateral (select entry_digest from work.work_journal_entry journal"
                "   where journal.realm_id=j.realm_id and journal.work_item_id=j.work_item_id"
                "   order by journal.sequence desc,journal.id desc limit 1) w on true"
                " where j.realm_id=%s and j.id=%s and j.state='running'"
                " and a.id=%s and a.outcome is null and a.fencing_token=%s"
                " and l.id=%s and l.attempt_id=a.id and l.owner_digest=%s"
                " and l.fencing_token=%s and l.expires_at>%s"
                " and r.state='active' and r.id=j.run_id and r.client_id=%s"
                " and r.session_id=%s and r.deadline>%s"
                " and e.fencing_token=%s and e.created_at<=%s"
                " and j.fencing_token=a.fencing_token"
                " and e.id=(select latest.id from runtime.execution_envelope latest"
                "   where latest.realm_id=e.realm_id and latest.run_id=e.run_id"
                "   and latest.job_id=e.job_id and latest.attempt_id=e.attempt_id"
                "   order by latest.request_ordinal desc,latest.created_at desc,latest.id desc limit 1)"
                " and e.source_revision=r.source_revision and e.source_revision=t.source_revision"
                " and e.source_revision=s.revision and s.tree_digest=%s"
                " and e.policy_digest=r.policy_digest and e.policy_digest=t.policy_digest"
                " and e.policy_digest=%s and t.plan_digest=%s"
                " and t.id=(select current_plan.id from work.task_plan current_plan"
                "   where current_plan.realm_id=t.realm_id"
                "   and current_plan.work_item_id=t.work_item_id"
                "   order by current_plan.revision desc,current_plan.id desc limit 1)"
                " and models.capability_runtime_jsonb_digest(to_jsonb(m.checksum))=%s"
                " and c.id=%s and c.attempt_id=a.id and c.fencing_token=%s"
                " and c.authorization_id=%s and c.effect_digest=%s"
                " and c.operation=%s and c.adapter_digest=%s"
                " and c.claim_digest=%s and c.authorization_digest=%s"
                " and c.execution_identity=l.worker_label||':'||l.fencing_token::text"
                " and c.resources=%s::jsonb"
                " and z.id=%s and z.state=any(%s) and z.expires_at>%s"
                " and za.status='active' and za.kind='human'"
                " and z.plan_digest=%s and z.effect_digest=%s"
                " and z.authorization_digest=c.authorization_digest"
                " and z.work_item_id=j.work_item_id and z.plan_id=j.plan_id"
                " and z.allowed_resources=array[%s]::text[]"
                " and z.allowed_effects=array['database-write']::text[]"
                " and cardinality(z.provider_refs)=0 and cardinality(z.secret_ref_ids)=0"
                " and z.risk=plan_step.body->>'risk' and z.risk='high'"
                " and plan_step.body->>'effect'='database-write'"
                " and plan_step.body->'logical_resources'=to_jsonb(j.write_resources)"
                " and z.issued_at<=c.claimed_at"
                " and ((z.state='issued' and z.consumed_at is null)"
                " or (z.state='consumed' and c.claimed_at<=z.consumed_at"
                " and z.consumed_by='client-lifecycle-bridge/v1'))"
                " and z.scope=%s::jsonb"
                " and j.read_resources='{}'::text[] and j.write_resources=array[%s]::text[]"
                " and exists(select 1 from runtime.resource_lock lock"
                "   where lock.realm_id=j.realm_id and lock.job_id=j.id"
                "   and lock.lease_id=l.id and lock.resource=%s and lock.mode='write')"
                " and (select count(*) from runtime.resource_lock lock"
                "   where lock.realm_id=j.realm_id and lock.job_id=j.id)=1"
                " and (e.checkpoint_disposition='not-applicable-genesis'"
                "   or (e.checkpoint_disposition='bound' and exists("
                "     select 1 from work.checkpoint checkpoint where checkpoint.realm_id=e.realm_id"
                "     and checkpoint.id=e.checkpoint_id"
                "     and checkpoint.checkpoint_digest=e.checkpoint_digest))"
                "   or (e.checkpoint_disposition='bound-v2' and exists("
                "     select 1 from work.checkpoint_v2 checkpoint where checkpoint.realm_id=e.realm_id"
                "     and checkpoint.id=e.checkpoint_v2_id"
                "     and checkpoint.checkpoint_digest=e.checkpoint_v2_digest)))"
                " for share of j,a,l,e,r,c,z,za,t",
                (
                    self.realm_id,
                    job_id,
                    attempt_id,
                    fencing_token,
                    lease_id,
                    owner_digest,
                    fencing_token,
                    now,
                    "codex",
                    session_id,
                    now,
                    fencing_token,
                    now,
                    source_digest,
                    policy_digest,
                    work_plan_digest,
                    migration_digest,
                    claim_id,
                    fencing_token,
                    authorization_id,
                    effect_digest,
                    operation,
                    adapter_digest,
                    claim_digest,
                    authorization_digest,
                    canonical_json([{"resource": resource, "mode": "write"}]),
                    authorization_id,
                    list(authorization_states),
                    now,
                    effect_plan_digest,
                    effect_digest,
                    resource,
                    canonical_json(
                        {
                            "allowed_resources": [resource],
                            "allowed_effects": ["database-write"],
                            "provider_refs": [],
                            "secret_ref_ids": [],
                            "data_classifications": ["internal"],
                        }
                    ),
                    resource,
                    resource,
                ),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Lifecycle exact live ClaimedWork/envelope/claim binding yok")
        row = rows[0]
        result = ActiveLifecycleExecution(
            UUID(str(row[0])),
            UUID(str(row[1])),
            UUID(str(row[2])),
            UUID(str(row[3])),
            UUID(str(row[4])),
            str(row[5]),
            str(row[6]),
            str(row[7]),
            str(row[8]),
            str(row[9]),
            str(row[10]),
            str(row[11]),
            str(row[12]),
        )
        for value in (
            result.envelope_digest,
            result.source_digest,
            result.policy_digest,
            result.migration_digest,
            result.context_manifest_digest,
            result.journal_head_digest,
            result.work_plan_digest,
        ):
            parse_digest(value)
        return result

    def previous_continuity_digest(
        self, *, client_id: str, session_id: str, sequence: int
    ) -> str | None:
        if sequence == 1:
            return None
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select event_digest from continuity.session_lifecycle_event"
                " where realm_id=%s and client_id=%s and session_id=%s and sequence=%s",
                (self.realm_id, client_id, session_id, sequence - 1),
            )
            row = cursor.fetchone()
        if row is None:
            raise ConcurrencyConflict("Lifecycle continuity predecessor receipt eksik")
        value = str(row[0])
        parse_digest(value)
        return value

    def store_job_checkpoint(
        self,
        *,
        execution: ActiveLifecycleExecution,
        job_id: UUID,
        step_id: str,
        result_digest: str,
        now: dt.datetime,
    ) -> UUID:
        """Persist the one exact job checkpoint after its terminal effect receipt."""

        from zekam.domain.context_continuity import Checkpoint
        from zekam.infrastructure.postgres.context_continuity_repository import (
            ContextContinuityRepository,
        )

        parse_digest(result_digest)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,step_results from work.checkpoint"
                " where realm_id=%s and job_id=%s",
                (self.realm_id, job_id),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if dict(existing[1] or {}).get(step_id) != result_digest:
                    raise ConcurrencyConflict("Lifecycle job checkpoint replay drift")
                return UUID(str(existing[0]))
            cursor.execute(
                "select steps from work.task_plan where realm_id=%s and id=%s",
                (self.realm_id, execution.plan_id),
            )
            row = cursor.fetchone()
            cursor.execute(
                "select job.step_id,attempt.result_digest from runtime.job job"
                " join lateral (select result_digest from runtime.job_attempt attempt"
                "   where attempt.realm_id=job.realm_id and attempt.job_id=job.id"
                "   and attempt.outcome='succeeded' order by attempt.attempt_number desc limit 1)"
                " attempt on true where job.realm_id=%s and job.plan_id=%s"
                " and job.state='completed' and job.id<>%s order by job.step_id",
                (self.realm_id, execution.plan_id, job_id),
            )
            previous_results = tuple((str(item[0]), str(item[1])) for item in cursor.fetchall())
        if row is None:
            raise NotFound("Lifecycle checkpoint task plan bulunamadi")
        steps = tuple(str(item["step_id"]) for item in row[0])
        if not step_id or step_id not in steps:
            raise PolicyViolation("Lifecycle job step exact task plan parcasi degil")
        result_map = dict(previous_results)
        if len(result_map) != len(previous_results) or step_id in result_map:
            raise PolicyViolation("Lifecycle checkpoint completed step identity drift")
        result_map[step_id] = result_digest
        if set(result_map) - set(steps):
            raise PolicyViolation("Lifecycle checkpoint plan disi completed step tasiyor")
        completed_steps = tuple(item for item in steps if item in result_map)
        checkpoint = Checkpoint(
            checkpoint_id=f"client-lifecycle-{job_id}",
            project_id=str(execution.project_id),
            work_item_id=str(execution.work_item_id),
            plan_revision_id=str(execution.plan_id),
            source_revision=execution.source_revision,
            plan_steps=steps,
            completed_steps=completed_steps,
            pending_steps=tuple(item for item in steps if item not in result_map),
            step_results=tuple((item, result_map[item]) for item in completed_steps),
            context_manifest_digest=execution.context_manifest_digest,
            journal_head_digest=execution.journal_head_digest,
            next_safe_action="client-lifecycle-next-job",
            created_at=now,
        )
        return ContextContinuityRepository(
            self.connection,
            self.realm_id,
            execution.project_id,
            execution.work_item_id,
        ).store_checkpoint(checkpoint, task_plan_id=execution.plan_id, job_id=job_id)

    def lookup_terminal_delivery(
        self,
        *,
        idempotency_key: str,
        effect_plan_digest: str,
        work_plan_digest: str,
        session_binding_id: UUID,
        event_type: str,
        hook_input_digest: str,
        job_id: UUID,
        attempt_id: UUID,
        claim_id: UUID,
        authorization_id: UUID,
        effect_digest: str,
        operation: str,
        adapter_digest: str,
        authorization_digest: str,
        fencing_token: int,
        resource: str,
    ) -> LifecycleTerminalRecord:
        """Read the exact continuity/hook terminal chain; never updates replay state."""

        for value in (
            effect_plan_digest,
            work_plan_digest,
            hook_input_digest,
            effect_digest,
            adapter_digest,
            authorization_digest,
        ):
            parse_digest(value)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select event.id,event.event_digest,outbox.id,outbox.terminal_receipt_digest,"
                " receipt.output_digest,receipt.output_body->'command'->>'compiler_enqueue',"
                " effect_receipt.id,effect_receipt.result_digest,"
                " checkpoint.id,checkpoint.checkpoint_digest,"
                " effect_receipt.adapter_evidence_digest"
                " from continuity.session_lifecycle_event event"
                " join continuity.lifecycle_delivery_outbox outbox"
                " on outbox.realm_id=event.realm_id and outbox.event_id=event.id"
                " join hooks.invocation invocation on invocation.realm_id=event.realm_id"
                "  and invocation.session_binding_id=%s and invocation.event_type=%s"
                "  and invocation.input_digest=%s"
                " join hooks.result_receipt receipt on receipt.realm_id=invocation.realm_id"
                "  and receipt.invocation_id=invocation.id"
                " join runtime.effect_claim claim on claim.realm_id=event.realm_id"
                "  and claim.id=%s and claim.job_id=%s and claim.attempt_id=%s"
                " join runtime.effect_receipt effect_receipt on effect_receipt.realm_id=claim.realm_id"
                "  and effect_receipt.claim_id=claim.id and effect_receipt.status='completed'"
                " join runtime.job job on job.realm_id=claim.realm_id and job.id=claim.job_id"
                " join runtime.job_attempt attempt on attempt.realm_id=job.realm_id"
                "  and attempt.id=claim.attempt_id and attempt.job_id=job.id"
                " join runtime.execution_envelope envelope on envelope.realm_id=job.realm_id"
                "  and envelope.job_id=job.id and envelope.attempt_id=attempt.id"
                " join runtime.execution_run run on run.realm_id=job.realm_id"
                "  and run.id=envelope.run_id and run.id=job.run_id"
                " join work.task_plan task_plan on task_plan.realm_id=job.realm_id"
                "  and task_plan.id=job.plan_id"
                " join security.authorization authorization on authorization.realm_id=claim.realm_id"
                "  and authorization.id=claim.authorization_id"
                " join work.checkpoint checkpoint on checkpoint.realm_id=job.realm_id"
                "  and checkpoint.job_id=job.id"
                " where event.realm_id=%s and event.idempotency_key=%s"
                " and outbox.plan_digest=%s and outbox.state='completed'"
                " and receipt.status='completed' and receipt.effect_performed=false"
                " and receipt.output_digest=outbox.terminal_receipt_digest"
                " and receipt.grants_authority=false and job.state='completed'"
                " and attempt.outcome='succeeded' and authorization.id=%s"
                " and authorization.state='consumed'"
                " and authorization.consumed_by='client-lifecycle-bridge/v1'"
                " and authorization.plan_digest=%s"
                " and authorization.effect_digest=claim.effect_digest"
                " and authorization.authorization_digest=claim.authorization_digest"
                " and authorization.work_item_id=job.work_item_id"
                " and authorization.plan_id=job.plan_id"
                " and authorization.allowed_resources=array[%s]::text[]"
                " and authorization.allowed_effects=array['database-write']::text[]"
                " and cardinality(authorization.provider_refs)=0"
                " and cardinality(authorization.secret_ref_ids)=0"
                " and authorization.scope=%s::jsonb"
                " and claim.effect_digest=%s and claim.operation=%s"
                " and claim.adapter_digest=%s and claim.authorization_digest=%s"
                " and claim.fencing_token=%s and attempt.fencing_token=%s"
                " and claim.fencing_token=job.fencing_token"
                " and claim.resources=%s::jsonb"
                " and job.read_resources='{}'::text[]"
                " and job.write_resources=array[%s]::text[]"
                " and authorization.consumed_at>=claim.claimed_at"
                " and invocation.created_at>=claim.claimed_at"
                " and effect_receipt.completed_at>=authorization.consumed_at"
                " and effect_receipt.completed_at>=receipt.completed_at"
                " and effect_receipt.adapter_evidence_digest is not null"
                " and attempt.result_digest=effect_receipt.result_digest"
                " and checkpoint.step_results->>job.step_id=effect_receipt.result_digest"
                " and checkpoint.created_at>=effect_receipt.completed_at"
                " and event.project_id=job.project_id and event.work_item_id=job.work_item_id"
                " and event.run_id=job.run_id and event.run_id=run.id"
                " and envelope.id=(select latest.id from runtime.execution_envelope latest"
                "   where latest.realm_id=envelope.realm_id and latest.run_id=envelope.run_id"
                "   and latest.job_id=envelope.job_id and latest.attempt_id=envelope.attempt_id"
                "   order by latest.request_ordinal desc,latest.created_at desc,latest.id desc limit 1)"
                " and envelope.fencing_token=claim.fencing_token"
                " and envelope.source_revision=task_plan.source_revision"
                " and envelope.policy_digest=task_plan.policy_digest"
                " and task_plan.plan_digest=%s"
                " and (envelope.checkpoint_disposition='not-applicable-genesis'"
                "   or (envelope.checkpoint_disposition='bound' and exists("
                "     select 1 from work.checkpoint prior where prior.realm_id=envelope.realm_id"
                "     and prior.id=envelope.checkpoint_id"
                "     and prior.checkpoint_digest=envelope.checkpoint_digest))"
                "   or (envelope.checkpoint_disposition='bound-v2' and exists("
                "     select 1 from work.checkpoint_v2 prior where prior.realm_id=envelope.realm_id"
                "     and prior.id=envelope.checkpoint_v2_id"
                "     and prior.checkpoint_digest=envelope.checkpoint_v2_digest)))"
                " and not exists(select 1 from runtime.lease lease"
                "   where lease.realm_id=job.realm_id and lease.job_id=job.id)"
                " and not exists(select 1 from runtime.resource_lock lock"
                "   where lock.realm_id=job.realm_id and lock.job_id=job.id)"
                " and checkpoint.id=(select latest_checkpoint.id from work.checkpoint latest_checkpoint"
                "   where latest_checkpoint.realm_id=job.realm_id"
                "   and latest_checkpoint.job_id=job.id"
                "   order by latest_checkpoint.created_at desc,latest_checkpoint.id desc limit 1)",
                (
                    session_binding_id,
                    event_type,
                    hook_input_digest,
                    claim_id,
                    job_id,
                    attempt_id,
                    self.realm_id,
                    idempotency_key,
                    effect_plan_digest,
                    authorization_id,
                    effect_plan_digest,
                    resource,
                    canonical_json(
                        {
                            "allowed_resources": [resource],
                            "allowed_effects": ["database-write"],
                            "provider_refs": [],
                            "secret_ref_ids": [],
                            "data_classifications": ["internal"],
                        }
                    ),
                    effect_digest,
                    operation,
                    adapter_digest,
                    authorization_digest,
                    fencing_token,
                    fencing_token,
                    canonical_json([{"resource": resource, "mode": "write"}]),
                    resource,
                    work_plan_digest,
                ),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Lifecycle exact terminal continuity/hook receipt zinciri yok")
        row = rows[0]
        terminal_digest = str(row[3])
        output_digest = str(row[4])
        if terminal_digest != output_digest:
            raise PolicyViolation("Lifecycle outbox/hook terminal receipt digest drift")
        compiler_text = str(row[5]).lower()
        if compiler_text not in {"true", "false"}:
            raise PolicyViolation("Lifecycle compiler enqueue receipt boolean degil")
        return LifecycleTerminalRecord(
            UUID(str(row[0])),
            str(row[1]),
            UUID(str(row[2])),
            terminal_digest,
            compiler_text == "true",
            UUID(str(row[6])),
            str(row[7]),
            UUID(str(row[8])),
            str(row[9]),
            str(row[10]),
        )

    def lookup_hook_terminal_output(
        self,
        *,
        session_binding_id: UUID,
        event_type: str,
        input_digest: str,
    ) -> HookTerminalOutput:
        """Read the just-persisted exact hook receipt inside the caller transaction."""

        parse_digest(input_digest)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select receipt.id,receipt.output_digest,"
                " receipt.output_body->'command'->>'compiler_enqueue'"
                " from hooks.invocation invocation join hooks.result_receipt receipt"
                " on receipt.realm_id=invocation.realm_id and receipt.invocation_id=invocation.id"
                " where invocation.realm_id=%s and invocation.session_binding_id=%s"
                " and invocation.event_type=%s and invocation.input_digest=%s"
                " and receipt.status='completed' and receipt.effect_performed=false"
                " and receipt.grants_authority=false",
                (self.realm_id, session_binding_id, event_type, input_digest),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Lifecycle exact hook terminal output receipt yok")
        output_digest = str(rows[0][1])
        parse_digest(output_digest)
        compiler_text = str(rows[0][2]).lower()
        if compiler_text not in {"true", "false"}:
            raise PolicyViolation("Lifecycle hook compiler enqueue boolean degil")
        return HookTerminalOutput(UUID(str(rows[0][0])), output_digest, compiler_text == "true")

    def ingest(
        self,
        document: Mapping[str, Any],
        *,
        client_instance_id: str,
        client_kind: ClientKind | None = None,
        now: dt.datetime | None = None,
    ) -> LifecycleAck:
        local_digest = str(document.get("event_digest", ""))
        parse_digest(local_digest)
        body = {key: value for key, value in document.items() if key != "event_digest"}
        if digest(body) != local_digest:
            raise ValidationFailed("Lifecycle supplied digest canonical body ile uyusmuyor")
        schema = body.get("schema")
        if schema == "zekam-opencode-lifecycle-event/v2":
            observed_kind = ClientKind.OPENCODE
        elif schema == "zekam-client-lifecycle-event/v1":
            expected_fields = {
                "schema",
                "client_id",
                "client_kind",
                "session_id",
                "sequence",
                "previous_digest",
                "event_type",
                "payload_digest",
                "occurred_at",
                "transcript_included",
                "grants_authority",
            }
            if set(body) != expected_fields:
                raise ValidationFailed("Canonical lifecycle schema disi alan tasiyor")
            try:
                observed_kind = ClientKind(str(body.get("client_kind")))
            except ValueError as exc:
                raise ValidationFailed("Canonical lifecycle client kind gecersiz") from exc
            if (
                body.get("transcript_included") is not False
                or body.get("grants_authority") is not False
            ):
                raise ValidationFailed("Canonical lifecycle transcript/authority tasiyamaz")
            if body.get("client_id") != client_instance_id:
                raise ValidationFailed("Canonical lifecycle client identity binding uyusmuyor")
        else:
            raise ValidationFailed("Canonical ingest desteklenen lifecycle schema ister")
        if client_kind is not None and client_kind is not observed_kind:
            raise ValidationFailed("Lifecycle client kind binding uyusmuyor")
        sequence = int(body["sequence"])
        previous = body.get("previous_digest")
        session_id = str(body["session_id"])
        occurred_at = dt.datetime.fromisoformat(str(body["occurred_at"]))
        if occurred_at.tzinfo is None:
            raise ValidationFailed("Canonical lifecycle zamani timezone-aware olmali")
        acknowledged_at = now or dt.datetime.now(dt.UTC)

        # connect() autocommit kullanir; stream head, event ve ACK tek transaction olmadan
        # crash sonrasi ayrisabilir ve replay kalici head mismatch'e dusebilir.
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select e.id,a.canonical_digest,a.acknowledged_at,o.id,o.payload_digest"
                " from client.lifecycle_event e join client.lifecycle_ack a"
                " on a.realm_id=e.realm_id and a.event_id=e.id"
                " left join work.compaction_checkpoint_outbox o"
                " on o.realm_id=e.realm_id and o.lifecycle_event_id=e.id"
                " where e.realm_id=%s and e.event_digest=%s",
                (self.realm_id, local_digest),
            )
            replay = cursor.fetchone()
            if replay is not None:
                return LifecycleAck(
                    UUID(str(replay[0])),
                    local_digest,
                    str(replay[1]),
                    replay[2],
                    None if replay[3] is None else UUID(str(replay[3])),
                    None if replay[4] is None else str(replay[4]),
                )

            cursor.execute(
                "select id,head_sequence,head_digest from client.lifecycle_stream"
                " where realm_id=%s and client_instance_id=%s and session_id=%s for update",
                (self.realm_id, client_instance_id, session_id),
            )
            stream = cursor.fetchone()
            if stream is None:
                if sequence != 1 or previous is not None:
                    raise ConcurrencyConflict("Lifecycle stream ilk sequence/previous gecersiz")
                stream_id = new_uuid7(now=acknowledged_at)
                cursor.execute(
                    "insert into client.lifecycle_stream"
                    " (id,realm_id,client_kind,client_instance_id,session_id,head_sequence,"
                    " head_digest,created_at,updated_at)"
                    " values (%s,%s,%s,%s,%s,0,null,%s,%s)",
                    (
                        stream_id,
                        self.realm_id,
                        observed_kind.value,
                        client_instance_id,
                        session_id,
                        acknowledged_at,
                        acknowledged_at,
                    ),
                )
                head_sequence, head_digest = 0, None
            else:
                stream_id = UUID(str(stream[0]))
                head_sequence, head_digest = int(stream[1]), stream[2]
            if sequence != head_sequence + 1 or previous != head_digest:
                raise ConcurrencyConflict("Lifecycle stream head/previous mismatch")

            event_id = new_uuid7(now=acknowledged_at)
            cursor.execute(
                "insert into client.lifecycle_event"
                " (id,realm_id,stream_id,sequence,previous_digest,event_digest,payload,"
                " occurred_at,ingested_at,grants_authority)"
                " values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,false)",
                (
                    event_id,
                    self.realm_id,
                    stream_id,
                    sequence,
                    previous,
                    local_digest,
                    canonical_json(body),
                    occurred_at,
                    acknowledged_at,
                ),
            )
            cursor.execute(
                "update client.lifecycle_stream set head_sequence=%s,head_digest=%s,updated_at=%s"
                " where realm_id=%s and id=%s",
                (sequence, local_digest, acknowledged_at, self.realm_id, stream_id),
            )
            canonical_digest = digest(
                {
                    "realm_id": self.realm_id,
                    "stream_id": stream_id,
                    "event_id": event_id,
                    "local_event_digest": local_digest,
                }
            )
            cursor.execute(
                "insert into client.lifecycle_ack"
                " (id,realm_id,event_id,local_event_digest,canonical_digest,acknowledged_at)"
                " values (%s,%s,%s,%s,%s,%s)",
                (
                    new_uuid7(now=acknowledged_at),
                    self.realm_id,
                    event_id,
                    local_digest,
                    canonical_digest,
                    acknowledged_at,
                ),
            )
            cursor.execute(
                "select id,payload_digest from work.compaction_checkpoint_outbox"
                " where realm_id=%s and lifecycle_event_id=%s",
                (self.realm_id, event_id),
            )
            compaction = cursor.fetchone()
        return LifecycleAck(
            event_id,
            local_digest,
            canonical_digest,
            acknowledged_at,
            None if compaction is None else UUID(str(compaction[0])),
            None if compaction is None else str(compaction[1]),
        )
