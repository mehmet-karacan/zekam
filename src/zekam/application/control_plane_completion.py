"""Exact control-plane completion for non-continuity maintenance Work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.work import EvidenceRef

CONTROL_PLANE_COMPLETION_OPERATION = "control-plane-completion"
CONTROL_PLANE_COMPLETION_CONSUMER = "control-plane-completion/v1"
CONTROL_PLANE_COMPLETION_ADAPTER_DIGEST = digest(
    {"adapter": "control-plane-completion-postgres", "revision": 1}
)


def control_plane_completion_resource(project_id: UUID, work_item_id: UUID) -> str:
    return f"work:{project_id}:{work_item_id}:control-plane-completion"


@dataclass(frozen=True, slots=True)
class ControlPlaneCompletionRequest:
    project_id: UUID
    work_item_id: UUID
    task_plan_id: UUID
    job_id: UUID
    attempt_id: UUID
    checkpoint_id: UUID
    source_authorization_id: UUID
    source_authorization_digest: str
    source_claim_id: UUID
    source_claim_digest: str
    source_effect_receipt_id: UUID
    source_operation: str
    source_consumed_by: str
    source_effect_digest: str
    source_adapter_digest: str
    source_adapter_evidence_digest: str
    source_resources: tuple[str, ...]
    source_effects: tuple[str, ...]
    source_data_classifications: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if not self.source_operation.strip() or not self.source_consumed_by.strip():
            raise PolicyViolation("Control-plane completion source operation/consumer ister")
        parse_digest(self.source_effect_digest)
        parse_digest(self.source_adapter_digest)
        parse_digest(self.source_adapter_evidence_digest)
        parse_digest(self.source_authorization_digest)
        parse_digest(self.source_claim_digest)
        for values, label in (
            (self.source_resources, "resource"),
            (self.source_effects, "effect"),
            (self.source_data_classifications, "classification"),
        ):
            if not values or tuple(sorted(set(values))) != values:
                raise PolicyViolation(
                    f"Control-plane completion source {label} exact sorted values ister"
                )
        if not self.evidence:
            raise PolicyViolation("Control-plane completion acceptance evidence ister")
        for item in self.evidence:
            if item.digest_value is not None:
                parse_digest(item.digest_value)
        if (
            len(self.evidence) != 1
            or sum(
                item.kind == "runtime-receipt"
                and item.reference == str(self.source_effect_receipt_id)
                for item in self.evidence
            )
            != 1
        ):
            raise PolicyViolation("Control-plane completion exact source receipt evidence ister")

    @property
    def evidence_digest(self) -> str:
        return digest([item.as_dict() for item in self.evidence])

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-control-plane-completion-request/v1",
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "task_plan_id": str(self.task_plan_id),
            "job_id": str(self.job_id),
            "attempt_id": str(self.attempt_id),
            "checkpoint_id": str(self.checkpoint_id),
            "source_authorization_id": str(self.source_authorization_id),
            "source_authorization_digest": self.source_authorization_digest,
            "source_claim_id": str(self.source_claim_id),
            "source_claim_digest": self.source_claim_digest,
            "source_effect_receipt_id": str(self.source_effect_receipt_id),
            "source_operation": self.source_operation,
            "source_consumed_by": self.source_consumed_by,
            "source_effect_digest": self.source_effect_digest,
            "source_adapter_digest": self.source_adapter_digest,
            "source_adapter_evidence_digest": self.source_adapter_evidence_digest,
            "source_resources": list(self.source_resources),
            "source_effects": list(self.source_effects),
            "source_data_classifications": list(self.source_data_classifications),
            "evidence_digest": self.evidence_digest,
            "grants_authority": False,
        }

    @property
    def request_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class ControlPlaneCompletionResult:
    work_item_id: UUID
    work_revision: int
    work_record_digest: str
    authorization_id: UUID
    claim_id: UUID
    effect_receipt_id: UUID
    admission_id: UUID
    checkpoint_id: UUID
    result_digest: str
    request_digest: str
    evidence_digest: str
    source_authorization_id: UUID
    source_claim_id: UUID
    source_effect_receipt_id: UUID
    grants_authority: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "zekam-control-plane-completion-result/v1",
            "work_item_id": str(self.work_item_id),
            "work_revision": self.work_revision,
            "work_record_digest": self.work_record_digest,
            "authorization_id": str(self.authorization_id),
            "claim_id": str(self.claim_id),
            "effect_receipt_id": str(self.effect_receipt_id),
            "admission_id": str(self.admission_id),
            "checkpoint_id": str(self.checkpoint_id),
            "result_digest": self.result_digest,
            "request_digest": self.request_digest,
            "evidence_digest": self.evidence_digest,
            "source_authorization_id": str(self.source_authorization_id),
            "source_claim_id": str(self.source_claim_id),
            "source_effect_receipt_id": str(self.source_effect_receipt_id),
            "grants_authority": False,
        }


class ControlPlaneCompletionStore(Protocol):
    def complete(self, request: ControlPlaneCompletionRequest) -> ControlPlaneCompletionResult: ...

    def readback(self, request: ControlPlaneCompletionRequest) -> ControlPlaneCompletionResult: ...


@dataclass(frozen=True, slots=True)
class ControlPlaneCompletionService:
    store: ControlPlaneCompletionStore

    def complete(self, request: ControlPlaneCompletionRequest) -> ControlPlaneCompletionResult:
        """Close one exact terminal maintenance chain; never retries an effect."""

        return self._verify(request, self.store.complete(request))

    def readback(self, request: ControlPlaneCompletionRequest) -> ControlPlaneCompletionResult:
        """Read a committed exact completion after an uncertain caller result."""

        return self._verify(request, self.store.readback(request))

    @staticmethod
    def _verify(
        request: ControlPlaneCompletionRequest,
        result: ControlPlaneCompletionResult,
    ) -> ControlPlaneCompletionResult:
        parse_digest(result.work_record_digest)
        parse_digest(result.result_digest)
        parse_digest(result.request_digest)
        parse_digest(result.evidence_digest)
        if result.work_item_id != request.work_item_id or result.grants_authority:
            raise PolicyViolation("Control-plane completion result identity drift")
        if (
            result.request_digest != request.request_digest
            or result.evidence_digest != request.evidence_digest
            or result.source_authorization_id != request.source_authorization_id
            or result.source_claim_id != request.source_claim_id
            or result.source_effect_receipt_id != request.source_effect_receipt_id
        ):
            raise PolicyViolation("Control-plane completion request/evidence binding drift")
        return result
