"""Bounded LoopPolicy canonical PostgreSQL adapter'i."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.identifiers import new_uuid7
from zekam.domain.loop_policy import (
    LoopAdmission,
    LoopAttemptRequest,
    LoopPolicy,
    LoopTerminalState,
    LoopValidation,
)


@dataclass(frozen=True, slots=True)
class PostgresLoopPolicyRepository:
    connection: Any
    realm_id: UUID

    def store_policy(self, policy: LoopPolicy) -> tuple[UUID, bool]:
        if policy.realm_id != self.realm_id:
            raise ValueError("Loop policy repository realm binding uyusmuyor")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select loop_id,inserted from runtime.create_loop_policy("
                " %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    policy.id,
                    policy.assignment_id,
                    policy.context_manifest_id,
                    policy.validator_assignment_id,
                    policy.max_attempts,
                    policy.max_tokens,
                    policy.max_cost_micros,
                    policy.deadline,
                    policy.validator_spec_digest,
                    [str(item) for item in policy.required_delta],
                    [str(item) for item in policy.forbidden_effects],
                    policy.policy_revision_digest,
                ),
            )
            row = cursor.fetchone()
            return UUID(str(row[0])), bool(row[1])

    def register_delta_evidence(self, loop_id: UUID, kind: str, source_id: UUID) -> UUID:
        evidence_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select runtime.register_loop_delta_evidence(%s,%s,%s,%s)",
                (evidence_id, loop_id, kind, source_id),
            )
            cursor.fetchone()
        return evidence_id

    def bind_dispatch(self, attempt_id: UUID, surface: str, dispatch_id: UUID) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select runtime.bind_loop_dispatch(%s,%s,%s)",
                (attempt_id, surface, dispatch_id),
            )

    def admit(
        self,
        request: LoopAttemptRequest,
        *,
        attempt_id: UUID | None = None,
    ) -> LoopAdmission:
        resolved_attempt_id = attempt_id or new_uuid7()
        legacy_semantic_digest = digest(
            {
                "prompt_digest": request.prompt_digest,
                "context_digest": request.context_digest,
                "action_digest": request.action_digest,
            }
        )
        legacy_binding_digest = digest(
            {
                "source_revision": request.source_revision,
                "plan_digest": request.plan_digest,
                "policy_revision_digest": request.policy_revision_digest,
                "validator_spec_digest": request.validator_spec_digest,
                "predecessor_attempt_id": (
                    None
                    if request.predecessor_attempt_id is None
                    else str(request.predecessor_attempt_id)
                ),
            }
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select admitted,attempt_id,ordinal,terminal_state,reason"
                " from runtime.admit_loop_attempt_current_v3("
                " %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                " %s,%s,%s,%s,%s,%s,%s::jsonb)",
                (
                    resolved_attempt_id,
                    request.loop_id,
                    request.predecessor_attempt_id,
                    legacy_semantic_digest,
                    request.prompt_digest,
                    request.context_digest,
                    request.action_digest,
                    legacy_binding_digest,
                    request.source_revision,
                    request.plan_digest,
                    request.policy_revision_digest,
                    request.validator_spec_digest,
                    request.reserved_input_tokens,
                    request.reserved_output_tokens,
                    request.reserved_cost_micros,
                    list(request.delta_evidence_ids),
                    request.delta_digest,
                    request.attempt_ordinal,
                    request.objective_digest,
                    request.validator_asset_manifest_digest,
                    request.progress_packet_digest,
                    request.metric_vector_digest,
                    request.novelty_digest,
                    canonical_json(request.novelty.semantic_body())
                    if request.novelty is not None
                    else None,
                ),
            )
            row = cursor.fetchone()
        admitted = bool(row[0])
        terminal = None if row[3] is None else LoopTerminalState(str(row[3]))
        decision_body = {
            "loop_id": str(request.loop_id),
            "attempt_id": None if row[1] is None else str(row[1]),
            "ordinal": row[2],
            "admitted": admitted,
            "terminal_state": None if terminal is None else str(terminal),
            "reason": str(row[4]),
            "semantic_request_digest": request.semantic_request_digest,
            "binding_digest": request.binding_digest,
            "delta_digest": request.delta_digest,
        }
        return LoopAdmission(
            admitted=admitted,
            loop_id=request.loop_id,
            attempt_id=None if row[1] is None else UUID(str(row[1])),
            ordinal=None if row[2] is None else int(row[2]),
            terminal_state=terminal,
            reason=str(row[4]),
            decision_digest=digest(decision_body),
        )

    def complete(self, attempt_id: UUID, validation: LoopValidation) -> str:
        outcome_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select runtime.complete_loop_attempt_current("
                " %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    outcome_id,
                    attempt_id,
                    str(validation.outcome),
                    validation.validator_spec_digest,
                    validation.result_invocation_id,
                    validation.verifier_invocation_id,
                    validation.effect_receipt_id,
                    validation.actual_input_tokens,
                    validation.actual_output_tokens,
                    validation.actual_cost_micros,
                    validation.progress_packet_digest,
                    validation.metric_vector_digest,
                    validation.progress_decision_digest,
                    list(validation.metric_evidence_refs),
                    None if validation.progress_state is None else str(validation.progress_state),
                ),
            )
            return str(cursor.fetchone()[0])

    def terminal_state(self, loop_id: UUID) -> LoopTerminalState | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select state from runtime.loop_terminal where realm_id=%s and loop_id=%s",
                (self.realm_id, loop_id),
            )
            row = cursor.fetchone()
        return None if row is None else LoopTerminalState(str(row[0]))

    def interrupt(self, attempt_id: UUID, failure_digest: str) -> LoopTerminalState:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select runtime.interrupt_loop_attempt(%s,%s)",
                (attempt_id, failure_digest),
            )
            return LoopTerminalState(str(cursor.fetchone()[0]))

    def current_database_time(self) -> dt.datetime:
        with self.connection.cursor() as cursor:
            cursor.execute("select clock_timestamp()")
            return cast(dt.datetime, cursor.fetchone()[0])
