"""PostgreSQL persistence for typed session continuity and Memory Compiler records."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConcurrencyConflict, NotFound, ValidationFailed
from zekam.domain.identifiers import new_uuid7
from zekam.domain.memory_compiler import MemoryCompilerOutput
from zekam.domain.memory_contract import MemoryContractEvaluation
from zekam.domain.session_continuity import (
    CompactionReceipt,
    ProjectionGenerationReceipt,
    SessionCloseReceipt,
    SessionHydrationReceipt,
    SessionLifecycleEvent,
)

TERMINAL_DELIVERY_STATES = frozenset({"completed", "failed", "recovery-required"})


def _idempotency_key(value: str) -> None:
    if not value or value != value.strip() or len(value) > 512:
        raise ValidationFailed("Idempotency key bos, padded veya fazla uzun olamaz")


@dataclass(frozen=True, slots=True)
class LifecycleDeliveryStage:
    event_id: UUID
    outbox_id: UUID
    created: bool


@dataclass(frozen=True, slots=True)
class CompilerWatermarkClaim:
    claim_id: UUID
    created: bool
    state: str


@dataclass(frozen=True, slots=True)
class GapRecoveryRecord:
    id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID | None
    gap_code: str
    gap_ref: str
    evidence_digest: str
    recovery_ref: str
    state: str
    created_at: dt.datetime
    recovery_receipt_ref: str | None = None
    recovery_receipt_digest: str | None = None
    resolved_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        if self.state not in {"open", "recovery-required", "resolved"}:
            raise ValidationFailed("Continuity gap state gecersiz")
        parse_digest(self.evidence_digest)
        if not self.gap_code or not self.gap_ref or not self.recovery_ref:
            raise ValidationFailed("Continuity gap code/ref/recovery ister")
        resolution_fields = (
            self.recovery_receipt_ref,
            self.recovery_receipt_digest,
            self.resolved_at,
        )
        if self.state == "resolved" and not all(item is not None for item in resolution_fields):
            raise ValidationFailed(
                "Resolved continuity gap receipt ref/digest ve resolved_at ister"
            )
        if self.state != "resolved" and any(item is not None for item in resolution_fields):
            raise ValidationFailed("Open continuity gap terminal receipt tasiyamaz")
        if self.recovery_receipt_digest is not None:
            parse_digest(self.recovery_receipt_digest)


@dataclass(frozen=True, slots=True)
class SessionContinuitySnapshot:
    run_id: UUID
    hydration_receipt_digest: str | None
    hydration_fresh: bool
    hydration_complete: bool
    close_receipt_digest: str | None
    close_status: str | None
    compaction_receipt_digest: str | None
    compaction_status: str | None
    contract_evaluation_digest: str | None
    contract_passed: bool
    open_gaps: tuple[GapRecoveryRecord, ...]

    @property
    def ready_for_mutation(self) -> bool:
        return (
            self.hydration_receipt_digest is not None
            and self.hydration_fresh
            and self.hydration_complete
            and not self.open_gaps
        )


@dataclass(frozen=True, slots=True)
class ProjectionFreshnessSnapshot:
    projection_ref: str
    receipt_digest: str
    projection_digest: str
    source_digest: str
    generated_at: dt.datetime
    current: bool
    grants_authority: bool = False


@dataclass(frozen=True, slots=True)
class MemoryContinuityRepository:
    connection: Any
    realm_id: UUID

    def stage_lifecycle_delivery(
        self,
        event: SessionLifecycleEvent,
        *,
        idempotency_key: str,
        plan_digest: str,
    ) -> LifecycleDeliveryStage:
        """Event ledger ve durable outbox'i tek transaction'da stage eder."""

        _idempotency_key(idempotency_key)
        parse_digest(plan_digest)
        if event.realm_id != self.realm_id:
            raise ValidationFailed("Lifecycle event repository realm binding drift")
        payload_digest = digest({"event_digest": event.event_digest, "plan_digest": plan_digest})
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select event.id,event.event_digest,outbox.id,outbox.plan_digest"
                " from continuity.session_lifecycle_event event"
                " join continuity.lifecycle_delivery_outbox outbox"
                " on outbox.realm_id=event.realm_id and outbox.event_id=event.id"
                " where event.realm_id=%s and event.idempotency_key=%s",
                (self.realm_id, idempotency_key),
            )
            replay = cursor.fetchone()
            if replay is not None:
                if str(replay[1]) != event.event_digest or str(replay[3]) != plan_digest:
                    raise ConcurrencyConflict("Lifecycle idempotency replay payload drift")
                return LifecycleDeliveryStage(UUID(str(replay[0])), UUID(str(replay[2])), False)

            cursor.execute(
                "insert into continuity.session_lifecycle_event"
                " (id,realm_id,project_id,work_item_id,run_id,session_id,client_id,event_type,"
                " sequence,previous_digest,origin,causation_id,correlation_id,recursion_depth,"
                " classification,idempotency_key,event_body,event_digest,occurred_at,ingested_at,"
                " grants_authority)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,"
                " %s,%s,false)",
                (
                    event.event_id,
                    self.realm_id,
                    event.project_id,
                    event.work_item_id,
                    event.run_id,
                    event.session_id,
                    event.client_id,
                    event.event_type,
                    event.sequence,
                    event.previous_digest,
                    event.origin,
                    event.causation_id,
                    event.correlation_id,
                    event.recursion_depth,
                    event.classification.value,
                    idempotency_key,
                    canonical_json(event.body()),
                    event.event_digest,
                    event.occurred_at,
                    event.ingested_at,
                ),
            )
            outbox_id = new_uuid7(now=event.ingested_at)
            cursor.execute(
                "insert into continuity.lifecycle_delivery_outbox"
                " (id,realm_id,event_id,plan_digest,payload_digest,state,created_at,"
                " grants_authority) values (%s,%s,%s,%s,%s,'pending',%s,false)",
                (
                    outbox_id,
                    self.realm_id,
                    event.event_id,
                    plan_digest,
                    payload_digest,
                    event.ingested_at,
                ),
            )
        return LifecycleDeliveryStage(event.event_id, outbox_id, True)

    def append_lifecycle_event(
        self,
        event: SessionLifecycleEvent,
        *,
        idempotency_key: str,
        plan_digest: str,
    ) -> LifecycleDeliveryStage:
        return self.stage_lifecycle_delivery(
            event, idempotency_key=idempotency_key, plan_digest=plan_digest
        )

    def finalize_lifecycle_delivery(
        self,
        *,
        outbox_id: UUID,
        receipt_digest: str,
        status: str,
        completed_at: dt.datetime,
    ) -> None:
        parse_digest(receipt_digest)
        if status not in TERMINAL_DELIVERY_STATES:
            raise ValidationFailed("Lifecycle delivery terminal status gecersiz")
        if completed_at.tzinfo is None or completed_at.tzinfo.utcoffset(completed_at) is None:
            raise ValidationFailed("Lifecycle delivery completed_at timezone-aware olmali")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select state,terminal_receipt_digest,completed_at"
                " from continuity.lifecycle_delivery_outbox"
                " where realm_id=%s and id=%s for update",
                (self.realm_id, outbox_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise NotFound("Lifecycle delivery outbox bulunamadi")
            if str(row[0]) in TERMINAL_DELIVERY_STATES:
                if str(row[0]) != status or str(row[1]) != receipt_digest:
                    raise ConcurrencyConflict("Lifecycle delivery terminal replay drift")
                return
            cursor.execute(
                "update continuity.lifecycle_delivery_outbox"
                " set state=%s,terminal_receipt_digest=%s,completed_at=%s"
                " where realm_id=%s and id=%s",
                (status, receipt_digest, completed_at, self.realm_id, outbox_id),
            )

    def store_hydration_receipt(
        self, receipt: SessionHydrationReceipt, *, idempotency_key: str
    ) -> bool:
        if receipt.realm_id != self.realm_id:
            raise ValidationFailed("Hydration receipt repository realm binding drift")
        return self._store_receipt(
            table="session_hydration_receipt",
            receipt_id=receipt.receipt_id,
            idempotency_key=idempotency_key,
            receipt_digest=receipt.receipt_digest,
            identity=(
                receipt.project_id,
                receipt.work_item_id,
                receipt.run_id,
                receipt.session_id,
                receipt.client_id,
            ),
            columns=("fresh", "complete"),
            values=(receipt.fresh, receipt.complete),
            body=receipt.body(),
            created_at=receipt.created_at,
        )

    def store_close_receipt(self, receipt: SessionCloseReceipt, *, idempotency_key: str) -> bool:
        if receipt.realm_id != self.realm_id:
            raise ValidationFailed("Close receipt repository realm binding drift")
        return self._store_receipt(
            table="session_close_receipt",
            receipt_id=receipt.receipt_id,
            idempotency_key=idempotency_key,
            receipt_digest=receipt.receipt_digest,
            identity=(
                receipt.project_id,
                receipt.work_item_id,
                receipt.run_id,
                receipt.session_id,
                receipt.client_id,
            ),
            columns=("job_id", "attempt_id", "close_status"),
            values=(receipt.job_id, receipt.attempt_id, receipt.status.value),
            body=receipt.body(),
            created_at=receipt.closed_at,
        )

    def store_compaction_receipt(self, receipt: CompactionReceipt, *, idempotency_key: str) -> bool:
        if receipt.realm_id != self.realm_id:
            raise ValidationFailed("Compaction receipt repository realm binding drift")
        hydration_id: UUID | None = None
        if receipt.rehydration_receipt_digest is not None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "select id from continuity.session_hydration_receipt"
                    " where realm_id=%s and receipt_digest=%s",
                    (self.realm_id, receipt.rehydration_receipt_digest),
                )
                row = cursor.fetchone()
            if row is None:
                raise NotFound("Compaction rehydration receipt bulunamadi")
            hydration_id = UUID(str(row[0]))
        return self._store_receipt(
            table="compaction_receipt",
            receipt_id=receipt.receipt_id,
            idempotency_key=idempotency_key,
            receipt_digest=receipt.receipt_digest,
            identity=(
                receipt.project_id,
                receipt.work_item_id,
                receipt.run_id,
                receipt.session_id,
                receipt.client_id,
            ),
            columns=(
                "pre_compaction_event_digest",
                "checkpoint_digest",
                "hydration_receipt_id",
                "status",
                "completed_at",
            ),
            values=(
                receipt.pre_compaction_event_digest,
                receipt.checkpoint_digest,
                hydration_id,
                receipt.status.value,
                receipt.completed_at,
            ),
            body=receipt.body(),
            created_at=receipt.created_at,
        )

    def store_contract_evaluation(
        self, evaluation: MemoryContractEvaluation, *, idempotency_key: str
    ) -> bool:
        _idempotency_key(idempotency_key)
        if evaluation.realm_id != self.realm_id:
            raise ValidationFailed("Memory Contract repository realm binding drift")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select evaluation_digest from continuity.memory_contract_evaluation"
                " where realm_id=%s and idempotency_key=%s",
                (self.realm_id, idempotency_key),
            )
            replay = cursor.fetchone()
            if replay is not None:
                if str(replay[0]) != evaluation.evaluation_digest:
                    raise ConcurrencyConflict("Memory Contract idempotency replay drift")
                return False
            cursor.execute(
                "insert into continuity.memory_contract_evaluation"
                " (id,realm_id,project_id,work_item_id,run_id,source_revision,policy_version,"
                " evaluator_version,passed,idempotency_key,evaluation_body,evaluation_digest,"
                " evaluated_at,grants_authority)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,false)",
                (
                    evaluation.evaluation_id,
                    self.realm_id,
                    evaluation.project_id,
                    evaluation.work_item_id,
                    evaluation.run_id,
                    evaluation.source_revision,
                    evaluation.policy_version,
                    evaluation.evaluator_version,
                    evaluation.passed,
                    idempotency_key,
                    canonical_json(evaluation.body()),
                    evaluation.evaluation_digest,
                    evaluation.evaluated_at,
                ),
            )
        return True

    def store_projection_receipt(
        self, receipt: ProjectionGenerationReceipt, *, idempotency_key: str
    ) -> bool:
        _idempotency_key(idempotency_key)
        if receipt.realm_id != self.realm_id:
            raise ValidationFailed("Projection receipt repository realm binding drift")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select receipt_digest from continuity.projection_generation_receipt"
                " where realm_id=%s and idempotency_key=%s",
                (self.realm_id, idempotency_key),
            )
            replay = cursor.fetchone()
            if replay is not None:
                if str(replay[0]) != receipt.receipt_digest:
                    raise ConcurrencyConflict("Projection receipt idempotency replay drift")
                return False
            cursor.execute(
                "insert into continuity.projection_generation_receipt"
                " (id,realm_id,project_id,work_item_id,idempotency_key,source_ref,source_digest,"
                " projection_ref,projection_digest,generator_version,classification,"
                " public_filtered,receipt_body,receipt_digest,generated_at,grants_authority)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'public',true,%s::jsonb,%s,%s,false)",
                (
                    receipt.receipt_id,
                    self.realm_id,
                    receipt.project_id,
                    receipt.work_item_id,
                    idempotency_key,
                    receipt.source_ref,
                    receipt.source_digest,
                    receipt.projection_ref,
                    receipt.projection_digest,
                    receipt.generator_version,
                    canonical_json(receipt.body()),
                    receipt.receipt_digest,
                    receipt.generated_at,
                ),
            )
        return True

    def read_latest_projection(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        projection_ref: str,
        expected_source_digest: str,
    ) -> ProjectionFreshnessSnapshot | None:
        parse_digest(expected_source_digest)
        if not projection_ref.strip() or len(projection_ref) > 512:
            raise ValidationFailed("Projection ref bos veya fazla uzun olamaz")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select receipt_digest,projection_digest,source_digest,generated_at,"
                " grants_authority from continuity.projection_generation_receipt"
                " where realm_id=%s and project_id=%s and work_item_id=%s and projection_ref=%s"
                " order by generated_at desc,id desc limit 1",
                (self.realm_id, project_id, work_item_id, projection_ref),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return ProjectionFreshnessSnapshot(
            projection_ref=projection_ref,
            receipt_digest=str(row[0]),
            projection_digest=str(row[1]),
            source_digest=str(row[2]),
            generated_at=row[3],
            current=str(row[2]) == expected_source_digest,
            grants_authority=bool(row[4]),
        )

    def claim_compiler_watermark(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        idempotency_key: str,
        source_set_digest: str,
        source_watermark: str,
        claimed_at: dt.datetime,
    ) -> CompilerWatermarkClaim:
        _idempotency_key(idempotency_key)
        parse_digest(source_set_digest)
        if not source_watermark or source_watermark != source_watermark.strip():
            raise ValidationFailed("Compiler source watermark bos/padded olamaz")
        if claimed_at.tzinfo is None or claimed_at.tzinfo.utcoffset(claimed_at) is None:
            raise ValidationFailed("Compiler claim zamani timezone-aware olmali")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"{self.realm_id}:{project_id}:{source_set_digest}:{source_watermark}",),
            )
            cursor.execute(
                "select id,state,source_set_digest,source_watermark"
                " from memory.compiler_watermark_claim"
                " where realm_id=%s and (idempotency_key=%s or"
                " (project_id=%s and source_set_digest=%s and source_watermark=%s))"
                " order by claimed_at limit 1 for update",
                (
                    self.realm_id,
                    idempotency_key,
                    project_id,
                    source_set_digest,
                    source_watermark,
                ),
            )
            replay = cursor.fetchone()
            if replay is not None:
                if str(replay[2]) != source_set_digest or str(replay[3]) != source_watermark:
                    raise ConcurrencyConflict("Compiler watermark idempotency replay drift")
                return CompilerWatermarkClaim(UUID(str(replay[0])), False, str(replay[1]))
            claim_id = new_uuid7(now=claimed_at)
            cursor.execute(
                "insert into memory.compiler_watermark_claim"
                " (id,realm_id,project_id,work_item_id,run_id,idempotency_key,source_set_digest,"
                " source_watermark,state,claimed_at,grants_authority)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,false)",
                (
                    claim_id,
                    self.realm_id,
                    project_id,
                    work_item_id,
                    run_id,
                    idempotency_key,
                    source_set_digest,
                    source_watermark,
                    claimed_at,
                ),
            )
        return CompilerWatermarkClaim(claim_id, True, "pending")

    def store_compiler_output(
        self, output: MemoryCompilerOutput, *, watermark_claim_id: UUID
    ) -> bool:
        if output.realm_id != self.realm_id:
            raise ValidationFailed("Compiler output repository realm binding drift")
        source_set_body = [item.as_dict() for item in output.source_set]
        source_set_digest = digest(source_set_body)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select project_id,work_item_id,run_id,source_set_digest,source_watermark,state,"
                " compiler_run_id,result_digest from memory.compiler_watermark_claim"
                " where realm_id=%s and id=%s for update",
                (self.realm_id, watermark_claim_id),
            )
            claim = cursor.fetchone()
            if claim is None:
                raise NotFound("Compiler watermark claim bulunamadi")
            expected_identity = (output.project_id, output.work_item_id, output.run_id)
            if tuple(claim[:3]) != expected_identity:
                raise ValidationFailed("Compiler output claim identity drift")
            if str(claim[3]) != source_set_digest or str(claim[4]) != output.source_watermark:
                raise ConcurrencyConflict("Compiler source set/watermark drift")
            if str(claim[5]) in TERMINAL_DELIVERY_STATES:
                if claim[6] != output.output_id or str(claim[7]) != output.output_digest:
                    raise ConcurrencyConflict("Compiler terminal replay drift")
                return False
            cursor.execute(
                "insert into memory.compiler_run"
                " (id,realm_id,project_id,work_item_id,run_id,watermark_claim_id,source_set,"
                " source_watermark,output_body,output_digest,created_at,grants_authority)"
                " values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,false)",
                (
                    output.output_id,
                    self.realm_id,
                    output.project_id,
                    output.work_item_id,
                    output.run_id,
                    watermark_claim_id,
                    canonical_json(source_set_body),
                    output.source_watermark,
                    canonical_json(output.body()),
                    output.output_digest,
                    output.created_at,
                ),
            )
            for candidate in output.candidates:
                candidate_id = new_uuid7(now=output.created_at)
                cursor.execute(
                    "insert into memory.compiler_candidate"
                    " (id,realm_id,compiler_run_id,logical_candidate_id,candidate_type,truth_class,"
                    " classification,risk,state,is_current,candidate_body,candidate_digest,"
                    " created_at,grants_authority)"
                    " values (%s,%s,%s,%s,%s,%s,%s,%s,'candidate',true,%s::jsonb,%s,%s,false)",
                    (
                        candidate_id,
                        self.realm_id,
                        output.output_id,
                        candidate.candidate_id,
                        candidate.candidate_type.value,
                        candidate.truth_class.value,
                        candidate.classification.value,
                        candidate.risk.value,
                        canonical_json(candidate.as_dict()),
                        candidate.candidate_digest,
                        output.created_at,
                    ),
                )
                for relation_kind, refs in (
                    ("source", candidate.source_refs),
                    ("evidence", candidate.evidence_refs),
                ):
                    for ordinal, reference in enumerate(refs, start=1):
                        cursor.execute(
                            "insert into memory.compiler_candidate_source"
                            " (id,realm_id,candidate_id,relation_kind,ordinal,source_ref,"
                            " source_digest,created_at,grants_authority)"
                            " values (%s,%s,%s,%s,%s,%s,%s,%s,false)",
                            (
                                new_uuid7(now=output.created_at),
                                self.realm_id,
                                candidate_id,
                                relation_kind,
                                ordinal,
                                reference.ref,
                                reference.digest_value,
                                output.created_at,
                            ),
                        )
            cursor.execute(
                "update memory.compiler_watermark_claim set state='completed',compiler_run_id=%s,"
                " result_digest=%s,completed_at=%s where realm_id=%s and id=%s",
                (
                    output.output_id,
                    output.output_digest,
                    output.created_at,
                    self.realm_id,
                    watermark_claim_id,
                ),
            )
        return True

    def finalize_compiler_claim(
        self,
        *,
        claim_id: UUID,
        status: str,
        result_digest: str,
        completed_at: dt.datetime,
    ) -> None:
        if status not in {"failed", "recovery-required"}:
            raise ValidationFailed("Compiler failure terminal status gecersiz")
        parse_digest(result_digest)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select state,result_digest from memory.compiler_watermark_claim"
                " where realm_id=%s and id=%s for update",
                (self.realm_id, claim_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise NotFound("Compiler watermark claim bulunamadi")
            if str(row[0]) in TERMINAL_DELIVERY_STATES:
                if str(row[0]) != status or str(row[1]) != result_digest:
                    raise ConcurrencyConflict("Compiler claim terminal replay drift")
                return
            cursor.execute(
                "update memory.compiler_watermark_claim set state=%s,result_digest=%s,"
                " completed_at=%s where realm_id=%s and id=%s",
                (status, result_digest, completed_at, self.realm_id, claim_id),
            )

    def record_candidate_review(
        self,
        *,
        candidate_id: str,
        compiler_identity: str,
        reviewer_identity: str,
        review_ref: str,
        review_digest: str,
        reviewed_at: dt.datetime,
        decision: str = "approved",
    ) -> bool:
        if decision not in {"approved", "rejected", "quarantined"}:
            raise ValidationFailed("Compiler candidate review decision gecersiz")
        if not compiler_identity.strip() or not reviewer_identity.strip():
            raise ValidationFailed("Compiler/reviewer identity bos olamaz")
        if compiler_identity == reviewer_identity:
            raise ValidationFailed("Compiler ve reviewer identity farkli olmali")
        if not review_ref.strip() or len(review_ref) > 512:
            raise ValidationFailed("Compiler review ref bos veya fazla uzun olamaz")
        parse_digest(review_digest)
        if reviewed_at.tzinfo is None or reviewed_at.tzinfo.utcoffset(reviewed_at) is None:
            raise ValidationFailed("Compiler review zamani timezone-aware olmali")
        target_state = "reviewed" if decision == "approved" else decision
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select id from memory.compiler_candidate"
                " where realm_id=%s and logical_candidate_id=%s for update",
                (self.realm_id, candidate_id),
            )
            candidate = cursor.fetchone()
            if candidate is None:
                raise NotFound("Compiler candidate bulunamadi")
            candidate_storage_id = UUID(str(candidate[0]))
            cursor.execute(
                "select id,compiler_identity,reviewer_identity,decision,review_ref,review_digest"
                " from memory.compiler_candidate_review"
                " where realm_id=%s and candidate_id=%s",
                (self.realm_id, candidate_storage_id),
            )
            replay = cursor.fetchone()
            if replay is not None:
                expected = (
                    compiler_identity,
                    reviewer_identity,
                    decision,
                    review_ref,
                    review_digest,
                )
                if tuple(str(item) for item in replay[1:]) != expected:
                    raise ConcurrencyConflict("Compiler candidate review replay drift")
                return False
            cursor.execute(
                "insert into memory.compiler_candidate_review"
                " (id,realm_id,candidate_id,compiler_identity,reviewer_identity,decision,"
                " review_ref,review_digest,reviewed_at,grants_authority)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,false)",
                (
                    new_uuid7(now=reviewed_at),
                    self.realm_id,
                    candidate_storage_id,
                    compiler_identity,
                    reviewer_identity,
                    decision,
                    review_ref,
                    review_digest,
                    reviewed_at,
                ),
            )
            cursor.execute(
                "update memory.compiler_candidate set state=%s"
                " where realm_id=%s and id=%s and state='candidate'",
                (target_state, self.realm_id, candidate_storage_id),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("Compiler candidate review state drift")
        return True

    def promote_reviewed_candidate(
        self,
        *,
        candidate_id: str,
        promotion_ref: str,
        promotion_digest: str,
        authorization_id: UUID,
        promoted_at: dt.datetime,
    ) -> bool:
        if not promotion_ref.strip() or len(promotion_ref) > 512:
            raise ValidationFailed("Compiler promotion ref bos veya fazla uzun olamaz")
        parse_digest(promotion_digest)
        if promoted_at.tzinfo is None or promoted_at.tzinfo.utcoffset(promoted_at) is None:
            raise ValidationFailed("Compiler promotion zamani timezone-aware olmali")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select id from memory.compiler_candidate"
                " where realm_id=%s and logical_candidate_id=%s for update",
                (self.realm_id, candidate_id),
            )
            candidate = cursor.fetchone()
            if candidate is None:
                raise NotFound("Compiler candidate bulunamadi")
            candidate_storage_id = UUID(str(candidate[0]))
            cursor.execute(
                "select authorization_id,promotion_ref,promotion_digest"
                " from memory.compiler_candidate_promotion"
                " where realm_id=%s and candidate_id=%s",
                (self.realm_id, candidate_storage_id),
            )
            replay = cursor.fetchone()
            if replay is not None:
                if (
                    UUID(str(replay[0])) != authorization_id
                    or str(replay[1]) != promotion_ref
                    or str(replay[2]) != promotion_digest
                ):
                    raise ConcurrencyConflict("Compiler candidate promotion replay drift")
                return False
            cursor.execute(
                "select id from memory.compiler_candidate_review"
                " where realm_id=%s and candidate_id=%s and decision='approved'",
                (self.realm_id, candidate_storage_id),
            )
            review = cursor.fetchone()
            if review is None:
                raise ValidationFailed("Compiler candidate approved review bulunamadi")
            cursor.execute(
                "insert into memory.compiler_candidate_promotion"
                " (id,realm_id,candidate_id,review_id,authorization_id,promotion_ref,"
                " promotion_digest,promoted_at,grants_authority)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,false)",
                (
                    new_uuid7(now=promoted_at),
                    self.realm_id,
                    candidate_storage_id,
                    UUID(str(review[0])),
                    authorization_id,
                    promotion_ref,
                    promotion_digest,
                    promoted_at,
                ),
            )
            cursor.execute(
                "update memory.compiler_candidate set state='promoted'"
                " where realm_id=%s and id=%s and state='reviewed'",
                (self.realm_id, candidate_storage_id),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("Compiler candidate promotion state drift")
        return True

    def record_gap(self, gap: GapRecoveryRecord) -> bool:
        if gap.realm_id != self.realm_id:
            raise ValidationFailed("Continuity gap repository realm binding drift")
        if gap.state == "resolved":
            raise ValidationFailed("Resolved gap yalniz resolve_gap ile yazilabilir")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select id,gap_code,recovery_ref,state from continuity.gap_recovery_reference"
                " where realm_id=%s and gap_ref=%s and evidence_digest=%s",
                (self.realm_id, gap.gap_ref, gap.evidence_digest),
            )
            replay = cursor.fetchone()
            if replay is not None:
                if tuple(str(item) for item in replay[1:]) != (
                    gap.gap_code,
                    gap.recovery_ref,
                    gap.state,
                ):
                    raise ConcurrencyConflict("Continuity gap replay drift")
                return False
            cursor.execute(
                "insert into continuity.gap_recovery_reference"
                " (id,realm_id,project_id,work_item_id,run_id,gap_code,gap_ref,evidence_digest,"
                " recovery_ref,recovery_receipt_ref,recovery_receipt_digest,state,created_at,"
                " resolved_at,grants_authority)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,false)",
                (
                    gap.id,
                    self.realm_id,
                    gap.project_id,
                    gap.work_item_id,
                    gap.run_id,
                    gap.gap_code,
                    gap.gap_ref,
                    gap.evidence_digest,
                    gap.recovery_ref,
                    gap.recovery_receipt_ref,
                    gap.recovery_receipt_digest,
                    gap.state,
                    gap.created_at,
                    gap.resolved_at,
                ),
            )
        return True

    def resolve_gap(
        self,
        *,
        gap_id: UUID,
        recovery_receipt_ref: str,
        recovery_receipt_digest: str,
        resolved_at: dt.datetime,
    ) -> bool:
        if not recovery_receipt_ref.strip() or len(recovery_receipt_ref) > 512:
            raise ValidationFailed("Gap recovery receipt ref bos veya fazla uzun olamaz")
        parse_digest(recovery_receipt_digest)
        if resolved_at.tzinfo is None or resolved_at.tzinfo.utcoffset(resolved_at) is None:
            raise ValidationFailed("Gap resolved_at timezone-aware olmali")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select state,recovery_receipt_ref,recovery_receipt_digest"
                " from continuity.gap_recovery_reference"
                " where realm_id=%s and id=%s for update",
                (self.realm_id, gap_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise NotFound("Continuity gap bulunamadi")
            if str(row[0]) == "resolved":
                if str(row[1]) != recovery_receipt_ref or str(row[2]) != recovery_receipt_digest:
                    raise ConcurrencyConflict("Continuity gap resolution replay drift")
                return False
            cursor.execute(
                "update continuity.gap_recovery_reference"
                " set state='resolved',recovery_receipt_ref=%s,recovery_receipt_digest=%s,"
                " resolved_at=%s where realm_id=%s and id=%s"
                " and state in ('open','recovery-required')",
                (
                    recovery_receipt_ref,
                    recovery_receipt_digest,
                    resolved_at,
                    self.realm_id,
                    gap_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("Continuity gap resolution state drift")
        return True

    def list_open_gaps(
        self, *, project_id: UUID, work_item_id: UUID
    ) -> tuple[GapRecoveryRecord, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,realm_id,project_id,work_item_id,run_id,gap_code,gap_ref,"
                " evidence_digest,recovery_ref,state,created_at,recovery_receipt_ref,"
                " recovery_receipt_digest,resolved_at"
                " from continuity.gap_recovery_reference where realm_id=%s and project_id=%s"
                " and work_item_id=%s and state<>'resolved' order by created_at,id",
                (self.realm_id, project_id, work_item_id),
            )
            return tuple(self._gap_from_row(row) for row in cursor.fetchall())

    def read_session_snapshot(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
    ) -> SessionContinuitySnapshot:
        params = (self.realm_id, project_id, work_item_id, run_id, session_id, client_id)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select receipt_digest,fresh,complete"
                " from continuity.session_hydration_receipt"
                " where realm_id=%s and project_id=%s and work_item_id=%s and run_id=%s"
                " and session_id=%s and client_id=%s order by created_at desc,id desc limit 1",
                params,
            )
            hydration = cursor.fetchone()
            cursor.execute(
                "select receipt_digest,close_status from continuity.session_close_receipt"
                " where realm_id=%s and project_id=%s and work_item_id=%s and run_id=%s"
                " and session_id=%s and client_id=%s order by created_at desc,id desc limit 1",
                params,
            )
            close = cursor.fetchone()
            cursor.execute(
                "select receipt_digest,status from continuity.compaction_receipt"
                " where realm_id=%s and project_id=%s and work_item_id=%s and run_id=%s"
                " and session_id=%s and client_id=%s order by created_at desc,id desc limit 1",
                params,
            )
            compaction = cursor.fetchone()
            cursor.execute(
                "select evaluation_digest,passed from continuity.memory_contract_evaluation"
                " where realm_id=%s and project_id=%s and work_item_id=%s and run_id=%s"
                " order by evaluated_at desc,id desc limit 1",
                params[:4],
            )
            evaluation = cursor.fetchone()
        return SessionContinuitySnapshot(
            run_id=run_id,
            hydration_receipt_digest=None if hydration is None else str(hydration[0]),
            hydration_fresh=False if hydration is None else bool(hydration[1]),
            hydration_complete=False if hydration is None else bool(hydration[2]),
            close_receipt_digest=None if close is None else str(close[0]),
            close_status=None if close is None else str(close[1]),
            compaction_receipt_digest=None if compaction is None else str(compaction[0]),
            compaction_status=None if compaction is None else str(compaction[1]),
            contract_evaluation_digest=(None if evaluation is None else str(evaluation[0])),
            contract_passed=False if evaluation is None else bool(evaluation[1]),
            open_gaps=self.list_open_gaps(project_id=project_id, work_item_id=work_item_id),
        )

    @staticmethod
    def _gap_from_row(row: Any) -> GapRecoveryRecord:
        return GapRecoveryRecord(
            id=UUID(str(row[0])),
            realm_id=UUID(str(row[1])),
            project_id=UUID(str(row[2])),
            work_item_id=UUID(str(row[3])),
            run_id=None if row[4] is None else UUID(str(row[4])),
            gap_code=str(row[5]),
            gap_ref=str(row[6]),
            evidence_digest=str(row[7]),
            recovery_ref=str(row[8]),
            state=str(row[9]),
            created_at=row[10],
            recovery_receipt_ref=row[11],
            recovery_receipt_digest=row[12],
            resolved_at=row[13],
        )

    def _store_receipt(
        self,
        *,
        table: str,
        receipt_id: UUID,
        idempotency_key: str,
        receipt_digest: str,
        identity: tuple[UUID, UUID, UUID, str, str],
        columns: tuple[str, ...],
        values: tuple[Any, ...],
        body: dict[str, Any],
        created_at: dt.datetime,
    ) -> bool:
        _idempotency_key(idempotency_key)
        parse_digest(receipt_digest)
        allowed = {
            "session_hydration_receipt",
            "session_close_receipt",
            "compaction_receipt",
        }
        if table not in allowed:
            raise ValidationFailed("Continuity receipt table allowlist disinda")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                f"select receipt_digest from continuity.{table}"
                " where realm_id=%s and idempotency_key=%s",
                (self.realm_id, idempotency_key),
            )
            replay = cursor.fetchone()
            if replay is not None:
                if str(replay[0]) != receipt_digest:
                    raise ConcurrencyConflict("Continuity receipt idempotency replay drift")
                return False
            column_sql = ",".join(columns)
            placeholders = ",".join("%s" for _ in columns)
            cursor.execute(
                f"insert into continuity.{table}"
                " (id,realm_id,project_id,work_item_id,run_id,session_id,client_id,"
                f" {column_sql},idempotency_key,receipt_body,receipt_digest,created_at,"
                " grants_authority)"
                f" values (%s,%s,%s,%s,%s,%s,%s,{placeholders},%s,%s::jsonb,%s,%s,false)",
                (
                    receipt_id,
                    self.realm_id,
                    *identity,
                    *values,
                    idempotency_key,
                    canonical_json(body),
                    receipt_digest,
                    created_at,
                ),
            )
        return True
