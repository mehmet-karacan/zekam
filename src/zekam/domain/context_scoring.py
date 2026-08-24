"""Context compiler v2 icin canonical ranking ve olcum sozlesmeleri."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class ScopeProximity(IntEnum):
    EXTERNAL = 0
    REALM = 1
    PROJECT = 2
    WORK = 3
    STEP = 4


class SourceRevisionState(StrEnum):
    CONFLICT = "conflict"
    MISMATCH = "mismatch"
    COMPATIBLE = "compatible"
    CURRENT = "current"

    @property
    def rank(self) -> int:
        return {
            SourceRevisionState.CONFLICT: 0,
            SourceRevisionState.MISMATCH: 1,
            SourceRevisionState.COMPATIBLE: 2,
            SourceRevisionState.CURRENT: 3,
        }[self]


class ContextSelectionReason(StrEnum):
    REQUIRED = "required"
    AUTHORITY = "authority"
    EXACT_IDENTITY = "exact-identity"
    SCOPE_PROXIMITY = "scope-proximity"
    SOURCE_COMPATIBLE = "source-compatible"
    EVIDENCE_STRENGTH = "evidence-strength"
    ROLE_RELEVANCE = "role-relevance"
    TASK_RELEVANCE = "task-relevance"
    FRESHNESS = "freshness"
    CONFLICT_PENALTY = "conflict-penalty"
    DUPLICATE_PENALTY = "duplicate-penalty"
    TOKEN_EFFICIENCY = "token-efficiency"
    STABLE_ID = "stable-id"


@dataclass(frozen=True, slots=True)
class ContextRankFeatures:
    """Caller puani degil, feature builder ciktisi olan kapali ranking girdisi."""

    exact_identity: bool
    scope_proximity: ScopeProximity
    source_revision_state: SourceRevisionState
    evidence_strength: int
    role_relevance: int
    task_relevance: int
    freshness_bucket: int
    conflict_count: int
    duplicate_group_digest: str | None
    duplicate_group_size: int
    tokenizer_profile_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope_proximity, ScopeProximity):
            raise ValidationFailed("Context scope proximity registry disinda")
        if not isinstance(self.source_revision_state, SourceRevisionState):
            raise ValidationFailed("Context source revision state registry disinda")
        for value, label, maximum in (
            (self.evidence_strength, "evidence strength", 4),
            (self.role_relevance, "role relevance", 4),
            (self.task_relevance, "task relevance", 4),
            (self.freshness_bucket, "freshness bucket", 4),
        ):
            if not 0 <= value <= maximum:
                raise ValidationFailed(f"Context {label} 0..{maximum} araliginda olmali")
        if self.conflict_count < 0 or self.duplicate_group_size < 1:
            raise ValidationFailed("Context conflict ve duplicate sayilari gecersiz")
        if self.duplicate_group_digest is None and self.duplicate_group_size != 1:
            raise ValidationFailed("Duplicate group digest olmadan group size bir olmali")
        if self.duplicate_group_digest is not None:
            parse_digest(self.duplicate_group_digest)
        parse_digest(self.tokenizer_profile_digest)

    def body(self) -> dict[str, Any]:
        return {
            "exact_identity": self.exact_identity,
            "scope_proximity": int(self.scope_proximity),
            "source_revision_state": self.source_revision_state.value,
            "evidence_strength": self.evidence_strength,
            "role_relevance": self.role_relevance,
            "task_relevance": self.task_relevance,
            "freshness_bucket": self.freshness_bucket,
            "conflict_count": self.conflict_count,
            "duplicate_group_digest": self.duplicate_group_digest,
            "duplicate_group_size": self.duplicate_group_size,
            "tokenizer_profile_digest": self.tokenizer_profile_digest,
        }

    @property
    def features_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class ContextScoreV2:
    required: int
    authority: int
    exact_identity: int
    scope_proximity: int
    source_compatible: int
    evidence_strength: int
    role_relevance: int
    task_relevance: int
    freshness_bucket: int
    conflict_penalty: int
    duplicate_penalty: int
    token_efficiency: int
    candidate_id: str

    @property
    def lexicographic(self) -> tuple[int | str, ...]:
        return (
            self.required,
            self.authority,
            self.exact_identity,
            self.scope_proximity,
            self.source_compatible,
            self.evidence_strength,
            self.role_relevance,
            self.task_relevance,
            self.freshness_bucket,
            self.conflict_penalty,
            self.duplicate_penalty,
            self.token_efficiency,
            self.candidate_id,
        )

    def body(self) -> dict[str, int | str]:
        return {
            "required": self.required,
            "authority": self.authority,
            "exact_identity": self.exact_identity,
            "scope_proximity": self.scope_proximity,
            "source_compatible": self.source_compatible,
            "evidence_strength": self.evidence_strength,
            "role_relevance": self.role_relevance,
            "task_relevance": self.task_relevance,
            "freshness_bucket": self.freshness_bucket,
            "conflict_penalty": self.conflict_penalty,
            "duplicate_penalty": self.duplicate_penalty,
            "token_efficiency": self.token_efficiency,
            "candidate_id": self.candidate_id,
        }


CONTEXT_SCORING_POLICY_VERSION = 2
CONTEXT_SCORING_POLICY_DIGEST = digest(
    {
        "schema": "zekam-context-scoring-policy/v2",
        "version": CONTEXT_SCORING_POLICY_VERSION,
        "order": tuple(item.value for item in ContextSelectionReason),
        "integer_only": True,
        "exact_partition": True,
        "exact_dedup_before_budget": True,
    }
)


@dataclass(frozen=True, slots=True)
class ContextCompilerMetricsV2:
    input_count: int
    input_tokens: int
    eligible_count: int
    eligible_tokens: int
    selected_count: int
    selected_tokens: int
    omitted_count: int
    omitted_tokens: int
    required_total: int
    required_selected: int
    duplicate_suppressed_count: int
    duplicate_suppressed_tokens: int
    token_budget: int
    token_utilization_ppm: int
    token_efficiency_ppm: int
    duplicate_token_ratio_ppm: int
    omission_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        numeric = (
            self.input_count,
            self.input_tokens,
            self.eligible_count,
            self.eligible_tokens,
            self.selected_count,
            self.selected_tokens,
            self.omitted_count,
            self.omitted_tokens,
            self.required_total,
            self.required_selected,
            self.duplicate_suppressed_count,
            self.duplicate_suppressed_tokens,
        )
        if any(value < 0 for value in numeric) or self.token_budget < 1:
            raise ValidationFailed("Context compiler metrics negatif deger tasiyamaz")
        if not 0 <= self.token_utilization_ppm <= 1_000_000:
            raise ValidationFailed("Context token utilization ppm gecersiz")
        if not 0 <= self.token_efficiency_ppm <= 1_000_000:
            raise ValidationFailed("Context token efficiency ppm gecersiz")
        if not 0 <= self.duplicate_token_ratio_ppm <= 1_000_000:
            raise ValidationFailed("Context duplicate token ratio ppm gecersiz")
        if self.required_selected != self.required_total:
            raise PolicyViolation("Context compiler required recall 1.0 olmali")
        if self.selected_count + self.omitted_count != self.input_count:
            raise ValidationFailed("Context compiler candidate count partition drift")
        if self.selected_tokens + self.omitted_tokens != self.input_tokens:
            raise ValidationFailed("Context compiler token partition drift")
        if tuple(sorted(self.omission_counts)) != self.omission_counts:
            raise ValidationFailed("Context omission metric keys kararli sirada olmali")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-context-compiler-metrics/v2",
            "input_count": self.input_count,
            "input_tokens": self.input_tokens,
            "eligible_count": self.eligible_count,
            "eligible_tokens": self.eligible_tokens,
            "selected_count": self.selected_count,
            "selected_tokens": self.selected_tokens,
            "omitted_count": self.omitted_count,
            "omitted_tokens": self.omitted_tokens,
            "required_total": self.required_total,
            "required_selected": self.required_selected,
            "duplicate_suppressed_count": self.duplicate_suppressed_count,
            "duplicate_suppressed_tokens": self.duplicate_suppressed_tokens,
            "token_budget": self.token_budget,
            "token_utilization_ppm": self.token_utilization_ppm,
            "token_efficiency_ppm": self.token_efficiency_ppm,
            "duplicate_token_ratio_ppm": self.duplicate_token_ratio_ppm,
            "omission_counts": dict(self.omission_counts),
        }

    @property
    def metrics_digest(self) -> str:
        return digest(self.body())
