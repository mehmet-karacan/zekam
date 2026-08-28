"""Fail-closed lookup of the current Codex lifecycle runtime template."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import parse_digest
from zekam.domain.errors import PolicyViolation


@dataclass(frozen=True, slots=True)
class LifecycleRuntimeTemplate:
    project_id: UUID
    source_revision: str
    policy_digest: str
    routing_context_snapshot_id: UUID
    routing_context_digest: str
    route_decision_id: UUID
    route_decision_digest: str
    route_expires_at: dt.datetime
    execution_target_id: UUID
    execution_target_digest: str
    model_id: str
    provider_binding_id: UUID
    provider_binding_digest: str
    provider_ref: str
    endpoint_ref: str
    operation: str
    execution_environment_snapshot_id: UUID
    execution_environment_snapshot_digest: str
    environment_capability_digest: str
    tool_runtime_digest: str
    config_effective_digest: str
    hook_set_digest: str
    hook_config_effective_digest: str
    compiled_tool_set_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.routing_context_digest,
            self.route_decision_digest,
            self.execution_target_digest,
            self.provider_binding_digest,
            self.execution_environment_snapshot_digest,
            self.environment_capability_digest,
            self.tool_runtime_digest,
            self.config_effective_digest,
            self.hook_set_digest,
            self.hook_config_effective_digest,
            self.compiled_tool_set_digest,
        ):
            parse_digest(value)
        if self.route_expires_at.tzinfo is None:
            raise PolicyViolation("Lifecycle runtime route expiry timezone-aware olmali")
        if any(
            not value.strip()
            for value in (self.source_revision, self.model_id, self.provider_ref, self.operation)
        ):
            raise PolicyViolation("Lifecycle runtime template kimlikleri bos olamaz")


@dataclass(frozen=True, slots=True)
class LifecycleRuntimeTemplateRepository:
    connection: Any
    realm_id: UUID

    def projection_facts(self, project_id: UUID, work_item_id: UUID) -> tuple[Any, ...]:
        """Return exact current Work/source/migration facts for projection materialization."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select work.revision,work.state,work.record_digest,revision.revision,"
                " revision.tree_digest,migration.version,"
                " models.capability_runtime_jsonb_digest(to_jsonb(migration.checksum))"
                " from work.work_item work join projects.source_binding binding"
                " on binding.realm_id=work.realm_id and binding.project_id=work.project_id"
                " join lateral(select item.revision,item.tree_digest"
                " from projects.source_revision item where item.realm_id=binding.realm_id"
                " and item.binding_id=binding.id order by item.observed_at desc,item.id desc"
                " limit 1) revision on true join lateral(select version,checksum"
                " from core.schema_migrations order by version desc limit 1) migration on true"
                " where work.realm_id=%s and work.project_id=%s and work.id=%s",
                (self.realm_id, project_id, work_item_id),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Lifecycle bootstrap projection facts exact degil")
        return tuple(rows[0])

    def run_bindings(self, run_id: UUID) -> tuple[Any, ...]:
        """Return immutable active run bindings required by an execution envelope."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select source_revision,policy_digest,max_input_tokens,max_output_tokens,"
                " max_cost_micros,deadline,session_id from runtime.execution_run"
                " where realm_id=%s and id=%s and state='active'",
                (self.realm_id, run_id),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Lifecycle child active run bindings exact degil")
        return tuple(rows[0])

    def current_for_bootstrap_job(self, job_id: UUID) -> LifecycleRuntimeTemplate:
        """Resolve the current template before a bootstrap parent is claimed."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select job.project_id,run.source_revision,plan.policy_digest"
                " from runtime.job job join runtime.execution_run run"
                " on run.realm_id=job.realm_id and run.id=job.run_id"
                " join work.task_plan plan on plan.realm_id=job.realm_id"
                " and plan.id=job.plan_id where job.realm_id=%s and job.id=%s"
                " and job.state in ('ready','running')"
                " and job.kind='mutation' and job.max_attempts=1"
                " and job.required_capabilities="
                " array['client.lifecycle.codex-bootstrap']::text[]",
                (self.realm_id, job_id),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Lifecycle bootstrap pre-claim job binding exact degil")
        project_id, source_revision, policy_digest = rows[0]
        return self.current(UUID(str(project_id)), str(source_revision), str(policy_digest))

    def assert_rebootstrap_admissible(self, work_item_id: UUID) -> None:
        """Require a closed prior bootstrap and no live ownership/effect ambiguity."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select count(*) filter(where job.step_id='client-lifecycle-bootstrap'),"
                " count(*) filter(where job.state in ('ready','running','recovery-required')),"
                " count(*) filter(where lease.id is not null),"
                " count(*) filter(where pending.claim_id is not null)"
                " from runtime.job job left join runtime.lease lease"
                " on lease.realm_id=job.realm_id and lease.job_id=job.id"
                " left join runtime.claim_without_receipt pending"
                " on pending.realm_id=job.realm_id and pending.job_id=job.id"
                " where job.realm_id=%s and job.work_item_id=%s",
                (self.realm_id, work_item_id),
            )
            row = cursor.fetchone()
        counts = () if row is None else tuple(int(value or 0) for value in row)
        if len(counts) != 4 or counts[0] < 1 or any(counts[index] for index in (1, 2, 3)):
            raise PolicyViolation("Lifecycle explicit re-bootstrap prior work acik veya belirsiz")

    def bootstrap_context(self, run_id: UUID) -> tuple[UUID, UUID, str, UUID, str]:
        """Resolve the one completed parent bootstrap envelope for a child run."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select job.id,envelope.context_manifest_id,envelope.context_manifest_digest,"
                " envelope.context_packet_id,envelope.context_packet_digest"
                " from runtime.job job join lateral(select item.*"
                " from runtime.execution_envelope item where item.realm_id=job.realm_id"
                " and item.job_id=job.id order by item.request_ordinal desc,"
                " item.created_at desc,item.id desc limit 1) envelope on true"
                " where job.realm_id=%s and job.run_id=%s and job.state='completed'"
                " and job.step_id='client-lifecycle-bootstrap'"
                " and job.required_capabilities="
                " array['client.lifecycle.codex-bootstrap']::text[]"
                " order by job.created_at desc,job.id desc limit 2",
                (self.realm_id, run_id),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Lifecycle child parent bootstrap context exact degil")
        row = rows[0]
        manifest_digest = str(row[2])
        packet_digest = str(row[4])
        parse_digest(manifest_digest)
        parse_digest(packet_digest)
        return (
            UUID(str(row[0])),
            UUID(str(row[1])),
            manifest_digest,
            UUID(str(row[3])),
            packet_digest,
        )

    def current_source_revision(self, project_id: UUID) -> str:
        """Resolve one latest canonical source revision for bootstrap planning."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select revision.revision from projects.source_binding binding"
                " join projects.source_revision revision on revision.realm_id=binding.realm_id"
                " and revision.binding_id=binding.id where binding.realm_id=%s"
                " and binding.project_id=%s order by revision.observed_at desc,revision.id desc"
                " limit 2",
                (self.realm_id, project_id),
            )
            rows = cursor.fetchall()
        if not rows:
            raise PolicyViolation("Lifecycle bootstrap canonical source revision bulunamadi")
        latest = str(rows[0][0])
        if len(rows) > 1 and str(rows[1][0]) == latest:
            raise PolicyViolation("Lifecycle bootstrap source revision belirsiz")
        return latest

    def next_bootstrap_job_id(self) -> UUID | None:
        """Return only an unambiguous reviewed bootstrap parent job."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id from runtime.job where realm_id=%s and state='ready'"
                " and kind='mutation' and max_attempts=1"
                " and required_capabilities=array['client.lifecycle.codex-bootstrap']::text[]"
                " and payload->>'schema'='zekam-codex-lifecycle-bootstrap-job/v1'"
                " and jsonb_typeof(payload->'entry_digest')='string'"
                " and jsonb_typeof(payload->'authorization_id')='string'"
                " and jsonb_typeof(payload->'effect_digest')='string'"
                " and jsonb_typeof(payload->'child_assignment_id')='string'"
                " and cardinality(write_resources)=1"
                " and not exists(select 1 from runtime.job_attempt attempt"
                " where attempt.realm_id=runtime.job.realm_id and attempt.job_id=runtime.job.id)"
                " order by priority,available_at,created_at,id limit 2",
                (self.realm_id,),
            )
            rows = cursor.fetchall()
        if len(rows) > 1:
            raise PolicyViolation("Lifecycle bootstrap parent secimi belirsiz")
        return None if not rows else UUID(str(rows[0][0]))

    def current(
        self,
        project_id: UUID,
        source_revision: str,
        policy_digest: str,
    ) -> LifecycleRuntimeTemplate:
        if not source_revision.strip():
            raise PolicyViolation("Lifecycle runtime template source revision ister")
        parse_digest(policy_digest)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "with context_candidates as ("
                " select c.*,dense_rank() over(order by c.captured_at desc) freshness"
                " from projects.routing_context_snapshot c"
                " where c.realm_id=%s and c.project_id=%s and c.source_revision=%s"
                " and c.policy_digest=%s and c.expires_at>statement_timestamp()),"
                " current_context as (select * from context_candidates where freshness=1),"
                " route_candidates as ("
                " select d.*,t.snapshot_digest target_digest,t.expires_at target_expires_at,"
                " dense_rank() over(order by case when d.project_id is null then 0 else 1 end desc,"
                " d.decided_at desc) freshness"
                " from models.model_route_decision d"
                " join models.execution_target_snapshot t on t.realm_id=d.realm_id"
                " and t.id=d.execution_target_id"
                " join current_context c on c.realm_id=d.realm_id"
                " and (d.project_context_id=c.id or (d.project_id is null"
                " and d.project_context_id is null and d.inventory_digest=c.inventory_digest))"
                " where d.realm_id=%s and d.status='selected' and d.policy_digest=%s"
                " and t.client_id='codex' and t.slot='lifecycle'"
                " and t.expires_at>statement_timestamp()),"
                " current_route as (select * from route_candidates where freshness=1),"
                " provider_candidates as ("
                " select p.*,dense_rank() over(order by p.captured_at desc) freshness"
                " from models.provider_binding_snapshot p join current_route r"
                " on r.realm_id=p.realm_id and r.primary_model_id=p.model_id"
                " where p.realm_id=%s and p.expires_at>statement_timestamp()),"
                " current_provider as (select * from provider_candidates where freshness=1),"
                " environment_candidates as ("
                " select e.*,dense_rank() over(order by probe.checked_at desc) freshness"
                " from runtime.environment_probe_evidence probe"
                " join runtime.execution_environment_snapshot e on e.realm_id=probe.realm_id"
                " and e.snapshot_digest=probe.sticky_snapshot_digest"
                " join runtime.execution_environment_snapshot current_env"
                " on current_env.realm_id=probe.realm_id"
                " and current_env.snapshot_digest=probe.current_snapshot_digest"
                " where probe.realm_id=%s and probe.drift_dimensions='{}'::text[]"
                " and e.source_revision=%s and e.expires_at>statement_timestamp()"
                " and current_env.expires_at>statement_timestamp()),"
                " current_environment as ("
                " select * from environment_candidates where freshness=1)"
                " select c.id,c.context_digest,r.id,r.evidence_digest,r.target_expires_at,"
                " r.execution_target_id,r.target_digest,r.primary_model_id,"
                " p.id,p.binding_digest,p.provider_ref,p.endpoint_ref,p.operation,"
                " e.id,e.snapshot_digest,e.capability_digest,e.tool_runtime_digest,"
                " e.config_effective_digest,hooks.hook_set_digest,compiled.config_effective_digest,"
                " tool_set.tool_set_digest"
                " from current_context c cross join current_route r"
                " cross join current_provider p cross join current_environment e"
                " cross join lateral(select item.tool_set_digest from tools.compiled_set item"
                " where item.realm_id=e.realm_id and item.role='builder'"
                " and item.permission_profile_digest=e.permission_profile_digest"
                " order by item.created_at desc,item.id desc limit 1) tool_set"
                " join hooks.current_generation hooks on hooks.realm_id=%s"
                " join hooks.compiled_set compiled on compiled.realm_id=hooks.realm_id"
                " and compiled.id=hooks.compiled_set_id"
                " and compiled.config_effective_digest=e.config_effective_digest",
                (
                    self.realm_id,
                    project_id,
                    source_revision,
                    policy_digest,
                    self.realm_id,
                    policy_digest,
                    self.realm_id,
                    self.realm_id,
                    source_revision,
                    self.realm_id,
                ),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise PolicyViolation("Lifecycle runtime template eksik, stale veya belirsiz")
        row = rows[0]
        return LifecycleRuntimeTemplate(
            project_id=project_id,
            source_revision=source_revision,
            policy_digest=policy_digest,
            routing_context_snapshot_id=UUID(str(row[0])),
            routing_context_digest=str(row[1]),
            route_decision_id=UUID(str(row[2])),
            route_decision_digest=str(row[3]),
            route_expires_at=row[4],
            execution_target_id=UUID(str(row[5])),
            execution_target_digest=str(row[6]),
            model_id=str(row[7]),
            provider_binding_id=UUID(str(row[8])),
            provider_binding_digest=str(row[9]),
            provider_ref=str(row[10]),
            endpoint_ref=str(row[11]),
            operation=str(row[12]),
            execution_environment_snapshot_id=UUID(str(row[13])),
            execution_environment_snapshot_digest=str(row[14]),
            environment_capability_digest=str(row[15]),
            tool_runtime_digest=str(row[16]),
            config_effective_digest=str(row[17]),
            hook_set_digest=str(row[18]),
            hook_config_effective_digest=str(row[19]),
            compiled_tool_set_digest=str(row[20]),
        )
