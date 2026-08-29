"""PostgreSQL persistence for typed session continuity and Memory Compiler records."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.application.continuity_projection import (
    ACTIVE_WORK_PROJECTION_REF,
    ProjectionReleaseSnapshot,
)
from zekam.application.memory_continuity_orchestrator import LifecycleCompilerRecord
from zekam.application.memory_upgrade import canonical_projection_source_digest
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConcurrencyConflict, NotFound, ValidationFailed
from zekam.domain.identifiers import new_uuid7
from zekam.domain.memory_compiler import MemoryCompilerOutput
from zekam.domain.memory_contract import MemoryContractEvaluation
from zekam.domain.session_continuity import (
    AUTO_HYDRATION_CLASSIFICATIONS,
    CompactionReceipt,
    ContextOmissionReference,
    DataClassification,
    DigestReference,
    HydrationInventoryEntry,
    HydrationInventorySnapshot,
    ProjectionGenerationReceipt,
    SessionCloseReceipt,
    SessionHydrationReceipt,
    SessionLifecycleEvent,
    TruthClass,
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
    claimed_at: dt.datetime


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
                "select id,state,project_id,work_item_id,run_id,source_set_digest,"
                " source_watermark,claimed_at"
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
                replay_identity = tuple(UUID(str(item)) for item in replay[2:5])
                if replay_identity != (project_id, work_item_id, run_id):
                    raise ConcurrencyConflict("Compiler watermark replay identity drift")
                if str(replay[5]) != source_set_digest or str(replay[6]) != source_watermark:
                    raise ConcurrencyConflict("Compiler watermark idempotency replay drift")
                return CompilerWatermarkClaim(
                    UUID(str(replay[0])), False, str(replay[1]), replay[7]
                )
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
        return CompilerWatermarkClaim(claim_id, True, "pending", claimed_at)

    def read_eligible_compiler_records(
        self,
        *,
        event_types: tuple[str, ...],
        classifications: tuple[str, ...],
        limit: int,
    ) -> tuple[LifecycleCompilerRecord, ...]:
        """Read a bounded, receipt-complete compiler batch without claiming it."""

        if not event_types or not classifications:
            return ()
        if not 1 <= limit <= 128:
            raise ValidationFailed("Compiler lifecycle read limiti 1..128 olmali")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select event.id,outbox.id,event.project_id,event.work_item_id,event.run_id,"
                " event.session_id,event.client_id,event.event_type,event.sequence,"
                " event.previous_digest,predecessor.event_digest,event.event_digest,"
                " event.event_body,event.event_body->>'source_revision',event.classification,"
                " hook.invocation_id,hook.structured_data,hook.input_digest,hook.receipt_id,"
                " hook.output_body,hook.output_digest,hook.receipt_count,"
                " outbox.terminal_receipt_digest,"
                " event.occurred_at,outbox.completed_at"
                " from continuity.lifecycle_delivery_outbox outbox"
                " join continuity.session_lifecycle_event event"
                " on event.realm_id=outbox.realm_id and event.id=outbox.event_id"
                " left join continuity.session_lifecycle_event predecessor"
                " on predecessor.realm_id=event.realm_id"
                " and predecessor.client_id=event.client_id"
                " and predecessor.session_id=event.session_id"
                " and predecessor.sequence=event.sequence-1"
                " join lateral ("
                "   select invocation.id invocation_id,"
                "   invocation.input_body->'data' structured_data,invocation.input_digest,"
                "   receipt.id receipt_id,receipt.output_body,receipt.output_digest,"
                "   count(*) over() receipt_count"
                "   from hooks.invocation invocation"
                "   join hooks.result_receipt receipt"
                "   on receipt.realm_id=invocation.realm_id"
                "   and receipt.invocation_id=invocation.id and receipt.status='completed'"
                "   join hooks.spec_revision spec on spec.realm_id=invocation.realm_id"
                "   and spec.id=invocation.spec_revision_id"
                "   where invocation.realm_id=event.realm_id"
                "   and invocation.event_type=event.event_type"
                "   and invocation.input_body->'lifecycle'->>'event_id'=event.id::text"
                "   and spec.source_layer='memory-continuity'"
                "   and receipt.output_body->'command'->>'compiler_enqueue'='true'"
                "   order by invocation.created_at,invocation.id limit 1"
                " ) hook on true"
                " where outbox.realm_id=%s and outbox.state='completed'"
                " and event.event_type=any(%s) and event.classification=any(%s)"
                " and not exists ("
                "   select 1 from memory.compiler_run compiler_run"
                "   cross join lateral jsonb_array_elements(compiler_run.source_set) source"
                "   where compiler_run.realm_id=event.realm_id"
                "   and source->>'ref'='hook-invocation:'||hook.invocation_id::text||':data'"
                " )"
                " order by event.project_id,event.work_item_id,event.run_id,event.session_id,"
                " event.sequence,event.id limit %s",
                (self.realm_id, list(event_types), list(classifications), limit),
            )
            rows = cursor.fetchall()
        records: list[LifecycleCompilerRecord] = []
        for row in rows:
            event_body = row[12]
            structured_data = row[16]
            hook_output = row[19]
            if not isinstance(event_body, dict):
                raise ValidationFailed("Compiler lifecycle event body object olmali")
            if not isinstance(structured_data, dict):
                raise ValidationFailed("Compiler lifecycle structured data object olmali")
            if not isinstance(hook_output, dict):
                raise ValidationFailed("Compiler lifecycle hook output object olmali")
            records.append(
                LifecycleCompilerRecord(
                    event_id=UUID(str(row[0])),
                    outbox_id=UUID(str(row[1])),
                    project_id=UUID(str(row[2])),
                    work_item_id=UUID(str(row[3])),
                    run_id=UUID(str(row[4])),
                    session_id=str(row[5]),
                    client_id=str(row[6]),
                    event_type=str(row[7]),
                    sequence=int(row[8]),
                    previous_digest=None if row[9] is None else str(row[9]),
                    predecessor_digest=None if row[10] is None else str(row[10]),
                    event_digest=str(row[11]),
                    event_body=event_body,
                    source_revision=str(row[13]),
                    classification=DataClassification(str(row[14])),
                    invocation_id=UUID(str(row[15])),
                    structured_data=structured_data,
                    input_digest=str(row[17]),
                    hook_receipt_id=UUID(str(row[18])),
                    hook_output=hook_output,
                    hook_output_digest=str(row[20]),
                    hook_receipt_count=int(row[21]),
                    lifecycle_receipt_digest=str(row[22]),
                    occurred_at=row[23],
                    completed_at=row[24],
                )
            )
        return tuple(records)

    def store_compiler_output(
        self,
        output: MemoryCompilerOutput,
        *,
        watermark_claim_id: UUID,
        completed_at: dt.datetime | None = None,
    ) -> bool:
        if output.realm_id != self.realm_id:
            raise ValidationFailed("Compiler output repository realm binding drift")
        terminal_at = completed_at or output.created_at
        if terminal_at.tzinfo is None or terminal_at.tzinfo.utcoffset(terminal_at) is None:
            raise ValidationFailed("Compiler terminal zamani timezone-aware olmali")
        source_set_body = [item.as_dict() for item in output.source_set]
        source_set_digest = digest(source_set_body)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select project_id,work_item_id,run_id,source_set_digest,source_watermark,state,"
                " compiler_run_id,result_digest,claimed_at from memory.compiler_watermark_claim"
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
            if terminal_at < claim[8]:
                raise ValidationFailed("Compiler terminal zamani claim oncesi olamaz")
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
                    terminal_at,
                ),
            )
            for candidate in output.candidates:
                candidate_id = new_uuid7(now=terminal_at)
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
                        terminal_at,
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
                                new_uuid7(now=terminal_at),
                                self.realm_id,
                                candidate_id,
                                relation_kind,
                                ordinal,
                                reference.ref,
                                reference.digest_value,
                                terminal_at,
                            ),
                        )
            cursor.execute(
                "update memory.compiler_watermark_claim set state='completed',compiler_run_id=%s,"
                " result_digest=%s,completed_at=%s where realm_id=%s and id=%s",
                (
                    output.output_id,
                    output.output_digest,
                    terminal_at,
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

    def record_compiler_gap(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        gap_code: str,
        gap_ref: str,
        evidence_digest: str,
        recovery_ref: str,
        observed_at: dt.datetime,
    ) -> bool:
        """Persist a sanitized compiler gap through the existing continuity ledger."""

        return self.record_gap(
            GapRecoveryRecord(
                id=new_uuid7(now=observed_at),
                realm_id=self.realm_id,
                project_id=project_id,
                work_item_id=work_item_id,
                run_id=run_id,
                gap_code=gap_code,
                gap_ref=gap_ref,
                evidence_digest=evidence_digest,
                recovery_ref=recovery_ref,
                state="recovery-required",
                created_at=observed_at,
            )
        )

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

    def _read_hydration_state(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
        preview_event_body: Mapping[str, Any] | None = None,
        preview_event_digest: str | None = None,
    ) -> tuple[HydrationInventorySnapshot, tuple[Any, ...]]:
        """Build one sanitized inventory from a single canonical DB statement.

        The latest execution envelope fixes source, policy, context and
        checkpoint bindings.  Fragment content is never selected here: only
        portable references, digests, bounded token counts and classifications
        leave PostgreSQL.
        """

        if not session_id.strip() or not client_id.strip():
            raise ValidationFailed("Hydration inventory session/client bos olamaz")
        if (preview_event_body is None) != (preview_event_digest is None):
            raise ValidationFailed("Hydration preview event body/digest birlikte ister")
        if (
            preview_event_body is not None
            and digest(dict(preview_event_body)) != preview_event_digest
        ):
            raise ValidationFailed("Hydration preview event digest drift")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select envelope.context_manifest_digest,envelope.context_packet_id,"
                " envelope.checkpoint_disposition,envelope.checkpoint_id,"
                " envelope.checkpoint_v2_id,source.revision,source.tree_digest,"
                " envelope.policy_digest,migration.version,"
                " models.capability_runtime_jsonb_digest(to_jsonb(migration.checksum)),"
                " task_plan.id,work_item.revision,work_item.state,work_item.record_digest,"
                " event.event_body,event.event_digest,manifest.omitted,fragments.entries,"
                " projection.id,projection.source_ref,projection.source_digest,"
                " projection.projection_ref,projection.projection_digest,"
                " projection.generator_version,projection.classification,"
                " projection.public_filtered,projection.receipt_body,"
                " projection.receipt_digest,projection.generated_at,"
                " projection.grants_authority,hydration.receipt_digest,"
                " hydration.receipt_body,hydration.fresh,hydration.complete,"
                " close_receipt.receipt_digest,close_receipt.close_status,"
                " compaction.receipt_digest,compaction.status,"
                " evaluation.evaluation_digest,evaluation.passed,gaps.entries"
                " from runtime.execution_run run"
                " join lateral (select current_envelope.*"
                " from runtime.execution_envelope current_envelope"
                " where current_envelope.realm_id=run.realm_id"
                " and current_envelope.run_id=run.id"
                " order by current_envelope.request_ordinal desc,"
                " current_envelope.created_at desc,current_envelope.id desc limit 1)"
                " envelope on true"
                " join work.task_plan task_plan on task_plan.realm_id=run.realm_id"
                " and task_plan.id=run.plan_id"
                " join work.work_item work_item on work_item.realm_id=run.realm_id"
                " and work_item.project_id=run.project_id"
                " and work_item.id=run.work_item_id"
                " join work.context_manifest manifest on manifest.realm_id=envelope.realm_id"
                " and manifest.id=envelope.context_manifest_id"
                " and manifest.manifest_digest=envelope.context_manifest_digest"
                " join projects.project project on project.realm_id=run.realm_id"
                " and project.id=run.project_id"
                " join projects.source_binding binding on binding.realm_id=project.realm_id"
                " and binding.project_id=project.id"
                " join lateral (select revision.revision,revision.tree_digest"
                " from projects.source_revision revision"
                " where revision.realm_id=binding.realm_id"
                " and revision.binding_id=binding.id"
                " order by revision.observed_at desc,revision.id desc limit 1) source on true"
                " join lateral (select version,checksum from core.schema_migrations"
                " order by version desc limit 1) migration on true"
                " join lateral (select lifecycle.event_body,lifecycle.event_digest,"
                " lifecycle.event_type from continuity.session_lifecycle_event lifecycle"
                " where lifecycle.realm_id=run.realm_id"
                " and lifecycle.project_id=run.project_id"
                " and lifecycle.work_item_id=run.work_item_id"
                " and lifecycle.run_id=run.id and lifecycle.session_id=run.session_id"
                " and lifecycle.client_id=run.client_id"
                " and lifecycle.event_type in ('session_start','hydration_required')"
                " and %s::jsonb is null"
                " union all select %s::jsonb,%s::text,'session_start'::text"
                " where %s::jsonb is not null limit 1) event"
                " on true"
                " join lateral (select jsonb_agg(jsonb_build_object("
                " 'candidate_id',fragment.candidate_id,'content_kind',fragment.content_kind,"
                " 'visibility',fragment.visibility,'authority',fragment.authority,"
                " 'source_ref',fragment.source_ref,'source_revision',fragment.source_revision,"
                " 'content_digest',fragment.content_digest,'token_count',fragment.token_count,"
                " 'required',fragment.required) order by fragment.fragment_order) entries"
                " from work.context_fragment fragment"
                " where fragment.realm_id=envelope.realm_id"
                " and fragment.context_manifest_id=envelope.context_manifest_id) fragments"
                " on fragments.entries is not null"
                " join lateral (select receipt.id,receipt.source_ref,receipt.source_digest,"
                " receipt.projection_ref,receipt.projection_digest,receipt.generator_version,"
                " receipt.classification,receipt.public_filtered,receipt.receipt_body,"
                " receipt.receipt_digest,receipt.generated_at,receipt.grants_authority"
                " from continuity.projection_generation_receipt receipt"
                " where receipt.realm_id=run.realm_id and receipt.project_id=run.project_id"
                " and receipt.work_item_id=run.work_item_id"
                " and receipt.projection_ref=%s"
                " order by receipt.generated_at desc,receipt.id desc limit 1) projection on true"
                " left join lateral (select receipt.receipt_digest,receipt.receipt_body,"
                " receipt.fresh,receipt.complete"
                " from continuity.session_hydration_receipt receipt"
                " where receipt.realm_id=run.realm_id and receipt.project_id=run.project_id"
                " and receipt.work_item_id=run.work_item_id and receipt.run_id=run.id"
                " and receipt.session_id=run.session_id and receipt.client_id=run.client_id"
                " order by receipt.created_at desc,receipt.id desc limit 1) hydration on true"
                " left join lateral (select receipt.receipt_digest,receipt.close_status"
                " from continuity.session_close_receipt receipt"
                " where receipt.realm_id=run.realm_id and receipt.project_id=run.project_id"
                " and receipt.work_item_id=run.work_item_id and receipt.run_id=run.id"
                " and receipt.session_id=run.session_id and receipt.client_id=run.client_id"
                " order by receipt.created_at desc,receipt.id desc limit 1) close_receipt on true"
                " left join lateral (select receipt.receipt_digest,receipt.status"
                " from continuity.compaction_receipt receipt"
                " where receipt.realm_id=run.realm_id and receipt.project_id=run.project_id"
                " and receipt.work_item_id=run.work_item_id and receipt.run_id=run.id"
                " and receipt.session_id=run.session_id and receipt.client_id=run.client_id"
                " order by receipt.created_at desc,receipt.id desc limit 1) compaction on true"
                " left join lateral (select result.evaluation_digest,result.passed"
                " from continuity.memory_contract_evaluation result"
                " where result.realm_id=run.realm_id and result.project_id=run.project_id"
                " and result.work_item_id=run.work_item_id and result.run_id=run.id"
                " order by result.evaluated_at desc,result.id desc limit 1) evaluation on true"
                " join lateral (select jsonb_agg(jsonb_build_object("
                " 'id',gap.id,'realm_id',gap.realm_id,'project_id',gap.project_id,"
                " 'work_item_id',gap.work_item_id,'run_id',gap.run_id,"
                " 'gap_code',gap.gap_code,'gap_ref',gap.gap_ref,"
                " 'evidence_digest',gap.evidence_digest,'recovery_ref',gap.recovery_ref,"
                " 'state',gap.state,'created_at',gap.created_at,"
                " 'recovery_receipt_ref',gap.recovery_receipt_ref,"
                " 'recovery_receipt_digest',gap.recovery_receipt_digest,"
                " 'resolved_at',gap.resolved_at) order by gap.created_at,gap.id) entries"
                " from continuity.gap_recovery_reference gap"
                " where gap.realm_id=run.realm_id and gap.project_id=run.project_id"
                " and gap.work_item_id=run.work_item_id and gap.state<>'resolved') gaps on true"
                " where run.realm_id=%s and run.project_id=%s and run.work_item_id=%s"
                " and run.id=%s and run.session_id=%s and run.client_id=%s"
                " and run.state='active' and envelope.source_revision=run.source_revision"
                " and envelope.source_revision=task_plan.source_revision"
                " and source.revision=(case when envelope.source_revision"
                " ~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'"
                " then substring(envelope.source_revision from 5 for 40)"
                " else envelope.source_revision end)"
                " and envelope.policy_digest=run.policy_digest"
                " and envelope.policy_digest=task_plan.policy_digest"
                " and task_plan.id=(select current_plan.id from work.task_plan current_plan"
                " where current_plan.realm_id=task_plan.realm_id"
                " and current_plan.work_item_id=task_plan.work_item_id"
                " order by current_plan.revision desc,current_plan.id desc limit 1)",
                (
                    None if preview_event_body is None else canonical_json(preview_event_body),
                    None if preview_event_body is None else canonical_json(preview_event_body),
                    preview_event_digest,
                    None if preview_event_body is None else canonical_json(preview_event_body),
                    ACTIVE_WORK_PROJECTION_REF,
                    self.realm_id,
                    project_id,
                    work_item_id,
                    run_id,
                    session_id,
                    client_id,
                ),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise NotFound("Hydration canonical inventory exact scope'ta cozulmedi")
        row = rows[0]
        event_body = dict(row[14] or {})
        event_digest = str(row[15])
        if digest(event_body) != event_digest:
            raise ValidationFailed("Hydration lifecycle event digest drift")
        expected_plan_ref = f"work-plan:{row[10]}"
        if str(event_body.get("plan_ref")) != expected_plan_ref:
            raise ValidationFailed("Hydration lifecycle current Work plan ref drift")
        checkpoint_disposition = str(row[2])
        if checkpoint_disposition == "bound":
            checkpoint_ref = f"checkpoint:{row[3]}"
        elif checkpoint_disposition == "bound-v2":
            checkpoint_ref = f"checkpoint-v2:{row[4]}"
        elif checkpoint_disposition == "not-applicable-genesis":
            checkpoint_ref = f"run:{run_id}:genesis"
        else:
            raise ValidationFailed("Hydration checkpoint disposition gecersiz")
        event_checkpoint = event_body.get("checkpoint_ref")
        if event_checkpoint is not None and str(event_checkpoint) != checkpoint_ref:
            raise ValidationFailed("Hydration lifecycle checkpoint ref drift")

        database_revision_digest = digest(
            {
                "project_id": str(project_id),
                "work_item_id": str(work_item_id),
                "work_revision": int(row[11]),
                "work_state": str(row[12]),
                "work_record_digest": str(row[13]),
            }
        )
        projection_source_digest = canonical_projection_source_digest(
            source_head=str(row[5]),
            source_tree_digest=str(row[6]),
            migration_head=int(row[8]),
            database_revision_digest=database_revision_digest,
        )
        projection_body = {
            "schema": "zekam-memory-continuity-public-projection/v1",
            "project_id": str(project_id),
            "work_item_id": str(work_item_id),
            "work_revision": int(row[11]),
            "work_state": str(row[12]),
            "source_head": str(row[5]),
            "source_tree_digest": str(row[6]),
            "migration_head": int(row[8]),
            "database_revision_digest": database_revision_digest,
            "source_digest": projection_source_digest,
            "classification": "public",
            "public_filtered": True,
            "content_included": False,
            "fresh": True,
            "read_only": True,
            "grants_authority": False,
        }
        expected_projection_digest = digest(projection_body)
        expected_source_ref = f"work-item/{work_item_id}/revision/{int(row[11])}"
        try:
            projection_receipt = ProjectionGenerationReceipt(
                receipt_id=UUID(str(row[18])),
                realm_id=self.realm_id,
                project_id=project_id,
                work_item_id=work_item_id,
                source_ref=str(row[19]),
                source_digest=str(row[20]),
                projection_ref=str(row[21]),
                projection_digest=str(row[22]),
                generator_version=str(row[23]),
                generated_at=row[28],
                classification=DataClassification(str(row[24])),
                public_filtered=bool(row[25]),
                grants_authority=bool(row[29]),
            )
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("Hydration active-work projection security drift") from exc
        if (
            projection_receipt.source_ref != expected_source_ref
            or projection_receipt.source_digest != projection_source_digest
            or projection_receipt.projection_ref != ACTIVE_WORK_PROJECTION_REF
            or projection_receipt.projection_digest != expected_projection_digest
            or projection_receipt.receipt_digest != str(row[27])
            or digest(dict(row[26] or {})) != str(row[27])
        ):
            raise ValidationFailed(
                "Hydration active-work projection canonical source/body/security drift"
            )

        inventory_entries: list[HydrationInventoryEntry] = []
        for document in tuple(row[17] or ()):
            item = dict(document)
            content_kind = str(item["content_kind"])
            visibility = str(item["visibility"])
            if content_kind in {"user-message", "assistant-message"}:
                classification = DataClassification.RAW_TRANSCRIPT
            elif content_kind == "tool-result" or visibility in {
                "runtime-only",
                "diagnostic-only",
            }:
                classification = DataClassification.DIAGNOSTIC_PAYLOAD
            else:
                classification = DataClassification.INTERNAL
            authority = int(item["authority"])
            truth_class = (
                TruthClass.REPO_FACT
                if authority >= 3
                else TruthClass.EXTERNAL_VERIFIED_FACT
                if authority == 2
                else TruthClass.UNKNOWN
            )
            inventory_entries.append(
                HydrationInventoryEntry(
                    ref=f"context/{item['candidate_id']}",
                    content_digest=str(item["content_digest"]),
                    token_count=int(item["token_count"]),
                    truth_class=truth_class,
                    classification=classification,
                    required=bool(item["required"]),
                    source_ref=str(item["source_ref"]),
                    source_revision=str(item["source_revision"]),
                )
            )
        omissions = tuple(
            ContextOmissionReference(
                f"context/{dict(item)['candidate_id']}",
                str(dict(item)["reason"]),
                required=False,
            )
            for item in tuple(row[16] or ())
        )
        projection_refs = (
            DigestReference(
                ACTIVE_WORK_PROJECTION_REF,
                expected_projection_digest,
                TruthClass.REPO_FACT,
            ),
        )
        snapshot = HydrationInventorySnapshot(
            realm_id=self.realm_id,
            project_id=project_id,
            work_item_id=work_item_id,
            run_id=run_id,
            session_id=session_id,
            client_id=client_id,
            plan_ref=expected_plan_ref,
            checkpoint_ref=checkpoint_ref,
            source_digest=str(row[6]),
            policy_digest=str(row[7]),
            migration_digest=str(row[9]),
            context_digest=str(row[0]),
            entries=tuple(inventory_entries),
            known_omissions=omissions,
            projection_refs=projection_refs,
            hydration_event_digest=event_digest,
        )
        return snapshot, tuple(row)

    def preview_hydration_inventory(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
        event_body: Mapping[str, Any],
        event_digest: str,
    ) -> HydrationInventorySnapshot:
        """Derive exact hydration inputs without persisting the prepared lifecycle event."""

        snapshot, _ = self._read_hydration_state(
            project_id=project_id,
            work_item_id=work_item_id,
            run_id=run_id,
            session_id=session_id,
            client_id=client_id,
            preview_event_body=event_body,
            preview_event_digest=event_digest,
        )
        return snapshot

    def read_hydration_inventory(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
    ) -> HydrationInventorySnapshot:
        snapshot, _ = self._read_hydration_state(
            project_id=project_id,
            work_item_id=work_item_id,
            run_id=run_id,
            session_id=session_id,
            client_id=client_id,
        )
        return snapshot

    @staticmethod
    def _hydration_receipt_is_current(
        *,
        receipt_digest: str,
        receipt_body: dict[str, Any],
        stored_fresh: bool,
        stored_complete: bool,
        inventory: HydrationInventorySnapshot,
    ) -> tuple[bool, bool]:
        """Recompute currentness; persisted convenience booleans never grant admission."""

        try:
            identity_matches = (
                str(receipt_body["realm_id"]) == str(inventory.realm_id)
                and str(receipt_body["project_id"]) == str(inventory.project_id)
                and str(receipt_body["work_item_id"]) == str(inventory.work_item_id)
                and str(receipt_body["run_id"]) == str(inventory.run_id)
                and str(receipt_body["session_id"]) == inventory.session_id
                and str(receipt_body["client_id"]) == inventory.client_id
            )
            bindings_match = all(
                str(receipt_body[name]) == expected
                for name, expected in (
                    ("plan_ref", inventory.plan_ref),
                    ("checkpoint_ref", inventory.checkpoint_ref),
                    ("source_digest", inventory.source_digest),
                    ("policy_digest", inventory.policy_digest),
                    ("migration_digest", inventory.migration_digest),
                    ("inventory_digest", inventory.inventory_digest),
                    ("context_digest", inventory.context_digest),
                    ("hydration_event_digest", inventory.hydration_event_digest),
                )
            )
            projection_matches = receipt_body["projection_refs"] == [
                item.as_dict() for item in inventory.projection_refs
            ]
            freshness_matches = receipt_body["freshness"] == [
                item.as_dict() for item in inventory.freshness
            ]
            entries = {item.ref: item for item in inventory.entries}
            required = tuple(receipt_body["required_selections"])
            optional = tuple(receipt_body["optional_selections"])
            required_expected = {
                item.ref
                for item in inventory.entries
                if item.required and item.classification in AUTO_HYDRATION_CLASSIFICATIONS
            }
            required_actual = {str(dict(item)["ref"]) for item in required}
            selections_current = required_expected == required_actual and all(
                (
                    str(dict(item)["ref"]) in entries
                    and entries[str(dict(item)["ref"])].selection.as_dict() == dict(item)
                    and entries[str(dict(item)["ref"])].classification
                    in AUTO_HYDRATION_CLASSIFICATIONS
                )
                for item in required + optional
            )
            omissions = tuple(receipt_body["omissions"])
            complete_now = (
                stored_complete
                and receipt_body["complete"] is True
                and not any(bool(dict(item).get("required")) for item in omissions)
            )
            current_now = (
                stored_fresh
                and receipt_body["fresh"] is True
                and digest(receipt_body) == receipt_digest
                and identity_matches
                and bindings_match
                and projection_matches
                and freshness_matches
                and selections_current
                and all(item.current for item in inventory.freshness)
            )
            return current_now, complete_now
        except (KeyError, TypeError, ValueError):
            return False, False

    def read_session_snapshot(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
    ) -> SessionContinuitySnapshot:
        # Inventory, stored receipt and every blocker come from one PostgreSQL
        # statement snapshot; persisted fresh/complete flags are then narrowed
        # by the recomputed canonical inventory and can never fail open.
        inventory, state = self._read_hydration_state(
            project_id=project_id,
            work_item_id=work_item_id,
            run_id=run_id,
            session_id=session_id,
            client_id=client_id,
        )
        hydration = None if state[30] is None else state[30:34]
        close = None if state[34] is None else state[34:36]
        compaction = None if state[36] is None else state[36:38]
        evaluation = None if state[38] is None else state[38:40]

        def timestamp(value: Any) -> dt.datetime | None:
            if value is None or isinstance(value, dt.datetime):
                return value
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValidationFailed("Continuity gap timestamp timezone-aware olmali")
            return parsed

        open_gaps = tuple(
            self._gap_from_row(
                (
                    item["id"],
                    item["realm_id"],
                    item["project_id"],
                    item["work_item_id"],
                    item.get("run_id"),
                    item["gap_code"],
                    item["gap_ref"],
                    item["evidence_digest"],
                    item["recovery_ref"],
                    item["state"],
                    timestamp(item["created_at"]),
                    item.get("recovery_receipt_ref"),
                    item.get("recovery_receipt_digest"),
                    timestamp(item.get("resolved_at")),
                )
            )
            for item in (dict(document) for document in tuple(state[40] or ()))
        )
        hydration_fresh = False
        hydration_complete = False
        if hydration is not None:
            hydration_fresh, hydration_complete = self._hydration_receipt_is_current(
                receipt_digest=str(hydration[0]),
                receipt_body=dict(hydration[1] or {}),
                stored_fresh=bool(hydration[2]),
                stored_complete=bool(hydration[3]),
                inventory=inventory,
            )
        return SessionContinuitySnapshot(
            run_id=run_id,
            hydration_receipt_digest=None if hydration is None else str(hydration[0]),
            hydration_fresh=hydration_fresh,
            hydration_complete=hydration_complete,
            close_receipt_digest=None if close is None else str(close[0]),
            close_status=None if close is None else str(close[1]),
            compaction_receipt_digest=None if compaction is None else str(compaction[0]),
            compaction_status=None if compaction is None else str(compaction[1]),
            contract_evaluation_digest=(None if evaluation is None else str(evaluation[0])),
            contract_passed=False if evaluation is None else bool(evaluation[1]),
            open_gaps=open_gaps,
        )

    def read_projection_release_snapshot(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
    ) -> ProjectionReleaseSnapshot:
        """Read the exact close/release gate inputs from one PostgreSQL snapshot.

        The caller owns the surrounding transaction.  In particular, ``apply``
        invokes this reader after entering its effect transaction, so all Work,
        source, migration, projection, and lifecycle checks are compared against
        the same database view before the authorization is consumed.
        """

        if not session_id.strip() or not client_id.strip():
            raise ValidationFailed("Projection release session/client bos olamaz")
        pending: list[str] = []
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select state from runtime.execution_run"
                " where realm_id=%s and project_id=%s and work_item_id=%s and id=%s",
                (self.realm_id, project_id, work_item_id, run_id),
            )
            run = cursor.fetchone()
            if run is None:
                raise NotFound("Projection release exact execution run bulunamadi")

            cursor.execute(
                "select revision,state,record_digest from work.work_item"
                " where realm_id=%s and project_id=%s and id=%s",
                (self.realm_id, project_id, work_item_id),
            )
            work = cursor.fetchone()
            if work is None:
                raise NotFound("Projection release exact Work bulunamadi")
            work_revision = int(work[0])
            work_state = str(work[1])
            work_record_digest = str(work[2])

            cursor.execute(
                "select revision.revision,revision.tree_digest"
                " from projects.source_revision revision"
                " join projects.source_binding binding"
                " on binding.realm_id=revision.realm_id and binding.id=revision.binding_id"
                " where binding.realm_id=%s and binding.project_id=%s"
                " order by revision.observed_at desc,revision.id desc limit 1",
                (self.realm_id, project_id),
            )
            source = cursor.fetchone()
            if source is None:
                raise NotFound("Projection release canonical source revision bulunamadi")
            source_head = str(source[0])
            source_tree_digest = str(source[1])

            cursor.execute("select coalesce(max(version),0) from core.schema_migrations")
            migration_head = int(cursor.fetchone()[0])
            if migration_head < 1:
                raise NotFound("Projection release migration head bulunamadi")

            database_revision_digest = digest(
                {
                    "project_id": str(project_id),
                    "work_item_id": str(work_item_id),
                    "work_revision": work_revision,
                    "work_state": work_state,
                    "work_record_digest": work_record_digest,
                }
            )
            cursor.execute(
                "select receipt_digest,projection_digest,source_digest"
                " from continuity.projection_generation_receipt"
                " where realm_id=%s and project_id=%s and work_item_id=%s"
                " and projection_ref=%s"
                " order by generated_at desc,id desc limit 1",
                (
                    self.realm_id,
                    project_id,
                    work_item_id,
                    ACTIVE_WORK_PROJECTION_REF,
                ),
            )
            projection = cursor.fetchone()
            if projection is None:
                raise NotFound("Projection release active-work receipt bulunamadi")

            identity = (
                self.realm_id,
                project_id,
                work_item_id,
                run_id,
                session_id,
                client_id,
            )
            cursor.execute(
                "select fresh,complete from continuity.session_hydration_receipt"
                " where realm_id=%s and project_id=%s and work_item_id=%s and run_id=%s"
                " and session_id=%s and client_id=%s"
                " order by created_at desc,id desc limit 1",
                identity,
            )
            hydration = cursor.fetchone()
            if hydration is None:
                pending.append("hydration-missing")
            else:
                if not bool(hydration[0]):
                    pending.append("hydration-stale")
                if not bool(hydration[1]):
                    pending.append("hydration-incomplete")

            cursor.execute(
                "select count(*) from continuity.gap_recovery_reference"
                " where realm_id=%s and project_id=%s and work_item_id=%s"
                " and state<>'resolved' and (run_id is null or run_id=%s)",
                (self.realm_id, project_id, work_item_id, run_id),
            )
            open_gap_count = int(cursor.fetchone()[0])
            if open_gap_count:
                pending.append(f"open-gaps:{open_gap_count}")

            cursor.execute(
                "select event.id,event.event_type,event.sequence,event.previous_digest,"
                " event.event_body,event.event_digest,outbox.id,outbox.plan_digest,"
                " outbox.payload_digest,outbox.state,outbox.terminal_receipt_digest"
                " from continuity.session_lifecycle_event event"
                " left join continuity.lifecycle_delivery_outbox outbox"
                " on outbox.realm_id=event.realm_id and outbox.event_id=event.id"
                " where event.realm_id=%s and event.project_id=%s"
                " and event.work_item_id=%s and event.run_id=%s"
                " and event.session_id=%s and event.client_id=%s"
                " order by event.sequence,event.id",
                identity,
            )
            lifecycle = cursor.fetchall()
            pre_close_outbox_id: UUID | None = None
            predecessor_digest: str | None = None
            for expected_sequence, row in enumerate(lifecycle, start=1):
                sequence = int(row[2])
                previous_digest = None if row[3] is None else str(row[3])
                event_digest = str(row[5])
                if sequence != expected_sequence:
                    pending.append("lifecycle-sequence-gap")
                if previous_digest != predecessor_digest:
                    pending.append("lifecycle-previous-digest-mismatch")
                if event_digest != digest(dict(row[4] or {})):
                    pending.append("lifecycle-event-digest-mismatch")
                if row[6] is None:
                    pending.append("lifecycle-outbox-missing")
                predecessor_digest = event_digest
            if not lifecycle or str(lifecycle[-1][1]) != "pre_close":
                pending.append("pre-close-not-current")
            else:
                latest = lifecycle[-1]
                if latest[6] is not None:
                    pre_close_outbox_id = UUID(str(latest[6]))
                    expected_payload = digest(
                        {
                            "event_digest": str(latest[5]),
                            "plan_digest": str(latest[7]),
                        }
                    )
                    if str(latest[8]) != expected_payload:
                        pending.append("pre-close-outbox-payload-drift")
                    if str(latest[9]) not in {"pending", "processing"}:
                        pending.append("pre-close-outbox-not-open")
                    if latest[10] is not None:
                        pending.append("pre-close-outbox-already-finalized")

            cursor.execute(
                "select state,count(*) from continuity.lifecycle_delivery_outbox outbox"
                " join continuity.session_lifecycle_event event"
                " on event.realm_id=outbox.realm_id and event.id=outbox.event_id"
                " where event.realm_id=%s and event.project_id=%s"
                " and event.work_item_id=%s and event.run_id=%s"
                " and event.session_id=%s and event.client_id=%s"
                " and outbox.state<>'completed' and (%s::uuid is null or outbox.id<>%s)"
                " group by state order by state",
                (*identity, pre_close_outbox_id, pre_close_outbox_id),
            )
            for state, count in cursor.fetchall():
                pending.append(f"lifecycle-outbox-{state}:{int(count)}")

            cursor.execute(
                "select status from continuity.compaction_receipt"
                " where realm_id=%s and project_id=%s and work_item_id=%s and run_id=%s"
                " and session_id=%s and client_id=%s"
                " order by created_at desc,id desc limit 1",
                identity,
            )
            compaction = cursor.fetchone()
            if compaction is not None and str(compaction[0]) != "completed":
                pending.append(f"compaction-{compaction[0]!s}")

            cursor.execute(
                "select state,count(*) from memory.compiler_watermark_claim"
                " where realm_id=%s and project_id=%s and work_item_id=%s and run_id=%s"
                " and state<>'completed' group by state order by state",
                (self.realm_id, project_id, work_item_id, run_id),
            )
            for state, count in cursor.fetchall():
                pending.append(f"compiler-{state}:{int(count)}")

        pending_steps = tuple(sorted(set(pending)))
        return ProjectionReleaseSnapshot(
            project_id=project_id,
            work_item_id=work_item_id,
            work_revision=work_revision,
            work_state=work_state,
            work_record_digest=work_record_digest,
            source_head=source_head,
            source_tree_digest=source_tree_digest,
            migration_head=migration_head,
            database_revision_digest=database_revision_digest,
            projection_ref=ACTIVE_WORK_PROJECTION_REF,
            projection_receipt_digest=str(projection[0]),
            projection_digest=str(projection[1]),
            projection_source_digest=str(projection[2]),
            lifecycle_complete=not pending_steps,
            pending_lifecycle_steps=pending_steps,
            next_safe_action=None if work_state == "completed" else "continue-current-work",
            grants_authority=False,
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
