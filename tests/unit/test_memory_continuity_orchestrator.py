from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from zekam.application.memory_candidate_compiler import MemoryCandidateCompiler
from zekam.application.memory_continuity_orchestrator import (
    CompilerWorkerStatus,
    LifecycleCompilerRecord,
    MemoryContinuityOrchestrator,
    MemoryLearningAction,
    plan_memory_hook,
)
from zekam.application.memory_policy import load_memory_policy
from zekam.domain.canonical import digest
from zekam.domain.hook_runtime import HookEventType
from zekam.domain.session_continuity import DataClassification

NOW = dt.datetime(2026, 8, 28, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(int=value) for value in range(1, 12))


@dataclass(frozen=True, slots=True)
class _Claim:
    claim_id: UUID
    created: bool
    state: str
    claimed_at: dt.datetime


@dataclass(slots=True)
class _Repository:
    realm_id: UUID = IDS[0]
    record: LifecycleCompilerRecord | None = None
    claim: _Claim | None = None
    outputs: list[Any] = field(default_factory=list)
    finalized: list[tuple[UUID, str, str]] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)

    def read_eligible_compiler_records(self, **_: Any) -> tuple[LifecycleCompilerRecord, ...]:
        return () if self.record is None else (self.record,)

    def claim_compiler_watermark(self, **_: Any) -> _Claim:
        return self.claim or _Claim(IDS[9], True, "pending", NOW)

    def store_compiler_output(
        self,
        output: Any,
        *,
        watermark_claim_id: UUID,
        completed_at: dt.datetime,
    ) -> bool:
        assert watermark_claim_id == IDS[9]
        assert completed_at == NOW
        self.outputs.append(output)
        return True

    def finalize_compiler_claim(
        self,
        *,
        claim_id: UUID,
        status: str,
        result_digest: str,
        completed_at: dt.datetime,
    ) -> None:
        assert completed_at == NOW
        self.finalized.append((claim_id, status, result_digest))

    def record_compiler_gap(self, **values: Any) -> bool:
        self.gaps.append(values)
        return True


def _record(**changes: Any) -> LifecycleCompilerRecord:
    event_type = str(changes.get("event_type", HookEventType.POST_CLOSE.value))
    sequence = int(changes.get("sequence", 1))
    previous_digest = changes.get("previous_digest")
    source_revision = str(changes.get("source_revision", "git:abc"))
    classification = changes.get("classification", DataClassification.INTERNAL)
    if not isinstance(classification, DataClassification):
        classification = DataClassification(str(classification))
    structured_data = changes.get("structured_data", {"summary_ref": "evidence:close-summary"})
    event_body = {
        "schema": "zekam-session-lifecycle-event/v1",
        "event_id": str(IDS[1]),
        "session_id": "session-one",
        "client_id": "opencode-local",
        "event_type": event_type,
        "sequence": sequence,
        "previous_digest": previous_digest,
        "source_revision": source_revision,
        "classification": classification.value,
        "payload_digest": digest(structured_data),
        "grants_authority": False,
    }
    hook_input = {
        "lifecycle": event_body,
        "data": structured_data,
    }
    command = plan_memory_hook(HookEventType(event_type), hook_input)
    hook_output = {
        "event_type": event_type,
        "accepted": True,
        "command": command.body(),
        "command_digest": command.command_digest,
        "grants_authority": False,
    }
    values: dict[str, Any] = {
        "event_id": IDS[1],
        "outbox_id": IDS[2],
        "project_id": IDS[3],
        "work_item_id": IDS[4],
        "run_id": IDS[5],
        "session_id": "session-one",
        "client_id": "opencode-local",
        "event_type": event_type,
        "sequence": sequence,
        "previous_digest": previous_digest,
        "predecessor_digest": None,
        "event_digest": digest(event_body),
        "event_body": event_body,
        "source_revision": source_revision,
        "classification": classification,
        "invocation_id": IDS[6],
        "structured_data": structured_data,
        "input_digest": digest(hook_input),
        "hook_receipt_id": IDS[7],
        "hook_output": hook_output,
        "hook_output_digest": digest(hook_output),
        "hook_receipt_count": 1,
        "lifecycle_receipt_digest": digest("lifecycle-terminal"),
        "occurred_at": NOW - dt.timedelta(seconds=1),
        "completed_at": NOW,
    }
    values.update(changes)
    return LifecycleCompilerRecord(**values)


def _service(repository: _Repository) -> MemoryContinuityOrchestrator:
    return MemoryContinuityOrchestrator(
        repository=repository,
        compiler=MemoryCandidateCompiler(),
        policy=load_memory_policy(),
    )


def test_hook_command_is_deterministic_authority_free_and_semantic() -> None:
    event_id = IDS[1]
    payload = {
        "lifecycle": {
            "schema": "zekam-session-lifecycle-event/v1",
            "event_id": str(event_id),
            "event_type": HookEventType.PRE_COMPACTION.value,
            "grants_authority": False,
        },
        "data": {"checkpoint_ref": "checkpoint:one"},
    }
    first = plan_memory_hook(HookEventType.PRE_COMPACTION, payload)
    second = plan_memory_hook(HookEventType.PRE_COMPACTION, payload)

    assert first.command_digest == second.command_digest
    assert first.actions == (
        MemoryLearningAction.CHECKPOINT,
        MemoryLearningAction.COMPACTION_BOUNDARY,
        MemoryLearningAction.COMPILER_ENQUEUE,
    )
    assert first.body()["provider_calls"] == 0
    assert first.body()["direct_promotion"] is False
    assert first.body()["grants_authority"] is False


def test_terminal_lifecycle_compiles_one_candidate_and_advances_watermark() -> None:
    repository = _Repository(record=_record())
    result = _service(repository).compile_due(now=NOW)

    assert result.status is CompilerWorkerStatus.COMPLETED
    assert result.source_count == 1
    assert result.candidate_count == 1
    assert result.rejection_count == 0
    assert result.watermark is not None
    assert len(repository.outputs) == 1
    assert repository.outputs[0].candidates[0].review_required is True
    assert repository.outputs[0].direct_promotion is False


def test_fresh_pending_claim_is_not_retried() -> None:
    repository = _Repository(
        record=_record(),
        claim=_Claim(IDS[9], False, "pending", NOW - dt.timedelta(seconds=1)),
    )
    result = _service(repository).compile_due(now=NOW)

    assert result.status is CompilerWorkerStatus.BUSY
    assert repository.outputs == []
    assert repository.finalized == []
    assert repository.gaps == []


def test_completed_claim_is_replayed_without_store_or_retry() -> None:
    repository = _Repository(
        record=_record(),
        claim=_Claim(IDS[9], False, "completed", NOW - dt.timedelta(minutes=10)),
    )
    result = _service(repository).compile_due(now=NOW)

    assert result.status is CompilerWorkerStatus.REPLAYED
    assert repository.outputs == []
    assert repository.finalized == []
    assert repository.gaps == []


def test_stale_receiptless_claim_becomes_recovery_required_without_retry() -> None:
    repository = _Repository(
        record=_record(),
        claim=_Claim(IDS[9], False, "pending", NOW - dt.timedelta(minutes=6)),
    )
    result = _service(repository).compile_due(now=NOW)

    assert result.status is CompilerWorkerStatus.RECOVERY_REQUIRED
    assert repository.outputs == []
    assert repository.finalized[0][0:2] == (IDS[9], "recovery-required")
    assert repository.gaps[0]["gap_code"] == "compiler-claim-without-receipt"


def test_out_of_order_source_records_gap_and_never_claims() -> None:
    repository = _Repository(
        record=_record(
            sequence=2,
            previous_digest=digest("expected"),
            predecessor_digest=digest("different"),
        )
    )
    result = _service(repository).compile_due(now=NOW)

    assert result.status is CompilerWorkerStatus.RECOVERY_REQUIRED
    assert repository.outputs == []
    assert repository.gaps[0]["gap_code"] == "compiler-lifecycle-chain-drift"
