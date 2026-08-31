from __future__ import annotations

from uuid import UUID

from zekam.application import project_rag_query
from zekam.application.retrieval_service import ChunkView
from zekam.domain.canonical import digest
from zekam.domain.knowledge import Locator
from zekam.domain.project import IntegrationStage
from zekam.domain.retrieval import RetrievalChannel, ScoredHit

PROJECT_ID = UUID("00000000-0000-0000-0000-000000000111")


class FakeProjectRepository:
    def __init__(self, connection: object, realm_id: UUID, project_id: UUID | None = None):
        del connection, realm_id
        assert project_id == PROJECT_ID

    def active_project_embedding_profile(self):  # type: ignore[no-untyped-def]
        return {
            "profile_id": UUID("00000000-0000-0000-0000-000000000222"),
            "model_ref": "local-test",
            "dimension": 4,
            "query_prefix": "",
            "profile_digest": digest("profile"),
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


def _query(monkeypatch, *, stage: IntegrationStage, index_state: str):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(project_rag_query, "RetrievalRepository", FakeProjectRepository)
    return project_rag_query.query_project_knowledge(
        connection=object(),
        realm_id=UUID("00000000-0000-0000-0000-000000000001"),
        project_id=PROJECT_ID,
        project_ref="gpu-fusion",
        query="hangi class?",
        integration_stage=stage,
        integration_detail={"knowledge_index": {"state": index_state}},
    )


def test_ready_project_runs_all_postgresql_channels_and_returns_citation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    result = _query(monkeypatch, stage=IntegrationStage.CURRENT, index_state="ready")
    assert result["state"] == "answered"
    assert result["searched_channels"] == ["exact", "lexical", "dense"]
    assert result["fallback_allowed"] is False
    assert result["citations"][0]["locator"]["relative_path"] == "src/GpuService.java"
    assert str(result["retrieval_digest"]).startswith("sha256:")


def test_pending_or_stale_index_discards_candidates_and_allows_bounded_fallback(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    result = _query(monkeypatch, stage=IntegrationStage.CURRENT, index_state="pending")
    assert result["state"] == "stale"
    assert result["candidate_count"] == 1
    assert result["citations"] == []
    assert result["fallback_allowed"] is True
    assert result["fallback_kind"] == "bounded-source-researcher"
