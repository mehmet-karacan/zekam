"""PostgreSQL persistence for execution run, packet and envelope."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json
from zekam.domain.errors import PolicyViolation
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
            "checkpoint_id,checkpoint_digest,checkpoint_disposition,source_revision,policy_digest,"
            "authorization_scope_digest,output_schema_digest,payload_digest,max_input_tokens,"
            "max_output_tokens,max_cost_micros,deadline,envelope_digest,grants_authority,created_at)",
            values,
            "envelope_digest",
            item.envelope_digest,
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
