"""PostgreSQL adapter for the append-only resume apply saga."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json
from zekam.domain.checkpoint_v2 import (
    CheckpointV2,
    NextSafeActionV2,
    RecoveryDirectiveV2,
    Resumability,
    SandboxBindingV2,
    SandboxDisposition,
    StaleDigestBindings,
    StepResultV2,
)
from zekam.domain.errors import NotFound, PolicyViolation
from zekam.domain.execution_run import CheckpointDisposition, ExecutionEnvelope
from zekam.domain.identifiers import new_uuid7
from zekam.domain.resume import ResumePlan
from zekam.domain.resume_apply import ResumeApplyEvent, ResumeApplyPhase, ResumeApplyState
from zekam.domain.work import EffectKind
from zekam.infrastructure.postgres.checkpoint_v2_repository import CheckpointV2Repository
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository


@dataclass(frozen=True, slots=True)
class ResumeApplyRepository:
    connection: Any
    realm_id: UUID

    def lock_work(self, work_item_id: UUID) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"resume:{self.realm_id}:{work_item_id}",),
            )

    def find_exact(
        self,
        plan: ResumePlan,
        *,
        actor_id: UUID,
        authorization_id: UUID,
        effect_digest: str,
    ) -> UUID | None:
        """Return only an exact replay header; reject same digest with scope drift."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,work_item_id,checkpoint_id,run_id,job_id,actor_id,authorization_id,"
                " target_client_id,effect_digest from runtime.resume_apply"
                " where realm_id=%s and resume_plan_digest=%s",
                (self.realm_id, plan.plan_digest),
            )
            existing = cursor.fetchone()
        if existing is None:
            return None
        expected = (
            plan.work_item_id,
            plan.checkpoint_id,
            plan.runtime.run_id,
            plan.runtime.job_id,
            actor_id,
            authorization_id,
            plan.target_client_id,
            effect_digest,
        )
        if tuple(existing[1:]) != expected:
            raise PolicyViolation("Resume apply replay scope drift")
        return UUID(str(existing[0]))

    def create(
        self,
        plan: ResumePlan,
        *,
        actor_id: UUID,
        authorization_id: UUID,
        effect_digest: str,
        now: dt.datetime,
    ) -> tuple[UUID, bool]:
        apply_id = new_uuid7(now=now)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into runtime.resume_apply"
                " (id,realm_id,work_item_id,checkpoint_id,run_id,job_id,actor_id,"
                " authorization_id,target_client_id,resume_plan_digest,effect_digest,created_at,"
                " grants_authority) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,false)"
                " on conflict(realm_id,resume_plan_digest) do nothing returning id",
                (
                    apply_id,
                    self.realm_id,
                    plan.work_item_id,
                    plan.checkpoint_id,
                    plan.runtime.run_id,
                    plan.runtime.job_id,
                    actor_id,
                    authorization_id,
                    plan.target_client_id,
                    plan.plan_digest,
                    effect_digest,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id,work_item_id,checkpoint_id,run_id,job_id,actor_id,authorization_id,"
                " target_client_id,effect_digest from runtime.resume_apply"
                " where realm_id=%s and resume_plan_digest=%s",
                (self.realm_id, plan.plan_digest),
            )
            existing = cursor.fetchone()
            if existing is None:
                raise NotFound("Resume apply replay kaydi bulunamadi")
            expected = (
                plan.work_item_id,
                plan.checkpoint_id,
                plan.runtime.run_id,
                plan.runtime.job_id,
                actor_id,
                authorization_id,
                plan.target_client_id,
                effect_digest,
            )
            if tuple(existing[1:]) != expected:
                raise PolicyViolation("Resume apply replay scope drift")
            return UUID(str(existing[0])), False

    def append_event(self, event: ResumeApplyEvent) -> tuple[UUID, bool]:
        event_id = new_uuid7(now=event.occurred_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into runtime.resume_apply_event"
                " (id,realm_id,resume_apply_id,sequence,phase,state,reason_code,attempt_id,"
                " lease_id,fencing_token,claim_id,receipt_id,result_digest,previous_digest,"
                " event_digest,event_body,occurred_at) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                " %s,%s,%s,%s,%s,%s::jsonb,%s)"
                " on conflict(realm_id,resume_apply_id,event_digest) do nothing"
                " returning id",
                (
                    event_id,
                    self.realm_id,
                    event.apply_id,
                    event.sequence,
                    event.phase.value,
                    event.state.value,
                    event.reason_code,
                    event.attempt_id,
                    event.lease_id,
                    event.fencing_token,
                    event.claim_id,
                    event.receipt_id,
                    event.result_digest,
                    event.previous_digest,
                    event.event_digest,
                    canonical_json(event.body()),
                    event.occurred_at,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from runtime.resume_apply_event"
                " where realm_id=%s and resume_apply_id=%s and event_digest=%s",
                (self.realm_id, event.apply_id, event.event_digest),
            )
            replay = cursor.fetchone()
            if replay is None:
                raise PolicyViolation("Resume apply event replay drift")
            return UUID(str(replay[0])), False

    def latest_event(self, apply_id: UUID) -> ResumeApplyEvent | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select sequence,phase,state,reason_code,occurred_at,attempt_id,lease_id,"
                " fencing_token,claim_id,receipt_id,result_digest,previous_digest"
                " from runtime.resume_apply_event"
                " where realm_id=%s and resume_apply_id=%s order by sequence desc limit 1",
                (self.realm_id, apply_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return ResumeApplyEvent(
            apply_id=apply_id,
            sequence=int(row[0]),
            phase=ResumeApplyPhase(str(row[1])),
            state=ResumeApplyState(str(row[2])),
            reason_code=str(row[3]),
            occurred_at=row[4],
            attempt_id=None if row[5] is None else UUID(str(row[5])),
            lease_id=None if row[6] is None else UUID(str(row[6])),
            fencing_token=None if row[7] is None else int(row[7]),
            claim_id=None if row[8] is None else UUID(str(row[8])),
            receipt_id=None if row[9] is None else UUID(str(row[9])),
            result_digest=None if row[10] is None else str(row[10]),
            previous_digest=None if row[11] is None else str(row[11]),
        )

    def lease_is_live(self, event: ResumeApplyEvent, *, now: dt.datetime) -> bool:
        if event.lease_id is None or event.attempt_id is None or event.fencing_token is None:
            return False
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select exists(select 1 from runtime.lease l"
                " join runtime.resume_apply a on a.realm_id=l.realm_id and a.job_id=l.job_id"
                " where a.realm_id=%s and a.id=%s and l.id=%s and l.attempt_id=%s"
                " and l.fencing_token=%s and l.expires_at>%s)",
                (
                    self.realm_id,
                    event.apply_id,
                    event.lease_id,
                    event.attempt_id,
                    event.fencing_token,
                    now,
                ),
            )
            return bool(cursor.fetchone()[0])

    def recover_interrupted(self, event: ResumeApplyEvent, *, now: dt.datetime) -> ResumeApplyEvent:
        """Terminalize an expired nonterminal replay without repeating its effect."""
        if event.state not in {ResumeApplyState.CLAIMED, ResumeApplyState.DISPATCHED}:
            raise PolicyViolation("Resume apply yalniz nonterminal replay recover eder")
        recovery = ResumeApplyEvent(
            apply_id=event.apply_id,
            sequence=event.sequence + 1,
            phase=ResumeApplyPhase.DISPATCH,
            state=ResumeApplyState.RECOVERY_REQUIRED,
            reason_code="resume.nonterminal-replay-lease-expired",
            occurred_at=now,
            attempt_id=event.attempt_id,
            lease_id=event.lease_id,
            fencing_token=event.fencing_token,
            claim_id=event.claim_id,
            previous_digest=event.event_digest,
        )
        self.append_event(recovery)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select a.job_id,l.attempt_id from runtime.resume_apply a"
                " join runtime.lease l on l.realm_id=a.realm_id and l.job_id=a.job_id"
                " where a.realm_id=%s and a.id=%s and l.id=%s and l.attempt_id=%s"
                " and l.fencing_token=%s for update of l",
                (
                    self.realm_id,
                    event.apply_id,
                    event.lease_id,
                    event.attempt_id,
                    event.fencing_token,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise PolicyViolation("Resume apply recovery lease identity kayip")
            job_id, attempt_id = row
            cursor.execute(
                "update runtime.job set state='recovery-required'"
                " where realm_id=%s and id=%s and state='running' and fencing_token=%s",
                (self.realm_id, job_id, event.fencing_token),
            )
            if cursor.rowcount != 1:
                raise PolicyViolation("Resume apply recovery job fence drift")
            cursor.execute(
                "update runtime.job_attempt set outcome='recovery-required',"
                " failure_category='adapter',finished_at=%s"
                " where realm_id=%s and id=%s and outcome is null",
                (now, self.realm_id, attempt_id),
            )
            if cursor.rowcount != 1:
                raise PolicyViolation("Resume apply recovery attempt drift")
            cursor.execute(
                "delete from runtime.resource_lock where realm_id=%s and job_id=%s",
                (self.realm_id, job_id),
            )
            cursor.execute(
                "delete from runtime.lease where realm_id=%s and id=%s",
                (self.realm_id, event.lease_id),
            )
            cursor.execute(
                "insert into runtime.execution_event"
                "(id,realm_id,job_id,attempt_id,event_type,payload,occurred_at)"
                " values(%s,%s,%s,%s,'job.recovery-required',%s::jsonb,%s)",
                (
                    new_uuid7(now=now),
                    self.realm_id,
                    job_id,
                    attempt_id,
                    canonical_json({"outcome": "recovery-required"}),
                    now,
                ),
            )
        return recovery

    def clone_envelope(
        self,
        plan: ResumePlan,
        *,
        apply_id: UUID,
        attempt_id: UUID,
        lease_id: UUID,
        fencing_token: int,
        authorization_scope_digest: str,
        payload_digest: str,
        now: dt.datetime,
    ) -> ExecutionEnvelope:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select role,route_decision_id,route_decision_digest,route_expires_at,model_id,"
                " provider_binding_id,provider_binding_digest,provider_ref,context_manifest_id,"
                " context_manifest_digest,context_packet_id,context_packet_digest,"
                " source_revision,policy_digest,output_schema_digest,max_input_tokens,"
                " max_output_tokens,max_cost_micros,checkpoint_id,checkpoint_digest,deadline"
                " from runtime.execution_envelope"
                " where realm_id=%s and id=%s",
                (self.realm_id, plan.runtime.execution_envelope_id),
            )
            old = cursor.fetchone()
            if old is None:
                raise NotFound("Resume source execution envelope bulunamadi")
            cursor.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"resume-envelope:{self.realm_id}:{plan.runtime.run_id}:{plan.runtime.job_id}",),
            )
            cursor.execute(
                "select coalesce(max(request_ordinal),0)+1 from runtime.execution_envelope"
                " where realm_id=%s and run_id=%s and job_id=%s",
                (self.realm_id, plan.runtime.run_id, plan.runtime.job_id),
            )
            ordinal = int(cursor.fetchone()[0])
        envelope = ExecutionEnvelope.create(
            realm_id=self.realm_id,
            run_id=plan.runtime.run_id,
            job_id=plan.runtime.job_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
            request_ordinal=ordinal,
            idempotency_key=f"resume:{apply_id}",
            assignment_id=plan.runtime.assignment_id,
            role=str(old[0]),
            route_decision_id=UUID(str(old[1])),
            route_decision_digest=str(old[2]),
            route_expires_at=old[3],
            model_id=str(old[4]),
            provider_binding_id=UUID(str(old[5])),
            provider_binding_digest=str(old[6]),
            provider_ref=str(old[7]),
            context_manifest_id=UUID(str(old[8])),
            context_manifest_digest=str(old[9]),
            context_packet_id=UUID(str(old[10])),
            context_packet_digest=str(old[11]),
            checkpoint_id=None,
            checkpoint_digest=None,
            checkpoint_disposition=CheckpointDisposition.BOUND_V2,
            checkpoint_v2_id=plan.checkpoint_id,
            checkpoint_v2_digest=plan.checkpoint_digest,
            source_revision=str(old[12]),
            policy_digest=str(old[13]),
            authorization_scope_digest=authorization_scope_digest,
            output_schema_digest=str(old[14]),
            payload_digest=payload_digest,
            max_input_tokens=int(old[15]),
            max_output_tokens=int(old[16]),
            max_cost_micros=int(old[17]),
            deadline=old[20],
            created_at=now,
        )
        ExecutionRunRepository(self.connection, self.realm_id).create_envelope(envelope)
        return envelope

    def store_result_checkpoint(
        self,
        plan: ResumePlan,
        *,
        envelope: ExecutionEnvelope,
        attempt_id: UUID,
        lease_id: UUID,
        fencing_token: int,
        receipt_id: UUID,
        result_digest: str,
        now: dt.datetime,
    ) -> CheckpointV2:
        """Append the exact post-dispatch checkpoint required by the terminal job gate."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select checkpoint_key,revision,intent_digest,plan_steps,completed_steps,"
                "logical_read_resources,logical_write_resources,sandbox_disposition,sandbox_id,"
                "base_revision,patch_digest,dirty_state_digest,test_and_eval_refs,tokens_used,"
                "cost_micros_used,attempts_used,deadline,rollback_recovery,"
                "routing_context_snapshot_id,source_revision,policy_digest,"
                "capability_profile_digest,dependency_snapshot_digest,migration_head_digest,"
                "route_decision_digest,context_manifest_digest,context_packet_digest,"
                "architecture_digest,rules_digest,test_suite_digest,model_inventory_digest,"
                "journal_head_digest from work.checkpoint_v2"
                " where realm_id=%s and id=%s",
                (self.realm_id, plan.checkpoint_id),
            )
            header = cursor.fetchone()
            if header is None:
                raise NotFound("Resume checkpoint v2 bulunamadi")
            cursor.execute(
                "select sr.step_id,sr.result_digest,sr.effect_kind,sr.job_id,sr.attempt_id,"
                "sr.assignment_id,sr.execution_envelope_id,sr.execution_envelope_digest,"
                "coalesce((select array_agg(x.receipt_id order by x.receipt_id)"
                " from work.checkpoint_v2_step_receipt x where x.realm_id=sr.realm_id"
                " and x.checkpoint_id=sr.checkpoint_id and x.step_id=sr.step_id),'{}'::uuid[]),"
                "coalesce((select array_agg(x.verifier_invocation_id"
                " order by x.verifier_invocation_id)"
                " from work.checkpoint_v2_step_verification x where x.realm_id=sr.realm_id"
                " and x.checkpoint_id=sr.checkpoint_id and x.step_id=sr.step_id),'{}'::uuid[])"
                " from work.checkpoint_v2_step_result sr"
                " where sr.realm_id=%s and sr.checkpoint_id=%s order by sr.step_id",
                (self.realm_id, plan.checkpoint_id),
            )
            previous_results = cursor.fetchall()
            cursor.execute(
                "select j.plan_id,p.plan_digest,step->>'effect',a.attempt_number"
                " from runtime.job j"
                " join work.task_plan p on p.realm_id=j.realm_id and p.id=j.plan_id"
                " join runtime.job_attempt a on a.realm_id=j.realm_id and a.job_id=j.id"
                " cross join lateral jsonb_array_elements(p.steps) step"
                " where j.realm_id=%s and j.id=%s and step->>'step_id'=%s and a.id=%s",
                (self.realm_id, plan.runtime.job_id, plan.next_step_id, attempt_id),
            )
            exact_plan = cursor.fetchone()
            if exact_plan is None:
                raise PolicyViolation("Resume step effect binding bulunamadi")
            cursor.execute(
                "select risk from agents.assignment where realm_id=%s and id=%s",
                (self.realm_id, plan.runtime.assignment_id),
            )
            risk_row = cursor.fetchone()
            if risk_row is None:
                raise NotFound("Resume assignment bulunamadi")
            verification_refs: tuple[UUID, ...] = ()
            verification_required = str(risk_row[0]) in {"high", "critical"}
            if verification_required:
                # Existing verifier receipts have no cryptographic binding to this
                # dispatch result/envelope.  Reusing one would turn a historical
                # review into false current evidence.  Until the post-result verifier
                # protocol records that binding, fail closed instead of completing.
                raise PolicyViolation(
                    "Resume high-risk sonucu exact post-result verifier binding ister"
                )

        old_results = tuple(
            StepResultV2(
                step_id=str(row[0]),
                result_digest=str(row[1]),
                effect_kind=EffectKind(str(row[2])),
                job_id=UUID(str(row[3])),
                attempt_id=UUID(str(row[4])),
                assignment_id=UUID(str(row[5])),
                execution_envelope_id=UUID(str(row[6])),
                execution_envelope_digest=str(row[7]),
                receipt_refs=tuple(UUID(str(value)) for value in row[8]),
                verification_refs=tuple(UUID(str(value)) for value in row[9]),
                verification_required=bool(row[9]),
            )
            for row in previous_results
        )
        new_result = StepResultV2(
            step_id=str(plan.next_step_id),
            result_digest=result_digest,
            effect_kind=EffectKind(str(exact_plan[2])),
            job_id=plan.runtime.job_id,
            attempt_id=attempt_id,
            assignment_id=plan.runtime.assignment_id,
            execution_envelope_id=envelope.id,
            execution_envelope_digest=envelope.envelope_digest,
            receipt_refs=(receipt_id,),
            verification_refs=verification_refs,
            verification_required=verification_required,
        )
        plan_steps = tuple(str(value) for value in header[3])
        completed_set = {str(value) for value in header[4]} | {str(plan.next_step_id)}
        completed_steps = tuple(value for value in plan_steps if value in completed_set)
        pending_steps = tuple(value for value in plan_steps if value not in completed_set)
        rollback = tuple(
            RecoveryDirectiveV2(
                kind=str(value["kind"]),
                reason=str(value["reason"]),
                evidence_digests=tuple(str(item) for item in value.get("evidence_digests", ())),
            )
            for value in header[17]
        )
        checkpoint = CheckpointV2(
            checkpoint_id=new_uuid7(now=now),
            checkpoint_key=str(header[0]),
            revision=int(header[1]) + 1,
            previous_checkpoint_id=plan.checkpoint_id,
            previous_checkpoint_digest=plan.checkpoint_digest,
            realm_id=self.realm_id,
            project_id=plan.project_id,
            work_item_id=plan.work_item_id,
            intent_digest=str(header[2]),
            plan_id=UUID(str(exact_plan[0])),
            plan_digest=str(exact_plan[1]),
            step_id=str(plan.next_step_id),
            run_id=plan.runtime.run_id,
            job_id=plan.runtime.job_id,
            attempt_id=attempt_id,
            assignment_id=plan.runtime.assignment_id,
            execution_envelope_id=envelope.id,
            execution_envelope_digest=envelope.envelope_digest,
            route_decision_id=envelope.route_decision_id,
            context_manifest_id=envelope.context_manifest_id,
            context_packet_id=envelope.context_packet_id,
            bindings=StaleDigestBindings(
                routing_context_snapshot_id=UUID(str(header[18])),
                source_revision=str(header[19]),
                policy_digest=str(header[20]),
                capability_profile_digest=str(header[21]),
                dependency_snapshot_digest=str(header[22]),
                migration_head_digest=str(header[23]),
                model_route_decision_digest=str(header[24]),
                context_manifest_digest=str(header[25]),
                context_packet_digest=str(header[26]),
                architecture_digest=str(header[27]),
                rules_digest=str(header[28]),
                test_suite_digest=str(header[29]),
                model_inventory_digest=str(header[30]),
                journal_head_digest=str(header[31]),
            ),
            plan_steps=plan_steps,
            completed_steps=completed_steps,
            pending_steps=pending_steps,
            step_results=(*old_results, new_result),
            open_effects=(),
            logical_read_resources=tuple(str(value) for value in header[5]),
            logical_write_resources=tuple(str(value) for value in header[6]),
            sandbox=SandboxBindingV2(
                SandboxDisposition(str(header[7])), header[8], header[9], header[10], header[11]
            ),
            tokens_used=int(header[13]),
            cost_micros_used=int(header[14]),
            attempts_used=int(exact_plan[3]),
            deadline=header[16],
            rollback_or_recovery=rollback,
            resumability=Resumability.SAFE_CONTINUE,
            next_safe_action=(
                None
                if not pending_steps
                else NextSafeActionV2("dispatch", pending_steps[0], "onceki adim tamamlandi")
            ),
            created_at=now,
            observed_lease_id=lease_id,
            observed_fencing_token=fencing_token,
            test_and_eval_digests=tuple(str(value) for value in header[12]),
        )
        stored_id, _ = CheckpointV2Repository(self.connection, self.realm_id).store(checkpoint)
        if stored_id != checkpoint.checkpoint_id:
            raise PolicyViolation("Resume terminal checkpoint replay kimligi drift")
        if not CheckpointV2Repository(self.connection, self.realm_id).is_complete(stored_id):
            raise PolicyViolation("Resume terminal checkpoint evidence eksik")
        return checkpoint
