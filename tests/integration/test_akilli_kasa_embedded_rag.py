"""Real bounded Akilli Kasa -> BGE -> SQLite hybrid RAG acceptance."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import NotRequired, TypedDict
from uuid import UUID

import pytest
from benchmarks.suites.wp07_akilli_kasa_golden import GOLDEN_CASES

from zekam.application.embedded_project_rag import (
    EmbeddedProjectRAG,
    build_embedded_project_generation,
)
from zekam.application.local_embedding_composition import build_verified_mac_embedding
from zekam.application.project_knowledge_index import build_project_index_plan
from zekam.application.source_discovery import discover
from zekam.domain.canonical import parse_digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.security import DataClassification
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex

pytestmark = pytest.mark.integration

AKILLI_KASA_PROJECT_ID = "00000000-0000-0000-0000-00000000a111"
BOUNDED_PATHS = (
    "belgeler/kararlar/ADR-0006-idempotent-dosya-ice-aktarma.md",
    "belgeler/kararlar/ADR-0005-parasal-tutarlarda-decimal-kullanimi.md",
    "belgeler/kararlar/ADR-0010-frontend-backend-api-siniri.md",
    "src/akilli_kasa/api/saglik.py",
    "tests/entegrasyon/test_saglik.py",
    "tests/birim/test_kurallar.py",
)


class RAGMetrics(TypedDict):
    case_count: int
    answerable_count: int
    exact_top_1: float
    citation_locator_validity: float
    cross_project_leakage: int
    fabricated_citations: int
    unsupported_factual_answers: int
    freshness_mismatch_silent: int
    recall_at_10: float
    mrr: float
    ndcg_at_10: float
    query_p50_ms: float
    query_p95_ms: float
    missed_cases: list[str]
    index_build_ms: NotRequired[float]
    index_size_bytes: NotRequired[int]
    scratch_rebuild_ms: NotRequired[float]
    scratch_generation_digest_equal: NotRequired[bool]


def _status_digest(root: Path) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-uall"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    return hashlib.sha256(status).hexdigest()


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.skipif(
    os.environ.get("ZEKAM_RUN_AKILLI_KASA_RAG_E2E") != "1",
    reason="real bounded Akilli Kasa hybrid RAG acceptance is explicit",
)
def test_real_bounded_project_hybrid_rag_restart_scope_abstain_and_reindex(
    tmp_path: Path,
) -> None:
    root = Path(os.environ.get("ZEKAM_AKILLI_KASA_ROOT", "/Users/mkaracan/Projeler/akilli-kasa"))
    if not all((root / path).is_file() for path in BOUNDED_PATHS):
        pytest.skip("Akilli Kasa bounded RAG fixture unavailable")
    before = _status_digest(root)
    discovery = discover(root)
    source_revision = f"{_head(root)}:status:{before}"
    plan = build_project_index_plan(
        project_id=UUID(AKILLI_KASA_PROJECT_ID),
        project_slug="akilli-kasa",
        source_root=root,
        source_revision=source_revision,
        expected_tree_digest=discovery.tree_digest,
        allowed_relative_paths=BOUNDED_PATHS,
    )
    embedding = build_verified_mac_embedding(
        plan.chunks, classification=DataClassification.LOCAL_ONLY
    )
    first_path = tmp_path / "knowledge.sqlite3"
    build_started = time.perf_counter()
    with SQLiteKnowledgeIndex(first_path, create=True) as index:
        bound_plan, generation = build_embedded_project_generation(
            index,
            plan,
            embedding_provider=embedding.provider,
            embedding_policy=embedding.policy,
        )
        rag = EmbeddedProjectRAG(index, embedding.provider, embedding.policy)
        cases = (
            (
                "ADR-0006 neden ayni dosyanin tekrar islenmesini engeller?",
                "belgeler/kararlar/ADR-0006-idempotent-dosya-ice-aktarma.md",
            ),
            (
                "Finansal tutarlar icin neden Decimal ve Numeric kullaniliyor?",
                "belgeler/kararlar/ADR-0005-parasal-tutarlarda-decimal-kullanimi.md",
            ),
            (
                "Domain hesaplari frontend tarafinda mi backend tarafinda mi kalir?",
                "belgeler/kararlar/ADR-0010-frontend-backend-api-siniri.md",
            ),
            ("SaglikYaniti hangi alanlari tasir?", "src/akilli_kasa/api/saglik.py"),
            (
                "test_saglik_endpointi hangi endpointi cagirir?",
                "tests/entegrasyon/test_saglik.py",
            ),
            (
                "test_oncelik_stop_ve_audit hangi kural sirasini dogrular?",
                "tests/birim/test_kurallar.py",
            ),
        )
        for query, expected_path in cases:
            result = rag.query(
                query,
                project_id=AKILLI_KASA_PROJECT_ID,
                expected_source_revision=source_revision,
                expected_tree_digest=bound_plan.tree_digest,
            )
            assert result["state"] == "answered", (query, result)
            assert result["citations"], (query, result)
            assert result["citations"][0]["source_ref"] == expected_path, (query, result)
            assert result["citations"][0]["project_scope"] == AKILLI_KASA_PROJECT_ID
            assert result["retrieval_digest"].startswith("sha256:")

        no_answer = rag.query(
            "ADR-9999 kuantum kasasinda mor muz sulama anahtari nedir?",
            project_id=AKILLI_KASA_PROJECT_ID,
            expected_source_revision=source_revision,
            expected_tree_digest=bound_plan.tree_digest,
        )
        assert no_answer["state"] == "abstained-low-evidence", no_answer
        assert no_answer["citations"] == []

        wrong_scope = rag.query(
            "ADR-0006 idempotent import",
            project_id="other-project",
            expected_source_revision=source_revision,
            expected_tree_digest=bound_plan.tree_digest,
        )
        assert wrong_scope["state"] == "abstained-index-unavailable"
        assert wrong_scope["citations"] == []

        stale = rag.query(
            "ADR-0006 idempotent import",
            project_id=AKILLI_KASA_PROJECT_ID,
            expected_source_revision="changed-source-revision",
            expected_tree_digest=bound_plan.tree_digest,
        )
        assert stale["state"] == "abstained-index-unavailable"
        assert stale["reason"] == "source-stale"

        answerable = 0
        recalled = 0
        reciprocal_ranks: list[float] = []
        ndcgs: list[float] = []
        exact_top_1 = 0
        citation_count = 0
        valid_citation_count = 0
        fabricated_citations = 0
        cross_project_leakage = 0
        freshness_mismatch_silent = 0
        unsupported_answers = 0
        missed_cases: list[str] = []
        query_latencies_ms: list[float] = []
        source_digest_by_path = {
            item.relative_path: item.content_digest
            for item in bound_plan.discovery.files
            if item.relative_path in BOUNDED_PATHS
        }
        for case in GOLDEN_CASES:
            expected_revision = "stale-revision" if case.stale else source_revision
            scope = (
                AKILLI_KASA_PROJECT_ID
                if case.project_scope == "akilli-kasa"
                else case.project_scope
            )
            query_started = time.perf_counter()
            result = rag.query(
                case.query,
                project_id=scope,
                expected_source_revision=expected_revision,
                expected_tree_digest=bound_plan.tree_digest,
            )
            query_latencies_ms.append((time.perf_counter() - query_started) * 1000)
            citations = result["citations"]
            citation_count += len(citations)
            for item in citations:
                valid = (
                    item["project_scope"] == AKILLI_KASA_PROJECT_ID
                    and item["source_ref"] in BOUNDED_PATHS
                    and item["source_digest"] == source_digest_by_path.get(item["source_ref"])
                    and item["locator_type"] == "project-file"
                    and item["locator"]["relative_path"] == item["source_ref"]
                    and item["rank_trace"]["fused_rank"] >= 1
                )
                try:
                    parse_digest(item["source_id"])
                    parse_digest(item["content_digest"])
                except (TypeError, ValidationFailed):
                    valid = False
                valid_citation_count += int(valid)
                fabricated_citations += int(not valid)
            if case.expected_source is None:
                if case.category == "project-scope":
                    cross_project_leakage += len(citations)
                elif case.category == "stale-superseded":
                    freshness_mismatch_silent += int(
                        result["state"] != "abstained-index-unavailable" or bool(citations)
                    )
                elif case.category == "no-answer-adversarial":
                    unsupported_answers += int(
                        result["state"] in {"answered", "lexical-only-degraded"}
                    )
                continue
            answerable += 1
            paths = [item["source_ref"] for item in citations]
            ranks = [index + 1 for index, path in enumerate(paths) if path == case.expected_source]
            if ranks and ranks[0] <= 10:
                recalled += 1
            else:
                missed_cases.append(
                    f"{case.case_id}:{result['state']}:"
                    f"{result['top_dense_similarity']:.4f}:"
                    f"{result['lexical_coverage']:.4f}:"
                    f"{result['dense_top_2_margin']:.4f}"
                )
            reciprocal_ranks.append(1.0 / ranks[0] if ranks else 0.0)
            ndcgs.append(1.0 / math.log2(ranks[0] + 1) if ranks else 0.0)
            if case.category == "exact-identifier":
                exact_channel_top = bool(citations) and (
                    "exact" in citations[0]["retrieval_channels"]
                    and citations[0]["rank_trace"]["exact_match"] is True
                )
                exact_top_1 += int(
                    bool(paths) and paths[0] == case.expected_source and exact_channel_top
                )

        metrics: RAGMetrics = {
            "case_count": len(GOLDEN_CASES),
            "answerable_count": answerable,
            "exact_top_1": exact_top_1 / 20,
            "citation_locator_validity": valid_citation_count / citation_count,
            "cross_project_leakage": cross_project_leakage,
            "fabricated_citations": fabricated_citations,
            "unsupported_factual_answers": unsupported_answers,
            "freshness_mismatch_silent": freshness_mismatch_silent,
            "recall_at_10": recalled / answerable,
            "mrr": sum(reciprocal_ranks) / answerable,
            "ndcg_at_10": sum(ndcgs) / answerable,
            "query_p50_ms": sorted(query_latencies_ms)[49],
            "query_p95_ms": sorted(query_latencies_ms)[94],
            "missed_cases": missed_cases,
        }
        assert metrics["case_count"] == 100
        assert metrics["exact_top_1"] == 1.0, metrics
        assert metrics["citation_locator_validity"] == 1.0, metrics
        assert metrics["cross_project_leakage"] == 0
        assert metrics["fabricated_citations"] == 0
        assert metrics["unsupported_factual_answers"] == 0, metrics
        assert metrics["freshness_mismatch_silent"] == 0, metrics
        assert metrics["recall_at_10"] >= 0.85, (
            metrics["recall_at_10"],
            ",".join(missed_cases),
        )
        assert metrics["mrr"] >= 0.75, metrics
        assert metrics["ndcg_at_10"] >= 0.80, metrics
        first_generation_digest = generation.generation_digest
        assert index.integrity()["status"] == "passed"
    metrics["index_build_ms"] = (time.perf_counter() - build_started) * 1000
    metrics["index_size_bytes"] = first_path.stat().st_size

    with SQLiteKnowledgeIndex(first_path) as restarted:
        rag = EmbeddedProjectRAG(restarted, embedding.provider, embedding.policy)
        result = rag.query(
            "ADR-0006 idempotent dosya ice aktarma",
            project_id=AKILLI_KASA_PROJECT_ID,
            expected_source_revision=source_revision,
            expected_tree_digest=plan.tree_digest,
        )
        assert result["state"] == "answered"
        assert result["generation_digest"] == first_generation_digest

    second_path = tmp_path / "knowledge-rebuilt.sqlite3"
    rebuild_started = time.perf_counter()
    with SQLiteKnowledgeIndex(second_path, create=True) as rebuilt:
        _, second_generation = build_embedded_project_generation(
            rebuilt,
            plan,
            embedding_provider=embedding.provider,
            embedding_policy=embedding.policy,
        )
        assert second_generation.generation_digest == first_generation_digest
        assert rebuilt.integrity()["status"] == "passed"
    metrics["scratch_rebuild_ms"] = (time.perf_counter() - rebuild_started) * 1000
    metrics["scratch_generation_digest_equal"] = True

    assert _status_digest(root) == before
    if os.environ.get("ZEKAM_PRINT_RAG_METRICS") == "1":
        print("WP07_METRICS=" + json.dumps(metrics, sort_keys=True))
