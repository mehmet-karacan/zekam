"""Fresh-source embedded project indexing and evidence-gated hybrid retrieval."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
from typing import Any

from zekam.application.embedding_provider import EmbeddingPolicy, EmbeddingProvider
from zekam.application.knowledge_index import (
    KnowledgeGeneration,
    KnowledgeIndexPort,
    KnowledgeIndexRecord,
)
from zekam.application.project_knowledge_index import ProjectIndexPlan
from zekam.application.retrieval_service import Reranker, RetrievalService, RetrievalTrace
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.retrieval import AnswerState, ScoredHit, extract_identifiers

_TOKEN = re.compile(r"\w+", re.UNICODE)
MAX_QUERY_BYTES = 16 * 1024
DEFAULT_DENSE_EVIDENCE_THRESHOLD = 0.49
DEFAULT_DENSE_MARGIN_THRESHOLD = 0.04
DEFAULT_LEXICAL_COVERAGE_THRESHOLD = 0.50


def _tokens(value: str) -> frozenset[str]:
    return frozenset(item.casefold() for item in _TOKEN.findall(value) if len(item) > 1)


def build_embedded_project_generation(
    index: KnowledgeIndexPort,
    plan: ProjectIndexPlan,
    *,
    embedding_provider: EmbeddingProvider,
    embedding_policy: EmbeddingPolicy,
    created_at: dt.datetime | None = None,
) -> tuple[ProjectIndexPlan, KnowledgeGeneration]:
    """Embed every bounded source chunk before atomically activating the generation."""

    profile = embedding_provider.describe()
    profile.assert_policy(embedding_policy)
    if (
        plan.embedding_profile.dimension != profile.dimension
        or plan.embedding_profile.model_ref
        not in {profile.exact_model_id, f"openai/{profile.exact_model_id}"}
    ):
        raise PolicyViolation("Embedded project plan/provider drift")
    bound_plan = replace(
        plan,
        embedding_profile=replace(
            plan.embedding_profile,
            provider_profile_digest=profile.profile_digest,
        ),
    )
    source_digests = {
        item.relative_path: item.content_digest for item in bound_plan.discovery.files
    }
    vectors: dict[str, tuple[float, ...]] = {}
    for offset in range(0, len(bound_plan.chunks), 8):
        chunks = bound_plan.chunks[offset : offset + 8]
        batch = embedding_provider.embed_documents(
            tuple(chunk.text for chunk in chunks), embedding_policy
        )
        if (
            len(batch.vectors) != len(chunks)
            or batch.receipt.vector_count != len(chunks)
            or batch.receipt.profile_digest != profile.profile_digest
            or batch.receipt.dimension != profile.dimension
        ):
            raise PolicyViolation("Embedded project provider partial/receipt drift")
        for chunk, vector in zip(chunks, batch.vectors, strict=True):
            profile.validate_vector(vector)
            vectors[chunk.chunk_id] = vector
    if len(vectors) != len(bound_plan.chunks):
        raise PolicyViolation("Embedded project vector set incomplete")
    records = tuple(
        KnowledgeIndexRecord(
            chunk_id=chunk.chunk_id,
            project_id=str(bound_plan.project_id),
            source_revision=bound_plan.source_revision,
            source_path=str(chunk.locator.relative_path),
            source_digest=source_digests[str(chunk.locator.relative_path)],
            locator=chunk.locator,
            text=chunk.text,
            content_digest=digest_of_bytes(chunk.text.encode("utf-8")),
            chunk_order=chunk.order,
            vector=vectors[chunk.chunk_id],
        )
        for chunk in bound_plan.chunks
    )
    moment = created_at or dt.datetime.now(dt.UTC)
    generation = index.build_generation(
        records,
        project_id=str(bound_plan.project_id),
        source_revision=bound_plan.source_revision,
        tree_digest=bound_plan.tree_digest,
        source_manifest_digest=digest_of_bytes(bound_plan.manifest),
        embedding_profile_digest=bound_plan.embedding_profile.profile_digest,
        provider_profile_digest=profile.profile_digest,
        created_at=moment.isoformat().replace("+00:00", "Z"),
    )
    return bound_plan, generation


@dataclass(slots=True)
class EmbeddedProjectSearchBackend:
    index: KnowledgeIndexPort
    project_id: str
    generation_digest: str
    embedding_provider: EmbeddingProvider
    embedding_policy: EmbeddingPolicy
    source_type: str = "embedded-project-knowledge"
    dense_enabled: bool = True
    dense_failure_reason: str | None = None
    last_exact: tuple[ScoredHit, ...] = ()
    last_lexical: tuple[ScoredHit, ...] = ()
    last_dense: tuple[ScoredHit, ...] = ()

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
        self.last_exact = self.index.exact(
            self.project_id,
            identifiers,
            limit=limit,
            generation_digest=self.generation_digest,
        )
        return self.last_exact

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        self.last_lexical = self.index.lexical(
            self.project_id,
            query,
            limit=limit,
            generation_digest=self.generation_digest,
        )
        return self.last_lexical

    def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        if not self.dense_enabled:
            self.last_dense = ()
            return ()
        try:
            profile = self.embedding_provider.describe()
            profile.assert_policy(self.embedding_policy)
            batch = self.embedding_provider.embed_query(query, self.embedding_policy)
            if (
                len(batch.vectors) != 1
                or batch.receipt.vector_count != 1
                or batch.receipt.profile_digest != profile.profile_digest
                or batch.receipt.dimension != profile.dimension
            ):
                raise PolicyViolation("Embedded query provider exact tek vector ister")
            profile.validate_vector(batch.vectors[0])
        except Exception as exc:
            self.dense_failure_reason = f"query-embedding-failed:{type(exc).__name__}"
            self.last_dense = ()
            return ()
        self.last_dense = self.index.dense(
            self.project_id,
            batch.vectors[0],
            limit=limit,
            generation_digest=self.generation_digest,
        )
        return self.last_dense


@dataclass(frozen=True, slots=True)
class EmbeddedProjectRAG:
    index: KnowledgeIndexPort
    embedding_provider: EmbeddingProvider
    embedding_policy: EmbeddingPolicy
    reranker: Reranker | None = None
    dense_evidence_threshold: float = DEFAULT_DENSE_EVIDENCE_THRESHOLD
    dense_margin_threshold: float = DEFAULT_DENSE_MARGIN_THRESHOLD
    lexical_coverage_threshold: float = DEFAULT_LEXICAL_COVERAGE_THRESHOLD

    def _stale_result(self, query: str, *, project_id: str, reason: str) -> dict[str, Any]:
        value = {
            "schema": "zekam-embedded-rag-result/v1",
            "state": "abstained-index-unavailable",
            "project_id": project_id,
            "query_digest": digest({"query": query}),
            "reason": reason,
            "citations": [],
            "searched_channels": [],
            "fallback_allowed": False,
        }
        value["retrieval_digest"] = digest(value)
        return value

    def query(
        self,
        query: str,
        *,
        project_id: str,
        expected_source_revision: str,
        expected_tree_digest: str,
        token_budget: int = 1200,
    ) -> dict[str, Any]:
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query.encode("utf-8")) > MAX_QUERY_BYTES
        ):
            raise ValidationFailed("RAG query bounded non-empty text olmali")
        try:
            generation = self.index.generation(project_id)
        except ValidationFailed:
            return self._stale_result(query, project_id=project_id, reason="generation-missing")
        if (
            generation.state != "ready"
            or generation.source_revision != expected_source_revision
            or generation.tree_digest != expected_tree_digest
        ):
            return self._stale_result(query, project_id=project_id, reason="source-stale")
        if generation.provider_profile_digest != self.embedding_policy.expected_profile_digest:
            return self._stale_result(query, project_id=project_id, reason="profile-stale")
        provider_available = False
        provider_failure_reason = "provider-unavailable"
        try:
            profile = self.embedding_provider.describe()
            if profile.profile_digest != generation.provider_profile_digest:
                return self._stale_result(query, project_id=project_id, reason="profile-stale")
            profile.assert_policy(self.embedding_policy)
            health = self.embedding_provider.health()
            provider_available = health.healthy and health.profile_digest == profile.profile_digest
            if not provider_available:
                provider_failure_reason = "provider-health-unavailable"
        except Exception as exc:
            provider_failure_reason = f"provider-probe-failed:{type(exc).__name__}"

        backend = EmbeddedProjectSearchBackend(
            self.index,
            project_id,
            generation.generation_digest,
            self.embedding_provider,
            self.embedding_policy,
            dense_enabled=provider_available,
            dense_failure_reason=None if provider_available else provider_failure_reason,
        )
        service = RetrievalService(backend, reranker=self.reranker)
        hits, trace = service.search(query)
        candidate_ids = tuple(hit.chunk_id for hit in hits)
        views = self.index.views(
            project_id,
            candidate_ids,
            generation_digest=generation.generation_digest,
        )
        query_terms = _tokens(query)
        lexical_coverage = max(
            (
                len(query_terms & _tokens(views[hit.chunk_id].text)) / len(query_terms)
                for hit in backend.last_lexical
                if query_terms and hit.chunk_id in views
            ),
            default=0.0,
        )
        top_dense = backend.last_dense[0].raw_score if backend.last_dense else -1.0
        dense_margin = (
            top_dense - backend.last_dense[1].raw_score if len(backend.last_dense) > 1 else 0.0
        )
        enough_evidence = (
            bool(backend.last_exact)
            or lexical_coverage >= self.lexical_coverage_threshold
            or (
                top_dense >= self.dense_evidence_threshold
                and (
                    dense_margin >= self.dense_margin_threshold
                    or lexical_coverage >= self.lexical_coverage_threshold / 2
                )
            )
        )
        if not enough_evidence:
            hits = ()
        answer = service.build_answer(
            query,
            hits,
            RetrievalTrace(
                identifiers=extract_identifiers(query),
                per_channel=trace.per_channel,
                fused_count=trace.fused_count,
                after_dedupe=trace.after_dedupe,
                reranker_used=trace.reranker_used,
                reranker_failed=trace.reranker_failed,
                source_type=trace.source_type,
            ),
            views=views,
            token_budget=token_budget,
        )
        citations: list[dict[str, Any]] = []
        for citation in answer.citations:
            identity = self.index.source_identity(
                project_id,
                citation.chunk_id,
                generation_digest=generation.generation_digest,
            )
            view = views[citation.chunk_id]
            fused_hit = next(hit for hit in hits if hit.chunk_id == citation.chunk_id)
            fused_rank = next(
                rank for rank, hit in enumerate(hits, start=1) if hit.chunk_id == citation.chunk_id
            )
            channel_ranks = {
                channel.value: next(
                    item.rank
                    for item in (
                        *backend.last_exact,
                        *backend.last_lexical,
                        *backend.last_dense,
                    )
                    if item.chunk_id == citation.chunk_id and item.channel is channel
                )
                for channel in fused_hit.channels
            }
            citations.append(
                {
                    "source_id": identity["source_id"],
                    "project_scope": project_id,
                    "source_ref": identity["source_ref"],
                    "source_revision": identity["source_revision"],
                    "source_digest": identity["source_digest"],
                    "content_digest": identity["content_digest"],
                    "chunk_id": citation.chunk_id,
                    "locator_type": "project-file",
                    "locator": view.locator.as_dict(),
                    "retrieval_channels": [item.value for item in fused_hit.channels],
                    "rank_trace": {
                        "fused_rank": fused_rank,
                        "rrf_score": fused_hit.score,
                        "exact_match": fused_hit.exact_match,
                        "channel_ranks": channel_ranks,
                    },
                }
            )
        state = answer.state.value
        if not enough_evidence and state == AnswerState.ABSTAINED_NO_HIT.value:
            state = AnswerState.ABSTAINED_LOW_EVIDENCE.value
        if backend.dense_failure_reason:
            if state == AnswerState.ANSWERED.value and (
                bool(backend.last_exact) or lexical_coverage >= self.lexical_coverage_threshold
            ):
                state = "lexical-only-degraded"
            else:
                state = "abstained-index-unavailable"
                citations = []
        result: dict[str, Any] = {
            "schema": "zekam-embedded-rag-result/v1",
            "state": state,
            "project_id": project_id,
            "query_digest": answer.query_digest,
            "generation_digest": generation.generation_digest,
            "source_revision": generation.source_revision,
            "tree_digest": generation.tree_digest,
            "embedding_profile_digest": generation.embedding_profile_digest,
            "provider_profile_digest": generation.provider_profile_digest,
            "searched_channels": (
                ["exact", "lexical", "dense"] if provider_available else ["exact", "lexical"]
            ),
            "channel_counts": trace.per_channel,
            "candidate_count": len(candidate_ids),
            "lexical_coverage": lexical_coverage,
            "top_dense_similarity": top_dense,
            "dense_top_2_margin": dense_margin,
            "evidence_sufficient": enough_evidence,
            "degraded_reason": backend.dense_failure_reason,
            "citations": citations,
            "used_chunk_ids": list(answer.used_chunk_ids),
            "tokens_used": answer.tokens_used,
            "fallback_allowed": False,
            "answer_excerpt": (
                views[answer.used_chunk_ids[0]].text[:500] if answer.used_chunk_ids else None
            ),
            "explanation": list(answer.explanation),
        }
        result["retrieval_digest"] = digest(result)
        return result
