"""PostgreSQL persistence for execution run, packet and envelope."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.execution_environment import (
    AssignmentEnvironmentBinding,
    EnvironmentDriftReport,
    ExecutionEnvironmentSnapshot,
    ShellSnapshot,
    TurnExecutionSnapshot,
)
from zekam.domain.execution_run import (
    ContextPacket,
    ExecutionEnvelope,
    ExecutionRun,
    ProviderBindingSnapshot,
)


@dataclass(frozen=True, slots=True)
class ExecutionRunRepository:
    connection: Any
    realm_id: UUID

    def create_run(self, item: ExecutionRun) -> tuple[UUID, bool]:
        self._realm(item.realm_id)
        values = (
            item.id,
            item.realm_id,
            item.project_id,
            item.work_item_id,
            item.plan_id,
            item.client_id,
            item.session_id,
            item.source_revision,
            item.policy_digest,
            item.max_input_tokens,
            item.max_output_tokens,
            item.max_cost_micros,
            item.deadline,
            item.state.value,
            item.run_digest,
            False,
            item.created_at,
        )
        return self._insert(
            "runtime.execution_run",
            "(id,realm_id,project_id,work_item_id,plan_id,client_id,session_id,source_revision,"
            "policy_digest,max_input_tokens,max_output_tokens,max_cost_micros,deadline,state,"
            "run_digest,grants_authority,created_at)",
            values,
            "run_digest",
            item.run_digest,
        )

    def create_packet(self, item: ContextPacket) -> tuple[UUID, bool]:
        self._realm(item.realm_id)
        values = (
            item.id,
            item.realm_id,
            item.project_id,
            item.work_item_id,
            item.manifest_id,
            item.manifest_digest,
            canonical_json([row.body() for row in item.sections]),
            item.packet_digest,
            False,
            item.created_at,
        )
        return self._insert(
            "work.context_packet",
            "(id,realm_id,project_id,work_item_id,manifest_id,manifest_digest,ordered_sections,"
            "packet_digest,grants_authority,created_at)",
            values,
            "packet_digest",
            item.packet_digest,
        )

    def create_envelope(self, item: ExecutionEnvelope) -> tuple[UUID, bool]:
        self._realm(item.realm_id)
        values = (
            item.id,
            item.realm_id,
            item.run_id,
            item.job_id,
            item.attempt_id,
            item.lease_id,
            item.fencing_token,
            item.request_ordinal,
            item.idempotency_key,
            item.assignment_id,
            item.role,
            item.route_decision_id,
            item.route_decision_digest,
            item.route_expires_at,
            item.model_id,
            item.provider_binding_id,
            item.provider_binding_digest,
            item.provider_ref,
            item.context_manifest_id,
            item.context_manifest_digest,
            item.context_packet_id,
            item.context_packet_digest,
            item.turn_execution_snapshot_id,
            item.turn_execution_snapshot_digest,
            item.checkpoint_id,
            item.checkpoint_digest,
            item.checkpoint_disposition.value,
            item.source_revision,
            item.policy_digest,
            item.authorization_scope_digest,
            item.output_schema_digest,
            item.payload_digest,
            item.max_input_tokens,
            item.max_output_tokens,
            item.max_cost_micros,
            item.deadline,
            item.envelope_digest,
            item.checkpoint_v2_id,
            item.checkpoint_v2_digest,
            False,
            item.created_at,
        )
        return self._insert(
            "runtime.execution_envelope",
            "(id,realm_id,run_id,job_id,attempt_id,lease_id,fencing_token,request_ordinal,"
            "idempotency_key,assignment_id,role,"
            "route_decision_id,route_decision_digest,route_expires_at,model_id,"
            "provider_binding_id,provider_binding_digest,provider_ref,"
            "context_manifest_id,context_manifest_digest,context_packet_id,context_packet_digest,"
            "turn_execution_snapshot_id,turn_execution_snapshot_digest,"
            "checkpoint_id,checkpoint_digest,checkpoint_disposition,source_revision,policy_digest,"
            "authorization_scope_digest,output_schema_digest,payload_digest,max_input_tokens,"
            "max_output_tokens,max_cost_micros,deadline,envelope_digest,checkpoint_v2_id,"
            "checkpoint_v2_digest,grants_authority,created_at)",
            values,
            "envelope_digest",
            item.envelope_digest,
        )

    def create_environment_snapshot(self, item: ExecutionEnvironmentSnapshot) -> tuple[UUID, bool]:
        self._realm(item.realm_id)
        return self._insert(
            "runtime.execution_environment_snapshot",
            "(id,realm_id,environment_id,execution_identity,provider,platform,"
            "executor_protocol_version,cwd_locator,workspace_roots,shell,permission_profile_id,"
            "permission_profile_digest,filesystem_policy_digest,network_policy_digest,"
            "tool_runtime_digest,capability_digest,config_effective_digest,source_revision,"
            "captured_at,expires_at,grants_authority,snapshot_digest)",
            (
                item.id,
                item.realm_id,
                item.environment_id,
                item.execution_identity,
                item.provider,
                item.platform,
                item.executor_protocol_version,
                item.cwd_locator,
                canonical_json(list(item.workspace_roots)),
                canonical_json(item.shell.body()),
                item.permission_profile_id,
                item.permission_profile_digest,
                item.filesystem_policy_digest,
                item.network_policy_digest,
                item.tool_runtime_digest,
                item.capability_digest,
                item.config_effective_digest,
                item.source_revision,
                item.captured_at,
                item.expires_at,
                False,
                item.snapshot_digest,
            ),
            "snapshot_digest",
            item.snapshot_digest,
        )

    def environment_for_envelope(self, envelope_id: UUID) -> ExecutionEnvironmentSnapshot:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select env.id,env.realm_id,env.environment_id,env.execution_identity,"
                "env.provider,env.platform,env.executor_protocol_version,env.cwd_locator,"
                "env.workspace_roots,env.shell,env.permission_profile_id,"
                "env.permission_profile_digest,env.filesystem_policy_digest,"
                "env.network_policy_digest,env.tool_runtime_digest,env.capability_digest,"
                "env.config_effective_digest,env.source_revision,env.captured_at,env.expires_at,"
                "env.snapshot_digest from runtime.execution_envelope e"
                " join runtime.turn_execution_snapshot t on t.realm_id=e.realm_id"
                " and t.id=e.turn_execution_snapshot_id"
                " join runtime.execution_environment_snapshot env on env.realm_id=t.realm_id"
                " and env.snapshot_digest=t.execution_environment_snapshot_digest"
                " where e.realm_id=%s and e.id=%s",
                (self.realm_id, envelope_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise PolicyViolation("Execution envelope sticky environment binding bulunamadi")
        shell = row[9]
        return ExecutionEnvironmentSnapshot(
            id=UUID(str(row[0])),
            realm_id=UUID(str(row[1])),
            environment_id=str(row[2]),
            execution_identity=str(row[3]),
            provider=str(row[4]),
            platform=str(row[5]),
            executor_protocol_version=str(row[6]),
            cwd_locator=str(row[7]),
            workspace_roots=tuple(str(item) for item in row[8]),
            shell=ShellSnapshot(
                str(shell["kind"]),
                str(shell["binary_digest"]),
                str(shell["startup_profile_digest"]),
            ),
            permission_profile_id=str(row[10]),
            permission_profile_digest=str(row[11]),
            filesystem_policy_digest=str(row[12]),
            network_policy_digest=str(row[13]),
            tool_runtime_digest=str(row[14]),
            capability_digest=str(row[15]),
            config_effective_digest=str(row[16]),
            source_revision=str(row[17]),
            captured_at=row[18],
            expires_at=row[19],
            snapshot_digest=str(row[20]),
        )

    def create_turn_snapshot(self, item: TurnExecutionSnapshot) -> tuple[UUID, bool]:
        self._realm(item.realm_id)
        return self._insert(
            "runtime.turn_execution_snapshot",
            "(id,realm_id,assignment_id,run_id,attempt_id,client_session_id,turn_id,model_id,"
            "provider_id,route_decision_digest,reasoning_profile_digest,"
            "execution_environment_snapshot_digest,context_manifest_digest,"
            "exposed_tool_set_digest,hook_set_digest,config_effective_digest,trace_id,"
            "grants_authority,created_at,turn_snapshot_digest)",
            (
                item.id,
                item.realm_id,
                item.assignment_id,
                item.run_id,
                item.attempt_id,
                item.client_session_id,
                item.turn_id,
                item.model_id,
                item.provider_id,
                item.route_decision_digest,
                item.reasoning_profile_digest,
                item.execution_environment_snapshot_digest,
                item.context_manifest_digest,
                item.exposed_tool_set_digest,
                item.hook_set_digest,
                item.config_effective_digest,
                item.trace_id,
                False,
                item.created_at,
                item.turn_snapshot_digest,
            ),
            "turn_snapshot_digest",
            item.turn_snapshot_digest,
        )

    def bind_assignment_environment(self, item: AssignmentEnvironmentBinding) -> tuple[UUID, bool]:
        self._realm(item.realm_id)
        return self._insert(
            "agents.assignment_environment_binding",
            "(id,realm_id,assignment_id,execution_environment_snapshot_digest,bound_at,"
            "grants_authority,binding_digest)",
            (
                item.id,
                item.realm_id,
                item.assignment_id,
                item.execution_environment_snapshot_digest,
                item.bound_at,
                False,
                item.binding_digest,
            ),
            "binding_digest",
            item.binding_digest,
        )

    def record_environment_probe(self, report: EnvironmentDriftReport) -> tuple[UUID, bool]:
        evidence_digest = digest(
            {
                "schema": "zekam-environment-probe-evidence/v1",
                "sticky_snapshot_digest": report.sticky_snapshot_digest,
                "current_snapshot_digest": report.current_snapshot_digest,
                "drift_dimensions": [item.value for item in report.dimensions],
                "checked_at": report.checked_at,
            }
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select execution_identity from runtime.execution_environment_snapshot"
                " where realm_id=%s and snapshot_digest=%s",
                (self.realm_id, report.sticky_snapshot_digest),
            )
            row = cursor.fetchone()
        if row is None:
            raise PolicyViolation("Sticky environment snapshot bulunamadi")
        return self._insert(
            "runtime.environment_probe_evidence",
            "(id,realm_id,execution_identity,sticky_snapshot_digest,current_snapshot_digest,"
            "drift_dimensions,checked_at,evidence_digest)",
            (
                uuid4(),
                self.realm_id,
                str(row[0]),
                report.sticky_snapshot_digest,
                report.current_snapshot_digest,
                [item.value for item in report.dimensions],
                report.checked_at,
                evidence_digest,
            ),
            "evidence_digest",
            evidence_digest,
        )

    def create_provider_binding(self, item: ProviderBindingSnapshot) -> tuple[UUID, bool]:
        self._realm(item.realm_id)
        return self._insert(
            "models.provider_binding_snapshot",
            "(id,realm_id,model_id,provider_ref,endpoint_ref,operation,binding_digest,"
            "captured_at,expires_at,grants_authority)",
            (
                item.id,
                item.realm_id,
                item.model_id,
                item.provider_ref,
                item.endpoint_ref,
                item.operation,
                item.binding_digest,
                item.captured_at,
                item.expires_at,
                False,
            ),
            "binding_digest",
            item.binding_digest,
        )

    def activate_run(self, run_id: UUID, *, started_at: dt.datetime) -> None:
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "update runtime.execution_run set state='active',started_at=%s"
                " where realm_id=%s and id=%s and state='prepared' returning id",
                (started_at, self.realm_id, run_id),
            )
            if cursor.fetchone() is None:
                raise PolicyViolation("Execution run activate current-state gate reddi")

    def record_usage(
        self,
        run_id: UUID,
        *,
        input_tokens_used: int,
        output_tokens_used: int,
        cost_micros_used: int,
    ) -> None:
        if min(input_tokens_used, output_tokens_used, cost_micros_used) < 0:
            raise PolicyViolation("Execution run usage negatif olamaz")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "update runtime.execution_run set input_tokens_used=%s,output_tokens_used=%s,"
                "cost_micros_used=%s where realm_id=%s and id=%s and state='active' returning id",
                (
                    input_tokens_used,
                    output_tokens_used,
                    cost_micros_used,
                    self.realm_id,
                    run_id,
                ),
            )
            if cursor.fetchone() is None:
                raise PolicyViolation("Execution run usage current-state gate reddi")

    def finish_run(self, run_id: UUID, *, state: str, terminal_at: dt.datetime) -> None:
        if state not in {
            "completed",
            "failed",
            "cancelled",
            "reconciliation-required",
        }:
            raise PolicyViolation("Execution run terminal state gecersiz")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "update runtime.execution_run set state=%s,terminal_at=%s"
                " where realm_id=%s and id=%s and state='active' returning id",
                (state, terminal_at, self.realm_id, run_id),
            )
            if cursor.fetchone() is None:
                raise PolicyViolation("Execution run terminal current-state gate reddi")

    def _insert(
        self,
        table: str,
        columns: str,
        values: tuple[Any, ...],
        digest_column: str,
        digest_value: str,
    ) -> tuple[UUID, bool]:
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                f"insert into {table} {columns} values ("
                + ",".join(["%s"] * len(values))
                + ") on conflict do nothing returning id",
                values,
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                f"select id from {table} where realm_id=%s and {digest_column}=%s",
                (self.realm_id, digest_value),
            )
            existing = cursor.fetchone()
            if existing is None:
                raise PolicyViolation("Execution identity/digest replay uyusmuyor")
            return UUID(str(existing[0])), False

    def _realm(self, realm_id: UUID) -> None:
        if realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm execution kaydi reddedildi")
