"""Read-only repeatable PostgreSQL projection for resume preparation."""

from __future__ import annotations

import datetime as dt
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.checkpoint_v2 import (
    OpenEffect,
    OpenEffectState,
    Resumability,
    SandboxBindingV2,
    SandboxDisposition,
    StaleDigestBindings,
)
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed
from zekam.domain.resume import ResumeObservation, RuntimeObservation


def _one(cursor: Any, label: str) -> tuple[Any, ...]:
    row = cursor.fetchone()
    if row is None:
        raise NotFound(f"Resume {label} bulunamadi")
    return cast(tuple[Any, ...], row)


@dataclass(frozen=True, slots=True)
class ResumeRepository:
    connection: Any
    realm_id: UUID
    manage_transaction: bool = True

    def read_snapshot(
        self,
        work_item_id: object,
        *,
        client_id: str,
        observed_at: dt.datetime | None = None,
    ) -> ResumeObservation:
        try:
            work_id = UUID(str(work_item_id))
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("Resume work item UUID olmali") from exc
        if not client_id.strip():
            raise ValidationFailed("Resume target client kimligi ister")
        if observed_at is not None and observed_at.tzinfo is None:
            raise ValidationFailed("Resume observed_at timezone-aware olmali")

        transaction = self.connection.transaction() if self.manage_transaction else nullcontext()
        with transaction, self.connection.cursor() as cursor:
            if self.manage_transaction:
                cursor.execute("set transaction isolation level repeatable read, read only")
            cursor.execute("select transaction_timestamp()")
            transaction_time = _one(cursor, "transaction zamani")[0]
            moment = observed_at or transaction_time

            cursor.execute(
                "select project_id,state from work.work_item where realm_id=%s and id=%s",
                (self.realm_id, work_id),
            )
            project_id, work_state = _one(cursor, "work item")

            cursor.execute(
                "select id,plan_digest from work.task_plan"
                " where realm_id=%s and work_item_id=%s order by revision desc,id asc limit 1",
                (self.realm_id, work_id),
            )
            current_plan_id, current_plan_digest = _one(cursor, "current task plan")

            cursor.execute(
                "select c.id,c.checkpoint_key,c.revision,c.checkpoint_digest,c.task_plan_id,"
                " c.plan_digest,c.routing_context_snapshot_id,c.source_revision,c.policy_digest,"
                " c.capability_profile_digest,c.dependency_snapshot_digest,c.migration_head_digest,"
                " c.route_decision_digest,c.context_manifest_digest,c.context_packet_digest,"
                " c.architecture_digest,c.rules_digest,c.test_suite_digest,"
                " c.model_inventory_digest,"
                " c.journal_head_digest,c.pending_steps,c.next_safe_action,c.resumability,"
                " c.logical_read_resources,c.logical_write_resources,c.job_id,c.attempt_id,"
                " work.validate_checkpoint_v2(c.realm_id,c.id),rd.role,c.run_id,c.assignment_id,"
                " c.execution_envelope_id,c.execution_envelope_digest,c.observed_lease_id,"
                " c.observed_fencing_token,j.state,l.expires_at,r.deadline,et.expires_at,"
                " c.sandbox_disposition,c.sandbox_id,c.base_revision,c.patch_digest,"
                " c.dirty_state_digest"
                " from work.checkpoint_v2 c"
                " join models.model_route_decision rd on rd.realm_id=c.realm_id"
                " and rd.id=c.route_decision_id"
                " join models.execution_target_snapshot et on et.realm_id=rd.realm_id"
                " and et.id=rd.execution_target_id"
                " join runtime.job j on j.realm_id=c.realm_id and j.id=c.job_id"
                " join runtime.lease l on l.realm_id=c.realm_id and l.id=c.observed_lease_id"
                " join runtime.execution_run r on r.realm_id=c.realm_id and r.id=c.run_id"
                " where c.realm_id=%s and c.work_item_id=%s"
                " and not exists(select 1 from work.checkpoint_v2 newer"
                "   where newer.realm_id=c.realm_id and newer.checkpoint_key=c.checkpoint_key"
                "   and newer.revision>c.revision)"
                " order by (c.task_plan_id=%s) desc,c.created_at desc,c.checkpoint_key,c.id",
                (self.realm_id, work_id, current_plan_id),
            )
            heads = cursor.fetchall()
            if not heads:
                cursor.execute(
                    "select id from work.checkpoint where realm_id=%s and work_item_id=%s limit 1",
                    (self.realm_id, work_id),
                )
                if cursor.fetchone() is not None:
                    raise PolicyViolation(
                        "Legacy checkpoint yalniz legacy-limited manual review ister"
                    )
                raise NotFound("Resume checkpoint v2 bulunamadi")
            row = heads[0]
            checkpoint_integrity = bool(row[27]) and len(heads) == 1
            next_action = row[21]
            next_step = None if next_action is None else str(next_action["step_id"])

            # A checkpoint describes the last structural observation.  Dispatch must
            # bind the exact runnable job for the pending next step, not blindly reuse
            # the checkpoint step's job/assignment identity.
            target_runtime: tuple[Any, ...] | None = None
            if next_step is not None:
                cursor.execute(
                    "select j.id,j.assignment_id,j.run_id,j.state,l.attempt_id,l.id,"
                    " l.fencing_token,l.expires_at,coalesce(e.id,%s),"
                    " coalesce(e.envelope_digest,%s),r.deadline"
                    " from runtime.job j"
                    " join runtime.execution_run r on r.realm_id=j.realm_id and r.id=j.run_id"
                    " left join runtime.lease l on l.realm_id=j.realm_id and l.job_id=j.id"
                    " left join lateral (select x.id,x.envelope_digest"
                    "   from runtime.execution_envelope x where x.realm_id=j.realm_id"
                    "   and x.job_id=j.id and x.attempt_id=l.attempt_id"
                    "   order by x.request_ordinal desc,x.id limit 1) e on true"
                    " where j.realm_id=%s and j.work_item_id=%s and j.plan_id=%s"
                    " and j.step_id=%s and j.state in ('ready','running','recovery-required')"
                    " order by j.created_at desc,j.id limit 2",
                    (
                        row[31],
                        row[32],
                        self.realm_id,
                        work_id,
                        current_plan_id,
                        next_step,
                    ),
                )
                candidates = cursor.fetchall()
                if len(candidates) == 1:
                    target_runtime = cast(tuple[Any, ...], candidates[0])
                elif len(candidates) > 1:
                    checkpoint_integrity = False
                else:
                    # A pending step without one exact runnable target must never
                    # fall back to the completed checkpoint job identity.
                    checkpoint_integrity = False

            cursor.execute(
                "select id,source_revision,policy_digest,capability_profile_digest,"
                " dependency_digest,architecture_digest,rules_digest,suite_digest,"
                " inventory_digest from projects.routing_context_snapshot"
                " where realm_id=%s and project_id=%s"
                " order by captured_at desc,id asc limit 1",
                (self.realm_id, project_id),
            )
            context = _one(cursor, "current routing context")
            cursor.execute(
                "select models.capability_runtime_jsonb_digest(to_jsonb(checksum))"
                " from core.schema_migrations order by version desc limit 1"
            )
            migration_digest = _one(cursor, "migration head")[0]
            cursor.execute(
                "select entry_digest from work.work_journal_entry"
                " where realm_id=%s and work_item_id=%s order by sequence desc limit 1",
                (self.realm_id, work_id),
            )
            journal_digest = _one(cursor, "journal head")[0]
            cursor.execute(
                "select evidence_digest from models.model_route_decision"
                " where realm_id=%s and project_id=%s and role=%s"
                " order by decided_at desc,id asc limit 1",
                (self.realm_id, project_id, row[28]),
            )
            latest_route = cursor.fetchone()
            route_digest = row[12] if latest_route is None else latest_route[0]
            cursor.execute(
                "select manifest_digest,packet_digest from work.context_packet"
                " where realm_id=%s and work_item_id=%s order by created_at desc,id asc limit 1",
                (self.realm_id, work_id),
            )
            latest_packet = cursor.fetchone()
            manifest_digest = row[13] if latest_packet is None else latest_packet[0]
            packet_digest = row[14] if latest_packet is None else latest_packet[1]

            effect_job_id = row[25] if target_runtime is None else target_runtime[0]
            effect_attempt_id = row[26] if target_runtime is None else target_runtime[4]
            cursor.execute(
                "select cl.id,cl.effect_digest,er.status from runtime.effect_claim cl"
                " left join runtime.effect_receipt er on er.realm_id=cl.realm_id"
                " and er.claim_id=cl.id where cl.realm_id=%s and cl.job_id=%s"
                " and cl.attempt_id=%s and (er.id is null or er.status='failed') order by cl.id",
                (self.realm_id, effect_job_id, effect_attempt_id),
            )
            open_effects = tuple(
                OpenEffect(
                    UUID(str(item[0])),
                    str(item[1]),
                    (
                        OpenEffectState.STARTED_NO_TERMINAL_RECEIPT
                        if item[2] is None
                        else OpenEffectState.FAILED_RECONCILIATION
                    ),
                )
                for item in cursor.fetchall()
            )

            checkpoint_bindings = StaleDigestBindings(
                routing_context_snapshot_id=UUID(str(row[6])),
                source_revision=str(row[7]),
                policy_digest=str(row[8]),
                capability_profile_digest=str(row[9]),
                dependency_snapshot_digest=str(row[10]),
                migration_head_digest=str(row[11]),
                model_route_decision_digest=str(row[12]),
                context_manifest_digest=str(row[13]),
                context_packet_digest=str(row[14]),
                architecture_digest=str(row[15]),
                rules_digest=str(row[16]),
                test_suite_digest=str(row[17]),
                model_inventory_digest=str(row[18]),
                journal_head_digest=str(row[19]),
            )
            current_bindings = StaleDigestBindings(
                routing_context_snapshot_id=UUID(str(context[0])),
                source_revision=str(context[1]),
                policy_digest=str(context[2]),
                capability_profile_digest=str(context[3]),
                dependency_snapshot_digest=str(context[4]),
                migration_head_digest=str(migration_digest),
                model_route_decision_digest=str(route_digest),
                context_manifest_digest=str(manifest_digest),
                context_packet_digest=str(packet_digest),
                architecture_digest=str(context[5]),
                rules_digest=str(context[6]),
                test_suite_digest=str(context[7]),
                model_inventory_digest=str(context[8]),
                journal_head_digest=str(journal_digest),
            )
            runtime_row = (
                (
                    row[25],
                    row[30],
                    row[29],
                    row[35],
                    row[26],
                    row[33],
                    row[34],
                    row[36],
                    row[31],
                    row[32],
                    row[37],
                )
                if target_runtime is None
                else target_runtime
            )
            valid_until = min(runtime_row[10], row[38])
            return ResumeObservation(
                realm_id=self.realm_id,
                project_id=UUID(str(project_id)),
                work_item_id=work_id,
                work_state=str(work_state),
                checkpoint_id=UUID(str(row[0])),
                checkpoint_digest=str(row[3]),
                checkpoint_revision=int(row[2]),
                checkpoint_key=str(row[1]),
                plan_id=UUID(str(row[4])),
                plan_digest=str(row[5]),
                current_plan_id=UUID(str(current_plan_id)),
                current_plan_digest=str(current_plan_digest),
                checkpoint_bindings=checkpoint_bindings,
                current_bindings=current_bindings,
                pending_steps=tuple(str(value) for value in row[20]),
                next_step_id=next_step,
                open_effects=open_effects,
                checkpoint_integrity=checkpoint_integrity,
                resumability=Resumability(str(row[22])),
                logical_read_resources=tuple(str(value) for value in row[23]),
                logical_write_resources=tuple(str(value) for value in row[24]),
                runtime=RuntimeObservation(
                    run_id=UUID(str(runtime_row[2])),
                    job_id=UUID(str(runtime_row[0])),
                    attempt_id=(None if runtime_row[4] is None else UUID(str(runtime_row[4]))),
                    assignment_id=UUID(str(runtime_row[1])),
                    execution_envelope_id=UUID(str(runtime_row[8])),
                    execution_envelope_digest=str(runtime_row[9]),
                    observed_lease_id=(
                        None if runtime_row[5] is None else UUID(str(runtime_row[5]))
                    ),
                    observed_fencing_token=(
                        None if runtime_row[6] is None else int(runtime_row[6])
                    ),
                    job_state=str(runtime_row[3]),
                    lease_expires_at=runtime_row[7],
                    deadline=runtime_row[10],
                ),
                target_client_id=client_id.strip().lower(),
                required_route_role=str(row[28]),
                context_recipe=f"resume:{client_id.strip().lower()}:{row[28]}",
                observed_at=moment,
                valid_until=valid_until,
                sandbox=SandboxBindingV2(
                    SandboxDisposition(str(row[39])), row[40], row[41], row[42], row[43]
                ),
            )

    def snapshot_digest(self, observation: ResumeObservation) -> str:
        """Sanitized helper for tests/diagnostics; never grants authority."""
        return digest(
            {
                "realm_id": str(observation.realm_id),
                "work_item_id": str(observation.work_item_id),
                "checkpoint_digest": observation.checkpoint_digest,
                "current_plan_digest": observation.current_plan_digest,
                "current_bindings": observation.current_bindings.body(),
                "sandbox": observation.sandbox.body(),
                "observed_at": observation.observed_at,
            }
        )
