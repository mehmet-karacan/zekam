"""Transactional Memory v2 promotion contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory import MemoryCandidate


def candidate_snapshot_digest(candidate: MemoryCandidate) -> str:
    """Promotion planina giren immutable candidate gorunumunun digest'i."""

    return digest(
        {
            "schema": "zekam-memory-candidate-snapshot/v1",
            **candidate.as_dict(),
            "observed_at": candidate.observed_at,
        }
    )


@dataclass(frozen=True, slots=True)
class MemoryReviewDecision:
    approved: bool
    reviewer_ref: str
    reason: str
    decided_at: dt.datetime
    policy_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.reviewer_ref.strip() or not self.reason.strip():
            raise ValidationFailed("Memory review reviewer ve gerekce ister")
        if self.decided_at.tzinfo is None:
            raise ValidationFailed("Memory review zamani timezone-aware olmali")
        if self.policy_digest is not None:
            parse_digest(self.policy_digest)

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-review/v1",
            "approved": self.approved,
            "reviewer_ref": self.reviewer_ref,
            "reason_digest": digest(self.reason),
            "policy_digest": self.policy_digest,
            "decided_at": self.decided_at,
            "grants_authority": False,
        }

    @property
    def review_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class MemoryPromotionPlan:
    realm_id: UUID
    candidate_storage_id: UUID
    candidate_id: str
    candidate_digest: str
    logical_memory_id: str
    predecessor_storage_id: UUID | None
    predecessor_digest: str | None
    next_revision: int
    review: MemoryReviewDecision
    evidence_digest: str
    embedding_profile_digest: str
    external_target_ref: str
    prepared_at: dt.datetime

    def __post_init__(self) -> None:
        parse_digest(self.candidate_digest)
        parse_digest(self.evidence_digest)
        parse_digest(self.embedding_profile_digest)
        if (self.predecessor_storage_id is None) != (self.predecessor_digest is None):
            raise ValidationFailed("Memory predecessor kimlik/digest birlikte olmali")
        if self.predecessor_digest is not None:
            parse_digest(self.predecessor_digest)
        if self.next_revision < 1:
            raise ValidationFailed("Memory revision pozitif olmali")
        if not self.logical_memory_id.strip() or not self.external_target_ref.strip():
            raise ValidationFailed("Memory logical kimlik ve external target bos olamaz")
        if self.prepared_at.tzinfo is None:
            raise ValidationFailed("Memory promotion plan zamani timezone-aware olmali")
        if not self.review.approved:
            raise PolicyViolation("Reddedilmis review icin promotion plani uretilemez")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-promotion-plan/v1",
            "realm_id": str(self.realm_id),
            "candidate_storage_id": str(self.candidate_storage_id),
            "candidate_id": self.candidate_id,
            "candidate_digest": self.candidate_digest,
            "logical_memory_id": self.logical_memory_id,
            "predecessor_storage_id": (
                None if self.predecessor_storage_id is None else str(self.predecessor_storage_id)
            ),
            "predecessor_digest": self.predecessor_digest,
            "next_revision": self.next_revision,
            "review_digest": self.review.review_digest,
            "evidence_digest": self.evidence_digest,
            "embedding_profile_digest": self.embedding_profile_digest,
            "external_target_ref": self.external_target_ref,
            "prepared_at": self.prepared_at,
            "grants_authority": False,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.body())

    @property
    def resources(self) -> tuple[str, ...]:
        return (
            f"memory:candidate:{self.candidate_storage_id}",
            f"memory:logical:{self.logical_memory_id}",
        )

    @property
    def effect_digest(self) -> str:
        return digest([{"effect": "database-write", "resources": sorted(self.resources)}])


@dataclass(frozen=True, slots=True)
class MemoryPromotionReceipt:
    id: UUID
    plan_digest: str
    record_storage_id: UUID
    logical_memory_id: str
    revision: int
    review_id: UUID
    authorization_id: UUID
    result_digest: str
    created_at: dt.datetime

    def __post_init__(self) -> None:
        parse_digest(self.plan_digest)
        parse_digest(self.result_digest)
