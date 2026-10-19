"""Source-architecture answer integration: evidence replacement, CLI selection.

Central case: a highly ranked future AKTIF_GOREV task-plan document AND real
code modules indexed in the same generation. Native RAG must emit code-only
citations and a responsibility summary the CLI explanation gate accepts;
task-plan docs never become citations. Also covers doc-only architecture
abstain, stale/invalid summaries never rendering the old excerpt,
normal-question excerpt unchanged and multi-project labels.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.test_embedded_project_rag import QueryProvider, _query, _rag, _vector

from zekam.application.embedded_project_rag import EmbeddedProjectRAG
from zekam.application.embedding_provider import EmbeddingPolicy
from zekam.application.knowledge_index import KnowledgeIndexRecord
from zekam.application.project_knowledge_index import LOCAL_E5_DIMENSION
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.knowledge import Locator
from zekam.domain.security import DataClassification
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex

pytestmark = pytest.mark.unit

PROJECT = "akilli-kasa"
REVISION = "source-revision-1"
TREE = digest("tree-1")

ARCH_QUERY = "kod mimarisi modul sorumluluklari nelerdir?"


_CODE_ANNOTATIONS = {
    "RouterModule": "@RestController",
    "RuntimeModule": "@Service",
    "StorageModule": "@Repository",
}


def _code_text(class_name: str) -> str:
    return (
        f"{_CODE_ANNOTATIONS[class_name]}\n"
        f"public class {class_name} {{}}\n"
    )


def _record(
    chunk_id: str,
    path: str,
    text: str,
    *,
    order: int,
    vector_slot: int,
) -> KnowledgeIndexRecord:
    return KnowledgeIndexRecord(
        chunk_id=chunk_id,
        project_id=PROJECT,
        source_revision=REVISION,
        source_path=path,
        source_digest=digest({"source": path}),
        locator=Locator(relative_path=path, line_start=1, line_end=4),
        text=text,
        content_digest=digest_of_bytes(text.encode("utf-8")),
        chunk_order=order,
        vector=_vector(vector_slot),
        vector_dimension=LOCAL_E5_DIMENSION,
    )


def _policy(provider: QueryProvider) -> EmbeddingPolicy:
    return EmbeddingPolicy(DataClassification.LOCAL_ONLY, provider.profile.profile_digest)


def _code_rag(
    tmp_path: Path, provider: QueryProvider
) -> tuple[SQLiteKnowledgeIndex, EmbeddedProjectRAG]:
    """Central fixture: 3 real code modules + highly ranked task-plan doc."""
    plan_text = (
        "AKTIF_GOREV WP-09 kod mimarisi plani: modul sorumluluklari ve "
        "katman yapisi kararlari burada yapilacak; siradaki adimlar: "
        "router, runtime ve diger modullerin gorev dagilimi netlescek."
    )
    records = (
        _record(
            "chunk-gorev-plan",
            "tasks/AKTIF_GOREV.md",
            plan_text,
            order=0,
            vector_slot=0,
        ),
        _record(
            "chunk-router-code",
            "api/src/RouterModule.java",
            _code_text("RouterModule"),
            order=1,
            vector_slot=1,
        ),
        _record(
            "chunk-runtime-code",
            "runtime/src/RuntimeModule.java",
            _code_text("RuntimeModule"),
            order=2,
            vector_slot=2,
        ),
        _record(
            "chunk-storage-code",
            "storage/src/StorageModule.java",
            _code_text("StorageModule"),
            order=3,
            vector_slot=3,
        ),
    )
    index = SQLiteKnowledgeIndex(
        tmp_path / "knowledge.sqlite3", create=True, dimension=LOCAL_E5_DIMENSION
    )
    index.build_generation(
        records,
        project_id=PROJECT,
        source_revision=REVISION,
        tree_digest=TREE,
        source_manifest_digest=digest("manifest"),
        embedding_profile_digest=digest("embedding"),
        provider_profile_digest=provider.profile.profile_digest,
        created_at="2026-09-02T00:00:00Z",
    )
    return index, EmbeddedProjectRAG(index, provider, _policy(provider))


def test_central_case_code_citations_replace_task_plan(tmp_path: Path) -> None:
    """Highly ranked plan doc + real code: citations are code-only, CLI accepts."""
    provider = QueryProvider()
    index, rag = _code_rag(tmp_path, provider)
    result = _query(rag, ARCH_QUERY)
    assert result["state"] == "answered"
    assert result["query_intent"] == "source-architecture"
    assert result["citations"], "supported summary must carry code citations"
    for citation in result["citations"]:
        assert citation["source_kind"] == "code"
        assert citation["rank_trace"]["provenance"] == "architecture-structural-expansion"
        assert citation["rank_trace"]["fused_rank"] is None
        assert citation["retrieval_channels"] == []
        assert "AKTIF_GOREV" not in str(citation["source_ref"])
        assert citation["source_ref"].endswith(".java")
    assert set(result["used_chunk_ids"]) == {c["chunk_id"] for c in result["citations"]}
    assert result["answer_excerpt"] is None
    assert result["answer_excerpts"] == []
    assert result["evidence_found"] is True
    assert result["evidence_sufficient"] is True
    assert result["retrieval_state"] == "answered-evidence"
    assert result["answer_kind"] == "retrieval_evidence"
    summary = result["architecture_summary"]
    assert isinstance(summary, dict)
    assert summary["status"] == "supported"
    modules = summary["modules"]
    assert {entry["module"] for entry in modules} == {"api", "runtime", "storage"}
    module_roles = {
        entry["role"] if isinstance(entry, dict) else entry
        for entry in summary["claims"]
    }
    assert {"http_api", "service_logic", "data_access"} <= module_roles
    assert "chunk-gorev-plan" not in summary["used_chunk_ids"]
    # Every summary coverage note is appended as an explicit provenance entry.
    summary_notes = (summary.get("coverage") or {}).get("notes") or []
    for note_text in summary_notes:
        assert any(
            note["code"] == "architecture-summary-coverage"
            and note["detail"] == str(note_text)
            for note in result["coverage_notes"]
        )
    from zekam.interfaces.cli.main import _retrieval_explanation

    entry = {"project_ref": PROJECT, "status": "answered", "result": result}
    explanation = _retrieval_explanation(
        {"results": [entry], "project_refs": [PROJECT]}
    )
    assert explanation == summary["text"]
    explanation_lower = explanation.lower()
    assert "http isteklerini karsilar" in explanation_lower
    assert "uygulama servis bileseni" in explanation_lower
    assert "veri erisimi ve kalicilik" in explanation_lower
    assert ".java" in explanation_lower
    index.close()


def test_doc_only_architecture_abstains_without_doc_fallback(tmp_path: Path) -> None:
    """Architecture query with only plan/doc chunks: explicit abstain, no docs."""
    provider = QueryProvider()
    plan_text = (
        "AKTIF_GOREV WP-09 kod mimarisi plani: modul sorumluluklari ve "
        "katman yapisi kararlari burada yapilacak; modullerin gorev dagilimi."
    )
    index, rag = _rag(tmp_path, provider, path="tasks/AKTIF_GOREV.md", text=plan_text)
    result = _query(rag, ARCH_QUERY)
    assert result["query_intent"] == "source-architecture"
    assert result["architecture_summary"]["status"] == "insufficient"
    assert result["state"] == "abstained-low-evidence"
    assert result["citations"] == []
    assert result["used_chunk_ids"] == []
    assert result["answer_excerpt"] is None
    assert result["evidence_found"] is False
    assert result["evidence_sufficient"] is False
    index.close()


def test_stale_summary_never_renders_old_excerpt(tmp_path: Path) -> None:
    """Stale freshness: CLI explanation refuses; old excerpt never leaks."""
    from zekam.interfaces.cli.main import _retrieval_explanation

    provider = QueryProvider()
    index, rag = _code_rag(tmp_path, provider)
    result = _query(rag, ARCH_QUERY)
    assert result["architecture_summary"]["status"] == "supported"
    stale_result = dict(result)
    stale_result["index_freshness"] = "stale"
    stale_result["snapshot_only"] = True
    entry = {"project_ref": PROJECT, "status": "answered", "result": stale_result}
    assert _retrieval_explanation({"results": [entry], "project_refs": [PROJECT]}) is None
    stale_result["answer_excerpt"] = "eski task-plan excerpt"
    explanation = _retrieval_explanation({"results": [entry], "project_refs": [PROJECT]})
    assert explanation is None
    index.close()


def test_invalid_summary_never_renders_excerpt(tmp_path: Path) -> None:
    """Invalid/insufficient summary: no excerpt fallback even with old excerpt."""
    from zekam.interfaces.cli.main import _retrieval_explanation

    provider = QueryProvider()
    index, rag = _code_rag(tmp_path, provider)
    result = _query(rag, ARCH_QUERY)
    invalid_result = dict(result)
    invalid_result["architecture_summary"] = {"status": "insufficient"}
    invalid_result["answer_excerpt"] = "eski excerpt"
    entry = {"project_ref": PROJECT, "status": "answered", "result": invalid_result}
    assert _retrieval_explanation({"results": [entry], "project_refs": [PROJECT]}) is None
    index.close()


def test_normal_question_excerpt_unchanged(tmp_path: Path) -> None:
    """Non-architecture query keeps the ordinary excerpt pipeline untouched."""
    provider = QueryProvider()
    index, rag = _rag(tmp_path, provider)
    result = _query(rag, "ADR-0006 idempotent dosya ice aktarma")
    assert result["query_intent"] != "source-architecture"
    assert result["architecture_summary"] is None
    assert result["architecture_code_inventory"] is None
    assert result["state"] == "answered"
    assert result["answer_excerpt"]
    assert result["used_chunk_ids"] == ["chunk-adr-0006"]
    assert result["citations"]
    assert result["coverage_notes"] == []
    index.close()


def test_multi_project_labels_preserved(tmp_path: Path) -> None:
    """Multiple project labels: architecture explanation carries each label."""
    from zekam.interfaces.cli.main import _retrieval_explanation

    provider = QueryProvider()
    index, rag = _code_rag(tmp_path, provider)
    result = _query(rag, ARCH_QUERY)
    summary_b = dict(result["architecture_summary"], project_id="proje-b")
    result_b = dict(
        result,
        project_id="proje-b",
        architecture_summary=summary_b,
        answer_excerpt=None,
        answer_excerpts=[],
        answer_excerpt_meta=None,
        used_chunk_ids=[],
        citations=[],
        state="abstained-low-evidence",
    )
    entry_a = {"project_ref": "proje-a", "status": "answered", "result": result}
    entry_b = {"project_ref": "proje-b", "status": "answered", "result": result_b}
    explanation = _retrieval_explanation(
        {"results": [entry_a, entry_b], "project_refs": ["proje-a", "proje-b"]}
    )
    assert explanation is not None
    assert explanation.startswith("proje-a: ")
    assert "proje-b: " not in explanation  # insufficient summary renders nothing
    index.close()


def test_unhealthy_provider_degrades_architecture_state(tmp_path: Path) -> None:
    """Unhealthy embedding provider: architecture stays code-only, state degrades."""
    provider = QueryProvider(mode="unhealthy")
    index, rag = _code_rag(tmp_path, provider)
    result = _query(rag, ARCH_QUERY)
    assert result["state"] == "lexical-only-degraded"
    assert result["state"] != "answered"
    assert result["degraded_reason"]
    assert result["citations"], "degraded architecture must still cite code sources"
    for citation in result["citations"]:
        assert citation["source_kind"] == "code"
        assert "AKTIF_GOREV" not in str(citation["source_ref"])
    assert "chunk-gorev-plan" not in result["used_chunk_ids"]
    index.close()


def test_architecture_summary_probes_following_chunk_for_header_only_frontend(
    tmp_path: Path,
) -> None:
    provider = QueryProvider()
    records = (
        _record(
            "web-page-header",
            "web/src/Page.tsx",
            "import React from 'react';",
            order=0,
            vector_slot=0,
        ),
        _record(
            "web-page-component",
            "web/src/Page.tsx",
            "export function Page() {\n    return <div>hello</div>;\n}",
            order=1,
            vector_slot=1,
        ),
        _record(
            "api-rest-controller",
            "api/src/Api.java",
            "@RestController\npublic class Api {\n}",
            order=2,
            vector_slot=2,
        ),
    )
    index = SQLiteKnowledgeIndex(
        tmp_path / "knowledge.sqlite3",
        create=True,
        dimension=LOCAL_E5_DIMENSION,
    )
    try:
        index.build_generation(
            records,
            project_id=PROJECT,
            source_revision=REVISION,
            tree_digest=TREE,
            source_manifest_digest=digest("manifest"),
            embedding_profile_digest=digest("embedding"),
            provider_profile_digest=provider.profile.profile_digest,
            created_at="2026-09-02T00:00:00Z",
        )
        rag = EmbeddedProjectRAG(index, provider, _policy(provider))
        result = _query(rag, ARCH_QUERY)
        summary = result["architecture_summary"]
        assert isinstance(summary["modules"], list)
        module_names = {module["module"] for module in summary["modules"]}
        assert "api" in module_names
        assert "web" in module_names
        assert isinstance(summary["claims"], list)
        claim_roles = {claim["role"] for claim in summary["claims"]}
        assert "frontend_rendering" in claim_roles
        assert "web-page-component" in summary["used_chunk_ids"]
        assert "web-page-header" not in summary["used_chunk_ids"]
        assert "web-page-component" in {citation["chunk_id"] for citation in result["citations"]}
        inventory = summary["source_inventory"]
        assert inventory["expanded_chunk_count"] >= 1
        assert inventory["total_chunk_count"] <= 96
        assert inventory["candidate_limit"] == 96
    finally:
        index.close()
