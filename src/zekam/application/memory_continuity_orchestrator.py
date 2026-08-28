"""Deterministic lifecycle-to-candidate orchestration.

The hook half of this module is pure: it emits a bounded command and cannot
write, call a provider, promote memory, or grant authority.  The worker half
consumes only terminal PostgreSQL lifecycle/hook receipts and persists
candidate-only compiler output behind a durable watermark claim.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid5

from zekam.application.memory_candidate_compiler import (
    CompilerDurabilityReceipt,
    CompilerSourceFragment,
    CompilerSourceKind,
    MemoryCandidateCompiler,
)
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.hook_runtime import HookEventType
from zekam.domain.memory_compiler import CompilerCandidateType, MemoryCompilerOutput
from zekam.domain.memory_policy import MemoryContinuityPolicy
from zekam.domain.policy import RiskLevel
from zekam.domain.session_continuity import (
    DataClassification,
    DigestReference,
    TruthClass,
)

COMPILER_JOB_NAME = "memory-candidate-compile"
COMPILER_BATCH_LIMIT = 128
COMPILER_CLAIM_STALE_AFTER = dt.timedelta(minutes=5)
_OUTPUT_NAMESPACE = UUID("3fa130f7-a6b3-47cd-a8ae-49f652538c0a")
_PARSER_DIGEST = digest({"parser": "memory-lifecycle-structured-delta", "revision": 1})
_PROFILE_DIGEST = digest(
    {
        "profile": "deterministic-candidate-only",
        "provider_calls": 0,
        "direct_promotion": False,
        "max_sources": COMPILER_BATCH_LIMIT,
    }
)


class MemoryLearningAction(StrEnum):
    HYDRATE = "hydrate"
    CHECKPOINT = "checkpoint"
    COMPACTION_BOUNDARY = "compaction-boundary"
    VERIFY_CHECKPOINT_LINK = "verify-checkpoint-link"
    CLOSE_OBSERVATION = "close-observation"
    COMPILER_ENQUEUE = "compiler-enqueue"
    PROJECTION_CATCH_UP = "projection-catch-up"
    GAP_OBSERVATION = "gap-observation"


_HOOK_ACTIONS: dict[HookEventType, tuple[MemoryLearningAction, ...]] = {
    HookEventType.CONTINUITY_SESSION_START: (MemoryLearningAction.HYDRATE,),
    HookEventType.HYDRATION_REQUIRED: (MemoryLearningAction.HYDRATE,),
    HookEventType.HYDRATION_COMPLETED: (),
    HookEventType.PRE_TASK: (),
    HookEventType.POST_TASK: (
        MemoryLearningAction.CHECKPOINT,
        MemoryLearningAction.COMPILER_ENQUEUE,
    ),
    HookEventType.PRE_COMPACTION: (
        MemoryLearningAction.CHECKPOINT,
        MemoryLearningAction.COMPACTION_BOUNDARY,
        MemoryLearningAction.COMPILER_ENQUEUE,
    ),
    HookEventType.POST_COMPACTION: (
        MemoryLearningAction.VERIFY_CHECKPOINT_LINK,
        MemoryLearningAction.COMPILER_ENQUEUE,
        MemoryLearningAction.PROJECTION_CATCH_UP,
    ),
    HookEventType.PRE_CLOSE: (
        MemoryLearningAction.CHECKPOINT,
        MemoryLearningAction.CLOSE_OBSERVATION,
        MemoryLearningAction.COMPILER_ENQUEUE,
    ),
    HookEventType.POST_CLOSE: (
        MemoryLearningAction.COMPILER_ENQUEUE,
        MemoryLearningAction.PROJECTION_CATCH_UP,
    ),
    HookEventType.ON_FAILURE: (
        MemoryLearningAction.GAP_OBSERVATION,
        MemoryLearningAction.COMPILER_ENQUEUE,
    ),
    HookEventType.ON_VALIDATION_FAILURE: (
        MemoryLearningAction.GAP_OBSERVATION,
        MemoryLearningAction.COMPILER_ENQUEUE,
    ),
    HookEventType.ON_MEMORY_WRITE_FAILURE: (
        MemoryLearningAction.GAP_OBSERVATION,
        MemoryLearningAction.COMPILER_ENQUEUE,
    ),
    HookEventType.ON_MEMORY_HYDRATION_FAILURE: (
        MemoryLearningAction.GAP_OBSERVATION,
        MemoryLearningAction.COMPILER_ENQUEUE,
    ),
    HookEventType.ON_SKILL_CANDIDATE: (MemoryLearningAction.COMPILER_ENQUEUE,),
    HookEventType.ON_SKILL_UPDATE: (
        MemoryLearningAction.COMPILER_ENQUEUE,
        MemoryLearningAction.PROJECTION_CATCH_UP,
    ),
    HookEventType.ON_STATE_DRIFT: (
        MemoryLearningAction.GAP_OBSERVATION,
        MemoryLearningAction.COMPILER_ENQUEUE,
    ),
    HookEventType.UNCLEAN_EXIT: (
        MemoryLearningAction.GAP_OBSERVATION,
        MemoryLearningAction.COMPILER_ENQUEUE,
    ),
}

COMPILER_EVENT_TYPES: tuple[str, ...] = tuple(
    sorted(
        event.value
        for event, actions in _HOOK_ACTIONS.items()
        if MemoryLearningAction.COMPILER_ENQUEUE in actions
    )
)

_FAILURE_EVENTS = frozenset(
    {
        HookEventType.ON_FAILURE,
        HookEventType.ON_VALIDATION_FAILURE,
        HookEventType.ON_MEMORY_WRITE_FAILURE,
        HookEventType.ON_MEMORY_HYDRATION_FAILURE,
        HookEventType.UNCLEAN_EXIT,
    }
)
_SKILL_EVENTS = frozenset({HookEventType.ON_SKILL_CANDIDATE, HookEventType.ON_SKILL_UPDATE})
_PROJECTION_EVENTS = frozenset(
    {
        HookEventType.PRE_COMPACTION,
        HookEventType.POST_COMPACTION,
        HookEventType.PRE_CLOSE,
        HookEventType.POST_CLOSE,
    }
)


@dataclass(frozen=True, slots=True)
class MemoryLearningCommand:
    event_type: HookEventType
    event_ref: str
    event_digest: str
    actions: tuple[MemoryLearningAction, ...]
    grants_authority: bool = False

    def __post_init__(self) -> None:
        parse_digest(self.event_digest)
        if not self.event_ref.startswith("continuity-event:"):
            raise ValidationFailed("Memory learning command event ref gecersiz")
        expected_actions = _HOOK_ACTIONS.get(self.event_type)
        if expected_actions is None or self.actions != expected_actions:
            raise ValidationFailed("Memory learning command action matrisi drift etti")
        if self.grants_authority:
            raise PolicyViolation("Memory learning command authority uretemez")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-learning-command/v1",
            "event_type": self.event_type.value,
            "event_ref": self.event_ref,
            "event_digest": self.event_digest,
            "actions": [item.value for item in self.actions],
            "compiler_enqueue": MemoryLearningAction.COMPILER_ENQUEUE in self.actions,
            "provider_calls": 0,
            "direct_promotion": False,
            "grants_authority": False,
        }

    @property
    def command_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"command_digest": self.command_digest}


def plan_memory_hook(
    event_type: HookEventType, payload: Mapping[str, Any]
) -> MemoryLearningCommand:
    """Translate a content-safe hook payload into one authority-free command."""

    lifecycle = payload.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        raise ValidationFailed("Memory hook lifecycle object ister")
    if lifecycle.get("event_type") != event_type.value:
        raise PolicyViolation("Memory hook event type/lifecycle binding drift")
    event_id = str(lifecycle.get("event_id", "")).strip()
    try:
        UUID(event_id)
    except ValueError as exc:
        raise ValidationFailed("Memory hook canonical event UUID ister") from exc
    if event_type not in _HOOK_ACTIONS:
        raise PolicyViolation("Memory hook continuity action registry disinda")
    lifecycle_body = dict(lifecycle)
    return MemoryLearningCommand(
        event_type=event_type,
        event_ref=f"continuity-event:{event_id}",
        event_digest=digest(lifecycle_body),
        actions=_HOOK_ACTIONS[event_type],
    )


@dataclass(frozen=True, slots=True)
class LifecycleCompilerRecord:
    event_id: UUID
    outbox_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID
    session_id: str
    client_id: str
    event_type: str
    sequence: int
    previous_digest: str | None
    predecessor_digest: str | None
    event_digest: str
    event_body: Mapping[str, Any]
    source_revision: str
    classification: DataClassification
    invocation_id: UUID
    structured_data: Mapping[str, Any]
    input_digest: str
    hook_receipt_id: UUID
    hook_output: Mapping[str, Any]
    hook_output_digest: str
    hook_receipt_count: int
    lifecycle_receipt_digest: str
    occurred_at: dt.datetime
    completed_at: dt.datetime

    def __post_init__(self) -> None:
        if self.event_type not in COMPILER_EVENT_TYPES:
            raise ValidationFailed("Compiler lifecycle event registry disinda")
        if self.sequence < 1:
            raise ValidationFailed("Compiler lifecycle sequence pozitif olmali")
        if not self.session_id.strip() or not self.client_id.strip():
            raise ValidationFailed("Compiler lifecycle session/client binding ister")
        if self.hook_receipt_count < 1:
            raise PolicyViolation("Compiler lifecycle terminal hook receipt ister")
        for value in (
            self.event_digest,
            self.input_digest,
            self.hook_output_digest,
            self.lifecycle_receipt_digest,
        ):
            parse_digest(value)
        if digest(self.event_body) != self.event_digest:
            raise PolicyViolation("Compiler lifecycle event body/digest drift")
        event_binding = (
            str(self.event_id),
            self.session_id,
            self.client_id,
            self.event_type,
            self.sequence,
            self.previous_digest,
            self.source_revision,
            self.classification.value,
        )
        body_binding = (
            str(self.event_body.get("event_id", "")),
            self.event_body.get("session_id"),
            self.event_body.get("client_id"),
            self.event_body.get("event_type"),
            self.event_body.get("sequence"),
            self.event_body.get("previous_digest"),
            self.event_body.get("source_revision"),
            self.event_body.get("classification"),
        )
        if body_binding != event_binding:
            raise PolicyViolation("Compiler lifecycle event identity binding drift")
        hook_input = {"lifecycle": dict(self.event_body), "data": dict(self.structured_data)}
        if digest(hook_input) != self.input_digest:
            raise PolicyViolation("Compiler lifecycle hook input/digest drift")
        if digest(self.structured_data) != self.event_body.get("payload_digest"):
            raise PolicyViolation("Compiler lifecycle structured payload/digest drift")
        if digest(self.hook_output) != self.hook_output_digest:
            raise PolicyViolation("Compiler lifecycle hook output/digest drift")
        expected_command = plan_memory_hook(HookEventType(self.event_type), hook_input)
        expected_output = {
            "event_type": self.event_type,
            "accepted": True,
            "command": expected_command.body(),
            "command_digest": expected_command.command_digest,
            "grants_authority": False,
        }
        if dict(self.hook_output) != expected_output:
            raise PolicyViolation("Compiler lifecycle hook command receipt drift")
        if self.previous_digest is not None:
            parse_digest(self.previous_digest)
        if self.predecessor_digest is not None:
            parse_digest(self.predecessor_digest)
        for timestamp in (self.occurred_at, self.completed_at):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValidationFailed("Compiler lifecycle zamani timezone-aware olmali")

    @property
    def chain_current(self) -> bool:
        if self.sequence == 1:
            return self.previous_digest is None and self.predecessor_digest is None
        return self.previous_digest is not None and self.previous_digest == self.predecessor_digest

    @property
    def receipt_current(self) -> bool:
        return self.hook_receipt_count == 1

    @property
    def source_ref(self) -> DigestReference:
        return DigestReference(
            f"hook-invocation:{self.invocation_id}:data",
            digest(self.structured_data),
            TruthClass.TEMPORARY_ASSUMPTION,
        )

    @property
    def evidence_refs(self) -> tuple[DigestReference, ...]:
        return tuple(
            sorted(
                (
                    DigestReference(
                        f"continuity-event:{self.event_id}",
                        self.event_digest,
                        TruthClass.REPO_FACT,
                    ),
                    DigestReference(
                        f"hook-output:{self.hook_receipt_id}",
                        self.hook_output_digest,
                        TruthClass.REPO_FACT,
                    ),
                    DigestReference(
                        f"continuity-outbox-terminal:{self.outbox_id}",
                        self.lifecycle_receipt_digest,
                        TruthClass.REPO_FACT,
                    ),
                ),
                key=lambda item: (item.ref, item.digest_value),
            )
        )


class CompilerClaim(Protocol):
    @property
    def claim_id(self) -> UUID: ...

    @property
    def created(self) -> bool: ...

    @property
    def state(self) -> str: ...

    @property
    def claimed_at(self) -> dt.datetime: ...


class MemoryLearningRepository(Protocol):
    @property
    def realm_id(self) -> UUID: ...

    def read_eligible_compiler_records(
        self,
        *,
        event_types: tuple[str, ...],
        classifications: tuple[str, ...],
        limit: int,
    ) -> tuple[LifecycleCompilerRecord, ...]: ...

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
    ) -> CompilerClaim: ...

    def store_compiler_output(
        self,
        output: MemoryCompilerOutput,
        *,
        watermark_claim_id: UUID,
        completed_at: dt.datetime,
    ) -> bool: ...

    def finalize_compiler_claim(
        self,
        *,
        claim_id: UUID,
        status: str,
        result_digest: str,
        completed_at: dt.datetime,
    ) -> None: ...

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
    ) -> bool: ...


class CompilerWorkerStatus(StrEnum):
    IDLE = "idle"
    COMPLETED = "completed"
    REPLAYED = "replayed"
    BUSY = "busy"
    RECOVERY_REQUIRED = "recovery-required"


@dataclass(frozen=True, slots=True)
class CompilerWorkerResult:
    status: CompilerWorkerStatus
    source_count: int = 0
    candidate_count: int = 0
    rejection_count: int = 0
    claim_id: UUID | None = None
    result_digest: str | None = None
    watermark: str | None = None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if min(self.source_count, self.candidate_count, self.rejection_count) < 0:
            raise ValidationFailed("Compiler worker sayaci negatif olamaz")
        if self.result_digest is not None:
            parse_digest(self.result_digest)
        if self.grants_authority:
            raise PolicyViolation("Compiler worker sonucu authority uretemez")

    def detail(self) -> str:
        claim = "none" if self.claim_id is None else str(self.claim_id)
        result = "none" if self.result_digest is None else self.result_digest
        return (
            f"memory compiler {self.status.value}: sources={self.source_count} "
            f"candidates={self.candidate_count} rejected={self.rejection_count} "
            f"claim={claim} result={result}"
        )


@dataclass(frozen=True, slots=True)
class MemoryContinuityOrchestrator:
    repository: MemoryLearningRepository
    compiler: MemoryCandidateCompiler
    policy: MemoryContinuityPolicy
    batch_limit: int = COMPILER_BATCH_LIMIT
    claim_stale_after: dt.timedelta = COMPILER_CLAIM_STALE_AFTER

    def __post_init__(self) -> None:
        if not 1 <= self.batch_limit <= COMPILER_BATCH_LIMIT:
            raise ValidationFailed("Memory compiler batch limiti 1..128 olmali")
        if self.claim_stale_after <= dt.timedelta(0):
            raise ValidationFailed("Memory compiler stale esigi pozitif olmali")

    def compile_due(self, *, now: dt.datetime | None = None) -> CompilerWorkerResult:
        moment = now or dt.datetime.now(dt.UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValidationFailed("Memory compiler worker zamani timezone-aware olmali")
        classifications = tuple(
            sorted(
                item.classification.value
                for item in self.policy.classifications
                if item.compiler_eligible
                and item.classification
                not in {
                    DataClassification.PII,
                    DataClassification.CORPORATE_CONFIDENTIAL,
                    DataClassification.SECRET,
                    DataClassification.RAW_TRANSCRIPT,
                    DataClassification.DIAGNOSTIC_PAYLOAD,
                }
            )
        )
        records = self.repository.read_eligible_compiler_records(
            event_types=COMPILER_EVENT_TYPES,
            classifications=classifications,
            limit=self.batch_limit,
        )
        if not records:
            return CompilerWorkerResult(CompilerWorkerStatus.IDLE)

        identity = (records[0].project_id, records[0].work_item_id, records[0].run_id)
        batch = tuple(
            record
            for record in records
            if (record.project_id, record.work_item_id, record.run_id) == identity
        )
        broken = tuple(
            record for record in batch if not record.chain_current or not record.receipt_current
        )
        if broken:
            for record in broken:
                evidence = digest(
                    {
                        "event_digest": record.event_digest,
                        "sequence": record.sequence,
                        "previous_digest": record.previous_digest,
                        "predecessor_digest": record.predecessor_digest,
                        "hook_receipt_count": record.hook_receipt_count,
                    }
                )
                self.repository.record_compiler_gap(
                    project_id=record.project_id,
                    work_item_id=record.work_item_id,
                    run_id=record.run_id,
                    gap_code=(
                        "compiler-lifecycle-chain-drift"
                        if not record.chain_current
                        else "compiler-hook-receipt-ambiguous"
                    ),
                    gap_ref=f"continuity-event:{record.event_id}",
                    evidence_digest=evidence,
                    recovery_ref=f"memory-compiler-recovery:{record.event_id}",
                    observed_at=moment,
                )
            return CompilerWorkerResult(
                CompilerWorkerStatus.RECOVERY_REQUIRED,
                source_count=len(broken),
                result_digest=digest(
                    {"gap": "compiler-lifecycle-input-invalid", "count": len(broken)}
                ),
            )

        fragments = tuple(self._fragment(record) for record in batch)
        stable_identity = digest(
            {
                "realm_id": str(self.repository.realm_id),
                "project_id": str(identity[0]),
                "work_item_id": str(identity[1]),
                "run_id": str(identity[2]),
                "sources": [record.source_ref.as_dict() for record in batch],
                "policy_digest": self.policy.policy_digest,
                "parser_digest": _PARSER_DIGEST,
            }
        )
        preparation = self.compiler.prepare(
            fragments,
            output_id=uuid5(_OUTPUT_NAMESPACE, stable_identity),
            realm_id=self.repository.realm_id,
            project_id=identity[0],
            work_item_id=identity[1],
            run_id=identity[2],
            parser_digest=_PARSER_DIGEST,
            policy_digest=self.policy.policy_digest,
            profile_digest=_PROFILE_DIGEST,
            known_references=frozenset(
                (reference.ref, reference.digest_value)
                for fragment in fragments
                for reference in (fragment.source, *fragment.evidence_refs)
            ),
            created_at=max(record.completed_at for record in batch),
        )
        claim = self.repository.claim_compiler_watermark(
            project_id=identity[0],
            work_item_id=identity[1],
            run_id=identity[2],
            idempotency_key=preparation.idempotency_key,
            source_set_digest=preparation.source_set_digest,
            source_watermark=preparation.output.source_watermark,
            claimed_at=moment,
        )
        if not claim.created:
            return self._reconcile_claim(claim, batch=batch, now=moment)

        try:
            self.repository.store_compiler_output(
                preparation.output,
                watermark_claim_id=claim.claim_id,
                completed_at=moment,
            )
        except Exception as exc:
            failure_digest = digest(
                {
                    "failure": type(exc).__name__,
                    "claim_id": str(claim.claim_id),
                    "source_set_digest": preparation.source_set_digest,
                }
            )
            self.repository.finalize_compiler_claim(
                claim_id=claim.claim_id,
                status="recovery-required",
                result_digest=failure_digest,
                completed_at=moment,
            )
            self.repository.record_compiler_gap(
                project_id=identity[0],
                work_item_id=identity[1],
                run_id=identity[2],
                gap_code="compiler-store-uncertain",
                gap_ref=f"compiler-claim:{claim.claim_id}",
                evidence_digest=failure_digest,
                recovery_ref=f"memory-compiler-recovery:{claim.claim_id}",
                observed_at=moment,
            )
            return CompilerWorkerResult(
                CompilerWorkerStatus.RECOVERY_REQUIRED,
                source_count=len(batch),
                claim_id=claim.claim_id,
                result_digest=failure_digest,
            )

        durability = CompilerDurabilityReceipt(
            output_digest=preparation.output.output_digest,
            source_set_digest=preparation.source_set_digest,
            candidate_queue_digest=preparation.candidate_queue_digest,
            compiler_receipt_digest=digest(
                {
                    "claim_id": str(claim.claim_id),
                    "status": "completed",
                    "result_digest": preparation.output.output_digest,
                }
            ),
            outbox_digest=digest(sorted(record.lifecycle_receipt_digest for record in batch)),
            committed_at=moment,
            durable=True,
        )
        committed = self.compiler.finalize_watermark(preparation, durability)
        return CompilerWorkerResult(
            CompilerWorkerStatus.COMPLETED,
            source_count=len(batch),
            candidate_count=len(preparation.output.candidates),
            rejection_count=len(preparation.output.rejected),
            claim_id=claim.claim_id,
            result_digest=preparation.output.output_digest,
            watermark=committed.value,
        )

    def _reconcile_claim(
        self,
        claim: CompilerClaim,
        *,
        batch: tuple[LifecycleCompilerRecord, ...],
        now: dt.datetime,
    ) -> CompilerWorkerResult:
        if claim.state == "completed":
            return CompilerWorkerResult(
                CompilerWorkerStatus.REPLAYED,
                source_count=len(batch),
                claim_id=claim.claim_id,
            )
        if claim.state in {"failed", "recovery-required"}:
            return CompilerWorkerResult(
                CompilerWorkerStatus.RECOVERY_REQUIRED,
                source_count=len(batch),
                claim_id=claim.claim_id,
            )
        if claim.claimed_at > now - self.claim_stale_after:
            return CompilerWorkerResult(
                CompilerWorkerStatus.BUSY,
                source_count=len(batch),
                claim_id=claim.claim_id,
            )
        evidence = digest(
            {
                "claim_id": str(claim.claim_id),
                "state": claim.state,
                "claimed_at": claim.claimed_at,
                "reason": "claim-without-terminal-compiler-receipt",
            }
        )
        self.repository.finalize_compiler_claim(
            claim_id=claim.claim_id,
            status="recovery-required",
            result_digest=evidence,
            completed_at=now,
        )
        first = batch[0]
        self.repository.record_compiler_gap(
            project_id=first.project_id,
            work_item_id=first.work_item_id,
            run_id=first.run_id,
            gap_code="compiler-claim-without-receipt",
            gap_ref=f"compiler-claim:{claim.claim_id}",
            evidence_digest=evidence,
            recovery_ref=f"memory-compiler-recovery:{claim.claim_id}",
            observed_at=now,
        )
        return CompilerWorkerResult(
            CompilerWorkerStatus.RECOVERY_REQUIRED,
            source_count=len(batch),
            claim_id=claim.claim_id,
            result_digest=evidence,
        )

    @staticmethod
    def _fragment(record: LifecycleCompilerRecord) -> CompilerSourceFragment:
        event = HookEventType(record.event_type)
        if event in _FAILURE_EVENTS:
            candidate_type = CompilerCandidateType.FAILURE_PATTERN
            risk = RiskLevel.HIGH
        elif event in _SKILL_EVENTS:
            candidate_type = CompilerCandidateType.SKILL_CANDIDATE
            risk = RiskLevel.HIGH
        elif event is HookEventType.ON_STATE_DRIFT:
            candidate_type = CompilerCandidateType.CONFLICT_CANDIDATE
            risk = RiskLevel.HIGH
        elif event in _PROJECTION_EVENTS:
            candidate_type = CompilerCandidateType.PROJECTION_REFRESH_REQUEST
            risk = RiskLevel.MEDIUM
        else:
            candidate_type = CompilerCandidateType.REUSABLE_LESSON
            risk = RiskLevel.MEDIUM
        content = canonical_json(record.structured_data)
        return CompilerSourceFragment(
            source=record.source_ref,
            source_kind=CompilerSourceKind.SESSION_EVENT,
            source_revision=record.source_revision,
            expected_source_revision=record.source_revision,
            logical_key=f"lifecycle:{record.event_type}:{record.event_id}",
            content_ref=record.source_ref.ref,
            content=content,
            expected_content_digest=digest(content),
            candidate_type=candidate_type,
            proposed_truth_class=TruthClass.TEMPORARY_ASSUMPTION,
            classification=record.classification,
            risk=risk,
            evidence_refs=record.evidence_refs,
        )
