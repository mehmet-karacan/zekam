"""Authority-free memory hygiene proposal and review queue contracts."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import canonical_bytes, digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory import HygieneFinding, HygieneReport, MemoryRecord


class HygieneProposalAction(StrEnum):
    REVIEW_DUPLICATE = "review-duplicate"
    REVIEW_CONFLICT = "review-conflict"
    REVIEW_STALE = "review-stale"
    REVIEW_UNUSED = "review-unused"
    REVIEW_RETENTION = "review-retention"
    REVIEW_SOURCE_VERSION = "review-source-version"


_ACTION_BY_FINDING = {
    HygieneFinding.DUPLICATE: HygieneProposalAction.REVIEW_DUPLICATE,
    HygieneFinding.CONFLICT: HygieneProposalAction.REVIEW_CONFLICT,
    HygieneFinding.STALE: HygieneProposalAction.REVIEW_STALE,
    HygieneFinding.UNUSED: HygieneProposalAction.REVIEW_UNUSED,
    HygieneFinding.RETENTION_REVIEW: HygieneProposalAction.REVIEW_RETENTION,
    HygieneFinding.SOURCE_VERSION_CONFLICT: HygieneProposalAction.REVIEW_SOURCE_VERSION,
}


@dataclass(frozen=True, slots=True)
class MemoryHygieneRecordSnapshot:
    """Self-contained immutable evidence for fields used by hygiene decisions."""

    memory_id: str
    record_digest: str
    created_at: dt.datetime
    valid_from: dt.datetime | None
    valid_until: dt.datetime | None
    last_used_at: dt.datetime | None

    def __post_init__(self) -> None:
        if not self.memory_id.strip():
            raise ValidationFailed("hijyen snapshot memory id ister")
        parse_digest(self.record_digest)
        for moment in (self.created_at, self.valid_from, self.valid_until, self.last_used_at):
            if moment is not None and moment.tzinfo is None:
                raise ValidationFailed("hijyen snapshot timezone-aware zaman ister")

    @classmethod
    def from_record(cls, record: MemoryRecord) -> MemoryHygieneRecordSnapshot:
        return cls(
            memory_id=record.memory_id,
            record_digest=record.record_digest,
            created_at=record.created_at,
            valid_from=record.valid_from,
            valid_until=record.valid_until,
            last_used_at=record.last_used_at,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-hygiene-record-snapshot/v1",
            "memory_id": self.memory_id,
            "record_digest": self.record_digest,
            "created_at": self.created_at.isoformat(),
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }

    @property
    def snapshot_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class MemoryHygienePolicy:
    review_slo_hours: int = 72
    policy_version: str = "1"

    def __post_init__(self) -> None:
        if self.review_slo_hours <= 0:
            raise ValidationFailed("hijyen review SLO pozitif olmali")
        if not self.policy_version.strip():
            raise ValidationFailed("hijyen policy version ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-hygiene-policy/v1",
            "policy_version": self.policy_version,
            "review_slo_hours": self.review_slo_hours,
            "automatic_delete": False,
            "automatic_merge": False,
        }

    @property
    def policy_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class MemoryHygieneProposal:
    finding: HygieneFinding
    action: HygieneProposalAction
    memory_id: str
    record_snapshot_digest: str
    detail: str
    report_digest: str
    policy_digest: str
    created_at: dt.datetime
    review_due_at: dt.datetime
    review_status: str = "review-required"
    grants_authority: bool = False
    automatic_effect: bool = False

    def __post_init__(self) -> None:
        if self.action is not _ACTION_BY_FINDING[self.finding]:
            raise ValidationFailed("hijyen finding/action eslesmiyor")
        if not self.memory_id.strip() or not self.detail.strip():
            raise ValidationFailed("hijyen onerisi kayit ve gerekce ister")
        parse_digest(self.record_snapshot_digest)
        parse_digest(self.report_digest)
        parse_digest(self.policy_digest)
        if self.created_at.tzinfo is None or self.review_due_at.tzinfo is None:
            raise ValidationFailed("hijyen onerisi timezone-aware zaman ister")
        if self.review_due_at <= self.created_at:
            raise ValidationFailed("hijyen review tarihi olusturma sonrasinda olmali")
        if self.review_status != "review-required":
            raise PolicyViolation("otomatik hijyen karari verilemez")
        if self.grants_authority or self.automatic_effect:
            raise PolicyViolation("hijyen onerisi authority veya otomatik etki veremez")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-hygiene-proposal/v1",
            "finding": str(self.finding),
            "action": str(self.action),
            "memory_id": self.memory_id,
            "record_snapshot_digest": self.record_snapshot_digest,
            "detail": self.detail,
            "report_digest": self.report_digest,
            "policy_digest": self.policy_digest,
            "created_at": self.created_at.isoformat(),
            "review_due_at": self.review_due_at.isoformat(),
            "review_status": "review-required",
            "grants_authority": False,
            "automatic_effect": False,
        }

    @property
    def proposal_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class MemoryHygieneReviewQueue:
    report: HygieneReport
    policy: MemoryHygienePolicy
    record_snapshots: tuple[MemoryHygieneRecordSnapshot, ...]
    report_digest: str
    policy_digest: str
    generated_at: dt.datetime
    proposals: tuple[MemoryHygieneProposal, ...]
    review_status: str = "review-required"
    grants_authority: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Rebuild the whole proposal set before it can cross a persistence boundary."""

        self.report.__post_init__()
        self.policy.__post_init__()
        parse_digest(self.report_digest)
        parse_digest(self.policy_digest)
        if self.generated_at.tzinfo is None:
            raise ValidationFailed("hijyen kuyrugu timezone-aware zaman ister")
        if self.review_status != "review-required" or self.grants_authority:
            raise PolicyViolation("hijyen kuyrugu yalniz authority-free review olabilir")
        if self.report_digest != digest(self.report.as_dict()):
            raise ValidationFailed("hijyen kuyrugu report digest uyusmuyor")
        if self.policy_digest != self.policy.policy_digest:
            raise ValidationFailed("hijyen kuyrugu policy digest uyusmuyor")
        snapshots = {item.memory_id: item for item in self.record_snapshots}
        if (
            len(snapshots) != len(self.record_snapshots)
            or tuple(snapshots) != tuple(sorted(snapshots))
            or self.report.scanned != len(snapshots)
        ):
            raise ValidationFailed("hijyen snapshot kapsami tekil, sirali ve raporla esit olmali")
        for snapshot in self.record_snapshots:
            snapshot.__post_init__()
        identities = tuple(item.proposal_digest for item in self.proposals)
        if identities != tuple(sorted(set(identities))):
            raise ValidationFailed("hijyen onerileri tekil ve deterministik sirali olmali")
        expected: dict[str, MemoryHygieneProposal] = {}
        due_at = self.generated_at + dt.timedelta(hours=self.policy.review_slo_hours)
        for finding, memory_id, detail in self.report.findings:
            found_snapshot = snapshots.get(memory_id)
            if found_snapshot is None:
                raise ValidationFailed("hijyen finding snapshot kapsami disinda")
            candidate = MemoryHygieneProposal(
                finding=finding,
                action=_ACTION_BY_FINDING[finding],
                memory_id=memory_id,
                record_snapshot_digest=found_snapshot.snapshot_digest,
                detail=detail,
                report_digest=self.report_digest,
                policy_digest=self.policy_digest,
                created_at=self.generated_at,
                review_due_at=due_at,
            )
            expected[candidate.proposal_digest] = candidate
        expected_proposals = tuple(expected[key] for key in sorted(expected))
        if self.proposals != expected_proposals:
            raise ValidationFailed("hijyen onerileri canonical rapordan yeniden uretilemedi")
        for proposal in self.proposals:
            proposal.__post_init__()
            if (
                proposal.report_digest != self.report_digest
                or proposal.policy_digest != self.policy_digest
                or proposal.created_at != self.generated_at
            ):
                raise ValidationFailed("hijyen onerisi kuyruk provenance zincirinden koptu")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-hygiene-review-queue/v1",
            "report": self.report.as_dict(),
            "policy": self.policy.as_dict(),
            "record_snapshots": [item.as_dict() for item in self.record_snapshots],
            "report_digest": self.report_digest,
            "policy_digest": self.policy_digest,
            "generated_at": self.generated_at.isoformat(),
            "review_status": "review-required",
            "grants_authority": False,
            "proposals": [item.as_dict() for item in self.proposals],
        }

    @property
    def queue_digest(self) -> str:
        return digest(self.as_dict())

    def to_bytes(self) -> bytes:
        return canonical_bytes(self.as_dict())


def build_hygiene_review_queue(
    report: HygieneReport,
    records: Sequence[MemoryRecord],
    *,
    generated_at: dt.datetime,
    policy: MemoryHygienePolicy | None = None,
) -> MemoryHygieneReviewQueue:
    """Turn a read-only scan into proposals; never mutate the supplied records."""

    report.__post_init__()
    selected_policy = policy or MemoryHygienePolicy()
    selected_policy.__post_init__()
    if generated_at.tzinfo is None:
        raise ValidationFailed("hijyen kuyrugu timezone-aware zaman ister")
    by_id = {record.memory_id: record for record in records}
    if len(by_id) != len(records):
        raise ValidationFailed("hijyen kayit kimlikleri tekil olmali")
    if report.scanned != len(records):
        raise ValidationFailed("hijyen raporu taranan kayit sayisi uyusmuyor")
    report_body = report.as_dict()
    report_digest = digest(report_body)
    due_at = generated_at + dt.timedelta(hours=selected_policy.review_slo_hours)
    snapshots = tuple(
        sorted(
            (MemoryHygieneRecordSnapshot.from_record(record) for record in records),
            key=lambda item: item.memory_id,
        )
    )
    snapshot_by_id = {item.memory_id: item for item in snapshots}
    proposals: dict[str, MemoryHygieneProposal] = {}
    for finding, memory_id, detail in report.findings:
        record = by_id.get(memory_id)
        if record is None:
            raise ValidationFailed("hijyen finding bilinmeyen kayda isaret ediyor")
        proposal = MemoryHygieneProposal(
            finding=finding,
            action=_ACTION_BY_FINDING[finding],
            memory_id=memory_id,
            record_snapshot_digest=snapshot_by_id[record.memory_id].snapshot_digest,
            detail=detail,
            report_digest=report_digest,
            policy_digest=selected_policy.policy_digest,
            created_at=generated_at,
            review_due_at=due_at,
        )
        proposals[proposal.proposal_digest] = proposal
    ordered = tuple(proposals[key] for key in sorted(proposals))
    return MemoryHygieneReviewQueue(
        report=report,
        policy=selected_policy,
        record_snapshots=snapshots,
        report_digest=report_digest,
        policy_digest=selected_policy.policy_digest,
        generated_at=generated_at,
        proposals=ordered,
    )
