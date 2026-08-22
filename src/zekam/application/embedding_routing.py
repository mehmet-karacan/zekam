"""Evidence-based embedding selection with an explicit local fallback."""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import ValidationFailed


class EmbeddingRouteKind(enum.StrEnum):
    QUALIFIED_REMOTE = "qualified-remote"
    LOCAL_FALLBACK = "local-deterministic-fallback"


@dataclass(frozen=True, slots=True)
class EmbeddingRouteCandidate:
    model_ref: str
    dimension: int
    qualified: bool
    health_fresh: bool
    verified: bool
    latency_ms: float
    semantic_margin: float
    qualification_evidence_digest: str
    probe_evidence_digest: str

    def __post_init__(self) -> None:
        if not self.model_ref.strip() or self.dimension < 1:
            raise ValidationFailed("Embedding route adayi model ve boyut ister")
        if self.latency_ms < 0:
            raise ValidationFailed("Embedding route latency negatif olamaz")
        parse_digest(self.qualification_evidence_digest)
        parse_digest(self.probe_evidence_digest)

    @property
    def eligible(self) -> bool:
        return self.qualified and self.health_fresh and self.verified


@dataclass(frozen=True, slots=True)
class EmbeddingRouteDecision:
    kind: EmbeddingRouteKind
    model_ref: str
    dimension: int
    reasons: tuple[str, ...]
    evidence_digests: tuple[str, ...]
    remote_source_allowed: bool

    @property
    def decision_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-embedding-route/v1",
                "kind": self.kind.value,
                "model_ref": self.model_ref,
                "dimension": self.dimension,
                "reasons": list(self.reasons),
                "evidence_digests": list(self.evidence_digests),
                "remote_source_allowed": self.remote_source_allowed,
            }
        )

    def sanitized(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "model_ref": self.model_ref,
            "dimension": self.dimension,
            "reasons": list(self.reasons),
            "evidence_digests": list(self.evidence_digests),
            "remote_source_allowed": self.remote_source_allowed,
            "decision_digest": self.decision_digest,
        }


def select_embedding_route(
    candidates: Sequence[EmbeddingRouteCandidate],
    *,
    local_model_ref: str,
    local_dimension: int,
    remote_source_allowed: bool,
) -> EmbeddingRouteDecision:
    """Prefer fresh qualified evidence; otherwise use the labelled local fallback."""

    if not local_model_ref.strip() or local_dimension < 1:
        raise ValidationFailed("Yerel embedding fallback profili gecersiz")
    eligible = tuple(candidate for candidate in candidates if candidate.eligible)
    if eligible and remote_source_allowed:
        selected = sorted(
            eligible,
            key=lambda item: (-item.semantic_margin, item.latency_ms, item.model_ref),
        )[0]
        return EmbeddingRouteDecision(
            kind=EmbeddingRouteKind.QUALIFIED_REMOTE,
            model_ref=selected.model_ref,
            dimension=selected.dimension,
            reasons=("fresh-qualified-and-project-data-authorized",),
            evidence_digests=(
                selected.qualification_evidence_digest,
                selected.probe_evidence_digest,
            ),
            remote_source_allowed=True,
        )
    reasons: list[str] = []
    if not eligible:
        reasons.append("qualified-embedding-candidate-missing")
    if not remote_source_allowed:
        reasons.append("remote-project-source-not-authorized")
    return EmbeddingRouteDecision(
        kind=EmbeddingRouteKind.LOCAL_FALLBACK,
        model_ref=local_model_ref,
        dimension=local_dimension,
        reasons=tuple(reasons),
        evidence_digests=tuple(
            sorted({item.qualification_evidence_digest for item in candidates if item.qualified})
        ),
        remote_source_allowed=remote_source_allowed,
    )
