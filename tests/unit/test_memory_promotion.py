from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory import (
    MemoryCandidate,
    MemoryClass,
    MemoryEvidence,
    MemoryKey,
    MemoryScope,
)
from zekam.domain.memory_promotion import (
    MemoryPromotionPlan,
    MemoryReviewDecision,
    candidate_snapshot_digest,
)

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 8, 24, 18, tzinfo=dt.UTC)
D = digest("memory-v2")


def _candidate() -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id="candidate-1",
        key=MemoryKey(MemoryScope.PROJECT, "realm", project_ref="zekam"),
        memory_class=MemoryClass.SEMANTIC,
        content="Promotion tek transaction olmalidir",
        author_ref="builder-a",
        observed_at=NOW,
        evidence=(MemoryEvidence("test", "tests/memory.py", D),),
    )


def _review() -> MemoryReviewDecision:
    return MemoryReviewDecision(True, "verifier-b", "kanitlandi", NOW, D)


def test_candidate_snapshot_and_review_semantics_are_digest_bound() -> None:
    candidate = _candidate()
    assert candidate_snapshot_digest(candidate) != candidate_snapshot_digest(
        replace(candidate, observed_at=NOW + dt.timedelta(seconds=1))
    )
    assert _review().review_digest != replace(_review(), reason="farkli").review_digest
    with pytest.raises(ValidationFailed, match="gerekce"):
        MemoryReviewDecision(True, "verifier", "", NOW)


def test_promotion_plan_binds_predecessor_review_outbox_and_effect() -> None:
    candidate = _candidate()
    plan = MemoryPromotionPlan(
        realm_id=uuid4(),
        candidate_storage_id=uuid4(),
        candidate_id=candidate.candidate_id,
        candidate_digest=candidate_snapshot_digest(candidate),
        logical_memory_id="memory-family-1",
        predecessor_storage_id=None,
        predecessor_digest=None,
        next_revision=1,
        review=_review(),
        evidence_digest=digest([item.as_dict() for item in candidate.evidence]),
        embedding_profile_digest=D,
        external_target_ref="mem0:local",
        prepared_at=NOW,
    )
    assert plan.plan_digest.startswith("sha256:")
    assert plan.effect_digest == digest(
        [{"effect": "database-write", "resources": sorted(plan.resources)}]
    )
    assert replace(plan, external_target_ref="mem0:other").plan_digest != plan.plan_digest
    with pytest.raises(PolicyViolation, match="Reddedilmis"):
        replace(plan, review=replace(_review(), approved=False))
