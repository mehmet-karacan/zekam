"""Project-scoped local RAG gate for natural-language questions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from zekam.application.embedding_provider import (
    EmbeddingDegradedState,
    EmbeddingPolicy,
    EmbeddingProvider,
)
from zekam.application.retrieval_service import ChunkView, RetrievalService
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.project import IntegrationStage
from zekam.domain.retrieval import AnswerState, ScoredHit


class ProjectRetrievalStore(Protocol):
    def active_project_embedding_profile(self) -> dict[str, Any] | None: ...

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]: ...

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]: ...

    def dense(
        self, vector: tuple[float, ...], profile_id: UUID, *, limit: int
    ) -> tuple[ScoredHit, ...]: ...

    def views(self, chunk_refs: tuple[str, ...]) -> dict[str, ChunkView]: ...


@dataclass(frozen=True, slots=True)
class ProjectRetrievalBackend:
    """Adapt the vector repository to RetrievalService without losing project scope."""

    repository: ProjectRetrievalStore
    profile_id: UUID
    dimension: int
    embedding_provider: EmbeddingProvider | None = None
    embedding_policy: EmbeddingPolicy | None = None
    source_type: str = "project-knowledge"

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
        return self.repository.exact(identifiers, limit=limit)

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        return self.repository.lexical(query, limit=limit)

    def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        if self.embedding_provider is None or self.embedding_policy is None:
            return ()
        result = self.embedding_provider.embed_query(query, self.embedding_policy)
        if len(result.vectors) != 1 or len(result.vectors[0]) != self.dimension:
            raise PolicyViolation("Project query embedding provider dimension drift")
        vector = result.vectors[0]
        return self.repository.dense(vector, self.profile_id, limit=limit)


def query_project_knowledge(
    *,
    repository: ProjectRetrievalStore,
    project_ref: str,
    query: str,
    integration_stage: IntegrationStage,
    integration_detail: dict[str, Any],
    embedding_provider: EmbeddingProvider | None = None,
    embedding_policy: EmbeddingPolicy | None = None,
    token_budget: int = 1200,
) -> dict[str, Any]:
    """Search exact/FTS/local-vector paths and emit a fail-closed source fallback gate."""

    profile = repository.active_project_embedding_profile()
    index_state = str((integration_detail.get("knowledge_index") or {}).get("state", "missing"))
    base: dict[str, Any] = {
        "schema": "zekam-project-rag-gate/v1",
        "project_ref": project_ref,
        "query_digest": digest({"query": query}),
        "index_state": index_state,
        "searched_index": profile is not None,
        "searched_channels": [],
        "fallback_allowed": True,
        "fallback_kind": "bounded-source-researcher",
        "grants_authority": False,
    }
    if profile is None:
        document = dict(base, state="unavailable", reason="project-index-profile-unavailable")
        document["retrieval_digest"] = digest(document)
        return document

    if (embedding_provider is None) != (embedding_policy is None):
        raise PolicyViolation("Embedding provider/policy birlikte verilmelidir")
    dense_enabled = embedding_provider is not None
    if embedding_provider is not None and embedding_policy is not None:
        provider_profile = embedding_provider.describe()
        expected_index_profile_digest = str(
            (integration_detail.get("knowledge_index") or {}).get("embedding_profile_digest", "")
        )
        expected_provider_digest = str(
            (integration_detail.get("knowledge_index") or {}).get("provider_profile_digest", "")
        )
        if (
            provider_profile.dimension != profile["dimension"]
            or expected_index_profile_digest != profile["profile_digest"]
            or expected_provider_digest != provider_profile.profile_digest
        ):
            raise PolicyViolation("Project index/provider profile drift; rebuild required")
    backend = ProjectRetrievalBackend(
        repository=repository,
        profile_id=profile["profile_id"],
        dimension=profile["dimension"],
        embedding_provider=embedding_provider,
        embedding_policy=embedding_policy,
    )
    service = RetrievalService(backend)
    hits, trace = service.search(query)
    views = repository.views(tuple(hit.chunk_id for hit in hits))
    answer = service.build_answer(
        query,
        hits,
        trace,
        views=views,
        token_budget=token_budget,
    )
    base.update(
        {
            "searched_channels": ["exact", "lexical"] + (["dense"] if dense_enabled else []),
            "channel_counts": trace.per_channel,
            "candidate_count": len(hits),
            "embedding_profile_digest": profile["profile_digest"],
            "source_content_digest": profile["source_content_digest"],
            "degraded_state": (
                None if dense_enabled else EmbeddingDegradedState.LEXICAL_ONLY.value
            ),
        }
    )

    index_ready = integration_stage is IntegrationStage.CURRENT and index_state == "ready"
    if not index_ready:
        document = dict(
            base,
            state="stale",
            reason=f"integration-{integration_stage.value}-index-{index_state}",
            citations=[],
        )
        document["retrieval_digest"] = digest(document)
        return document

    state = {
        AnswerState.ANSWERED: "answered",
        AnswerState.ABSTAINED_NO_HIT: "no-hit",
        AnswerState.ABSTAINED_LOW_EVIDENCE: "low-evidence",
    }[answer.state]
    document = dict(
        base,
        state=state,
        reason=None if answer.is_answered else str(answer.state),
        citations=[citation.as_dict() for citation in answer.citations],
        fallback_allowed=not answer.is_answered,
        fallback_kind=None if answer.is_answered else "bounded-source-researcher",
    )
    document["retrieval_digest"] = digest(document)
    return document
