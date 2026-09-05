"""PostgreSQL persistence for the append-only checkpoint v2 manifest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json
from zekam.domain.checkpoint_v2 import CheckpointV2
from zekam.domain.errors import PolicyViolation


@dataclass(frozen=True, slots=True)
class CheckpointV2Repository:
    connection: Any
    realm_id: UUID

    def store(self, item: CheckpointV2) -> tuple[UUID, bool]:
        """Atomically store a header and its normalized evidence graph."""
        if item.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm checkpoint v2 reddedildi")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select id from work.checkpoint_v2 where realm_id=%s and checkpoint_digest=%s",
                (self.realm_id, item.checkpoint_digest),
            )
            replay = cursor.fetchone()
            if replay is not None:
                return UUID(str(replay[0])), False
            cursor.execute(
                "insert into work.checkpoint_v2"
                " (id,realm_id,checkpoint_key,revision,previous_checkpoint_id,"
                "previous_checkpoint_digest,project_id,work_item_id,task_plan_id,intent_digest,"
                "plan_digest,step_id,run_id,job_id,attempt_id,assignment_id,"
                "execution_envelope_id,execution_envelope_digest,route_decision_id,"
                "route_decision_digest,context_manifest_id,context_manifest_digest,"
                "context_packet_id,context_packet_digest,source_revision,policy_digest,"
                "routing_context_snapshot_id,"
                "capability_profile_digest,dependency_snapshot_digest,migration_head_digest,"
                "architecture_digest,rules_digest,test_suite_digest,model_inventory_digest,"
                "journal_head_digest,observed_lease_id,observed_fencing_token,plan_steps,"
                "completed_steps,pending_steps,"
                "logical_read_resources,logical_write_resources,sandbox_disposition,sandbox_id,"
                "base_revision,patch_digest,dirty_state_digest,test_and_eval_refs,tokens_used,"
                "cost_micros_used,attempts_used,deadline,rollback_recovery,next_safe_action,"
                "resumability,grants_authority,carries_active_lease,approval_inherited,created_at,"
                "checkpoint_digest) values ("
                + ",".join(["%s"] * 60)
                + ") on conflict do nothing returning id",
                self._header_values(item),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "select id from work.checkpoint_v2 where realm_id=%s and checkpoint_digest=%s",
                    (self.realm_id, item.checkpoint_digest),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise PolicyViolation("Checkpoint v2 identity/digest replay uyusmuyor")
                return UUID(str(existing[0])), False

            for result in item.step_results:
                cursor.execute(
                    "insert into work.checkpoint_v2_step_result"
                    " (realm_id,checkpoint_id,step_id,effect_kind,result_digest,job_id,attempt_id,"
                    "assignment_id,execution_envelope_id,execution_envelope_digest)"
                    " values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        self.realm_id,
                        item.checkpoint_id,
                        result.step_id,
                        result.effect_kind.value,
                        result.result_digest,
                        result.job_id,
                        result.attempt_id,
                        result.assignment_id,
                        result.execution_envelope_id,
                        result.execution_envelope_digest,
                    ),
                )
                for receipt_id in result.receipt_refs:
                    cursor.execute(
                        "select claim_id from runtime.effect_receipt where realm_id=%s and id=%s",
                        (self.realm_id, receipt_id),
                    )
                    receipt = cursor.fetchone()
                    if receipt is None:
                        raise PolicyViolation("Checkpoint terminal receipt bulunamadi")
                    cursor.execute(
                        "insert into work.checkpoint_v2_step_receipt"
                        " (realm_id,checkpoint_id,step_id,claim_id,receipt_id)"
                        " values(%s,%s,%s,%s,%s)",
                        (
                            self.realm_id,
                            item.checkpoint_id,
                            result.step_id,
                            receipt[0],
                            receipt_id,
                        ),
                    )
                for invocation_id in result.verification_refs:
                    cursor.execute(
                        "select i.assignment_id,r.envelope_digest from agents.invocation i"
                        " join agents.result_receipt r on r.realm_id=i.realm_id"
                        " and r.invocation_id=i.id where i.realm_id=%s and i.id=%s",
                        (self.realm_id, invocation_id),
                    )
                    verification = cursor.fetchone()
                    if verification is None:
                        raise PolicyViolation("Checkpoint verifier sonucu bulunamadi")
                    cursor.execute(
                        "insert into work.checkpoint_v2_step_verification"
                        " (realm_id,checkpoint_id,step_id,verifier_assignment_id,"
                        "verifier_invocation_id,envelope_digest) values(%s,%s,%s,%s,%s,%s)",
                        (
                            self.realm_id,
                            item.checkpoint_id,
                            result.step_id,
                            verification[0],
                            invocation_id,
                            verification[1],
                        ),
                    )
            for effect in item.open_effects:
                cursor.execute(
                    "insert into work.checkpoint_v2_open_effect"
                    " (realm_id,checkpoint_id,claim_id,state,effect_digest)"
                    " values(%s,%s,%s,%s,%s)",
                    (
                        self.realm_id,
                        item.checkpoint_id,
                        effect.claim_id,
                        effect.state.value,
                        effect.effect_digest,
                    ),
                )
            return UUID(str(row[0])), True

    def latest(self, checkpoint_key: str) -> tuple[UUID, int, str] | None:
        """Return the latest immutable revision identity without inventing resume authority."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,revision,checkpoint_digest from work.checkpoint_v2"
                " where realm_id=%s and checkpoint_key=%s order by revision desc limit 1",
                (self.realm_id, checkpoint_key),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return UUID(str(row[0])), int(row[1]), str(row[2])

    def is_complete(self, checkpoint_id: UUID) -> bool:
        """Evaluate the database receipt/verifier/open-effect completeness gate."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select work.validate_checkpoint_v2(%s,%s)",
                (self.realm_id, checkpoint_id),
            )
            return bool(cursor.fetchone()[0])

    @classmethod
    def _header_values(cls, item: CheckpointV2) -> tuple[Any, ...]:
        bindings = item.bindings
        sandbox = item.sandbox
        return (
            item.checkpoint_id,
            item.realm_id,
            item.checkpoint_key,
            item.revision,
            item.previous_checkpoint_id,
            item.previous_checkpoint_digest,
            item.project_id,
            item.work_item_id,
            item.plan_id,
            item.intent_digest,
            item.plan_digest,
            item.step_id,
            item.run_id,
            item.job_id,
            item.attempt_id,
            item.assignment_id,
            item.execution_envelope_id,
            item.execution_envelope_digest,
            item.route_decision_id,
            bindings.model_route_decision_digest,
            item.context_manifest_id,
            bindings.context_manifest_digest,
            item.context_packet_id,
            bindings.context_packet_digest,
            bindings.source_revision,
            bindings.policy_digest,
            bindings.routing_context_snapshot_id,
            bindings.capability_profile_digest,
            bindings.dependency_snapshot_digest,
            bindings.migration_head_digest,
            bindings.architecture_digest,
            bindings.rules_digest,
            bindings.test_suite_digest,
            bindings.model_inventory_digest,
            bindings.journal_head_digest,
            item.observed_lease_id,
            item.observed_fencing_token,
            list(item.plan_steps),
            list(item.completed_steps),
            list(item.pending_steps),
            list(item.logical_read_resources),
            list(item.logical_write_resources),
            sandbox.disposition.value,
            sandbox.sandbox_id,
            sandbox.base_revision,
            sandbox.patch_digest,
            sandbox.dirty_state_digest,
            canonical_json(list(item.test_and_eval_digests)),
            item.tokens_used,
            item.cost_micros_used,
            item.attempts_used,
            item.deadline,
            canonical_json([value.body() for value in item.rollback_or_recovery]),
            (
                None
                if item.next_safe_action is None
                else canonical_json(item.next_safe_action.body())
            ),
            item.resumability.value,
            False,
            False,
            False,
            item.created_at,
            item.checkpoint_digest,
        )
