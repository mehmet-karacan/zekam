"""Project-scoped PostgreSQL RAG gate for natural-language questions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.application.project_knowledge_index import deterministic_local_embedding
from zekam.application.retrieval_service import RetrievalService
from zekam.domain.canonical import digest
from zekam.domain.project import IntegrationStage
from zekam.domain.retrieval import AnswerState, ScoredHit
from zekam.infrastructure.postgres.retrieval_repository import RetrievalRepository


@dataclass(frozen=True, slots=True)
class ProjectRetrievalBackend:
    """Adapt the vector repository to RetrievalService without losing project scope."""

    repository: RetrievalRepository
    profile_id: UUID
    dimension: int
    query_prefix: str = ""
    source_type: str = "project-knowledge"

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
        return self.repository.exact(identifiers, limit=limit)

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        return self.repository.lexical(query, limit=limit)

    def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        vector = deterministic_local_embedding(
            f"{self.query_prefix}{query}", dimensions=self.dimension
        )
        return self.repository.dense(vector, self.profile_id, limit=limit)


def query_project_knowledge(
    *,
    connection: Any,
    realm_id: UUID,
    project_id: UUID,
    project_ref: str,
    query: str,
    integration_stage: IntegrationStage,
    integration_detail: dict[str, Any],
    token_budget: int = 1200,
) -> dict[str, Any]:
    """Search exact/FTS/pgvector first and emit a fail-closed source fallback gate."""

    repository = RetrievalRepository(connection, realm_id, project_id=project_id)
    profile = repository.active_project_embedding_profile()
    index_state = str((integration_detail.get("knowledge_index") or {}).get("state", "missing"))
    base: dict[str, Any] = {
        "schema": "zekam-project-rag-gate/v1",
        "project_ref": project_ref,
        "query_digest": digest({"query": query}),
        "index_state": index_state,
        "searched_postgresql": profile is not None,
        "searched_channels": [],
        "fallback_allowed": True,
        "fallback_kind": "bounded-source-researcher",
        "grants_authority": False,
    }
    if profile is None:
        document = dict(base, state="unavailable", reason="project-index-profile-unavailable")
        document["retrieval_digest"] = digest(document)
        return document

    backend = ProjectRetrievalBackend(
        repository=repository,
        profile_id=profile["profile_id"],
        dimension=profile["dimension"],
        query_prefix=profile["query_prefix"],
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
            "searched_channels": ["exact", "lexical", "dense"],
            "channel_counts": trace.per_channel,
            "candidate_count": len(hits),
            "embedding_profile_digest": profile["profile_digest"],
            "source_content_digest": profile["source_content_digest"],
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
