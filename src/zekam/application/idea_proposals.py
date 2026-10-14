"""Authority-free idea proposal and review surfaces.

The ideas surface deliberately stops at a bounded, secret-free proposal.  It
does not persist a candidate, create a Work item, or infer approval.  A caller
must carry an explicit project scope and approval digest to obtain a
``ready_for_work`` plan; that plan still has no runtime effect.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from zekam.application.knowledge_file_plane import assert_public_safe_projection
from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import assert_portable, validate_slug
from zekam.domain.intake import normalize_text

IDEA_PROPOSAL_SCHEMA = "zekam-idea-proposal/v1"
IDEA_REVIEW_SCHEMA = "zekam-idea-review/v1"
IDEA_WORK_PLAN_SCHEMA = "zekam-idea-work-transition-plan/v1"

_MAX_TITLE_CHARS = 240
_MAX_SUMMARY_CHARS = 2_000
_MAX_IMPACT_CHARS = 1_000
_MAX_REASON_CHARS = 500
_MAX_REFERENCE_CHARS = 256
_RISKS = frozenset({"unknown", "low", "medium", "high", "critical"})
_COST_STATUSES = frozenset({"unknown", "bounded", "measured"})


def _safe_text(value: str, *, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValidationFailed(f"{label} metin olmali")
    candidate = value.strip()
    if not candidate:
        raise ValidationFailed(f"{label} bos olamaz")
    if len(candidate) > limit:
        raise ValidationFailed(f"{label} bounded siniri asiyor")
    if any(ord(char) < 32 and char not in "\n\t" for char in candidate):
        raise ValidationFailed(f"{label} kontrol karakteri tasiyamaz")
    if scan_text(candidate, relative_path=f"ideas/{label}"):
        raise PolicyViolation(f"{label} secret benzeri icerik tasiyamaz")
    assert_public_safe_projection(candidate.encode("utf-8"), relative_path=f"ideas/{label}")
    return candidate


class CostStatus(StrEnum):
    UNKNOWN = "unknown"
    BOUNDED = "bounded"
    MEASURED = "measured"


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """A cost claim whose unknown values remain explicitly unknown."""

    status: CostStatus = CostStatus.UNKNOWN
    duration_ms: int | None = None
    tokens: int | None = None
    cost_micros: int | None = None
    reason: str = "not-measured"

    @classmethod
    def unknown(cls, reason: str = "not-measured") -> CostEstimate:
        return cls(reason=reason)

    def __post_init__(self) -> None:
        try:
            status = CostStatus(self.status)
        except ValueError as exc:
            raise ValidationFailed("fikir maliyet durumu taninmiyor") from exc
        object.__setattr__(self, "status", status)
        for label, value in (
            ("duration_ms", self.duration_ms),
            ("tokens", self.tokens),
            ("cost_micros", self.cost_micros),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValidationFailed(f"{label} sifir veya pozitif tamsayi olmali")
        reason = _safe_text(self.reason, label="cost-reason", limit=_MAX_REASON_CHARS)
        object.__setattr__(self, "reason", reason)
        values = (self.duration_ms, self.tokens, self.cost_micros)
        if status is CostStatus.UNKNOWN and any(value is not None for value in values):
            raise ValidationFailed("unknown maliyet olcum degeri tasiyamaz")
        if status is not CostStatus.UNKNOWN and not any(value is not None for value in values):
            raise ValidationFailed("olculmus maliyet en az bir bounded deger ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "cost_micros": self.cost_micros,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CostEstimate:
        if not isinstance(value, Mapping):
            raise ValidationFailed("fikir maliyet kaydi nesne olmali")
        try:
            status = CostStatus(str(value["status"]))
            reason = str(value["reason"])
            fields = {key: value.get(key) for key in ("duration_ms", "tokens", "cost_micros")}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailed("fikir maliyet kaydi eksik") from exc
        return cls(status=status, reason=reason, **fields)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class IdeaEvidence:
    """A digest-only evidence pointer; raw evidence content is never carried."""

    kind: str
    ref: str
    digest: str

    def __post_init__(self) -> None:
        kind = _safe_text(self.kind, label="evidence-kind", limit=64).lower()
        ref = _safe_text(self.ref, label="evidence-ref", limit=_MAX_REFERENCE_CHARS)
        assert_portable(ref)
        if not isinstance(self.digest, str):
            raise ValidationFailed("fikir evidence digest metin olmali")
        parse_digest(self.digest)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "ref", ref)

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class IdeaProposal:
    title: str
    summary: str
    project_ref: str | None
    impact: str
    cost: CostEstimate
    risk: str
    evidence: tuple[IdeaEvidence, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "title", _safe_text(self.title, label="title", limit=_MAX_TITLE_CHARS)
        )
        object.__setattr__(
            self, "summary", _safe_text(self.summary, label="summary", limit=_MAX_SUMMARY_CHARS)
        )
        object.__setattr__(
            self, "impact", _safe_text(self.impact, label="impact", limit=_MAX_IMPACT_CHARS)
        )
        if self.project_ref is not None:
            if not isinstance(self.project_ref, str):
                raise ValidationFailed("fikir proje scope metin olmali")
            validate_slug(self.project_ref)
        risk = _safe_text(self.risk, label="risk", limit=32).lower()
        if risk not in _RISKS:
            raise ValidationFailed("fikir riski unknown/low/medium/high/critical olmali")
        object.__setattr__(self, "risk", risk)
        if not isinstance(self.cost, CostEstimate):
            raise ValidationFailed("fikir maliyet tahmini gecersiz")
        evidence = tuple(self.evidence)
        if any(not isinstance(item, IdeaEvidence) for item in evidence):
            raise ValidationFailed("fikir kaniti IdeaEvidence olmali")
        identities = [(item.kind, item.ref, item.digest) for item in evidence]
        if len(set(identities)) != len(identities):
            raise ValidationFailed("fikir kanitlari tekil olmali")
        object.__setattr__(
            self,
            "evidence",
            tuple(sorted(evidence, key=lambda item: (item.kind, item.ref, item.digest))),
        )

    @property
    def dedup_key(self) -> str:
        return cast(
            str,
            digest(
                {
                    "schema": "zekam-idea-dedup/v1",
                    "project_ref": self.project_ref,
                    "title": normalize_text(self.title),
                    "summary": normalize_text(self.summary),
                    "impact": normalize_text(self.impact),
                }
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": IDEA_PROPOSAL_SCHEMA,
            "title": self.title,
            "summary": self.summary,
            "project_ref": self.project_ref,
            "impact": self.impact,
            "cost": self.cost.as_dict(),
            "risk": self.risk,
            "evidence": [item.as_dict() for item in self.evidence],
            "dedup_key": self.dedup_key,
            "candidate_only": True,
            "activatable": False,
            "read_only": True,
            "grants_authority": False,
        }
        return body | {"proposal_digest": digest(body)}

    @property
    def proposal_digest(self) -> str:
        return cast(str, self.as_dict()["proposal_digest"])


def build_idea_proposal(
    *,
    title: str,
    summary: str,
    project_ref: str | None,
    impact: str,
    cost: CostEstimate,
    risk: str,
    evidence: Sequence[IdeaEvidence] = (),
) -> IdeaProposal:
    """Build a bounded candidate without persistence or provider/model calls."""

    return IdeaProposal(
        title=title,
        summary=summary,
        project_ref=project_ref,
        impact=impact,
        cost=cost,
        risk=risk,
        evidence=tuple(evidence),
    )


def idea_proposal_from_dict(value: Mapping[str, object]) -> IdeaProposal:
    """Rebuild and verify a proposal received from a read-only CLI boundary."""

    if value.get("schema") != IDEA_PROPOSAL_SCHEMA:
        raise ValidationFailed("fikir proposal schema uyusmuyor")
    try:
        evidence_raw = value["evidence"]
        if not isinstance(evidence_raw, list):
            raise TypeError
        if any(not isinstance(item, Mapping) for item in evidence_raw):
            raise TypeError
        evidence = tuple(
            IdeaEvidence(
                kind=str(item["kind"]),
                ref=str(item["ref"]),
                digest=str(item["digest"]),
            )
            for item in evidence_raw
        )
        proposal = build_idea_proposal(
            title=str(value["title"]),
            summary=str(value["summary"]),
            project_ref=str(value["project_ref"]) if value.get("project_ref") is not None else None,
            impact=str(value["impact"]),
            cost=CostEstimate.from_dict(
                value["cost"] if isinstance(value["cost"], Mapping) else {}
            ),
            risk=str(value["risk"]),
            evidence=evidence,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationFailed("fikir proposal alani eksik veya bozuk") from exc
    if (
        value.get("dedup_key") != proposal.dedup_key
        or value.get("proposal_digest") != proposal.proposal_digest
    ):
        raise ValidationFailed("fikir proposal digest dogrulamasi basarisiz")
    return proposal


def review_idea_proposals(proposals: Sequence[IdeaProposal]) -> dict[str, Any]:
    """Deterministically deduplicate candidates while preserving safe evidence."""

    ordered = tuple(sorted(proposals, key=lambda item: item.proposal_digest))
    groups: dict[str, list[IdeaProposal]] = defaultdict(list)
    for proposal in ordered:
        groups[proposal.dedup_key].append(proposal)
    items: list[dict[str, Any]] = []
    for proposal in ordered:
        group = groups[proposal.dedup_key]
        winner = group[0]
        item = {
            "proposal_digest": proposal.proposal_digest,
            "dedup_key": proposal.dedup_key,
            "decision": "unique" if proposal is winner else "duplicate",
            "duplicate_of": None if proposal is winner else winner.proposal_digest,
            "project_ref": proposal.project_ref,
            "title": proposal.title,
            "impact": proposal.impact,
            "cost": proposal.cost.as_dict(),
            "risk": proposal.risk,
            "evidence": [evidence.as_dict() for evidence in proposal.evidence],
        }
        items.append(item)
    body = {
        "schema": IDEA_REVIEW_SCHEMA,
        "items": items,
        "read_only": True,
        "grants_authority": False,
        "candidate_only": True,
    }
    return body | {"review_digest": digest(body)}


def build_work_transition_plan(
    proposal: IdeaProposal,
    *,
    project_ref: str | None,
    approved: bool = False,
    approval_scope_digest: object = None,
) -> dict[str, Any]:
    """Return a no-effect Work bridge plan bound to exact scope and approval."""

    if not isinstance(proposal, IdeaProposal):
        raise ValidationFailed("fikir proposal gecersiz")
    if project_ref is None or not isinstance(project_ref, str) or not project_ref.strip():
        raise ValidationFailed("Work gecisi explicit project scope ister")
    validate_slug(project_ref)
    if proposal.project_ref is not None and proposal.project_ref != project_ref:
        raise PolicyViolation("fikir proposal/project scope eslesmiyor")
    scope_digest = digest(
        {
            "schema": "zekam-idea-work-scope/v1",
            "project_ref": project_ref,
            "proposal_digest": proposal.proposal_digest,
        }
    )
    if approved:
        if not isinstance(approval_scope_digest, str):
            raise ValidationFailed("explicit approval scope digest ister")
        parse_digest(approval_scope_digest)
        if approval_scope_digest != scope_digest:
            raise PolicyViolation("approval scope digest fikir kapsamiyla eslesmiyor")
    elif approval_scope_digest is not None:
        raise PolicyViolation("onaysiz Work plan approval digest tasiyamaz")
    body: dict[str, Any] = {
        "schema": IDEA_WORK_PLAN_SCHEMA,
        "proposal_digest": proposal.proposal_digest,
        "project_ref": project_ref,
        "scope_digest": scope_digest,
        "approval_required": True,
        "approved": approved,
        "approval_scope_digest": scope_digest if approved else None,
        "state": "approval-recorded" if approved else "approval-required",
        "ready_for_work": approved,
        "creates_work": False,
        "read_only": True,
        "grants_authority": False,
    }
    return body | {"plan_digest": digest(body)}
