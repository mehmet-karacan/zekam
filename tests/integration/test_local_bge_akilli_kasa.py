"""Real offline Mac BGE proof over a small read-only Akilli Kasa fixture."""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

import pytest

from zekam.application.embedding_provider import EmbeddingPolicy, EmbeddingProbeFixture
from zekam.application.local_embedding_composition import build_verified_mac_embedding
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.knowledge import Locator, UnitKind
from zekam.domain.retrieval import Chunk, estimate_tokens
from zekam.domain.security import DataClassification
from zekam.infrastructure.embedding.infinity_bge import (
    MAX_BATCH_DELTA,
    MAX_REPEAT_DELTA,
    MIN_BATCH_COSINE,
    build_local_bge_provider,
    default_mac_bge_configuration,
)

pytestmark = pytest.mark.integration


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.skipif(
    os.environ.get("ZEKAM_RUN_LOCAL_BGE_E2E") != "1",
    reason="real local BGE acceptance is explicit",
)
def test_real_local_bge_is_offline_semantic_deterministic_and_read_only() -> None:
    root = Path(os.environ.get("ZEKAM_AKILLI_KASA_ROOT", "/Users/mkaracan/Projeler/akilli-kasa"))
    positive_ref = "belgeler/kararlar/ADR-0006-idempotent-dosya-ice-aktarma.md"
    negative_ref = "belgeler/kararlar/ADR-0005-parasal-tutarlarda-decimal-kullanimi.md"
    third_ref = "belgeler/kararlar/ADR-0010-frontend-backend-api-siniri.md"
    selected = tuple(root / ref for ref in (positive_ref, negative_ref, third_ref))
    if not all(path.is_file() for path in selected):
        pytest.skip("Akilli Kasa bounded fixture is unavailable")
    before = tuple((path.stat().st_mtime_ns, _sha(path)) for path in selected)
    payloads = tuple(path.read_bytes() for path in selected)
    texts = tuple(payload.decode("utf-8") for payload in payloads)
    fixture = EmbeddingProbeFixture(
        query="Dosya ice aktarma islemi neden idempotent olmalidir?",
        positive_passage=texts[0],
        negative_passage=texts[1],
        source_refs=(positive_ref, negative_ref),
        source_digests=(digest_of_bytes(payloads[0]), digest_of_bytes(payloads[1])),
        classification=DataClassification.PUBLIC,
    )

    provider = build_local_bge_provider(default_mac_bge_configuration())
    probe = provider.probe(fixture)
    profile = provider.describe()
    policy = EmbeddingPolicy(DataClassification.PUBLIC, profile.profile_digest)
    documents = provider.embed_documents((texts[0], texts[0], texts[1], texts[2]), policy)
    query = provider.embed_query(fixture.query, policy)
    scores = tuple(
        sum(a * b for a, b in zip(query.vectors[0], vector, strict=True))
        for vector in documents.vectors
    )

    assert profile.exact_model_id == "BAAI/bge-m3"
    assert profile.dimension == 1024
    assert profile.device_scope.endswith(":mps")
    assert profile.provider_kind.value == "local"
    assert probe.semantic_margin > 0.05
    assert probe.max_repeat_delta <= MAX_REPEAT_DELTA
    assert probe.max_batch_delta <= MAX_BATCH_DELTA
    assert probe.batch_cosine >= MIN_BATCH_COSINE
    assert documents.vectors[0] == documents.vectors[1]
    assert scores[0] > scores[2]
    assert all(
        len(vector) == 1024
        and all(math.isfinite(value) for value in vector)
        and math.isclose(sum(value * value for value in vector), 1.0, rel_tol=1e-4)
        for vector in (*documents.vectors, *query.vectors)
    )
    assert provider.health().healthy is True
    chunks = tuple(
        Chunk(
            chunk_id=f"akilli-kasa-bge-{index}",
            document_id="akilli-kasa-bounded-fixture",
            text=text,
            locator=Locator(relative_path=ref),
            kind=UnitKind.PARAGRAPH,
            token_count=estimate_tokens(text),
            order=index,
            profile_digest=digest_of_bytes(b"bounded-chunk-profile"),
        )
        for index, (ref, text) in enumerate(
            zip((positive_ref, negative_ref, third_ref), texts, strict=True)
        )
    )
    composed = build_verified_mac_embedding(chunks, classification=DataClassification.PUBLIC)
    assert composed.profile.profile_digest == composed.policy.expected_profile_digest
    assert composed.provider.health().healthy is True
    assert tuple((path.stat().st_mtime_ns, _sha(path)) for path in selected) == before
