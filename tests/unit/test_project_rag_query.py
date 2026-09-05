from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from zekam.application import project_rag_query
from zekam.application.embedding_provider import (
    EmbeddingBatch,
    EmbeddingHealth,
    EmbeddingPolicy,
    EmbeddingProbeFixture,
    EmbeddingProbeResult,
    EmbeddingProfile,
    EmbeddingProviderKind,
    EmbeddingPurpose,
    EmbeddingReceipt,
)
from zekam.application.retrieval_service import ChunkView
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.knowledge import Locator
from zekam.domain.project import IntegrationStage
from zekam.domain.retrieval import RetrievalChannel, ScoredHit
from zekam.domain.security import DataClassification


def _embedding_profile() -> EmbeddingProfile:
    evidence = digest("probe-evidence")
    return EmbeddingProfile(
        profile_id="local-test",
        display_name="Local test semantic provider",
        provider_kind=EmbeddingProviderKind.LOCAL,
        provider_identity_digest=digest("provider"),
        exact_model_id="BAAI/bge-m3",
        model_revision_fingerprint=digest("revision"),
        dimension=4,
        vector_dtype="float32",
        normalized=True,
        distance_metric="cosine",
        query_prefix="",
        passage_prefix="",
        preprocessor_digest=digest("preprocessor"),
        tokenizer_digest=digest("tokenizer"),
        batch_policy_digest=digest("batch"),
        device_scope="darwin-arm64:mps",
        data_classification_allowlist=(DataClassification.PUBLIC,),
        verified_at="2026-09-02T00:00:00Z",
        probe_evidence_digest=evidence,
    )


class FakeSemanticProvider:
    def __init__(self) -> None:
        self.profile = _embedding_profile()

    def describe(self) -> EmbeddingProfile:
        return self.profile

    def embed_query(self, text: str, policy: EmbeddingPolicy) -> EmbeddingBatch:
        self.profile.assert_policy(policy)
        vector = (1.0, 0.0, 0.0, 0.0)
        return EmbeddingBatch(
            (vector,),
            EmbeddingReceipt(
                purpose=EmbeddingPurpose.QUERY,
                profile_digest=self.profile.profile_digest,
                input_digest=digest(text),
                output_digest=digest(vector),
                vector_count=1,
                dimension=4,
                latency_ms=1,
                provider_call_count=1,
            ),
        )

    def embed_documents(self, texts: tuple[str, ...], policy: EmbeddingPolicy) -> EmbeddingBatch:
        del texts, policy
        raise AssertionError("query test document embedding cagirmamali")

    def probe(self, fixture: EmbeddingProbeFixture) -> EmbeddingProbeResult:
        del fixture
        raise AssertionError("query test probe cagirmamali")

    def health(self) -> EmbeddingHealth:
        return EmbeddingHealth(True, self.profile.profile_digest, None, digest("health"))


class FakeProjectRepository:
    def __init__(self, profile_digest: str | None = None) -> None:
        self.profile_digest = profile_digest

    def active_project_embedding_profile(self):  # type: ignore[no-untyped-def]
        return {
            "profile_id": UUID("00000000-0000-0000-0000-000000000222"),
            "model_ref": "local-test",
            "dimension": 4,
            "query_prefix": "",
            "profile_digest": digest("profile"),
            "provider_profile_digest": self.profile_digest,
            "source_content_digest": digest("source"),
            "source_revision": 1,
            "document_id": "document-1",
        }

    def exact(self, identifiers, *, limit):  # type: ignore[no-untyped-def]
        del identifiers, limit
        return ()

    def lexical(self, query, *, limit):  # type: ignore[no-untyped-def]
        del query, limit
        return (ScoredHit("chunk-1", RetrievalChannel.LEXICAL, 1, 1.0),)

    def dense(self, vector, profile_id, *, limit):  # type: ignore[no-untyped-def]
        del vector, profile_id, limit
        return (ScoredHit("chunk-1", RetrievalChannel.DENSE, 1, 0.01),)

    def views(self, chunk_refs):  # type: ignore[no-untyped-def]
        assert chunk_refs == ("chunk-1",)
        return {
            "chunk-1": ChunkView(
                chunk_id="chunk-1",
                document_id="document-1",
                text="GPU servis sinifi",
                locator=Locator(relative_path="src/GpuService.java", line_start=1, line_end=5),
                content_digest=digest("chunk"),
            )
        }


def _query(
    *, stage: IntegrationStage, index_state: str, with_semantic_provider: bool = False
) -> dict[str, Any]:
    provider = FakeSemanticProvider() if with_semantic_provider else None
    return project_rag_query.query_project_knowledge(
        repository=FakeProjectRepository(
            provider.profile.profile_digest if provider is not None else None
        ),
        project_ref="gpu-fusion",
        query="hangi class?",
        integration_stage=stage,
        integration_detail={
            "knowledge_index": {
                "state": index_state,
                "provider_profile_digest": (
                    provider.profile.profile_digest if provider is not None else None
                ),
                "embedding_profile_digest": digest("profile"),
            }
        },
        embedding_provider=provider,
        embedding_policy=(
            EmbeddingPolicy(DataClassification.PUBLIC, provider.profile.profile_digest)
            if provider is not None
            else None
        ),
    )


def test_ready_project_without_provider_degrades_to_lexical_and_returns_citation() -> None:
    result = _query(stage=IntegrationStage.CURRENT, index_state="ready")
    assert result["state"] == "answered"
    assert result["searched_channels"] == ["exact", "lexical"]
    assert result["degraded_state"] == "lexical-only-degraded"
    assert result["fallback_allowed"] is False
    assert result["citations"][0]["locator"]["relative_path"] == "src/GpuService.java"
    assert str(result["retrieval_digest"]).startswith("sha256:")


def test_ready_project_with_matching_provider_runs_dense_channel() -> None:
    result = _query(
        stage=IntegrationStage.CURRENT,
        index_state="ready",
        with_semantic_provider=True,
    )
    assert result["state"] == "answered"
    assert result["searched_channels"] == ["exact", "lexical", "dense"]
    assert result["degraded_state"] is None
    assert result["channel_counts"]["dense"] == 1


def test_pending_or_stale_index_discards_candidates_and_allows_bounded_fallback() -> None:
    result = _query(stage=IntegrationStage.CURRENT, index_state="pending")
    assert result["state"] == "stale"
    assert result["candidate_count"] == 1
    assert result["citations"] == []
    assert result["fallback_allowed"] is True
    assert result["fallback_kind"] == "bounded-source-researcher"


def test_partial_provider_policy_binding_is_rejected() -> None:
    provider = FakeSemanticProvider()
    with pytest.raises(PolicyViolation, match="birlikte"):
        project_rag_query.query_project_knowledge(
            repository=FakeProjectRepository(provider.profile.profile_digest),
            project_ref="gpu-fusion",
            query="hangi class?",
            integration_stage=IntegrationStage.CURRENT,
            integration_detail={
                "knowledge_index": {
                    "state": "ready",
                    "provider_profile_digest": provider.profile.profile_digest,
                    "embedding_profile_digest": digest("profile"),
                }
            },
            embedding_provider=provider,
        )


def test_stored_index_profile_drift_is_rejected_before_dense_query() -> None:
    provider = FakeSemanticProvider()
    with pytest.raises(PolicyViolation, match="rebuild required"):
        project_rag_query.query_project_knowledge(
            repository=FakeProjectRepository(provider.profile.profile_digest),
            project_ref="gpu-fusion",
            query="hangi class?",
            integration_stage=IntegrationStage.CURRENT,
            integration_detail={
                "knowledge_index": {
                    "state": "ready",
                    "provider_profile_digest": provider.profile.profile_digest,
                    "embedding_profile_digest": digest("different-index-profile"),
                }
            },
            embedding_provider=provider,
            embedding_policy=EmbeddingPolicy(
                DataClassification.PUBLIC, provider.profile.profile_digest
            ),
        )
