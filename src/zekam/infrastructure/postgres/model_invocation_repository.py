"""PostgreSQL persistence for the universal model invocation ledger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from zekam.domain.canonical import parse_digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_invocation import GatewayMode, ModelRequestManifest


@dataclass(frozen=True, slots=True)
class ModelInvocationRepository:
    connection: Any
    realm_id: UUID

    def store_manifest(self, item: ModelRequestManifest) -> tuple[UUID, bool]:
        if item.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm model manifest reddedildi")
        item.assert_digest()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
                (str(self.realm_id),),
            )
            cursor.execute(
                "insert into models.request_manifest"
                " (id,realm_id,project_id,work_item_id,plan_id,step_id,execution_envelope_id,"
                " execution_envelope_digest,run_id,job_id,attempt_id,"
                " assignment_id,role,risk,route_decision_digest,model_id,provider_ref,"
                " context_manifest_digest,context_fragment_set_digest,"
                " model_visible_payload_digest,context_packet_digest,checkpoint_digest,"
                " source_revision,"
                " policy_digest,payload_digest,authorization_scope_digest,output_schema_digest,"
                " idempotency_key,max_input_tokens,max_output_tokens,max_cost_micros,deadline,"
                " route_expires_at,"
                " source_label,missing_bindings,binding_status,tool_contract_digest,"
                " environment_digest,"
                " permission_profile_digest,tool_set_digest,"
                " tool_visible_payload_digest,tool_visible_payload_mode,"
                " turn_execution_snapshot_digest,config_effective_digest,hook_set_digest,"
                " created_at,manifest_digest)"
                " values (" + ",".join(["%s"] * 47) + ")"
                " on conflict do nothing returning id",
                (
                    item.id,
                    item.realm_id,
                    item.project_id,
                    item.work_item_id,
                    item.plan_id,
                    item.step_id,
                    item.execution_envelope_id,
                    item.execution_envelope_digest,
                    item.run_id,
                    item.job_id,
                    item.attempt_id,
                    item.assignment_id,
                    item.role,
                    item.risk,
                    item.route_decision_digest,
                    item.model_id,
                    item.provider_ref,
                    item.context_manifest_digest,
                    item.context_fragment_set_digest,
                    item.model_visible_payload_digest,
                    item.context_packet_digest,
                    item.checkpoint_digest,
                    item.source_revision,
                    item.policy_digest,
                    item.payload_digest,
                    item.authorization_scope_digest,
                    item.output_schema_digest,
                    item.idempotency_key,
                    item.max_input_tokens,
                    item.max_output_tokens,
                    item.max_cost_micros,
                    item.deadline,
                    item.route_expires_at,
                    item.source_label.value,
                    list(item.missing_bindings),
                    item.binding_status.value,
                    item.tool_contract_digest,
                    item.environment_digest,
                    item.permission_profile_digest,
                    item.tool_set_digest,
                    item.tool_visible_payload_digest,
                    item.tool_visible_payload_mode,
                    item.turn_execution_snapshot_digest,
                    item.config_effective_digest,
                    item.hook_set_digest,
                    item.created_at,
                    item.manifest_digest,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from models.request_manifest where realm_id=%s and manifest_digest=%s",
                (self.realm_id, item.manifest_digest),
            )
            return UUID(str(cursor.fetchone()[0])), False

    def envelope_bindings(self, envelope_id: UUID) -> dict[str, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select e.id,e.envelope_digest,e.run_id,e.role,e.route_decision_digest,"
                "e.route_expires_at,e.context_manifest_digest,e.context_packet_digest,"
                "e.checkpoint_digest,e.source_revision,e.policy_digest,e.output_schema_digest,"
                "e.max_input_tokens,e.max_output_tokens,e.max_cost_micros,e.deadline,"
                "e.turn_execution_snapshot_digest,t.execution_environment_snapshot_digest,"
                "env.permission_profile_digest,tool_set.tool_set_digest,"
                "t.config_effective_digest,t.hook_set_digest"
                " from runtime.execution_envelope e"
                " left join runtime.turn_execution_snapshot t on t.realm_id=e.realm_id"
                " and t.id=e.turn_execution_snapshot_id"
                " left join runtime.execution_environment_snapshot env on env.realm_id=t.realm_id"
                " and env.snapshot_digest=t.execution_environment_snapshot_digest"
                " left join tools.compiled_set tool_set on tool_set.realm_id=t.realm_id"
                " and tool_set.tool_set_digest=t.exposed_tool_set_digest"
                " join runtime.execution_run r on r.realm_id=e.realm_id and r.id=e.run_id"
                " join runtime.job j on j.realm_id=e.realm_id and j.id=e.job_id"
                " join agents.assignment a on a.realm_id=e.realm_id and a.id=e.assignment_id"
                " join runtime.lease l on l.realm_id=e.realm_id and l.id=e.lease_id"
                " where e.realm_id=%s and e.id=%s and r.state='active'"
                " and j.state='running' and a.status='active'"
                " and l.expires_at>statement_timestamp()"
                " and e.route_expires_at>statement_timestamp()"
                " and e.deadline>statement_timestamp()"
                " and (e.turn_execution_snapshot_id is null or ("
                " env.expires_at>statement_timestamp()"
                " and exists(select 1 from runtime.environment_probe_evidence p"
                " join runtime.execution_environment_snapshot current_env"
                " on current_env.realm_id=p.realm_id"
                " and current_env.snapshot_digest=p.current_snapshot_digest"
                " where p.realm_id=e.realm_id"
                " and p.sticky_snapshot_digest=t.execution_environment_snapshot_digest"
                " and cardinality(p.drift_dimensions)=0"
                " and p.checked_at>=statement_timestamp()-interval '5 minutes'"
                " and current_env.expires_at>statement_timestamp()"
                " and p.id=(select latest.id from runtime.environment_probe_evidence latest"
                " where latest.realm_id=p.realm_id"
                " and latest.sticky_snapshot_digest=p.sticky_snapshot_digest"
                " order by latest.checked_at desc,latest.id desc limit 1))))",
                (self.realm_id, envelope_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise PolicyViolation("Gateway execution envelope bulunamadi veya stale")
        names = (
            "execution_envelope_id",
            "execution_envelope_digest",
            "run_id",
            "role",
            "route_decision_digest",
            "route_expires_at",
            "context_manifest_digest",
            "context_packet_digest",
            "checkpoint_digest",
            "source_revision",
            "policy_digest",
            "output_schema_digest",
            "max_input_tokens",
            "max_output_tokens",
            "max_cost_micros",
            "deadline",
            "turn_execution_snapshot_digest",
            "environment_digest",
            "permission_profile_digest",
            "tool_set_digest",
            "config_effective_digest",
            "hook_set_digest",
        )
        return dict(zip(names, row, strict=True))

    def assert_current_envelope(self, item: ModelRequestManifest) -> None:
        if item.execution_envelope_id is None or item.execution_envelope_digest is None:
            raise PolicyViolation("Gateway enforce canonical execution envelope ister")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select 1 from runtime.execution_envelope e"
                " join runtime.execution_run r on r.realm_id=e.realm_id and r.id=e.run_id"
                " join runtime.job j on j.realm_id=e.realm_id and j.id=e.job_id"
                " join agents.assignment a on a.realm_id=e.realm_id and a.id=e.assignment_id"
                " join runtime.lease l on l.realm_id=e.realm_id and l.id=e.lease_id"
                " join runtime.turn_execution_snapshot t on t.realm_id=e.realm_id"
                " and t.id=e.turn_execution_snapshot_id"
                " join runtime.execution_environment_snapshot env on env.realm_id=t.realm_id"
                " and env.snapshot_digest=t.execution_environment_snapshot_digest"
                " where e.realm_id=%s and e.id=%s and e.envelope_digest=%s"
                " and r.state='active' and j.state='running' and a.status='active'"
                " and l.expires_at>statement_timestamp()"
                " and e.route_expires_at>statement_timestamp()"
                " and e.deadline>statement_timestamp()"
                " and env.expires_at>statement_timestamp()"
                " and exists(select 1 from runtime.environment_probe_evidence p"
                " join runtime.execution_environment_snapshot current_env"
                " on current_env.realm_id=p.realm_id"
                " and current_env.snapshot_digest=p.current_snapshot_digest"
                " where p.realm_id=e.realm_id"
                " and p.sticky_snapshot_digest=t.execution_environment_snapshot_digest"
                " and cardinality(p.drift_dimensions)=0"
                " and p.checked_at>=statement_timestamp()-interval '5 minutes'"
                " and current_env.expires_at>statement_timestamp()"
                " and p.id=(select latest.id from runtime.environment_probe_evidence latest"
                " where latest.realm_id=p.realm_id"
                " and latest.sticky_snapshot_digest=p.sticky_snapshot_digest"
                " order by latest.checked_at desc,latest.id desc limit 1))",
                (self.realm_id, item.execution_envelope_id, item.execution_envelope_digest),
            )
            if cursor.fetchone() is None:
                raise PolicyViolation("Gateway enforce execution envelope stale veya gecersiz")

    def assert_current_context_fragment_set(self, item: ModelRequestManifest) -> None:
        if item.context_fragment_set_digest is None or item.context_manifest_digest is None:
            raise PolicyViolation("Gateway enforce canonical context fragment set ister")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select 1 from work.context_fragment_set s"
                " join work.context_manifest m on m.realm_id=s.realm_id"
                " and m.id=s.context_manifest_id"
                " where s.realm_id=%s and s.project_id=%s and s.work_item_id=%s"
                " and s.fragment_set_digest=%s and m.manifest_digest=%s",
                (
                    self.realm_id,
                    item.project_id,
                    item.work_item_id,
                    item.context_fragment_set_digest,
                    item.context_manifest_digest,
                ),
            )
            if cursor.fetchone() is None:
                raise PolicyViolation("Gateway context fragment set stale veya gecersiz")

    def assert_current_tool_set(self, item: ModelRequestManifest) -> None:
        if item.tool_set_digest is None or item.permission_profile_digest is None:
            raise PolicyViolation("Gateway enforce compiled tool set binding ister")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select 1 from tools.compiled_set s"
                " where s.realm_id=%s and s.tool_set_digest=%s and s.role=%s"
                " and s.permission_profile_digest=%s"
                " and not exists(select 1 from jsonb_array_elements(s.entries) entry"
                " where not exists(select 1 from tools.runtime_revision r"
                " where r.realm_id=s.realm_id and r.tool_id=entry->>'tool_id'"
                " and r.revision=(entry->>'revision')::integer"
                " and r.runtime_digest=entry->>'runtime_digest'"
                " and r.captured_at<=statement_timestamp()"
                " and r.expires_at>statement_timestamp()"
                " and r.id=(select latest.id from tools.runtime_revision latest"
                " where latest.realm_id=r.realm_id and latest.tool_id=r.tool_id"
                " and latest.captured_at<=statement_timestamp()"
                " and latest.expires_at>statement_timestamp()"
                " order by latest.revision desc,latest.captured_at desc,latest.id desc limit 1)))",
                (
                    self.realm_id,
                    item.tool_set_digest,
                    item.role,
                    item.permission_profile_digest,
                ),
            )
            if cursor.fetchone() is None:
                raise PolicyViolation("Gateway compiled tool set stale veya runtime drift")

    def record_audit(
        self,
        *,
        source_label: str,
        disposition: str,
        call_digest: str,
        payload_digest: str,
        missing_bindings: tuple[str, ...] = (),
        manifest_id: UUID | None = None,
        response_digest: str | None = None,
    ) -> UUID:
        for value in (call_digest, payload_digest, response_digest):
            if value is not None:
                parse_digest(value)
        audit_id = uuid4()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
                (str(self.realm_id),),
            )
            cursor.execute(
                "insert into models.invocation_audit"
                " (id,realm_id,manifest_id,source_label,disposition,missing_bindings,"
                " call_digest,payload_digest,response_digest) values (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    audit_id,
                    self.realm_id,
                    manifest_id,
                    source_label,
                    disposition,
                    list(missing_bindings),
                    call_digest,
                    payload_digest,
                    response_digest,
                ),
            )
        return audit_id

    def mode(self) -> GatewayMode:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select mode from models.gateway_policy where realm_id=%s", (self.realm_id,)
            )
            row = cursor.fetchone()
        return GatewayMode.AUDIT if row is None else GatewayMode(str(row[0]))

    def record_attempt(
        self,
        *,
        manifest_id: UUID,
        effect_claim_id: UUID,
        authorization_id: UUID,
        state: str = "sent",
    ) -> UUID:
        attempt_id = uuid4()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.invocation_attempt"
                " (id,realm_id,manifest_id,ordinal,effect_claim_id,authorization_id,state)"
                " values (%s,%s,%s,1,%s,%s,%s)",
                (
                    attempt_id,
                    self.realm_id,
                    manifest_id,
                    effect_claim_id,
                    authorization_id,
                    state,
                ),
            )
        return attempt_id

    def record_result(
        self,
        *,
        manifest_id: UUID,
        attempt_id: UUID,
        effect_receipt_id: UUID | None,
        state: str,
        response_digest: str | None = None,
        failure_digest: str | None = None,
    ) -> UUID:
        for value in (response_digest, failure_digest):
            if value is not None:
                parse_digest(value)
        result_id = uuid4()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.invocation_result"
                " (id,realm_id,manifest_id,attempt_id,effect_receipt_id,state,response_digest,"
                " failure_digest) values (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    result_id,
                    self.realm_id,
                    manifest_id,
                    attempt_id,
                    effect_receipt_id,
                    state,
                    response_digest,
                    failure_digest,
                ),
            )
        return result_id

    def activate_enforce(self, policy_digest: str) -> None:
        parse_digest(policy_digest)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute("select models.activate_gateway_enforce(%s)", (policy_digest,))
