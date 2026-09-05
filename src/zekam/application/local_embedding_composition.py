"""Compose the verified Mac BGE provider from bounded real project chunks."""

from __future__ import annotations

import re
from dataclasses import dataclass

from zekam.application.embedding_provider import (
    EmbeddingPolicy,
    EmbeddingProbeFixture,
    EmbeddingProfile,
)
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import ValidationFailed
from zekam.domain.retrieval import Chunk
from zekam.domain.security import DataClassification
from zekam.infrastructure.embedding.infinity_bge import (
    LocalInfinityBGEProvider,
    build_local_bge_provider,
    default_mac_bge_configuration,
)

_WORD = re.compile(r"\w+", re.UNICODE)
_MAX_CANDIDATES = 32
_MAX_PROBE_CHARACTERS = 12_000


@dataclass(frozen=True, slots=True)
class VerifiedLocalEmbeddingBinding:
    provider: LocalInfinityBGEProvider
    profile: EmbeddingProfile
    policy: EmbeddingPolicy


def _terms(value: str) -> frozenset[str]:
    return frozenset(item.casefold() for item in _WORD.findall(value))


def _dissimilarity(left: str, right: str) -> float:
    left_terms = _terms(left)
    right_terms = _terms(right)
    union = left_terms | right_terms
    if not union:
        return 0.0
    return 1.0 - len(left_terms & right_terms) / len(union)


def _source_ref(chunk: Chunk) -> str:
    locator = chunk.locator
    suffix = ""
    if locator.line_start is not None:
        suffix = f":{locator.line_start}"
    return f"{locator.relative_path}{suffix}#{chunk.chunk_id}"


def project_embedding_probe_fixture(
    chunks: tuple[Chunk, ...],
    *,
    classification: DataClassification,
) -> EmbeddingProbeFixture:
    """Select a bounded, reproducible positive/negative pair from real source chunks."""

    if not isinstance(chunks, tuple) or len(chunks) < 2:
        raise ValidationFailed("Embedding probe en az iki gercek source chunk ister")
    candidates = tuple(
        chunk
        for chunk in chunks[:_MAX_CANDIDATES]
        if chunk.text.strip() and len(chunk.text) <= _MAX_PROBE_CHARACTERS
    )
    if len(candidates) < 2:
        raise ValidationFailed("Embedding probe bounded iki source chunk bulamadi")
    positive = candidates[0]
    negative = max(
        candidates[1:],
        key=lambda item: (_dissimilarity(positive.text, item.text), item.chunk_id),
    )
    if positive.text == negative.text:
        raise ValidationFailed("Embedding probe duplicate source chunk kullanamaz")
    return EmbeddingProbeFixture(
        # Exact positive passage is a technical provider qualification probe. RAG
        # semantic quality is measured separately by the WP-07 golden corpus.
        query=positive.text,
        positive_passage=positive.text,
        negative_passage=negative.text,
        source_refs=(_source_ref(positive), _source_ref(negative)),
        source_digests=(
            digest_of_bytes(positive.text.encode("utf-8")),
            digest_of_bytes(negative.text.encode("utf-8")),
        ),
        classification=classification,
    )


def build_verified_mac_embedding(
    chunks: tuple[Chunk, ...],
    *,
    classification: DataClassification = DataClassification.LOCAL_ONLY,
) -> VerifiedLocalEmbeddingBinding:
    """Discover, fingerprint and probe the actual loopback BGE-M3 provider."""

    provider = build_local_bge_provider(default_mac_bge_configuration())
    result = provider.probe(project_embedding_probe_fixture(chunks, classification=classification))
    policy = EmbeddingPolicy(classification, result.profile.profile_digest)
    if not provider.health().healthy:
        raise ValidationFailed("Verified local embedding provider health gecemedi")
    return VerifiedLocalEmbeddingBinding(provider, result.profile, policy)
