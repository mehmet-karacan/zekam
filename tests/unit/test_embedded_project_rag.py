"""Embedded hybrid RAG evidence, citation and provider-failure boundaries."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from zekam.application.embedded_project_rag import (
    MAX_QUERY_BYTES,
    EmbeddedProjectRAG,
    build_embedded_project_generation,
)
from zekam.application.embedding_provider import (
    EmbeddingBatch,
    EmbeddingDegradedState,
    EmbeddingHealth,
    EmbeddingPolicy,
    EmbeddingProfile,
    EmbeddingProviderKind,
    EmbeddingPurpose,
    EmbeddingReceipt,
)
from zekam.application.knowledge_index import KnowledgeIndexRecord
from zekam.application.project_knowledge_index import build_project_index_plan
from zekam.application.source_discovery import discover
from zekam.domain.canonical import digest, digest_of_bytes, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import Locator
from zekam.domain.security import DataClassification
from zekam.infrastructure.sqlite.knowledge_index import (
    VECTOR_DIMENSION,
    SQLiteKnowledgeIndex,
)

pytestmark = pytest.mark.unit

PROJECT = "akilli-kasa"
REVISION = "source-revision-1"
TREE = digest("tree-1")
PATH = "belgeler/kararlar/ADR-0006.md"


def _vector(slot: int = 0) -> tuple[float, ...]:
    values = [0.0] * VECTOR_DIMENSION
    values[slot] = 1.0
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="local-bge-test",
        display_name="Local BGE test",
        provider_kind=EmbeddingProviderKind.LOCAL,
        provider_identity_digest=digest("provider"),
        exact_model_id="BAAI/bge-m3",
        model_revision_fingerprint=digest("revision"),
        dimension=VECTOR_DIMENSION,
        vector_dtype="float32",
        normalized=True,
        distance_metric="cosine",
        query_prefix="",
        passage_prefix="",
        preprocessor_digest=digest("preprocessor"),
        tokenizer_digest=digest("tokenizer"),
        batch_policy_digest=digest("batch"),
        device_scope="darwin-arm64:mps",
        data_classification_allowlist=(DataClassification.LOCAL_ONLY,),
        verified_at="2026-09-02T00:00:00Z",
        probe_evidence_digest=digest("probe"),
    )


class QueryProvider:
    def __init__(self, *, mode: str = "ok") -> None:
        self.profile = _profile()
        self.mode = mode
        self.query_calls = 0
        self.query_hook: Callable[[], None] | None = None

    def describe(self) -> EmbeddingProfile:
        return self.profile

    def health(self) -> EmbeddingHealth:
        if self.mode == "unhealthy":
            return EmbeddingHealth(
                False,
                self.profile.profile_digest,
                EmbeddingDegradedState.UNAVAILABLE,
                digest("unhealthy"),
            )
        return EmbeddingHealth(True, self.profile.profile_digest, None, digest("healthy"))

    def embed_query(self, text: str, policy: EmbeddingPolicy) -> EmbeddingBatch:
        self.query_calls += 1
        self.profile.assert_policy(policy)
        if self.query_hook is not None:
            hook = self.query_hook
            self.query_hook = None
            hook()
        if self.mode == "timeout":
            raise TimeoutError("provider timeout")
        vector = (1.0, 0.0) if self.mode == "wrong-dimension" else _vector()
        vectors = () if self.mode == "partial" else (vector,)
        return EmbeddingBatch(
            vectors,
            EmbeddingReceipt(
                purpose=EmbeddingPurpose.QUERY,
                profile_digest=self.profile.profile_digest,
                input_digest=digest(text),
                output_digest=digest(vectors),
                vector_count=1,
                dimension=VECTOR_DIMENSION,
                latency_ms=1,
                provider_call_count=1,
            ),
        )

    def embed_documents(self, texts: tuple[str, ...], policy: EmbeddingPolicy) -> EmbeddingBatch:
        self.profile.assert_policy(policy)
        if self.mode == "document-timeout":
            raise TimeoutError("document provider timeout")
        vector = (1.0, 0.0) if self.mode == "document-wrong-dimension" else _vector()
        vectors = tuple(vector for _ in texts)
        if self.mode == "document-partial":
            vectors = vectors[:-1]
        return EmbeddingBatch(
            vectors,
            EmbeddingReceipt(
                purpose=EmbeddingPurpose.DOCUMENT,
                profile_digest=self.profile.profile_digest,
                input_digest=digest(texts),
                output_digest=digest(vectors),
                vector_count=len(texts),
                dimension=VECTOR_DIMENSION,
                latency_ms=1,
                provider_call_count=1,
            ),
        )

    def probe(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("query test must not probe")


def _rag(
    tmp_path: Path, provider: QueryProvider
) -> tuple[SQLiteKnowledgeIndex, EmbeddedProjectRAG]:
    index = SQLiteKnowledgeIndex(tmp_path / "knowledge.sqlite3", create=True)
    text = "ADR-0006 idempotent dosya ice aktarma SHA-256 ile tekrar engeller."
    index.build_generation(
        (
            KnowledgeIndexRecord(
                chunk_id="chunk-adr-0006",
                project_id=PROJECT,
                source_revision=REVISION,
                source_path=PATH,
                source_digest=digest({"source": PATH}),
                locator=Locator(relative_path=PATH, line_start=1, line_end=3),
                text=text,
                content_digest=digest_of_bytes(text.encode("utf-8")),
                chunk_order=0,
                vector=_vector(),
            ),
        ),
        project_id=PROJECT,
        source_revision=REVISION,
        tree_digest=TREE,
        source_manifest_digest=digest("manifest"),
        embedding_profile_digest=digest("embedding"),
        provider_profile_digest=provider.profile.profile_digest,
        created_at="2026-09-02T00:00:00Z",
    )
    policy = EmbeddingPolicy(DataClassification.LOCAL_ONLY, provider.profile.profile_digest)
    return index, EmbeddedProjectRAG(index, provider, policy)


def _query(rag: EmbeddedProjectRAG, query: str) -> dict[str, Any]:
    return rag.query(
        query,
        project_id=PROJECT,
        expected_source_revision=REVISION,
        expected_tree_digest=TREE,
    )


def test_answer_citation_has_complete_validated_identity_and_rank_trace(tmp_path: Path) -> None:
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)
    try:
        result = _query(rag, "ADR-0006")
        assert result["state"] == "answered"
        citation = result["citations"][0]
        parse_digest(citation["source_id"])
        parse_digest(citation["source_digest"])
        parse_digest(citation["content_digest"])
        assert citation["source_ref"] == PATH
        assert citation["source_revision"] == REVISION
        assert citation["locator_type"] == "project-file"
        assert citation["locator"]["relative_path"] == PATH
        assert citation["rank_trace"]["fused_rank"] == 1
        assert citation["rank_trace"]["exact_match"] is True
        assert citation["rank_trace"]["channel_ranks"]["exact"] == 1
        assert "exact" in citation["retrieval_channels"]
    finally:
        index.close()


def test_reranker_failure_is_explicit_and_preserves_base_fusion(tmp_path: Path) -> None:
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)

    def broken(_query: str, _hits: Any) -> Any:
        raise TimeoutError("reranker timeout")

    try:
        result = _query(replace(rag, reranker=broken), "ADR-0006")
        assert result["state"] == "answered"
        assert result["citations"][0]["source_ref"] == PATH
        assert "reranker basarisiz" in " ".join(result["explanation"])
    finally:
        index.close()


@pytest.mark.parametrize("mode", ["timeout", "partial", "wrong-dimension"])
def test_query_provider_failure_uses_only_explicit_lexical_degraded_state(
    tmp_path: Path, mode: str
) -> None:
    provider = QueryProvider(mode=mode)
    index, rag = _rag(tmp_path, provider)
    try:
        result = _query(rag, "ADR-0006 idempotent dosya ice aktarma")
        assert result["state"] == "lexical-only-degraded"
        assert result["citations"][0]["source_ref"] == PATH
        assert result["degraded_reason"].startswith("query-embedding-failed:")
        assert result["fallback_allowed"] is False
    finally:
        index.close()


def test_unhealthy_provider_skips_dense_call_and_unsupported_query_abstains(
    tmp_path: Path,
) -> None:
    provider = QueryProvider(mode="unhealthy")
    index, rag = _rag(tmp_path, provider)
    try:
        supported = _query(rag, "ADR-0006 idempotent dosya ice aktarma")
        assert supported["state"] == "lexical-only-degraded"
        assert supported["searched_channels"] == ["exact", "lexical"]
        unsupported = _query(rag, "kuantum muz sulama protokolu")
        assert unsupported["state"] == "abstained-index-unavailable"
        assert unsupported["citations"] == []
        assert unsupported["answer_excerpt"] is None
        assert provider.query_calls == 0
    finally:
        index.close()


@pytest.mark.parametrize("query", [None, "", "   ", "x" * (MAX_QUERY_BYTES + 1)])
def test_invalid_query_types_and_bounds_are_rejected(tmp_path: Path, query: Any) -> None:
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)
    try:
        with pytest.raises(ValidationFailed, match="bounded non-empty"):
            _query(rag, query)
    finally:
        index.close()


def test_profile_mismatch_is_stale_and_never_searches(tmp_path: Path) -> None:
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)
    try:
        provider.profile = replace(provider.profile, device_scope="other-device")
        result = _query(rag, "ADR-0006")
        assert result["state"] == "abstained-index-unavailable"
        assert result["reason"] == "profile-stale"
        assert result["citations"] == []
        assert provider.query_calls == 0
    finally:
        index.close()


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("document-partial", PolicyViolation),
        ("document-timeout", TimeoutError),
        ("document-wrong-dimension", ValidationFailed),
    ],
)
def test_generation_provider_failure_cannot_replace_current_generation(
    tmp_path: Path, mode: str, error: type[Exception]
) -> None:
    provider = QueryProvider(mode=mode)
    policy = EmbeddingPolicy(DataClassification.LOCAL_ONLY, provider.profile.profile_digest)
    project_id = UUID("00000000-0000-0000-0000-00000000a111")
    source = tmp_path / "source"
    source.mkdir()
    (source / "service.py").write_text("class PaymentService:\n    pass\n", encoding="utf-8")
    report = discover(source)
    plan = build_project_index_plan(
        project_id=project_id,
        project_slug="akilli-kasa",
        source_root=source,
        source_revision="rev-new",
        expected_tree_digest=report.tree_digest,
    )
    path = tmp_path / "knowledge-build.sqlite3"
    with SQLiteKnowledgeIndex(path, create=True) as index:
        stable = index.build_generation(
            (
                KnowledgeIndexRecord(
                    chunk_id="stable-old",
                    project_id=str(project_id),
                    source_revision="rev-old",
                    source_path=PATH,
                    source_digest=digest("old-source"),
                    locator=Locator(relative_path=PATH, line_start=1, line_end=1),
                    text="STABLE_OLD",
                    content_digest=digest_of_bytes(b"STABLE_OLD"),
                    chunk_order=0,
                    vector=_vector(),
                ),
            ),
            project_id=str(project_id),
            source_revision="rev-old",
            tree_digest=digest("old-tree"),
            source_manifest_digest=digest("old-manifest"),
            embedding_profile_digest=digest("old-embedding"),
            provider_profile_digest=provider.profile.profile_digest,
            created_at="2026-09-02T00:00:00Z",
        )
        with pytest.raises(error):
            build_embedded_project_generation(
                index,
                plan,
                embedding_provider=provider,
                embedding_policy=policy,
            )
        assert index.generation(str(project_id)).generation_digest == stable.generation_digest
        assert index.exact(str(project_id), ("STABLE_OLD",), limit=1)
        assert index.integrity()["status"] == "passed"


def test_concurrent_reindex_cannot_mix_pinned_generation_and_citation(
    tmp_path: Path,
) -> None:
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)
    second = SQLiteKnowledgeIndex(index.path)
    replacement_path = "belgeler/kararlar/ADR-0006-rev-b.md"

    def activate_replacement() -> None:
        text = "ADR-0006 replacement generation B"
        second.build_generation(
            (
                KnowledgeIndexRecord(
                    chunk_id="chunk-rev-b",
                    project_id=PROJECT,
                    source_revision="rev-b",
                    source_path=replacement_path,
                    source_digest=digest("source-b"),
                    locator=Locator(relative_path=replacement_path, line_start=1, line_end=1),
                    text=text,
                    content_digest=digest_of_bytes(text.encode("utf-8")),
                    chunk_order=0,
                    vector=_vector(),
                ),
            ),
            project_id=PROJECT,
            source_revision="rev-b",
            tree_digest=digest("tree-b"),
            source_manifest_digest=digest("manifest-b"),
            embedding_profile_digest=digest("embedding-b"),
            provider_profile_digest=provider.profile.profile_digest,
            created_at="2026-09-02T00:00:01Z",
        )

    provider.query_hook = activate_replacement
    try:
        result = _query(rag, "ADR-0006")
        assert result["state"] == "answered"
        assert result["source_revision"] == REVISION
        assert result["citations"][0]["source_revision"] == REVISION
        assert result["citations"][0]["source_ref"] == PATH
        assert second.generation(PROJECT).source_revision == "rev-b"
    finally:
        second.close()
        index.close()
