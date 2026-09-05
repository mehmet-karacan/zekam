"""Typed embedding provider port, profile, policy and receipt contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.security import NEVER_OUTBOUND, DataClassification


class EmbeddingProviderKind(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


class EmbeddingPurpose(StrEnum):
    DOCUMENT = "document"
    QUERY = "query"
    PROBE = "probe"


class EmbeddingDegradedState(StrEnum):
    UNAVAILABLE = "embedding-unavailable"
    PROFILE_STALE = "embedding-profile-stale"
    DIMENSION_DRIFT = "embedding-dimension-drift"
    REMOTE_DISCLOSURE_NOT_AUTHORIZED = "remote-disclosure-not-authorized"
    LEXICAL_ONLY = "lexical-only-degraded"
    INDEX_REBUILD_REQUIRED = "index-rebuild-required"


@dataclass(frozen=True, slots=True)
class EmbeddingPolicy:
    classification: DataClassification
    expected_profile_digest: str
    remote_disclosure_authorized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.classification, DataClassification):
            raise ValidationFailed("Embedding policy classification typed olmali")
        parse_digest(self.expected_profile_digest)


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    profile_id: str
    display_name: str
    provider_kind: EmbeddingProviderKind
    provider_identity_digest: str
    exact_model_id: str
    model_revision_fingerprint: str
    dimension: int
    vector_dtype: str
    normalized: bool
    distance_metric: str
    query_prefix: str
    passage_prefix: str
    preprocessor_digest: str
    tokenizer_digest: str
    batch_policy_digest: str
    device_scope: str
    data_classification_allowlist: tuple[DataClassification, ...]
    verified_at: str
    probe_evidence_digest: str

    def __post_init__(self) -> None:
        if not self.profile_id or self.profile_id != self.profile_id.strip():
            raise ValidationFailed("Embedding profile id exact metin olmali")
        if not self.display_name.strip() or not self.exact_model_id.strip():
            raise ValidationFailed("Embedding profile model/display name ister")
        if not isinstance(self.provider_kind, EmbeddingProviderKind):
            raise ValidationFailed("Embedding provider kind typed olmali")
        for value in (
            self.provider_identity_digest,
            self.model_revision_fingerprint,
            self.preprocessor_digest,
            self.tokenizer_digest,
            self.batch_policy_digest,
            self.probe_evidence_digest,
        ):
            parse_digest(value)
        if type(self.dimension) is not int or self.dimension < 1:
            raise ValidationFailed("Embedding dimension pozitif integer olmali")
        if self.vector_dtype not in {"float16", "float32", "float64"}:
            raise ValidationFailed("Embedding vector dtype desteklenmiyor")
        if self.normalized is not True or self.distance_metric != "cosine":
            raise ValidationFailed("Embedding profile normalized cosine olmali")
        if not self.device_scope.strip() or not self.verified_at.endswith("Z"):
            raise ValidationFailed("Embedding device/timestamp exact olmali")
        if (
            not isinstance(self.data_classification_allowlist, tuple)
            or not self.data_classification_allowlist
            or len(set(self.data_classification_allowlist))
            != len(self.data_classification_allowlist)
            or any(
                not isinstance(item, DataClassification)
                for item in self.data_classification_allowlist
            )
        ):
            raise ValidationFailed("Embedding classification allowlist exact tuple olmali")
        if self.provider_kind is EmbeddingProviderKind.REMOTE and any(
            item in NEVER_OUTBOUND for item in self.data_classification_allowlist
        ):
            raise ValidationFailed("Remote embedding profili never-outbound veri kabul edemez")

    @property
    def profile_digest(self) -> str:
        """Stable index compatibility identity, excluding probe freshness metadata."""

        return digest(self.identity_dict())

    def identity_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "provider_kind": self.provider_kind.value,
            "provider_identity_digest": self.provider_identity_digest,
            "exact_model_id": self.exact_model_id,
            "model_revision_fingerprint": self.model_revision_fingerprint,
            "dimension": self.dimension,
            "vector_dtype": self.vector_dtype,
            "normalized": self.normalized,
            "distance_metric": self.distance_metric,
            "query_prefix": self.query_prefix,
            "passage_prefix": self.passage_prefix,
            "preprocessor_digest": self.preprocessor_digest,
            "tokenizer_digest": self.tokenizer_digest,
            "batch_policy_digest": self.batch_policy_digest,
            "device_scope": self.device_scope,
            "data_classification_allowlist": [
                item.value for item in self.data_classification_allowlist
            ],
        }

    def as_dict(self) -> dict[str, object]:
        return self.identity_dict() | {
            "display_name": self.display_name,
            "verified_at": self.verified_at,
            "probe_evidence_digest": self.probe_evidence_digest,
        }

    def assert_policy(self, policy: EmbeddingPolicy) -> None:
        if policy.expected_profile_digest != self.profile_digest:
            raise PolicyViolation("Embedding profile digest drift")
        if policy.classification not in self.data_classification_allowlist:
            raise PolicyViolation("Embedding data classification provider disinda")
        if (
            self.provider_kind is EmbeddingProviderKind.REMOTE
            and not policy.remote_disclosure_authorized
        ):
            raise PolicyViolation(EmbeddingDegradedState.REMOTE_DISCLOSURE_NOT_AUTHORIZED.value)

    def validate_vector(self, vector: tuple[float, ...]) -> None:
        if len(vector) != self.dimension:
            raise ValidationFailed(EmbeddingDegradedState.DIMENSION_DRIFT.value)
        if any(type(value) is not float or not math.isfinite(value) for value in vector):
            raise ValidationFailed("Embedding vector finite float olmali")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isclose(norm, 1.0, rel_tol=1e-4, abs_tol=1e-4):
            raise ValidationFailed("Embedding vector normalized olmali")


@dataclass(frozen=True, slots=True)
class EmbeddingReceipt:
    purpose: EmbeddingPurpose
    profile_digest: str
    input_digest: str
    output_digest: str
    vector_count: int
    dimension: int
    latency_ms: int
    provider_call_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.purpose, EmbeddingPurpose):
            raise ValidationFailed("Embedding receipt purpose typed olmali")
        for value in (self.profile_digest, self.input_digest, self.output_digest):
            parse_digest(value)
        if (
            type(self.vector_count) is not int
            or self.vector_count < 1
            or type(self.dimension) is not int
            or self.dimension < 1
            or type(self.latency_ms) is not int
            or self.latency_ms < 0
            or type(self.provider_call_count) is not int
            or self.provider_call_count < 1
        ):
            raise ValidationFailed("Embedding receipt numeric contract gecersiz")


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    receipt: EmbeddingReceipt


@dataclass(frozen=True, slots=True)
class EmbeddingProbeFixture:
    query: str
    positive_passage: str
    negative_passage: str
    source_refs: tuple[str, str]
    source_digests: tuple[str, str]
    classification: DataClassification

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.query, self.positive_passage, self.negative_passage)
        ):
            raise ValidationFailed("Embedding probe fixture bos metin tasiyamaz")
        if self.positive_passage == self.negative_passage:
            raise ValidationFailed("Embedding probe positive/negative farkli olmali")
        if len(set(self.source_refs)) != 2 or any(not item for item in self.source_refs):
            raise ValidationFailed("Embedding probe exact iki source ref ister")
        for value in self.source_digests:
            parse_digest(value)
        if not isinstance(self.classification, DataClassification):
            raise ValidationFailed("Embedding probe classification typed olmali")


@dataclass(frozen=True, slots=True)
class EmbeddingProbeResult:
    profile: EmbeddingProfile
    semantic_margin: float
    positive_score: float
    negative_score: float
    max_repeat_delta: float
    max_batch_delta: float
    batch_cosine: float
    latency_ms: int
    evidence_digest: str
    provider_call_count: int

    def __post_init__(self) -> None:
        parse_digest(self.evidence_digest)
        if self.profile.probe_evidence_digest != self.evidence_digest:
            raise ValidationFailed("Embedding probe/profile evidence drift")
        if not all(
            math.isfinite(value)
            for value in (
                self.semantic_margin,
                self.positive_score,
                self.negative_score,
                self.max_repeat_delta,
                self.max_batch_delta,
                self.batch_cosine,
            )
        ):
            raise ValidationFailed("Embedding probe metric finite olmali")
        if self.semantic_margin <= 0:
            raise ValidationFailed("Embedding probe semantic margin pozitif olmali")
        if type(self.latency_ms) is not int or self.latency_ms < 0:
            raise ValidationFailed("Embedding probe latency non-negative integer olmali")


@dataclass(frozen=True, slots=True)
class EmbeddingHealth:
    healthy: bool
    profile_digest: str | None
    degraded_state: EmbeddingDegradedState | None
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.profile_digest is not None:
            parse_digest(self.profile_digest)
        parse_digest(self.evidence_digest)
        if self.healthy == (self.degraded_state is not None):
            raise ValidationFailed("Embedding health/degraded state drift")


class EmbeddingProvider(Protocol):
    def describe(self) -> EmbeddingProfile: ...

    def probe(self, fixture: EmbeddingProbeFixture) -> EmbeddingProbeResult: ...

    def embed_documents(
        self, texts: tuple[str, ...], policy: EmbeddingPolicy
    ) -> EmbeddingBatch: ...

    def embed_query(self, text: str, policy: EmbeddingPolicy) -> EmbeddingBatch: ...

    def health(self) -> EmbeddingHealth: ...
