"""Real canonical notes -> real SQLite FTS/vector generation, synthetic embeddings; UNRUN."""

from uuid import UUID

import pytest

from tests.integration.test_jira_knowledge_import import _setup
from tests.integration.test_project_rag_pending_publish import (
    _bind_counting_local_provider,
    _init_git_source,
    _run_index,
)
from zekam.application import jira_knowledge_import as imports
from zekam.application import project_rag_runtime as runtime
from zekam.application.config import EmbeddingRoute, KnowledgeSettings
from zekam.application.reference_corpus import collect_reference_corpus, REFERENCE_PATH_PREFIX
from zekam.application.project_knowledge_index import LOCAL_E5_DIMENSION
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex


def test_index_includes_terminal_reference_reuses_cache_and_removes_superseded_vectors(
    tmp_path, monkeypatch
):
    home, store, files, project, first, _, observation = _setup(tmp_path, monkeypatch)
    imports.apply_jira_knowledge_plan(
        store, files, home, first.body, expected_plan_digest=first.plan_digest
    )
    corpus = collect_reference_corpus(home, project.id, project.slug, with_chunks=True)
    assert len(corpus.notes) == 1 and corpus.chunks
    assert all(
        chunk.locator.relative_path.startswith(REFERENCE_PATH_PREFIX) for chunk in corpus.chunks
    )
    source = tmp_path / "source"
    _init_git_source(source)
    _provider, _ = _bind_counting_local_provider(
        monkeypatch,
        KnowledgeSettings(embedding_route=EmbeddingRoute.LOCAL),
        canonical_references=True,
    )
    indexed = _run_index(source, home, UUID(project.id), project.slug)
    assert indexed["reference_snapshot_digest"] == corpus.snapshot_digest
    assert indexed["reference_chunk_count"] == len(corpus.chunks)
    again = _run_index(source, home, UUID(project.id), project.slug)
    assert again["provider_batches"] == 0
    old_paths = {chunk.locator.relative_path for chunk in corpus.chunks}
    observation["normalized_digest"] = digest("changed normalized")
    observation["units"][0]["text"] = "Changed specification uniquephrase"
    second = imports.build_jira_knowledge_plan(
        store,
        project_ref=project.id,
        issue_key="DEMO-42",
        attachment_id="123",
        metadata_digest=digest("metadata"),
        files=files,
    )
    imports.apply_jira_knowledge_plan(
        store, files, home, second.body, expected_plan_digest=second.plan_digest
    )
    updated = _run_index(source, home, UUID(project.id), project.slug)
    assert updated["generation_digest"] != indexed["generation_digest"]
    with SQLiteKnowledgeIndex(
        runtime._runtime_paths(home, project.slug)["index"],
        read_only=True,
        dimension=LOCAL_E5_DIMENSION,
    ) as index:
        hits = index.lexical(
            project.id, "uniquephrase", limit=10, generation_digest=updated["generation_digest"]
        )
        assert hits
        views = index.views(
            project.id,
            tuple(hit.chunk_id for hit in hits),
            generation_digest=updated["generation_digest"],
        )
        assert not any(view.locator.relative_path in old_paths for view in views.values())
    current = collect_reference_corpus(home, project.id, project.slug)
    with pytest.raises(PolicyViolation):
        current.verify_citation(home, next(iter(old_paths)), corpus.notes[0].content_digest)
    (home / second.body["target_ref"]).write_bytes(b"tampered")
    with pytest.raises(ZekamError):
        current.verify_citation(
            home,
            REFERENCE_PATH_PREFIX + current.notes[0].id + ".md",
            current.notes[0].content_digest,
        )


def test_canonical_reference_pin_rejects_new_corpus_before_provider(tmp_path, monkeypatch):
    home, store, files, project, first, _, _ = _setup(tmp_path, monkeypatch)
    empty = collect_reference_corpus(home, project.id, project.slug)
    imports.apply_jira_knowledge_plan(
        store, files, home, first.body, expected_plan_digest=first.plan_digest
    )
    source = tmp_path / "source"
    _init_git_source(source)
    _bind_counting_local_provider(
        monkeypatch,
        KnowledgeSettings(embedding_route=EmbeddingRoute.LOCAL),
        canonical_references=True,
    )
    monkeypatch.setattr(
        runtime, "_provider", lambda *a, **kw: pytest.fail("provider before approved reference pin")
    )
    with pytest.raises(PolicyViolation, match="approved plan"):
        runtime._index(
            source,
            home,
            UUID(project.id),
            project.slug,
            None,
            None,
            batch_size=4,
            expected_reference_snapshot_digest=empty.snapshot_digest,
        )
