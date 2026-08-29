"""Deterministic context compilation, journal, checkpoint ve handoff contracts."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.context_scoring import ContextCompilerMetricsV2
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_FRESHNESS_SECONDS = 30 * 24 * 60 * 60
EVIDENCE_KINDS = frozenset(
    {
        "work",
        "run",
        "receipt",
        "test",
        "artifact",
        "source",
        "citation",
        "memory",
        "benchmark",
        "commit",
    }
)
DEFAULT_TOKENIZER_PROFILE_DIGEST = digest(
    {"schema": "zekam-tokenizer-profile/v1", "profile": "utf8-byte-count"}
)
_SENSITIVE = re.compile(
    r"(?:secret|credential|password|private[-_ ]?key|owner[-_ ]?token|"
    r"authorization|approval|lease)",
    re.IGNORECASE,
)


class AuthorityLevel(IntEnum):
    UNTRUSTED = 0
    OBSERVED = 1
    VERIFIED = 2
    CANONICAL = 3


class OmittedReason(StrEnum):
    BUDGET = "budget-exhausted"
    STALE = "stale"
    INSUFFICIENT_AUTHORITY = "insufficient-authority"
    SUPERSEDED = "superseded"
    RECIPE_EXCLUDED = "recipe-excluded"
    DUPLICATE = "duplicate"
    IDENTITY_MISMATCH = "identity-mismatch"
    SCOPE_MISMATCH = "scope-mismatch"
    SOURCE_REVISION_MISMATCH = "source-revision-mismatch"
    CONFLICT = "conflict"
    ROLE_MISMATCH = "role-mismatch"
    LOW_RELEVANCE = "low-relevance"


class ContextCandidateKind(StrEnum):
    """Recipe registry tarafindan kullanilan typed context bolumleri."""

    GENERAL = "general"
    SYSTEM_POLICY = "system-policy"
    WORK_CONTRACT = "work-contract"
    RUN_STATUS = "run-status"
    ARCHITECTURE_RULE = "architecture-rule"
    DEPENDENCY_MANIFEST = "dependency-manifest"
    SOURCE_SLICE = "source-slice"
    SOURCE_DIFF = "source-diff"
    RESEARCH_EVIDENCE = "research-evidence"
    CITATION = "citation"
    KNOWLEDGE = "knowledge"
    MEMORY_SUMMARY = "memory-summary"
    EFFECT_RECEIPT = "effect-receipt"
    VERIFICATION_RESULT = "verification-result"
    TOOL_RESULT_SUMMARY = "tool-result-summary"
    TEST_EVIDENCE = "test-evidence"
    CHECKPOINT = "checkpoint"
    LOOP_PROGRESS_PACKET = "loop-progress-packet"


def _safe_logical(value: str, label: str) -> None:
    if not value.strip() or _SENSITIVE.search(value):
        raise PolicyViolation(f"{label} hassas veya bos olamaz")
    if PureWindowsPath(value).is_absolute() or value.startswith("/") or "\\" in value:
        raise PolicyViolation(f"{label} absolute path tasiyamaz")
    path = PurePosixPath(value)
    if ".." in path.parts:
        raise PolicyViolation(f"{label} traversal tasiyamaz")


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    kind: str
    ref: str
    evidence_digest: str
    revision: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            raise ValidationFailed("Evidence ref kind gecersiz")
        _safe_logical(self.ref, "Evidence ref")
        parse_digest(self.evidence_digest)
        if self.revision is not None and self.revision < 1:
            raise ValidationFailed("Evidence revision pozitif olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "revision": self.revision,
            "digest": self.evidence_digest,
        }


@dataclass(frozen=True, slots=True)
class ContextCandidate:
    candidate_id: str
    authority: AuthorityLevel
    observed_at: dt.datetime
    source_revision: str
    content_digest: str
    token_count: int
    required: bool = False
    valid_until: dt.datetime | None = None
    superseded: bool = False
    evidence_refs: tuple[EvidenceReference, ...] = ()
    kind: ContextCandidateKind = ContextCandidateKind.GENERAL
    source_ref: str = "context/unspecified"
    identity_refs: tuple[str, ...] = ()
    scope_ref: str = "scope/unspecified"
    applicable_roles: tuple[str, ...] = ()
    task_terms: tuple[str, ...] = ()
    compatible_source_revisions: tuple[str, ...] = ()
    conflict_refs: tuple[str, ...] = ()
    canonical_revision_id: str | None = None
    tokenizer_profile_digest: str = DEFAULT_TOKENIZER_PROFILE_DIGEST

    def __post_init__(self) -> None:
        _safe_logical(self.candidate_id, "Context candidate")
        _safe_logical(self.source_revision, "Source revision")
        parse_digest(self.content_digest)
        if self.token_count < 1:
            raise ValidationFailed("Context candidate token sayisi pozitif olmali")
        if self.observed_at.tzinfo is None:
            raise ValidationFailed("Context observed_at timezone ister")
        if self.valid_until is not None and self.valid_until.tzinfo is None:
            raise ValidationFailed("Context valid_until timezone ister")
        if self.valid_until is not None and self.valid_until <= self.observed_at:
            raise ValidationFailed("Context valid_until observed_at sonrasinda olmali")
        if not isinstance(self.kind, ContextCandidateKind):
            raise ValidationFailed("Context candidate kind registry disinda")
        _safe_logical(self.source_ref, "Context candidate source")
        _safe_logical(self.scope_ref, "Context candidate scope")
        for values, label in (
            (self.identity_refs, "Context identity ref"),
            (self.applicable_roles, "Context applicable role"),
            (self.task_terms, "Context task term"),
            (self.compatible_source_revisions, "Context compatible revision"),
            (self.conflict_refs, "Context conflict ref"),
        ):
            if len(set(values)) != len(values):
                raise ValidationFailed(f"{label} degerleri tekil olmali")
            for value in values:
                _safe_logical(value, label)
        parse_digest(self.tokenizer_profile_digest)
        if self.canonical_revision_id is not None:
            try:
                UUID(self.canonical_revision_id)
            except ValueError as exc:
                raise ValidationFailed("Context canonical revision UUID gecersiz") from exc

    def score(self, now: dt.datetime) -> tuple[int, int, str]:
        """Float kullanmadan authority-first, freshness-second kararli score."""
        age = max(0, int((now - self.observed_at).total_seconds()))
        freshness = max(0, MAX_FRESHNESS_SECONDS - age)
        return int(self.authority), freshness, self.candidate_id

    @property
    def provenance_body(self) -> dict[str, Any]:
        return {
            "id": self.candidate_id,
            "digest": self.content_digest,
            "revision": self.source_revision,
            "source_ref": self.source_ref,
            "tokens": self.token_count,
            "authority": int(self.authority),
            "observed_at": self.observed_at,
            "valid_until": self.valid_until,
            "superseded": self.superseded,
            "evidence_refs": [ref.as_dict() for ref in self.evidence_refs],
            "kind": self.kind.value,
            "identity_refs": sorted(self.identity_refs),
            "scope_ref": self.scope_ref,
            "applicable_roles": sorted(self.applicable_roles),
            "task_terms": sorted(self.task_terms),
            "compatible_source_revisions": sorted(self.compatible_source_revisions),
            "conflict_refs": sorted(self.conflict_refs),
            "canonical_revision_id": self.canonical_revision_id,
            "tokenizer_profile_digest": self.tokenizer_profile_digest,
        }

    @property
    def candidate_digest(self) -> str:
        return digest(self.provenance_body)

    def rejection(self, now: dt.datetime, minimum: AuthorityLevel) -> OmittedReason | None:
        if self.superseded:
            return OmittedReason.SUPERSEDED
        if self.authority < minimum:
            return OmittedReason.INSUFFICIENT_AUTHORITY
        if self.observed_at > now:
            return OmittedReason.STALE
        if self.valid_until is not None and self.valid_until <= now:
            return OmittedReason.STALE
        if now - self.observed_at > dt.timedelta(seconds=MAX_FRESHNESS_SECONDS):
            return OmittedReason.STALE
        return None


@dataclass(frozen=True, slots=True)
class ContextSelection:
    candidate_id: str
    content_digest: str
    token_count: int
    score: tuple[int | str, ...]
    reason: str
    kind: ContextCandidateKind = ContextCandidateKind.GENERAL
    source_ref: str = "context/unspecified"
    source_revision: str = "revision/unspecified"
    candidate_digest: str = (
        "sha256:4cc1a7fe85cc58f8f2c659675ddcb6a3622b7b423ff6cf12ca11c852d7a86435"
    )
    authority: AuthorityLevel = AuthorityLevel.UNTRUSTED
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ContextCandidateKind):
            raise ValidationFailed("Context selection kind registry disinda")
        _safe_logical(self.source_ref, "Context selection source")
        _safe_logical(self.source_revision, "Context selection revision")
        parse_digest(self.candidate_digest)
        if not isinstance(self.authority, AuthorityLevel):
            raise ValidationFailed("Context selection authority registry disinda")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValidationFailed("Context selection reason codes tekil olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "content_digest": self.content_digest,
            "token_count": self.token_count,
            "score": list(self.score),
            "reason": self.reason,
            "kind": self.kind.value,
            "source_ref": self.source_ref,
            "source_revision": self.source_revision,
            "candidate_digest": self.candidate_digest,
            "authority": int(self.authority),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class ContextOmission:
    candidate_id: str
    reason: OmittedReason
    token_count: int = 0
    canonical_candidate_id: str | None = None
    group_digest: str | None = None

    def __post_init__(self) -> None:
        if self.token_count < 0:
            raise ValidationFailed("Context omission token count negatif olamaz")
        if self.group_digest is not None:
            parse_digest(self.group_digest)

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "candidate_id": self.candidate_id,
            "reason": self.reason.value,
            "token_count": self.token_count,
            "canonical_candidate_id": self.canonical_candidate_id,
            "group_digest": self.group_digest,
        }


@dataclass(frozen=True, slots=True)
class ContextManifest:
    token_budget: int
    selected: tuple[ContextSelection, ...]
    omitted: tuple[ContextOmission, ...]
    candidate_fingerprint: str
    created_at: dt.datetime
    recipe_id: str | None = None
    recipe_digest: str | None = None
    target_role: str | None = None
    compiler_version: int = 1
    scoring_policy_digest: str | None = None
    compiler_metrics: ContextCompilerMetricsV2 | None = None
    ranking_snapshot_digest: str | None = None
    candidate_set_digest: str | None = None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Context manifest authority uretemez")
        if sum(item.token_count for item in self.selected) > self.token_budget:
            raise ValidationFailed("Context manifest token budget asiyor")
        parse_digest(self.candidate_fingerprint)
        recipe_fields = (self.recipe_id, self.recipe_digest, self.target_role)
        if any(item is not None for item in recipe_fields):
            if any(item is None for item in recipe_fields):
                raise ValidationFailed("Context recipe binding tum alanlari ister")
            _safe_logical(self.recipe_id or "", "Context recipe")
            _safe_logical(self.target_role or "", "Context target role")
            parse_digest(self.recipe_digest or "")
        if self.compiler_version not in {1, 2}:
            raise ValidationFailed("Context compiler version desteklenmiyor")
        if self.compiler_version == 2:
            if (
                self.scoring_policy_digest is None
                or self.compiler_metrics is None
                or self.ranking_snapshot_digest is None
                or self.candidate_set_digest is None
            ):
                raise ValidationFailed(
                    "Context compiler v2 policy, metrics ve ranking snapshot ister"
                )
            parse_digest(self.scoring_policy_digest)
            parse_digest(self.ranking_snapshot_digest)
            parse_digest(self.candidate_set_digest)
        elif (
            self.scoring_policy_digest is not None
            or self.compiler_metrics is not None
            or self.ranking_snapshot_digest is not None
            or self.candidate_set_digest is not None
        ):
            raise ValidationFailed("Context compiler v1 v2 metrics tasiyamaz")

    @property
    def manifest_digest(self) -> str:
        return digest(self.body())

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": self.compiler_version,
            "token_budget": self.token_budget,
            "selected": [item.as_dict() for item in self.selected],
            "omitted": [item.as_dict() for item in self.omitted],
            "candidate_fingerprint": self.candidate_fingerprint,
            "created_at": self.created_at,
            "recipe_id": self.recipe_id,
            "recipe_digest": self.recipe_digest,
            "target_role": self.target_role,
            "compiler_version": self.compiler_version,
            "scoring_policy_digest": self.scoring_policy_digest,
            "compiler_metrics": (
                None if self.compiler_metrics is None else self.compiler_metrics.body()
            ),
            "ranking_snapshot_digest": self.ranking_snapshot_digest,
            "candidate_set_digest": self.candidate_set_digest,
            "grants_authority": False,
        }


def compile_context(
    candidates: tuple[ContextCandidate, ...],
    *,
    token_budget: int,
    minimum_authority: AuthorityLevel,
    now: dt.datetime,
    recipe_id: str | None = None,
    recipe_digest: str | None = None,
    target_role: str | None = None,
) -> ContextManifest:
    if token_budget < 1 or now.tzinfo is None:
        raise ValidationFailed("Context budget ve timezone zorunludur")
    recipe_bound = recipe_id is not None or recipe_digest is not None or target_role is not None
    if (
        any(item.kind is not ContextCandidateKind.GENERAL for item in candidates)
        and not recipe_bound
    ):
        raise PolicyViolation("Typed agent context role recipe binding ister")
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise ValidationFailed("Context candidate kimlikleri tekil olmali")
    fingerprint = digest(
        [item.candidate_digest for item in sorted(candidates, key=lambda row: row.candidate_id)]
    )
    eligible: list[ContextCandidate] = []
    omitted: list[ContextOmission] = []
    for item in candidates:
        rejection = item.rejection(now, minimum_authority)
        if rejection is None:
            eligible.append(item)
        elif item.required:
            raise PolicyViolation(
                f"Required context candidate uygun degil: {item.candidate_id} ({rejection.value})"
            )
        else:
            omitted.append(ContextOmission(item.candidate_id, rejection))
    required = sorted(
        (item for item in eligible if item.required),
        key=lambda row: (-row.score(now)[0], -row.score(now)[1], row.candidate_id),
    )
    if sum(item.token_count for item in required) > token_budget:
        raise PolicyViolation("Required context token budget'e sigmiyor")
    optional = sorted(
        (item for item in eligible if not item.required),
        key=lambda row: (-row.score(now)[0], -row.score(now)[1], row.candidate_id),
    )
    remaining = token_budget
    selected: list[ContextSelection] = []
    for item in (*required, *optional):
        if item.token_count > remaining:
            omitted.append(ContextOmission(item.candidate_id, OmittedReason.BUDGET))
            continue
        selected.append(
            ContextSelection(
                item.candidate_id,
                item.content_digest,
                item.token_count,
                item.score(now),
                "required-first" if item.required else "authority-freshness-score",
                item.kind,
                item.source_ref,
                item.source_revision,
                item.candidate_digest,
                item.authority,
                ("required",) if item.required else ("authority", "freshness", "stable-id"),
            )
        )
        remaining -= item.token_count
    return ContextManifest(
        token_budget,
        tuple(selected),
        tuple(sorted(omitted, key=lambda row: row.candidate_id)),
        fingerprint,
        now,
        recipe_id,
        recipe_digest,
        target_role,
    )


@dataclass(frozen=True, slots=True)
class JournalEntry:
    sequence: int
    work_item_id: str
    event_kind: str
    payload_digest: str
    previous_digest: str | None
    truncated: bool
    created_at: dt.datetime

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValidationFailed("Journal sequence pozitif olmali")
        _safe_logical(self.work_item_id, "Work item")
        parse_digest(self.payload_digest)
        if self.previous_digest is not None:
            parse_digest(self.previous_digest)

    @property
    def entry_digest(self) -> str:
        return digest(
            {
                "sequence": self.sequence,
                "work_item_id": self.work_item_id,
                "event_kind": self.event_kind,
                "payload_digest": self.payload_digest,
                "previous_digest": self.previous_digest,
                "truncated": self.truncated,
                "created_at": self.created_at,
            }
        )


def verify_journal(entries: tuple[JournalEntry, ...], expected_head: str | None = None) -> str:
    previous: str | None = None
    for sequence, entry in enumerate(entries, 1):
        if entry.sequence != sequence or entry.previous_digest != previous:
            raise ValidationFailed("WorkJournal sequence/hash chain gecersiz")
        previous = entry.entry_digest
    if previous is None:
        raise ValidationFailed("WorkJournal bos olamaz")
    if expected_head is not None and previous != expected_head:
        raise ValidationFailed("WorkJournal head digest mismatch")
    return previous


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    project_id: str
    work_item_id: str
    plan_revision_id: str
    source_revision: str
    plan_steps: tuple[str, ...]
    completed_steps: tuple[str, ...]
    pending_steps: tuple[str, ...]
    step_results: tuple[tuple[str, str], ...]
    context_manifest_digest: str
    journal_head_digest: str
    next_safe_action: str
    created_at: dt.datetime
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Checkpoint authority uretemez")
        for value in (
            self.checkpoint_id,
            self.project_id,
            self.work_item_id,
            self.plan_revision_id,
        ):
            _safe_logical(value, "Checkpoint identity")
        _safe_logical(self.source_revision, "Checkpoint source revision")
        for value in (self.context_manifest_digest, self.journal_head_digest):
            parse_digest(value)
        plan = set(self.plan_steps)
        completed = set(self.completed_steps)
        pending = set(self.pending_steps)
        if len(plan) != len(self.plan_steps) or completed & pending or completed | pending != plan:
            raise ValidationFailed("Checkpoint completed/pending exact plan partition olmali")
        results = dict(self.step_results)
        if set(results) != completed or len(results) != len(self.step_results):
            raise ValidationFailed("Checkpoint completed step'ler exact result ister")
        for value in results.values():
            parse_digest(value)
        _safe_logical(self.next_safe_action, "Checkpoint next safe action")

    @property
    def checkpoint_digest(self) -> str:
        return digest(self.body())

    def body(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "project_id": self.project_id,
            "work_item_id": self.work_item_id,
            "plan_revision_id": self.plan_revision_id,
            "source_revision": self.source_revision,
            "plan_steps": list(self.plan_steps),
            "completed_steps": list(self.completed_steps),
            "pending_steps": list(self.pending_steps),
            "step_results": [
                {"step_id": key, "result_digest": value} for key, value in self.step_results
            ],
            "context_manifest_digest": self.context_manifest_digest,
            "journal_head_digest": self.journal_head_digest,
            "next_safe_action": self.next_safe_action,
            "created_at": self.created_at,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class ContinuitySnapshot:
    project_id: str
    work_item_id: str
    checkpoint_digest: str
    journal_head_digest: str
    context_manifest_digest: str
    source_revision: str
    first_reads: tuple[str, ...]
    next_safe_actions: tuple[str, ...]
    evidence_refs: tuple[EvidenceReference, ...]
    created_at: dt.datetime
    grants_authority: bool = False
    carries_active_lease: bool = False
    approval_inherited: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority or self.carries_active_lease or self.approval_inherited:
            raise PolicyViolation("Continuity authority/lease/approval tasiyamaz")
        for value in (self.project_id, self.work_item_id, self.source_revision):
            _safe_logical(value, "Continuity identity")
        for value in (
            self.checkpoint_digest,
            self.journal_head_digest,
            self.context_manifest_digest,
        ):
            parse_digest(value)
        if not self.first_reads or not self.next_safe_actions or not self.evidence_refs:
            raise ValidationFailed("Continuity bounded reads/actions/evidence ister")
        for value in (*self.first_reads, *self.next_safe_actions):
            _safe_logical(value, "Continuity content")

    @property
    def snapshot_digest(self) -> str:
        return digest(self.body())

    def body(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "work_item_id": self.work_item_id,
            "checkpoint_digest": self.checkpoint_digest,
            "journal_head_digest": self.journal_head_digest,
            "context_manifest_digest": self.context_manifest_digest,
            "source_revision": self.source_revision,
            "first_reads": list(self.first_reads),
            "next_safe_actions": list(self.next_safe_actions),
            "evidence_refs": [item.as_dict() for item in self.evidence_refs],
            "created_at": self.created_at,
            "grants_authority": False,
            "carries_active_lease": False,
            "approval_inherited": False,
        }


@dataclass(frozen=True, slots=True)
class TargetRouteBinding:
    """Canonical model route decision'in cross-client devam kesiti."""

    decision_id: UUID
    evidence_digest: str
    target_model_ref: str
    valid_until: dt.datetime
    observed_at: dt.datetime

    def __post_init__(self) -> None:
        parse_digest(self.evidence_digest)
        _safe_logical(self.target_model_ref, "Target route model")
        if self.valid_until.tzinfo is None or self.observed_at.tzinfo is None:
            raise ValidationFailed("Target route zamanlari timezone-aware olmali")
        if self.observed_at >= self.valid_until:
            raise PolicyViolation("Target route binding fresh degil")


@dataclass(frozen=True, slots=True)
class FinalizedHandoff:
    from_client: str
    to_client: str
    from_model_ref: str
    to_model_ref: str
    snapshot_digest: str
    checkpoint_digest: str
    source_revision: str
    created_at: dt.datetime
    source_client_capability_digest: str | None = None
    target_client_capability_digest: str | None = None
    source_client_permission_digest: str | None = None
    target_client_permission_digest: str | None = None
    unsupported_capabilities: tuple[str, ...] = ()
    unsupported_permissions: tuple[str, ...] = ()
    required_replan_items: tuple[str, ...] = ()
    target_route_decision_id: UUID | None = None
    target_route_decision_digest: str | None = None
    target_route_valid_until: dt.datetime | None = None
    target_route_fresh: bool = False
    transcript_included: bool = False
    grants_authority: bool = False
    carries_active_lease: bool = False
    approval_inherited: bool = False
    reacquire_required: bool = True

    def __post_init__(self) -> None:
        if (
            self.transcript_included
            or self.grants_authority
            or self.carries_active_lease
            or self.approval_inherited
            or not self.reacquire_required
        ):
            raise PolicyViolation("Handoff transcript/authority tasiyamaz ve re-acquire ister")
        for value in (
            self.from_client,
            self.to_client,
            self.from_model_ref,
            self.to_model_ref,
            self.source_revision,
        ):
            _safe_logical(value, "Handoff identity")
        parse_digest(self.snapshot_digest)
        parse_digest(self.checkpoint_digest)
        for optional_digest in (
            self.source_client_capability_digest,
            self.target_client_capability_digest,
            self.source_client_permission_digest,
            self.target_client_permission_digest,
            self.target_route_decision_digest,
        ):
            if optional_digest is not None:
                parse_digest(optional_digest)
        if tuple(sorted(set(self.unsupported_capabilities))) != self.unsupported_capabilities:
            raise ValidationFailed("Handoff unsupported capability listesi kanonik olmali")
        if tuple(sorted(set(self.unsupported_permissions))) != self.unsupported_permissions:
            raise ValidationFailed("Handoff unsupported permission listesi kanonik olmali")
        if tuple(sorted(set(self.required_replan_items))) != self.required_replan_items:
            raise ValidationFailed("Handoff replan listesi kanonik olmali")
        if self.target_route_valid_until is not None:
            if self.target_route_valid_until.tzinfo is None:
                raise ValidationFailed("Handoff route expiry timezone-aware olmali")
            if self.target_route_fresh != (self.created_at < self.target_route_valid_until):
                raise ValidationFailed("Handoff route freshness expiry ile uyusmuyor")
        elif self.target_route_fresh:
            raise ValidationFailed("Fresh handoff target route expiry ister")

    @property
    def cross_client_ready(self) -> bool:
        if self.from_client == self.to_client:
            return True
        return bool(
            self.source_client_capability_digest
            and self.target_client_capability_digest
            and self.source_client_permission_digest
            and self.target_client_permission_digest
            and self.target_route_decision_id
            and self.target_route_decision_digest
            and self.target_route_valid_until
            and self.target_route_fresh
            and not self.unsupported_capabilities
            and not self.unsupported_permissions
            and not self.required_replan_items
        )

    @property
    def legacy_limited(self) -> bool:
        return (
            self.from_client != self.to_client
            and self.source_client_capability_digest is None
            and self.target_client_capability_digest is None
            and self.source_client_permission_digest is None
            and self.target_client_permission_digest is None
            and self.target_route_decision_id is None
            and self.target_route_decision_digest is None
            and self.target_route_valid_until is None
            and not self.target_route_fresh
            and not self.unsupported_capabilities
            and not self.unsupported_permissions
            and not self.required_replan_items
        )

    @property
    def handoff_digest(self) -> str:
        body = {
            "from_client": self.from_client,
            "to_client": self.to_client,
            "from_model_ref": self.from_model_ref,
            "to_model_ref": self.to_model_ref,
            "snapshot_digest": self.snapshot_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "source_revision": self.source_revision,
            "created_at": self.created_at,
            "transcript_included": False,
            "grants_authority": False,
            "carries_active_lease": False,
            "approval_inherited": False,
            "reacquire_required": True,
        }
        if not self.legacy_limited:
            body |= {
                "source_client_capability_digest": self.source_client_capability_digest,
                "target_client_capability_digest": self.target_client_capability_digest,
                "source_client_permission_digest": self.source_client_permission_digest,
                "target_client_permission_digest": self.target_client_permission_digest,
                "unsupported_capabilities": list(self.unsupported_capabilities),
                "unsupported_permissions": list(self.unsupported_permissions),
                "required_replan_items": list(self.required_replan_items),
                "target_route_decision_id": self.target_route_decision_id,
                "target_route_decision_digest": self.target_route_decision_digest,
                "target_route_valid_until": self.target_route_valid_until,
                "target_route_fresh": self.target_route_fresh,
            }
        return digest(body)


def validate_resume(
    handoff: FinalizedHandoff,
    snapshot: ContinuitySnapshot,
    checkpoint: Checkpoint,
    *,
    current_source_revision: str,
) -> None:
    if handoff.from_client != handoff.to_client and not handoff.cross_client_ready:
        raise PolicyViolation("Cross-client handoff capability/route replan kapisini gecemedi")
    if handoff.snapshot_digest != snapshot.snapshot_digest:
        raise ValidationFailed("Handoff snapshot digest mismatch")
    if handoff.checkpoint_digest != checkpoint.checkpoint_digest:
        raise ValidationFailed("Handoff checkpoint digest mismatch")
    if snapshot.checkpoint_digest != checkpoint.checkpoint_digest:
        raise ValidationFailed("Continuity checkpoint digest mismatch")
    if (
        handoff.source_revision != current_source_revision
        or snapshot.source_revision != current_source_revision
    ):
        raise PolicyViolation(
            "Continuity source revision stale; yeniden compile/re-acquire gerekir"
        )
