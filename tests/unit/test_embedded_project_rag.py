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
from zekam.domain.retrieval import AnswerKind, GenerationState, RetrievalState
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
    tmp_path: Path,
    provider: QueryProvider,
    *,
    path: str = PATH,
    object_name: str | None = None,
    text: str = "ADR-0006 idempotent dosya ice aktarma SHA-256 ile tekrar engeller.",
) -> tuple[SQLiteKnowledgeIndex, EmbeddedProjectRAG]:
    index = SQLiteKnowledgeIndex(tmp_path / "knowledge.sqlite3", create=True)
    index.build_generation(
        (
            KnowledgeIndexRecord(
                chunk_id="chunk-adr-0006",
                project_id=PROJECT,
                source_revision=REVISION,
                source_path=path,
                source_digest=digest({"source": path}),
                locator=Locator(
                    relative_path=path,
                    line_start=None if object_name else 1,
                    line_end=None if object_name else 3,
                    object_name=object_name,
                ),
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


def _rag_multi(
    tmp_path: Path, provider: QueryProvider, *, text_a: str, text_b: str
) -> tuple[SQLiteKnowledgeIndex, EmbeddedProjectRAG]:
    """Build a generation with two distinct chunks to exercise multi-citation."""
    index = SQLiteKnowledgeIndex(tmp_path / "knowledge.sqlite3", create=True)
    index.build_generation(
        (
            KnowledgeIndexRecord(
                chunk_id="chunk-a",
                project_id=PROJECT,
                source_revision=REVISION,
                source_path=PATH,
                source_digest=digest({"source": "a"}),
                locator=Locator(relative_path=PATH, line_start=1, line_end=3),
                text=text_a,
                content_digest=digest_of_bytes(text_a.encode("utf-8")),
                chunk_order=0,
                vector=_vector(),
            ),
            KnowledgeIndexRecord(
                chunk_id="chunk-b",
                project_id=PROJECT,
                source_revision=REVISION,
                source_path="belgeler/kararlar/ADR-0005.md",
                source_digest=digest({"source": "b"}),
                locator=Locator(
                    relative_path="belgeler/kararlar/ADR-0005.md", line_start=1, line_end=3
                ),
                text=text_b,
                content_digest=digest_of_bytes(text_b.encode("utf-8")),
                chunk_order=1,
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


# -- WP6 (B07): multi-object vs single-object evidence contract ----------------

ALPHA = "OBJ_ALPHA"
BETA = "OBJ_BETA"


def test_wp6_a1_multi_file_comparison_cites_both_objects(tmp_path: Path) -> None:
    """WP6-A-1 (B07): object A in one file, object B in another -> comparison query
    cites BOTH (no false abstain from the single-chunk requirement).  On the
    baseline the blanket single-chunk-everything filter dropped the pair; after
    the intent-aware collective contract both objects are cited."""
    provider = QueryProvider()
    index, rag = _rag_multi(
        tmp_path,
        provider,
        text_a=f"{ALPHA} birinci dosyada tanimlanir birinci islem.",
        text_b=f"{BETA} ikinci dosyada yazin islem.",
    )
    try:
        result = _query(rag, f"{ALPHA} {BETA} farki nedir")
        assert result["state"] == "answered"
        assert result["query_intent"] == "multi-object-comparison"
        assert result["uncovered_identifiers"] == []
        cited = {item["chunk_id"] for item in result["citations"]}
        assert "chunk-a" in cited
        assert "chunk-b" in cited
    finally:
        index.close()


def test_wp6_a2_single_object_strict_support_preserved(tmp_path: Path) -> None:
    """WP6-A-2 (B07): a single-object query still requires genuine object support
    in a chunk.  A missing object must abstain even though a near-by object exists
    (strict per-object verification preserved; not loosened)."""
    provider = QueryProvider()
    index, rag = _rag(
        tmp_path, provider, text=f"{ALPHA} tek dosyada tanimlama islem aciklamasi."
    )
    try:
        supported = _query(rag, ALPHA)
        assert supported["state"] == "answered"
        assert supported["citations"][0]["chunk_id"] == "chunk-adr-0006"

        missing = _query(rag, "OBJ_GAMA_YOK")
        assert missing["state"] == "abstained-low-evidence"
        assert missing["citations"] == []
        assert missing["evidence_sufficient"] is False
    finally:
        index.close()


def test_wp6_a3_relationship_no_fabricated_edge(tmp_path: Path) -> None:
    """WP6-A-3 (B07): A and B both present but NO call-site / dependency edge ->
    the relationship query must abstain (no-edge) and never claim a relationship
    fabricated from mere co-presence of names."""
    provider = QueryProvider()
    index, rag = _rag_multi(
        tmp_path,
        provider,
        text_a=f"{ALPHA} birinci dosyada tanimlanir.",
        text_b=f"{BETA} ikinci dosyada tanimlanir.",
    )
    try:
        result = _query(rag, f"{ALPHA} {BETA} arasindaki bagimliligi")
        assert result["query_intent"] == "relationship-call-chain"
        # Both names are present (they cover the identifiers) but no edge evidence
        # exists, so the system abstains with an explicit no-edge state rather
        # than fabricating a dependency.
        assert result["relationship_state"] == "abstained-no-edge"
        assert result["state"] == "abstained-no-edge"
        assert result["citations"] == []
        assert result["evidence_sufficient"] is False
    finally:
        index.close()


def test_wp6_a3b_relationship_with_real_edge_is_answered(tmp_path: Path) -> None:
    """WP6-A-3 (B07) positive control: when a genuine call-site / dependency edge
    exists in the gathered source, a relationship question IS answered (the
    no-fabrication rule only abstains for real missing edges)."""
    provider = QueryProvider()
    index, rag = _rag_multi(
        tmp_path,
        provider,
        text_a=f"{ALPHA} cagirir ve {BETA} bagimli olan islem cagrisi.",
        text_b=f"{BETA} ikinci dosyada tanimlanir.",
    )
    try:
        result = _query(rag, f"{ALPHA} {BETA} arasindaki bagimliligi")
        assert result["query_intent"] == "relationship-call-chain"
        assert result["relationship_state"] is None
        assert result["state"] == "answered"
    finally:
        index.close()


def test_wp6_b1_excerpt_digest_is_derived_and_linked(tmp_path: Path) -> None:
    """WP6-B-2 (B08): the answer excerpt's digest is a DERIVED digest of the
    excerpt window, kept distinct from the full source/chunk ``content_digest``,
    and the locator relationship is carried accurately (not a wrong line-range on
    an arbitrarily truncated text)."""
    provider = QueryProvider()
    long_text = " ".join(["kelime"] * 800)
    index, rag = _rag(
        tmp_path, provider, text=f"{long_text} {ALPHA} belgesel uzun satirlar.",
    )
    try:
        result = _query(rag, ALPHA)
        assert result["state"] == "answered"
        # answer_excerpt stays a string (backward compatible) and is a window,
        # not a blind first-500 slice necessarily identical to text[:500].
        assert isinstance(result["answer_excerpt"], str)
        meta = result["answer_excerpt_meta"]
        assert meta is not None
        assert meta["truncated"] is True
        # Derived digest: exactly the digest of the excerpt text.
        assert meta["excerpt_digest"] == digest_of_bytes(result["answer_excerpt"].encode("utf-8"))
        # Distinct from the full source/chunk digest.
        assert meta["excerpt_digest"] != meta["content_digest"]
        assert meta["chunk_id"] == result["citations"][0]["chunk_id"]
        assert meta["source_ref"] == PATH
        assert meta["locator"]["relative_path"] == PATH
        assert len(result["answer_excerpt"]) <= 500
    finally:
        index.close()


def test_wp6_b2_rag_budget_reserves_second_object(tmp_path: Path) -> None:
    """WP6-B-1 (B08) end-to-end: with a tight budget a multi-object comparison
    still cites BOTH objects — the first object does not consume the whole
    budget (reserved for all mandate objects)."""
    provider = QueryProvider()
    index, rag = _rag_multi(
        tmp_path,
        provider,
        text_a=f"{ALPHA} " + " ".join(["filler"] * 400) + " birinci",
        text_b=f"{BETA} ikinci dosyada islem tanimi.",
    )
    try:
        result = rag.query(
            f"{ALPHA} {BETA} farki nedir",
            project_id=PROJECT,
            expected_source_revision=REVISION,
            expected_tree_digest=TREE,
            token_budget=120,
        )
        assert result["state"] == "answered"
        cited = {item["chunk_id"] for item in result["citations"]}
        assert "chunk-a" in cited
        assert "chunk-b" in cited
        assert result["tokens_used"] <= 120
    finally:
        index.close()




def test_multiple_technical_identifiers_abstain_without_single_chunk_support(
    tmp_path: Path,
) -> None:
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)
    try:
        result = _query(rag, "ADR-0006 MISSING_IDENTIFIER")
        assert result["state"] == "abstained-low-evidence"
        assert result["evidence_sufficient"] is False
        assert result["single_chunk_identifier_support"] is False
        assert result["citations"] == []
        assert result["answer_excerpt"] is None
    finally:
        index.close()


def test_database_object_citation_is_typed_from_locator(tmp_path: Path) -> None:
    provider = QueryProvider()
    object_name = "GPU_USER.LOG_REPORT_CREATION:TABLE"
    index, rag = _rag(
        tmp_path,
        provider,
        path="oracle/table/LOG_REPORT_CREATION",
        object_name=object_name,
        text=f"Object: {object_name}\nCREATE TABLE LOG_REPORT_CREATION (ID NUMBER)",
    )
    try:
        result = _query(rag, object_name)
        citation = result["citations"][0]
        assert citation["locator_type"] == "database-object"
        assert citation["locator"]["object_name"] == object_name
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
        assert unsupported["state"] == "abstained-low-evidence"
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


def test_profile_mismatch_keeps_lexical_snapshot_search(tmp_path: Path) -> None:
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)
    try:
        provider.profile = replace(
            provider.profile, model_revision_fingerprint=digest("other-revision")
        )
        result = _query(rag, "ADR-0006")
        assert result["state"] == "lexical-only-degraded"
        assert result["index_freshness"] == "stale"
        assert result["stale_reasons"] == ["embedding-profile-stale"]
        assert result["citations"][0]["source_ref"] == PATH
        assert provider.query_calls == 0
    finally:
        index.close()


@pytest.mark.parametrize(
    ("expected_revision", "expected_tree", "message"),
    [
        ("newer-revision", TREE, "source revision binding drift"),
        (REVISION, digest("newer-tree"), "source tree binding drift"),
    ],
)
def test_generation_identity_mismatch_fails_closed(
    tmp_path: Path, expected_revision: str, expected_tree: str, message: str
) -> None:
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)
    try:
        with pytest.raises(PolicyViolation, match=message):
            rag.query(
                "ADR-0006",
                project_id=PROJECT,
                expected_source_revision=expected_revision,
                expected_tree_digest=expected_tree,
            )
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
        # NOTE: WP2 B05 makes a *pure single-object exact lookup* (e.g. the bare
        # identifier "ADR-0006") skip the dense channel entirely.  This test's
        # concurrent-reindex safety intent needs a query that actually invokes
        # dense embedding (so ``provider.query_hook`` fires while a competing
        # writer publishes a new generation).  A prose/explanation form keeps the
        # dense channel enabled and still pins to the original generation, which
        # is the invariant under test.  (Dense fast-path is not a concurrency
        # escape hatch; see WP3/WP4.)
        result = _query(rag, "ADR-0006 idempotent dosya ice aktarma")
        assert provider.query_calls >= 1
        assert result["state"] == "answered"
        assert result["source_revision"] == REVISION
        assert result["citations"][0]["source_revision"] == REVISION
        assert result["citations"][0]["source_ref"] == PATH
        assert second.generation(PROJECT).source_revision == "rev-b"
    finally:
        second.close()
        index.close()


EXPECTED_CITATION_KEYS = {
    "source_id",
    "project_scope",
    "source_ref",
    "source_revision",
    "source_digest",
    "content_digest",
    "chunk_id",
    "locator_type",
    "locator",
    "retrieval_channels",
    "rank_trace",
}
EXPECTED_RANK_TRACE_KEYS = {
    "fused_rank",
    "rrf_score",
    "exact_match",
    "channel_ranks",
}


def test_wp4_a1_multiple_citations_use_one_bulk_hydration_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP4 regression: the per-citation N+1 source_identity loop is eliminated.

    A query that produces multiple citations must hydrate identity fields with
    exactly ONE bulk source_identities call and ZERO per-citation
    source_identity calls.  On the baseline (per-citation loop) this test would
    FAIL (one source_identity per citation); after the WP4 bulk read it passes.
    """
    provider = QueryProvider()
    index, rag = _rag_multi(
        tmp_path,
        provider,
        text_a="dosya ice aktarma AES onerisi disk yazma guvenligi",
        text_b="dosya ice aktarma Decimal sifreleme modulu kaynak",
    )
    calls = {"bulk": 0, "single": 0}
    original_bulk = index.source_identities
    original_single = index.source_identity

    def spy_bulk(*args: Any, **kwargs: Any) -> Any:
        calls["bulk"] += 1
        return original_bulk(*args, **kwargs)

    def spy_single(*args: Any, **kwargs: Any) -> Any:
        calls["single"] += 1
        return original_single(*args, **kwargs)

    monkeypatch.setattr(index, "source_identities", spy_bulk)
    monkeypatch.setattr(index, "source_identity", spy_single)
    try:
        result = _query(rag, "dosya ice aktarma salt okunur dosya")
        assert result["state"] == "answered"
        assert len(result["citations"]) >= 2
        assert calls["bulk"] == 1, "identity hydration must be a single bulk call"
        assert calls["single"] == 0, "the N+1 per-citation source_identity loop must be gone"
    finally:
        index.close()


def test_wp4_a2_multiple_citations_preserve_output_contract(tmp_path: Path) -> None:
    """WP4 regression: the multi-citation JSON output contract is intact.

    Every citation must still carry the full field set (source_id, project_scope,
    source_ref, source_revision, source_digest, content_digest, chunk_id,
    locator_type, locator, retrieval_channels, rank_trace) with the complete
    rank_trace shape.  Guards against silent field loss in the bulk path.
    """
    provider = QueryProvider()
    index, rag = _rag_multi(
        tmp_path,
        provider,
        text_a="dosya ice aktarma AES onerisi disk yazma guvenligi",
        text_b="dosya ice aktarma Decimal sifreleme modulu kaynak",
    )
    try:
        result = _query(rag, "dosya ice aktarma salt okunur dosya")
        assert result["state"] == "answered"
        citation_ids = {item["chunk_id"] for item in result["citations"]}
        assert len(citation_ids) >= 2
        for citation in result["citations"]:
            assert set(citation) == EXPECTED_CITATION_KEYS
            assert set(citation["rank_trace"]) == EXPECTED_RANK_TRACE_KEYS
            assert set(citation["locator"]) >= {"relative_path"}
            # Same per-chunk identity must match the single-chunk contract.
            single = index.source_identity(
                PROJECT, citation["chunk_id"], generation_digest=result["generation_digest"]
            )
            assert citation["source_id"] == single["source_id"]
            assert citation["source_ref"] == single["source_ref"]
            assert citation["source_revision"] == single["source_revision"]
            assert citation["source_digest"] == single["source_digest"]
            assert citation["content_digest"] == single["content_digest"]
    finally:
        index.close()


def test_wp4_a3_stale_chunk_in_cited_set_fails_closed(tmp_path: Path) -> None:
    """WP4 security regression: a corrupt cited chunk still fails closed.

    A body/content content_digest mismatch anywhere in the cited set must raise
    PolicyViolation through the query path (never silently bypass integrity in
    the name of performance).  The precise bulk-path validation is asserted in
    test_sqlite_knowledge_index.py::test_source_identities_stale_chunk_still_raises_policy_violation;
    this guards the end-to-end query path too.
    """
    provider = QueryProvider()
    index, rag = _rag_multi(
        tmp_path,
        provider,
        text_a="dosya ice aktarma AES onerisi disk yazma guvenligi",
        text_b="dosya ice aktarma Decimal sifreleme modulu kaynak",
    )
    try:
        index._connection.execute(
            "update chunk set body='TAMPERED UNSUPPORTED CLAIM' where id='chunk-b'"
        )
        with pytest.raises(PolicyViolation, match=r"Citation .* drift|body/content digest drift"):
            _query(rag, "dosya ice aktarma salt okunur dosya")
    finally:
        index.close()


# -- WP7 (B08): retrieval output vs generated-answer separation ------------------


def test_wp7_a1_retrieval_vs_answer_separation(tmp_path: Path) -> None:
    """WP7-A-1 (B08): an evidence-answered query returns the additive
    retrieval/answer semantics that make clear NO generated answer was produced.

    ``retrieval_state`` = answered-evidence, ``generation_state`` = not_generated,
    ``answer_kind`` = retrieval_evidence, while the legacy ``state`` field stays
    "answered" for backward compatibility.
    """
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)
    try:
        result = _query(rag, "ADR-0006")
        assert result["state"] == "answered"  # legacy v1 contract preserved
        assert result["evidence_found"] is True
        assert result["retrieval_state"] == RetrievalState.ANSWERED_EVIDENCE.value
        assert result["generation_state"] == GenerationState.NOT_GENERATED.value
        assert result["answer_kind"] == AnswerKind.RETRIEVAL_EVIDENCE.value
        # The retrieval-only path never claims a generated answer.
        assert result["answer_kind"] != AnswerKind.GENERATED_ANSWER.value
        assert result["generation_state"] != GenerationState.GENERATED.value
        assert "answered" in result["state"]
    finally:
        index.close()


def test_wp7_a2_evidence_packet_reaches_consumer_bounded(tmp_path: Path) -> None:
    """WP7-A-2 (B08): the retrieval evidence packet reaches the consumer with full
    provenance (source_ref/source_revision/source_digest/content_digest/locator),
    score/selection trace and freshness — all present and bounded (only used
    chunks, never the whole corpus)."""
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)
    try:
        result = _query(rag, "ADR-0006")
        assert result["state"] == "answered"
        citation = result["citations"][0]
        for key in (
            "source_ref",
            "source_revision",
            "source_digest",
            "content_digest",
            "locator",
            "rank_trace",
            "retrieval_channels",
        ):
            assert key in citation, key
        parse_digest(citation["source_digest"])
        parse_digest(citation["content_digest"])
        assert citation["source_ref"] == PATH
        assert citation["source_revision"] == REVISION
        assert set(citation["rank_trace"]) == EXPECTED_RANK_TRACE_KEYS
        # freshness + trace present and bounded.
        assert result["index_freshness"] in {"current", "stale"}
        assert result["searched_channels"]
        assert len(result["citations"]) <= len(result["used_chunk_ids"]) + 1
        # The excerpt is a bounded evidence window, not a whole-corpus hydrate.
        assert len(result["answer_excerpt"] or "") <= 500
        assert isinstance(result["answer_excerpt"], str)
    finally:
        index.close()


def test_wp7_c2_no_fabricated_generation_when_abstained(tmp_path: Path) -> None:
    """WP7-C-2 (B08): in abstained low-evidence state generation is never claimed
    and answer_kind is NOT generated_answer — no fabricated answer."""
    provider = QueryProvider()
    index, rag = _rag(
        tmp_path, provider, text=f"{ALPHA} tek dosyada tanimlama islem aciklamasi."
    )
    try:
        result = _query(rag, "OBJ_GAMA_YOK")
        assert result["state"] == "abstained-low-evidence"
        assert result["evidence_found"] is False
        assert result["retrieval_state"] == RetrievalState.ABSTAINED_LOW_EVIDENCE.value
        assert result["generation_state"] == GenerationState.NOT_GENERATED.value
        assert result["answer_kind"] == AnswerKind.ABSTAINED.value
        assert result["answer_kind"] != AnswerKind.GENERATED_ANSWER.value
        assert result["citations"] == []
    finally:
        index.close()


def test_wp7_c2_no_fabricated_generation_providerless(tmp_path: Path) -> None:
    """WP7-C-2 (B08): even with partial lexical-onl evidence (dense provider
    unavailable / provider-less), generation stays not_generated and the result is
    never presented as a fabricated generated answer."""
    provider = QueryProvider(mode="timeout")
    index, rag = _rag(tmp_path, provider)
    try:
        result = _query(rag, "ADR-0006 idempotent dosya ice aktarma")
        assert result["state"] == "lexical-only-degraded"
        assert result["evidence_found"] is True
        assert result["retrieval_state"] == RetrievalState.DEGRADED_PROVIDER_UNAVAILABLE.value
        assert result["generation_state"] == GenerationState.NOT_GENERATED.value
        assert result["answer_kind"] == AnswerKind.RETRIEVAL_EVIDENCE.value
        assert result["answer_kind"] != AnswerKind.GENERATED_ANSWER.value
        assert "lexical-only-degraded" in result["state"]
    finally:
        index.close()


def test_wp7_c2_stale_index_is_not_a_generated_answer(tmp_path: Path) -> None:
    """WP7-C-2 (B08): the fail-closed stale/index-unavailable result also carries
    no fabricated generated answer."""
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)
    try:
        # Force generation-missing by querying a different project id.
        stale = rag.query(
            "ADR-0006",
            project_id="başka-proje",
            expected_source_revision=REVISION,
            expected_tree_digest=TREE,
        )
        assert stale["state"] == "abstained-index-unavailable"
        assert stale["evidence_found"] is False
        assert stale["generation_state"] == GenerationState.NOT_GENERATED.value
        assert stale["answer_kind"] == AnswerKind.ABSTAINED.value
        assert stale["answer_kind"] != AnswerKind.GENERATED_ANSWER.value
    finally:
        index.close()


def test_wp7_b1_generated_answer_contract_ready_but_defaults_retrieval_only(
    tmp_path: Path,
) -> None:
    """WP7-B-1 (B08): the enum/contract includes the generated-answer states
    (schema readiness) but the core route defaults to retrieval-only.  A future
    authorized synthesis path would set generation_state/answer_kind distinctly —
    the core never emits them today."""
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)
    try:
        result = _query(rag, "ADR-0006")
        # Contract readiness: the enum members exist for a synthesized path.
        assert GenerationState.GENERATED.value == "generated"
        assert GenerationState.GENERATION_FAILED.value == "generation-failed"
        assert AnswerKind.GENERATED_ANSWER.value == "generated_answer"
        # The core route defaults to retrieval-only.
        assert result["generation_state"] == GenerationState.NOT_GENERATED.value
        assert result["answer_kind"] == AnswerKind.RETRIEVAL_EVIDENCE.value
        assert result["answer_kind"] != AnswerKind.GENERATED_ANSWER.value
    finally:
        index.close()
