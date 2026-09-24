"""Experience -> personal-skill candidate bridge (AC-07).

Consumes the content-free learning/evolution daily snapshot record index and
maps verified learning events into a bounded personal-skill candidate proposal
via the canonical ``SkillOriginEvidence`` contract (``personal_skill``).

This bridge only *proposes* a candidacy envelope.  It never assigns a skill an
``active`` state and never grants execution/tool/provider/claim authority.  The
canonical lifecycle (``skill_lifecycle``) is the single authority that persists
the candidate as ``candidate`` and requires fresh, independent evaluation +
review before any ``active`` activation.  A candidate produced here is therefore
a proposal input, not an activation.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.personal_skill import SkillOriginEvidence, SkillOriginKind

# Learning daily-snapshot record kinds the bridge understands.
LEARNING_RECORD_KINDS = frozenset(
    {"lesson", "failure_occurrence", "failure_card", "memory_revision",
     "skill_evaluation", "skill_review", "skill_activation", "skill_usage",
     "skill_outcome"}
)

MINIMUM_EVIDENCE_ORIGINS = 2


class ExperienceSource(StrEnum):
    EVOLUTION = "evolution"
    LEARNING = "learning"


class ExperienceVerdict(StrEnum):
    VERIFIED_SUCCESS = "verified_success"
    CORRECTION = "correction"
    FAILURE_LESSON = "failure_lesson"
    USER_REQUEST = "user_request"


_VERDICT_TO_ORIGIN = {
    ExperienceVerdict.VERIFIED_SUCCESS: SkillOriginKind.VERIFIED_SUCCESS,
    ExperienceVerdict.CORRECTION: SkillOriginKind.USER_CORRECTION,
    ExperienceVerdict.FAILURE_LESSON: SkillOriginKind.FAILURE_LESSON,
    ExperienceVerdict.USER_REQUEST: SkillOriginKind.USER_REQUEST,
}


def _ref(value: object, label: str) -> str:
    if isinstance(value, str) and value.strip() and len(value.encode("utf-8")) <= 512:
        return value
    raise ValidationFailed(f"Experience skill {label} ref invalid")


@dataclass(frozen=True, slots=True)
class ExperienceSkillOrigin:
    """One canonical, content-free origin derived from a learning/evolution event."""

    kind: SkillOriginKind
    verdict: ExperienceVerdict
    evidence_digest: str
    work_ref: str
    run_ref: str
    source_ref: str
    source_kind: ExperienceSource
    artifact_revision_digest: str | None
    user_ref: str | None
    observed_at: dt.datetime

    def __post_init__(self) -> None:
        if self.kind is not _VERDICT_TO_ORIGIN[self.verdict]:
            raise ValidationFailed("Experience skill verdict/origin kind mismatch")
        parse_digest(self.evidence_digest)
        _ref(self.work_ref, "work")
        _ref(self.run_ref, "run")
        _ref(self.source_ref, "source")
        if self.artifact_revision_digest is not None:
            parse_digest(self.artifact_revision_digest)
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValidationFailed("Experience skill observed_at must be timezone-aware")
        if self.kind is SkillOriginKind.USER_CORRECTION and (
            self.artifact_revision_digest is None or self.user_ref is None
        ):
            raise ValidationFailed("Correction needs accepted artifact and user source")
        if self.kind is SkillOriginKind.USER_REQUEST and (
            self.user_ref is None or self.artifact_revision_digest is not None
        ):
            raise ValidationFailed("User request needs user source without fabricated artifact")
        if self.kind not in {
            SkillOriginKind.USER_CORRECTION,
            SkillOriginKind.USER_REQUEST,
        } and self.user_ref is not None:
            raise ValidationFailed("Only real user correction/request may carry user_ref")

    def to_origin(self) -> SkillOriginEvidence:
        """Project to the canonical lifecycle origin contract (authority-free)."""
        return SkillOriginEvidence(
            kind=self.kind,
            evidence_digest=self.evidence_digest,
            work_ref=self.work_ref,
            run_ref=self.run_ref,
            artifact_revision_digest=self.artifact_revision_digest,
            user_ref=self.user_ref,
        )

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-experience-skill-origin/v1",
            "kind": str(self.kind),
            "verdict": str(self.verdict),
            "evidence_digest": self.evidence_digest,
            "work_ref": self.work_ref,
            "run_ref": self.run_ref,
            "source_ref": self.source_ref,
            "source_kind": str(self.source_kind),
            "artifact_revision_digest": self.artifact_revision_digest,
            "user_ref": self.user_ref,
            "observed_at": self.observed_at.astimezone(dt.UTC)
            .replace(microsecond=0)
            .isoformat(),
            "candidate_only": True,
            "grants_authority": False,
        }

    @property
    def origin_digest(self) -> str:
        return digest(self.body())


def _verdict_for_record(record: Mapping[str, object]) -> tuple[ExperienceVerdict, str]:
    """Map a content-free learning record to a verdict + evidence digest.

    The mapping is exact by record kind and never reads raw transcript content.
    ``evidence_digest`` is the exact canonical record digest so the lifecycle
    terminal-evidence check cannot be satisfied by fabricated text.
    """
    kind = record.get("kind")
    record_digest = record.get("record_digest")
    if not isinstance(kind, str) or kind not in LEARNING_RECORD_KINDS:
        raise ValidationFailed("Experience skill unknown learning record kind")
    if not isinstance(record_digest, str):
        raise ValidationFailed("Experience skill learning record digest required")
    parse_digest(record_digest)
    if kind == "lesson":
        return ExperienceVerdict.FAILURE_LESSON, record_digest
    if kind == "skill_outcome":
        status = record.get("status")
        if status == "verified-success":
            return ExperienceVerdict.VERIFIED_SUCCESS, record_digest
        raise ValidationFailed("Experience skill outcome not verified success")
    raise ValidationFailed(
        f"Experience skill record kind {kind} not directly candidacy-bearing"
    )


def candidate_proposal_from_experience(
    *,
    skill_id: str,
    name: str,
    description: str,
    trigger_terms: tuple[str, ...],
    records: tuple[Mapping[str, object], ...],
    source_kind: ExperienceSource,
    operational_run_ref: str,
    observed_at: dt.datetime,
    user_ref: str | None = None,
    artifact_revision_digest: str | None = None,
) -> dict[str, object]:
    """Build a bound personal-skill candidate proposal from learning events.

    This is read-only advisory: it returns a proposal envelope.  Persisting the
    proposal as a lifecycle ``candidate`` and any later ``active`` activation
    flow through the canonical ``PersonalSkillRevision``/``SkillEvaluationV2``
    authority which independently requires passing evaluation + review evidence.
    """
    if type(source_kind) is not ExperienceSource:
        raise ValidationFailed("Experience skill exact source kind required")
    _ref(skill_id, "skill id")
    _ref(name, "name")
    _ref(description, "description")
    if not isinstance(trigger_terms, tuple) or not trigger_terms or len(set(trigger_terms)) != len(
        trigger_terms
    ):
        raise ValidationFailed("Experience skill trigger terms must be bounded")
    _ref(operational_run_ref, "proposal run")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValidationFailed("Experience skill proposal observed_at timezone-aware olmali")
    if not records or len(records) > 64:
        raise ValidationFailed("Experience skill bounded record set required")

    origins: list[ExperienceSkillOrigin] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValidationFailed("Experience skill record mapping required")
        try:
            verdict, evidence_digest = _verdict_for_record(record)
        except ValidationFailed:
            continue
        source_ref = _ref(record.get("source_ref", f"learning:{skill_id}"), "source")
        origin = ExperienceSkillOrigin(
            kind=_VERDICT_TO_ORIGIN[verdict],
            verdict=verdict,
            evidence_digest=evidence_digest,
            work_ref=f"learning:{skill_id}",
            run_ref=operational_run_ref,
            source_ref=source_ref,
            source_kind=source_kind,
            artifact_revision_digest=(
                artifact_revision_digest
                if verdict is ExperienceVerdict.CORRECTION
                else None
            ),
            user_ref=(user_ref if verdict is ExperienceVerdict.CORRECTION else None),
            observed_at=observed_at,
        )
        origins.append(origin)

    if len(origins) < MINIMUM_EVIDENCE_ORIGINS:
        return {
            "schema": "zekam-experience-skill-candidate/v1",
            "skill_id": skill_id,
            "state": "insufficient-evidence",
            "reason": f"need at least {MINIMUM_EVIDENCE_ORIGINS} candidate origins",
            "origin_count": len(origins),
            "candidate_only": True,
            "activatable": False,
            "grants_authority": False,
        }

    body: dict[str, object] = {
        "schema": "zekam-experience-skill-candidate/v1",
        "skill_id": skill_id,
        "name": name,
        "description": description,
        "trigger_terms": list(trigger_terms),
        "state": "candidate",
        "origin_count": len(origins),
        "origins": [
            origin.body() | {"origin_digest": origin.origin_digest} for origin in origins
        ],
        "source_kind": str(source_kind),
        "proposal_run_ref": operational_run_ref,
        "observed_at": observed_at.astimezone(dt.UTC)
        .replace(microsecond=0)
        .isoformat(),
        "candidate_only": True,
        "activatable": False,
        "grants_authority": False,
    }
    return body | {"proposal_digest": digest(body)}


def is_candidate_only(proposal: Mapping[str, object]) -> bool:
    """Guarantee a proposal can never silently read as an active skill."""
    return (
        isinstance(proposal.get("candidate_only"), bool)
        and proposal.get("candidate_only") is True
        and proposal.get("activatable") is False
        and proposal.get("grants_authority") is False
    )


def proposal_origins(proposal: Mapping[str, object]) -> tuple[SkillOriginEvidence, ...]:
    """Rebuild the canonical lifecycle origins from a bounded proposal."""
    origins_raw = proposal.get("origins")
    if not isinstance(origins_raw, (list, tuple)):
        raise PolicyViolation("Experience skill proposal origins malformed")
    rebuilt: list[SkillOriginEvidence] = []
    for raw in origins_raw:
        if not isinstance(raw, Mapping):
            raise PolicyViolation("Experience skill origin malformed")
        try:
            kind = SkillOriginKind(str(raw["kind"]))
            origin = ExperienceSkillOrigin(
                kind=kind,
                verdict=ExperienceVerdict(str(raw["verdict"])),
                evidence_digest=str(raw["evidence_digest"]),
                work_ref=str(raw["work_ref"]),
                run_ref=str(raw["run_ref"]),
                source_ref=str(raw.get("source_ref", raw["work_ref"])),
                source_kind=ExperienceSource(str(raw["source_kind"])),
                artifact_revision_digest=(
                    str(raw["artifact_revision_digest"])
                    if raw.get("artifact_revision_digest") is not None
                    else None
                ),
                user_ref=(
                    str(raw["user_ref"]) if raw.get("user_ref") is not None else None
                ),
                observed_at=dt.datetime.fromisoformat(
                    str(raw["observed_at"]).replace("Z", "+00:00")
                ),
            )
        except (KeyError, TypeError, ValueError, ValidationFailed) as exc:
            raise PolicyViolation("Experience skill proposal origin invalid") from exc
        rebuilt.append(origin.to_origin())
    return tuple(rebuilt)
