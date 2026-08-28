"""PostgreSQL producer for the 0057 control-plane completion admission."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import UUID

from zekam.application.control_plane_completion import (
    CONTROL_PLANE_COMPLETION_ADAPTER_DIGEST,
    CONTROL_PLANE_COMPLETION_CONSUMER,
    CONTROL_PLANE_COMPLETION_OPERATION,
    ControlPlaneCompletionRequest,
    ControlPlaneCompletionResult,
    control_plane_completion_resource,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import (
    AuthorizationRequired,
    ConcurrencyConflict,
    NotFound,
    PolicyViolation,
)
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import EffectClaim, EffectReceipt
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.work import WORK_ENTITY_TYPE, EvidenceRef, WorkState, WorkType
from zekam.infrastructure.postgres.core_repository import EventStore, RevisionStore
from zekam.infrastructure.postgres.runtime_repository import EffectLedger
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository
from zekam.infrastructure.postgres.work_repository import (
    TaskPlanRepository,
    WorkItemRepository,
)

_CONTROL_COMPLETION_ADMISSION_SQL = (
    "select work.admit_control_plane_completion("
    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
    "%s,%s,%s,%s,%s,%s,%s,%s,"
    "%s,%s,%s,%s,%s,%s::jsonb,%s,%s)"
)


@dataclass(frozen=True, slots=True)
class PostgresControlPlaneCompletionRepository:
    connection: object
    realm_id: UUID

    def complete(self, request: ControlPlaneCompletionRequest) -> ControlPlaneCompletionResult:
        with self.connection.transaction():  # type: ignore[attr-defined]
            items = WorkItemRepository(self.connection, self.realm_id)
            work = items.get_for_update(request.work_item_id)
            moment = self._database_now()
            if (
                work.project_id != request.project_id
                or work.type is not WorkType.MAINTENANCE
                or any(not criterion.verified for criterion in work.acceptance_criteria)
            ):
                raise PolicyViolation("Control-plane Work identity/criteria binding drift")
            if work.state is WorkState.COMPLETED:
                return self._read_completed(
                    request,
                    work_revision=work.revision,
                    work_record_digest=work.record_digest,
                    work_evidence=work.acceptance_evidence,
                )

            plan = TaskPlanRepository(self.connection, self.realm_id).current(work.id)
            if plan is None or plan.id != request.task_plan_id:
                raise PolicyViolation("Control-plane latest TaskPlan binding drift")
            actor_id = self._source_actor(
                request,
                plan_id=plan.id,
                plan_digest=plan.plan_digest,
            )
            if work.state is not WorkState.VERIFICATION:
                raise PolicyViolation("Control-plane Work verification binding drift")
            runtime = self._terminal_runtime(request)
            if runtime[0] != plan.id:
                raise PolicyViolation("Control-plane runtime/TaskPlan binding drift")
            fencing_token = int(runtime[2])
            worker_label = str(runtime[3])
            result_digest = str(runtime[4])
            checkpoint_digest = self._checkpoint_digest(request, plan_id=plan.id)

            completed = work.with_state(
                WorkState.COMPLETED,
                evidence=request.evidence,
                now=moment,
            )
            resource = control_plane_completion_resource(work.project_id, work.id)
            authorization = Authorization.issue(
                realm_id=self.realm_id,
                actor_id=actor_id,
                work_item_id=work.id,
                plan_id=plan.id,
                plan_digest=plan.plan_digest,
                effect_digest=plan.effect_digest,
                scope=AuthorizationScope(
                    allowed_resources=(resource,),
                    allowed_effects=("database-write",),
                ),
                risk="high",
                lifetime=dt.timedelta(minutes=15),
                now=moment,
            )
            authorizations = AuthorizationRepository(self.connection, self.realm_id)
            authorizations.issue(authorization)

            claim = EffectClaim.create(
                realm_id=self.realm_id,
                job_id=request.job_id,
                attempt_id=request.attempt_id,
                operation=CONTROL_PLANE_COMPLETION_OPERATION,
                effect_digest=plan.effect_digest,
                authorization_digest=authorization.authorization_digest,
                idempotency_key=request.request_digest,
                resources=parse_requests(write=(resource,)),
                execution_identity=f"{worker_label}:{fencing_token}",
                fencing_token=fencing_token,
                adapter_digest=CONTROL_PLANE_COMPLETION_ADAPTER_DIGEST,
                now=moment,
            )
            ledger = EffectLedger(self.connection, self.realm_id)
            ledger.claim(claim, authorization_id=authorization.id)
            consumed = authorizations.consume(
                authorization.id,
                effect_digest=plan.effect_digest,
                consumed_by=CONTROL_PLANE_COMPLETION_CONSUMER,
                now=moment,
            )
            if not consumed.consumed:
                raise AuthorizationRequired(
                    f"Control-plane completion authorization tuketilemedi: {consumed.reason}"
                )

            adapter_evidence_digest = digest(
                {
                    "schema": "zekam-control-plane-completion-adapter-evidence/v2",
                    "work_item_id": str(work.id),
                    "completed_work_record_digest": completed.record_digest,
                    "checkpoint_digest": checkpoint_digest,
                    "plan_digest": plan.plan_digest,
                    "operation": CONTROL_PLANE_COMPLETION_OPERATION,
                    "source_authorization_id": str(request.source_authorization_id),
                    "source_claim_id": str(request.source_claim_id),
                    "source_effect_receipt_id": str(request.source_effect_receipt_id),
                    "request_digest": request.request_digest,
                    "evidence_digest": request.evidence_digest,
                }
            )
            receipt = EffectReceipt.completed(
                realm_id=self.realm_id,
                claim=claim,
                result_digest=result_digest,
                adapter_evidence_digest=adapter_evidence_digest,
                now=moment,
            )
            ledger.receipt(receipt)

            admission_id = self._admit(
                request,
                work_revision=completed.revision,
                work_record_digest=completed.record_digest,
                plan_digest=plan.plan_digest,
                authorization_id=authorization.id,
                claim_id=claim.id,
                effect_receipt_id=receipt.id,
            )
            items.replace(completed, expected_revision=work.revision)
            revision = RevisionStore(self.connection, self.realm_id).append(
                entity_type=WORK_ENTITY_TYPE,
                entity_id=work.id,
                payload=completed.body(),
                reason="control-plane terminal completion verified",
                actor_id=actor_id,
                expected_revision=work.revision,
                now=moment,
            )
            if revision.revision != completed.revision:
                raise ConcurrencyConflict("Control-plane Work revision chain drift")
            EventStore(self.connection, self.realm_id).append(
                event_type="work.state.completed",
                entity_type=WORK_ENTITY_TYPE,
                entity_id=work.id,
                revision_id=revision.id,
                actor_id=actor_id,
                payload={
                    "state": "completed",
                    "revision": completed.revision,
                    "claim_id": str(claim.id),
                    "effect_receipt_id": str(receipt.id),
                    "admission_id": str(admission_id),
                },
                occurred_at=moment,
            )
            return ControlPlaneCompletionResult(
                work_item_id=work.id,
                work_revision=completed.revision,
                work_record_digest=completed.record_digest,
                authorization_id=authorization.id,
                claim_id=claim.id,
                effect_receipt_id=receipt.id,
                admission_id=admission_id,
                checkpoint_id=request.checkpoint_id,
                result_digest=result_digest,
                request_digest=request.request_digest,
                evidence_digest=request.evidence_digest,
                source_authorization_id=request.source_authorization_id,
                source_claim_id=request.source_claim_id,
                source_effect_receipt_id=request.source_effect_receipt_id,
            )

    def readback(self, request: ControlPlaneCompletionRequest) -> ControlPlaneCompletionResult:
        items = WorkItemRepository(self.connection, self.realm_id)
        work = items.get(request.work_item_id)
        if (
            work.project_id != request.project_id
            or work.type is not WorkType.MAINTENANCE
            or work.state is not WorkState.COMPLETED
        ):
            raise ConcurrencyConflict("Control-plane completion readback henuz mevcut degil")
        return self._read_completed(
            request,
            work_revision=work.revision,
            work_record_digest=work.record_digest,
            work_evidence=work.acceptance_evidence,
        )

    def _database_now(self) -> dt.datetime:
        with self.connection.cursor() as cursor:  # type: ignore[attr-defined]
            cursor.execute("select statement_timestamp()")
            row = cursor.fetchone()
        if row is None:
            raise ConcurrencyConflict("Control-plane DB zamani okunamadi")
        value = row[0]
        if not isinstance(value, dt.datetime):
            raise ConcurrencyConflict("Control-plane DB zamani datetime degil")
        return value

    def _terminal_runtime(
        self, request: ControlPlaneCompletionRequest
    ) -> tuple[UUID, str, int, str, str]:
        with self.connection.cursor() as cursor:  # type: ignore[attr-defined]
            cursor.execute(
                "select job.plan_id,job.step_id,job.fencing_token,attempt.worker_label,"
                " attempt.result_digest from runtime.job job"
                " join runtime.job_attempt attempt on attempt.realm_id=job.realm_id"
                " and attempt.id=%s and attempt.job_id=job.id"
                " where job.realm_id=%s and job.id=%s and job.project_id=%s"
                " and job.work_item_id=%s and job.state='completed'"
                " and (job.run_id is null or exists(select 1"
                " from runtime.execution_run source_run"
                " join work.checkpoint_v2 source_checkpoint"
                " on source_checkpoint.realm_id=source_run.realm_id"
                " and source_checkpoint.run_id=source_run.id"
                " and source_checkpoint.job_id=job.id"
                " and source_checkpoint.attempt_id=attempt.id"
                " and source_checkpoint.step_id=job.step_id"
                " where source_run.realm_id=job.realm_id and source_run.id=job.run_id"
                " and source_run.state='completed'"
                " and job.step_id=any(source_checkpoint.completed_steps)"
                " and work.validate_checkpoint_v2(source_checkpoint.realm_id,"
                " source_checkpoint.id)))"
                " and attempt.outcome='succeeded' and attempt.finished_at is not null"
                " and attempt.fencing_token=job.fencing_token",
                (
                    request.attempt_id,
                    self.realm_id,
                    request.job_id,
                    request.project_id,
                    request.work_item_id,
                ),
            )
            row = cursor.fetchone()
        if row is None or row[4] is None:
            raise NotFound("Control-plane terminal job/attempt bulunamadi")
        return UUID(str(row[0])), str(row[1]), int(row[2]), str(row[3]), str(row[4])

    def _checkpoint_digest(self, request: ControlPlaneCompletionRequest, *, plan_id: UUID) -> str:
        with self.connection.cursor() as cursor:  # type: ignore[attr-defined]
            cursor.execute(
                "select checkpoint_digest from work.checkpoint"
                " where realm_id=%s and id=%s and project_id=%s and work_item_id=%s"
                " and task_plan_id=%s and job_id=%s",
                (
                    self.realm_id,
                    request.checkpoint_id,
                    request.project_id,
                    request.work_item_id,
                    plan_id,
                    request.job_id,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Control-plane exact checkpoint bulunamadi")
        return str(row[0])

    def _source_actor(
        self,
        request: ControlPlaneCompletionRequest,
        *,
        plan_id: UUID,
        plan_digest: str,
    ) -> UUID:
        """Derive authority from a consumed terminal effect, never caller input."""

        with self.connection.cursor() as cursor:  # type: ignore[attr-defined]
            cursor.execute(
                "select source.actor_id,receipt.result_digest,"
                " source.authorization_digest,source.work_item_id,source.plan_digest,"
                " source.effect_digest,source.scope,source.risk,source.issued_at,"
                " source.expires_at"
                " from security.authorization source"
                " join runtime.effect_claim claim on claim.realm_id=source.realm_id"
                " and claim.id=%s and claim.authorization_id=source.id"
                " join runtime.effect_receipt receipt on receipt.realm_id=claim.realm_id"
                " and receipt.id=%s and receipt.claim_id=claim.id"
                " join runtime.job job on job.realm_id=claim.realm_id"
                " and job.id=claim.job_id"
                " join runtime.job_attempt attempt on attempt.realm_id=claim.realm_id"
                " and attempt.id=claim.attempt_id and attempt.job_id=claim.job_id"
                " where source.realm_id=%s and source.id=%s and source.work_item_id=%s"
                " and source.authorization_digest=%s"
                " and source.plan_id=%s and source.plan_digest=%s"
                " and source.state='consumed' and source.consumed_by=%s"
                " and claim.job_id=%s and claim.attempt_id=%s"
                " and job.project_id=%s and job.work_item_id=%s and job.plan_id=%s"
                " and job.step_id=%s and job.state='completed'"
                " and (job.run_id is null or exists(select 1"
                " from runtime.execution_run source_run"
                " join work.checkpoint_v2 source_checkpoint"
                " on source_checkpoint.realm_id=source_run.realm_id"
                " and source_checkpoint.run_id=source_run.id"
                " and source_checkpoint.job_id=job.id"
                " and source_checkpoint.attempt_id=attempt.id"
                " and source_checkpoint.step_id=job.step_id"
                " where source_run.realm_id=job.realm_id and source_run.id=job.run_id"
                " and source_run.state='completed'"
                " and job.step_id=any(source_checkpoint.completed_steps)"
                " and work.validate_checkpoint_v2(source_checkpoint.realm_id,"
                " source_checkpoint.id)))"
                " and attempt.outcome='succeeded' and attempt.finished_at is not null"
                " and attempt.fencing_token=job.fencing_token"
                " and claim.operation=job.step_id and claim.operation=%s"
                " and claim.fencing_token=job.fencing_token"
                " and claim.execution_identity=attempt.worker_label||':'"
                " ||job.fencing_token::text"
                " and claim.effect_digest=source.effect_digest"
                " and claim.claim_digest=%s"
                " and claim.effect_digest=%s and claim.adapter_digest=%s"
                " and claim.authorization_digest=source.authorization_digest"
                " and claim.claim_digest=continuity.jsonb_digest(jsonb_build_object("
                " 'job_id',job.id::text,'operation',claim.operation,"
                " 'effect_digest',claim.effect_digest,"
                " 'authorization_digest',claim.authorization_digest,"
                " 'idempotency_key',claim.idempotency_key,'resources',claim.resources,"
                " 'execution_identity',claim.execution_identity,"
                " 'fencing_token',claim.fencing_token,"
                " 'adapter_digest',claim.adapter_digest))"
                " and source.allowed_resources=%s"
                " and source.allowed_effects=%s"
                " and source.scope->'allowed_resources'=%s::jsonb"
                " and source.scope->'allowed_effects'=%s::jsonb"
                " and claim.resources=(select jsonb_agg(jsonb_build_object("
                " 'resource',resource,'mode','write') order by resource)"
                " from unnest(source.allowed_resources) resource)"
                " and (select array_agg(resource order by resource)"
                " from (select distinct value->>'resource' resource"
                " from jsonb_array_elements(claim.resources) value) resources)"
                " =(select array_agg(resource order by resource)"
                " from unnest(source.allowed_resources) resource)"
                " and cardinality(source.provider_refs)=0"
                " and cardinality(source.secret_ref_ids)=0"
                " and source.scope->'provider_refs'='[]'::jsonb"
                " and source.scope->'secret_ref_ids'='[]'::jsonb"
                " and source.scope->'data_classifications'=%s::jsonb"
                " and receipt.status='completed'"
                " and receipt.result_digest=attempt.result_digest"
                " and receipt.adapter_evidence_digest=%s"
                " and attempt.started_at<=claim.claimed_at"
                " and source.issued_at<=claim.claimed_at"
                " and claim.claimed_at<=source.consumed_at"
                " and source.consumed_at<=receipt.completed_at"
                " and receipt.completed_at<=attempt.finished_at"
                " and source.expires_at>=source.consumed_at",
                (
                    request.source_claim_id,
                    request.source_effect_receipt_id,
                    self.realm_id,
                    request.source_authorization_id,
                    request.work_item_id,
                    request.source_authorization_digest,
                    plan_id,
                    plan_digest,
                    request.source_consumed_by,
                    request.job_id,
                    request.attempt_id,
                    request.project_id,
                    request.work_item_id,
                    plan_id,
                    request.source_operation,
                    request.source_operation,
                    request.source_claim_digest,
                    request.source_effect_digest,
                    request.source_adapter_digest,
                    list(request.source_resources),
                    list(request.source_effects),
                    canonical_json(list(request.source_resources)),
                    canonical_json(list(request.source_effects)),
                    canonical_json(list(request.source_data_classifications)),
                    request.source_adapter_evidence_digest,
                ),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise AuthorizationRequired(
                "Control-plane completion exact source authority/receipt ister"
            )
        result_digest = str(rows[0][1])
        authorization_digest = digest(
            {
                "actor_id": str(rows[0][0]),
                "work_item_id": str(rows[0][3]),
                "plan_digest": str(rows[0][4]),
                "effect_digest": str(rows[0][5]),
                "scope": dict(rows[0][6]),
                "risk": str(rows[0][7]),
                "issued_at": rows[0][8],
                "expires_at": rows[0][9],
            }
        )
        if authorization_digest != str(rows[0][2]):
            raise AuthorizationRequired(
                "Control-plane completion source authorization digest drift"
            )
        if (
            sum(
                item.kind == "runtime-receipt"
                and item.reference == str(request.source_effect_receipt_id)
                and item.digest_value == result_digest
                for item in request.evidence
            )
            != 1
        ):
            raise PolicyViolation("Control-plane completion source receipt/result evidence drift")
        return UUID(str(rows[0][0]))

    def _read_completed(
        self,
        request: ControlPlaneCompletionRequest,
        *,
        work_revision: int,
        work_record_digest: str,
        work_evidence: tuple[EvidenceRef, ...],
    ) -> ControlPlaneCompletionResult:
        """Read back one committed exact chain; never mint on uncertain replay."""

        if work_evidence != request.evidence:
            raise ConcurrencyConflict("Completed Work request evidence digest drift")

        with self.connection.cursor() as cursor:  # type: ignore[attr-defined]
            cursor.execute(
                "select admission.expected_work_revision,admission.authorization_id,"
                " admission.claim_id,admission.effect_receipt_id,admission.id,"
                " effect.result_digest from work.completion_admission admission"
                " join runtime.effect_claim claim on claim.realm_id=admission.realm_id"
                " and claim.id=admission.claim_id"
                " join runtime.effect_receipt effect on effect.realm_id=admission.realm_id"
                " and effect.id=admission.effect_receipt_id"
                " where admission.realm_id=%s and admission.project_id=%s"
                " and admission.work_item_id=%s and admission.mode='control-plane'"
                " and admission.plan_id=%s and admission.job_id=%s"
                " and admission.attempt_id=%s and admission.checkpoint_id=%s"
                " and admission.expected_work_revision=%s"
                " and admission.expected_work_record_digest=%s"
                " and admission.source_authorization_id=%s"
                " and admission.source_authorization_digest=%s"
                " and admission.source_claim_id=%s and admission.source_claim_digest=%s"
                " and admission.source_effect_receipt_id=%s"
                " and admission.source_operation=%s and admission.source_consumed_by=%s"
                " and admission.source_effect_digest=%s"
                " and admission.source_adapter_digest=%s"
                " and admission.source_adapter_evidence_digest=%s"
                " and admission.source_resources=%s and admission.source_effects=%s"
                " and admission.source_data_classifications=%s"
                " and admission.completion_evidence=%s::jsonb"
                " and admission.request_digest=%s and admission.evidence_digest=%s"
                " and admission.operation=%s and admission.consumed_at is not null"
                " and claim.idempotency_key=%s and effect.status='completed'",
                (
                    self.realm_id,
                    request.project_id,
                    request.work_item_id,
                    request.task_plan_id,
                    request.job_id,
                    request.attempt_id,
                    request.checkpoint_id,
                    work_revision,
                    work_record_digest,
                    request.source_authorization_id,
                    request.source_authorization_digest,
                    request.source_claim_id,
                    request.source_claim_digest,
                    request.source_effect_receipt_id,
                    request.source_operation,
                    request.source_consumed_by,
                    request.source_effect_digest,
                    request.source_adapter_digest,
                    request.source_adapter_evidence_digest,
                    list(request.source_resources),
                    list(request.source_effects),
                    list(request.source_data_classifications),
                    canonical_json([item.as_dict() for item in request.evidence]),
                    request.request_digest,
                    request.evidence_digest,
                    CONTROL_PLANE_COMPLETION_OPERATION,
                    request.request_digest,
                ),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise ConcurrencyConflict(
                "Completed Work exact control-plane admission readback vermedi"
            )
        row = rows[0]
        return ControlPlaneCompletionResult(
            work_item_id=request.work_item_id,
            work_revision=int(row[0]),
            work_record_digest=work_record_digest,
            authorization_id=UUID(str(row[1])),
            claim_id=UUID(str(row[2])),
            effect_receipt_id=UUID(str(row[3])),
            admission_id=UUID(str(row[4])),
            checkpoint_id=request.checkpoint_id,
            result_digest=str(row[5]),
            request_digest=request.request_digest,
            evidence_digest=request.evidence_digest,
            source_authorization_id=request.source_authorization_id,
            source_claim_id=request.source_claim_id,
            source_effect_receipt_id=request.source_effect_receipt_id,
        )

    def _admit(
        self,
        request: ControlPlaneCompletionRequest,
        *,
        work_revision: int,
        work_record_digest: str,
        plan_digest: str,
        authorization_id: UUID,
        claim_id: UUID,
        effect_receipt_id: UUID,
    ) -> UUID:
        return self._execute_admission(
            self._admission_parameters(
                request,
                work_revision=work_revision,
                work_record_digest=work_record_digest,
                plan_digest=plan_digest,
                authorization_id=authorization_id,
                claim_id=claim_id,
                effect_receipt_id=effect_receipt_id,
            )
        )

    def _admission_parameters(
        self,
        request: ControlPlaneCompletionRequest,
        *,
        work_revision: int,
        work_record_digest: str,
        plan_digest: str,
        authorization_id: UUID,
        claim_id: UUID,
        effect_receipt_id: UUID,
    ) -> tuple[object, ...]:
        return (
            self.realm_id,
            request.project_id,
            request.work_item_id,
            work_revision,
            work_record_digest,
            request.task_plan_id,
            plan_digest,
            request.job_id,
            request.attempt_id,
            claim_id,
            authorization_id,
            request.checkpoint_id,
            effect_receipt_id,
            CONTROL_PLANE_COMPLETION_OPERATION,
            request.source_authorization_id,
            request.source_authorization_digest,
            request.source_claim_id,
            request.source_claim_digest,
            request.source_effect_receipt_id,
            request.source_operation,
            request.source_consumed_by,
            request.source_effect_digest,
            request.source_adapter_digest,
            request.source_adapter_evidence_digest,
            list(request.source_resources),
            list(request.source_effects),
            list(request.source_data_classifications),
            canonical_json([item.as_dict() for item in request.evidence]),
            request.request_digest,
            request.evidence_digest,
        )

    def _execute_admission(self, parameters: tuple[object, ...]) -> UUID:
        with self.connection.cursor() as cursor:  # type: ignore[attr-defined]
            cursor.execute(_CONTROL_COMPLETION_ADMISSION_SQL, parameters)
            row = cursor.fetchone()
        if row is None:
            raise ConcurrencyConflict("Control-plane completion admission uretilmedi")
        return UUID(str(row[0]))
