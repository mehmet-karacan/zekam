"""Deterministic twenty-invariant Memory Contract result model."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.session_continuity import DigestReference, _bounded, _portable_ref

MEMORY_INVARIANT_IDS = (
    "durable-information-persisted",
    "clean-close-checkpoint",
    "pre-compaction-durable-ack",
    "hydration-before-mutation",
    "active-task-from-work-graph",
    "human-decision-durable",
    "adr-rationale-preserved",
    "pending-work-continuation-pointer",
    "critical-record-provenance",
    "inference-not-fact",
    "memory-write-failure-visible",
    "hydration-failure-visible",
    "broken-state-enters-recovery",
    "memory-mutation-versioned-reversible",
    "self-modification-governed",
    "sensitive-data-correct-tier",
    "public-private-separation",
    "stale-information-not-current",
    "duplicate-conflict-policy",
    "remember-claim-has-source",
)


class InvariantStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not-applicable"


@dataclass(frozen=True, slots=True)
class MemoryInvariantResult:
    invariant_id: str
    status: InvariantStatus
    enforcement_point: str
    evidence_refs: tuple[DigestReference, ...]
    failure_code: str | None = None
    recovery_directive: str | None = None

    def __post_init__(self) -> None:
        if self.invariant_id not in MEMORY_INVARIANT_IDS:
            raise ValidationFailed("Memory invariant registry disinda")
        _portable_ref(self.enforcement_point, "Invariant enforcement point")
        _bounded(self.evidence_refs, "Invariant evidence")
        if self.status is not InvariantStatus.NOT_APPLICABLE and not self.evidence_refs:
            raise ValidationFailed("Applicable invariant evidence ister")
        if self.status is InvariantStatus.FAILED:
            if self.failure_code is None or self.recovery_directive is None:
                raise ValidationFailed("Failed invariant failure code ve recovery ister")
            _portable_ref(self.failure_code, "Invariant failure code")
            _portable_ref(self.recovery_directive, "Invariant recovery directive")
        elif self.failure_code is not None or self.recovery_directive is not None:
            raise ValidationFailed("Passed/NA invariant failure alani tasiyamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "invariant_id": self.invariant_id,
            "status": self.status.value,
            "enforcement_point": self.enforcement_point,
            "evidence_refs": [item.as_dict() for item in self.evidence_refs],
            "failure_code": self.failure_code,
            "recovery_directive": self.recovery_directive,
        }


@dataclass(frozen=True, slots=True)
class MemoryContractEvaluation:
    evaluation_id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID
    results: tuple[MemoryInvariantResult, ...]
    source_revision: str
    policy_version: str
    evaluator_version: str
    evaluated_at: dt.datetime
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if len(self.results) != len(MEMORY_INVARIANT_IDS):
            raise ValidationFailed("Memory Contract exact 20 invariant sonucu ister")
        ids = tuple(result.invariant_id for result in self.results)
        if set(ids) != set(MEMORY_INVARIANT_IDS) or len(set(ids)) != len(ids):
            raise ValidationFailed("Memory Contract invariant seti eksik veya duplicate")
        for value, label in (
            (self.source_revision, "Evaluation source revision"),
            (self.policy_version, "Evaluation policy version"),
            (self.evaluator_version, "Evaluation evaluator version"),
        ):
            _portable_ref(value, label)
        if (
            self.evaluated_at.tzinfo is None
            or self.evaluated_at.tzinfo.utcoffset(self.evaluated_at) is None
        ):
            raise ValidationFailed("Memory Contract evaluated_at timezone-aware olmali")
        if self.grants_authority:
            raise PolicyViolation("Memory Contract evaluation authority uretemez")

    @property
    def passed(self) -> bool:
        return all(result.status is not InvariantStatus.FAILED for result in self.results)

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-contract-evaluation/v1",
            "evaluation_id": str(self.evaluation_id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "run_id": str(self.run_id),
            "results": [result.as_dict() for result in self.results],
            "passed": self.passed,
            "source_revision": self.source_revision,
            "policy_version": self.policy_version,
            "evaluator_version": self.evaluator_version,
            "evaluated_at": self.evaluated_at,
            "grants_authority": False,
        }

    @property
    def evaluation_digest(self) -> str:
        return digest(self.body())

    def assert_digest(self, supplied: str) -> None:
        parse_digest(supplied)
        if supplied != self.evaluation_digest:
            raise ValidationFailed("Memory Contract evaluation digest mismatch")

    def document(self) -> dict[str, Any]:
        return self.body() | {"evaluation_digest": self.evaluation_digest}
