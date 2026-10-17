"""Compose verified local embedding providers for bounded project chunks."""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass

from zekam.application.embedding_provider import (
    EmbeddingProvider,
    EmbeddingPolicy,
    EmbeddingProbeFixture,
    EmbeddingProfile,
)
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import ValidationFailed
from zekam.domain.retrieval import Chunk
from zekam.domain.security import DataClassification
from zekam.infrastructure.embedding.onnx_e5 import (
    LocalE5Configuration,
    LocalOnnxE5Provider,
    build_local_e5_provider,
)

_WORD = re.compile(r"\w+", re.UNICODE)
_MAX_CANDIDATES = 32
_MAX_PROBE_CHARACTERS = 12_000


@dataclass(frozen=True, slots=True)
class VerifiedLocalEmbeddingBinding:
    provider: EmbeddingProvider
    profile: EmbeddingProfile
    policy: EmbeddingPolicy


_VERIFIED_E5_CACHE_MAX = 2
_verified_e5_cache_lock = threading.Lock()
_verified_e5_cache: OrderedDict[
    tuple[LocalE5Configuration, DataClassification], VerifiedLocalEmbeddingBinding
] = OrderedDict()


_E5_PROBE_QUERY = "database connection pool configuration"
_E5_PROBE_POSITIVE = "configure the database connection pool size"
_E5_PROBE_NEGATIVE = "optimize image compression quality"
_E5_PROBE_FIXTURE = EmbeddingProbeFixture(
    query=_E5_PROBE_QUERY,
    positive_passage=_E5_PROBE_POSITIVE,
    negative_passage=_E5_PROBE_NEGATIVE,
    source_refs=("synthetic/query", "synthetic/positive"),
    source_digests=(
        digest_of_bytes(_E5_PROBE_POSITIVE.encode("utf-8")),
        digest_of_bytes(_E5_PROBE_NEGATIVE.encode("utf-8")),
    ),
    classification=DataClassification.LOCAL_ONLY,
)


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


def build_verified_local_e5_embedding(
    *,
    classification: DataClassification = DataClassification.LOCAL_ONLY,
    configuration: LocalE5Configuration | None = None,
) -> VerifiedLocalEmbeddingBinding:
    """Build and probe the pinned Windows CPU E5 provider with public text only."""

    provider: LocalOnnxE5Provider = build_local_e5_provider(configuration)
    cache_key = (provider.configuration, classification)
    with _verified_e5_cache_lock:
        cached = _verified_e5_cache.get(cache_key)
        if cached is not None and cached.provider.health().healthy:
            _verified_e5_cache.move_to_end(cache_key)
            return cached
        if cached is not None:
            _verified_e5_cache.pop(cache_key, None)
    fixture = EmbeddingProbeFixture(
        query=_E5_PROBE_FIXTURE.query,
        positive_passage=_E5_PROBE_FIXTURE.positive_passage,
        negative_passage=_E5_PROBE_FIXTURE.negative_passage,
        source_refs=_E5_PROBE_FIXTURE.source_refs,
        source_digests=_E5_PROBE_FIXTURE.source_digests,
        classification=classification,
    )
    result = provider.probe(fixture)
    policy = EmbeddingPolicy(classification, result.profile.profile_digest)
    if not provider.health().healthy:
        raise ValidationFailed("Verified local E5 embedding provider health gecemedi")
    binding = VerifiedLocalEmbeddingBinding(provider, result.profile, policy)
    with _verified_e5_cache_lock:
        _verified_e5_cache[cache_key] = binding
        _verified_e5_cache.move_to_end(cache_key)
        while len(_verified_e5_cache) > _VERIFIED_E5_CACHE_MAX:
            _verified_e5_cache.popitem(last=False)
    return binding
