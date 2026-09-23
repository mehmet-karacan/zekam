"""WP-08 Context Graph benchmark varyant-matris rehearsals (unit, provider-free).

Purpose
-------
This is the *mutation + dry-run* runner for the context-graph retrieval
benchmark.  It wires the deterministic golden corpus
(``benchmarks/suites/wp08_context_graph_golden.py``) into
``retrieval_service.evaluate`` across the six task-#17 variants and verifies the
**metric code path** (recall/mrr/ndcg and ``improves_on``).  It is a DENSE-CAPALI
rehearsal: every embedding channel is served by a local, deterministic Jaccard
stub and the graph variant is served by a fake read-only graph.

Provider discipline
-------------------
No provider/model/network call is made.  ``DeterministicBackend.dense`` never
invokes an embedding factory or endpoint; it computes a local Jaccard score and
counts each stub call via ``dense_calls``.  The fake graph is pure in-memory.
This file validates the metric/runner skeleton only; the dense-open full run
against the real knowledge index is a separate, explicitly authorised step.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from benchmarks.suites.wp08_context_graph_golden import (
    DOMAIN,
    EXACT_CASES,
    GOLDEN_CASES,
    QUERY,
    RAG,
    RANKING,
    RETRIEVAL,
    SQLITE,
    ContextGraphGoldenCase,
)

from zekam.application.code_graph_ranking import ProjectGraphReranker
from zekam.application.retrieval_service import GoldenCase, RetrievalService, evaluate
from zekam.domain.canonical import digest
from zekam.domain.code_graph import (
    GraphConfidence,
    GraphEdge,
    GraphFile,
    GraphGeneration,
    GraphRelation,
    GraphSymbol,
)
from zekam.domain.errors import ValidationFailed
from zekam.domain.retrieval import RetrievalChannel, ScoredHit

pytestmark = pytest.mark.unit

TREE_DIGEST = digest("wp08-tree")
ALL_FILES: tuple[str, ...] = tuple(
    sorted({p for case in GOLDEN_CASES for p in case.expected_source})
)

#: Deterministic chunk registry: each target file owns two logical chunks whose
#: ids are digests of the project-relative path (portable, repo-local).
CHUNK_IDS: dict[str, tuple[str, ...]] = {
    path: (digest("wp08-chunk:" + path) + "-0", digest("wp08-chunk:" + path) + "-1")
    for path in ALL_FILES
}
CHUNK_TO_FILE: dict[str, str] = {
    cid: path for path in ALL_FILES for cid in CHUNK_IDS[path]
}

#: Query terms each real file conceptually answers.  Used only by the local stub.
FILE_TERMS: dict[str, tuple[str, ...]] = {
    RANKING: (
        "graph",
        "rerank",
        "pagerank",
        "seed",
        "sibling",
        "promote",
        "fused",
        "reranker",
        "connectivity",
    ),
    QUERY: ("outline", "impact", "freshness", "map", "blast", "reachability", "bfs", "dependency"),
    DOMAIN: ("relation", "contains", "dependency", "edge", "symbol", "allowlist", "neighbor"),
    SQLITE: (
        "chunk",
        "link",
        "generation",
        "store",
        "symbols_for_file",
        "graph_chunk_link",
        "current_generation",
    ),
    RAG: ("embedded", "rag", "chunk", "generation", "knowledge", "project"),
    RETRIEVAL: ("exact", "lexical", "dense", "fusion", "channel", "combine", "rrf", "retrieval"),
}

#: File-path terms for the file-FTS variant (+file FTS signal).
PATH_TERMS: dict[str, tuple[str, ...]] = {
    path: tuple(part for part in re.findall(r"[a-zA-Z0-9_]+", path.lower()))
    for path in ALL_FILES
}

#: Exact identifier -> files that define it (drives the EXACT channel).
SYMBOL_INDEX: dict[str, tuple[str, ...]] = {
    case.query: case.expected_source for case in EXACT_CASES
}


def file_chunks(path: str) -> tuple[str, ...]:
    return CHUNK_IDS[path]


def relevant_ids(paths: tuple[str, ...]) -> frozenset[str]:
    """Path -> chunk-id builder: maps expected source files to chunk ids."""
    return frozenset(cid for path in paths for cid in CHUNK_IDS[path])


def to_golden(cases: tuple[ContextGraphGoldenCase, ...]) -> tuple[GoldenCase, ...]:
    """Convert the corpus (file-path based) into chunk-id based GoldenCases."""
    return tuple(
        GoldenCase(query=case.query, relevant_ids=relevant_ids(case.expected_source))
        for case in cases
    )


def _query_tokens(query: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-zA-Z0-9_]+", query.lower()))


@dataclass
class DeterministicBackend:
    """Local, deterministic search backend (exact/lexical/dense stub).

    ``dense`` is only "open" when ``dense_enabled`` is true; it still returns a
    local Jaccard score and never talks to a provider.  ``dense_calls`` counts
    stub invocations so a test can prove the channel ran provider-free.
    """

    files: tuple[str, ...]
    chunk_ids: dict[str, tuple[str, ...]]
    file_terms: dict[str, tuple[str, ...]]
    path_terms: dict[str, tuple[str, ...]]
    symbol_index: dict[str, tuple[str, ...]]
    dense_enabled: bool = False
    file_fts: bool = False
    dense_calls: int = 0
    source_type: str = "context-graph"

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
        out: list[ScoredHit] = []
        rank = 0
        for identifier in identifiers:
            for path in self.symbol_index.get(identifier, ()):
                for cid in self.chunk_ids.get(path, ()):
                    rank += 1
                    out.append(ScoredHit(cid, RetrievalChannel.EXACT, rank, 1.0))
                    if rank >= limit:
                        return tuple(out)
        return tuple(out)

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        tokens = set(_query_tokens(query))
        scored: dict[str, float] = {}
        for path in self.files:
            terms = set(self.file_terms.get(path, ()))
            score = float(len(terms & tokens))
            if self.file_fts:
                score += 2.0 * sum(1 for pt in self.path_terms.get(path, ()) if pt in tokens)
            if score > 0.0:
                scored[path] = score
        ranked = sorted(scored, key=lambda path: (-scored[path], path))
        return self._hits(ranked, RetrievalChannel.LEXICAL, limit)

    def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        # Provider-free local Jaccard stub. No embedding factory is touched.
        if not self.dense_enabled:
            return ()
        self.dense_calls += 1
        tokens = set(_query_tokens(query))
        scored: dict[str, float] = {}
        for path in self.files:
            terms = set(self.file_terms.get(path, ()))
            if not terms:
                continue
            union = tokens | terms
            scored[path] = len(tokens & terms) / (len(union) or 1.0)
        ranked = sorted(scored, key=lambda path: (-scored[path], path))
        return self._hits(ranked, RetrievalChannel.DENSE, limit)

    def _hits(
        self, ranked: list[str], channel: RetrievalChannel, limit: int
    ) -> tuple[ScoredHit, ...]:
        out: list[ScoredHit] = []
        rank = 0
        for path in ranked:
            for cid in self.chunk_ids.get(path, ()):
                rank += 1
                out.append(ScoredHit(cid, channel, rank, 1.0))
                if rank >= limit:
                    return tuple(out)
        return tuple(out)


def _generation() -> GraphGeneration:
    return GraphGeneration(
        generation_digest=digest("wp08-gen"),
        project_id="proj",
        source_revision="r1",
        tree_digest=TREE_DIGEST,
        source_manifest_digest=digest("manifest"),
        extractor_profile_digest=digest("profile"),
        file_count=2,
        symbol_count=2,
        edge_count=2,
        error_count=0,
        state="ready",
        created_at="2026-09-23T00:00:00Z",
    )


@dataclass
class FakeGraph:
    """In-memory read-only graph for the reranker / one-hop variants."""

    generation: GraphGeneration
    symbols_by_file: dict[str, tuple[str, ...]]
    raw_edges: dict[str, list[tuple[str, str]]]
    file_by_symbol: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for path, symbols in self.symbols_by_file.items():
            for symbol in symbols:
                self.file_by_symbol.setdefault(symbol, path)

    def current_generation(self, project_id: str) -> GraphGeneration:
        return self.generation

    def file_for_symbol(self, symbol_id: str) -> str:
        return self.file_by_symbol[symbol_id]

    def symbols_for_file(self, relative_path: str) -> tuple[str, ...]:
        return self.symbols_by_file.get(relative_path, ())

    def symbols(self) -> tuple[GraphSymbol, ...]:
        return ()

    def files(self) -> tuple[GraphFile, ...]:
        return ()

    def neighbors(self, symbol_id: str) -> tuple[GraphEdge, ...]:
        out: list[GraphEdge] = []
        for relation, target in self.raw_edges.get(symbol_id, []):
            out.append(
                GraphEdge(
                    edge_id=digest(f"edge:{symbol_id}:{relation}:{target}"),
                    source_symbol_id=symbol_id,
                    relation=GraphRelation(relation),
                    target_symbol_id=target,
                    target_qualified_name=target,
                    confidence=GraphConfidence.EXTRACTED,
                    provenance="fake",
                )
            )
        return tuple(out)


def _sym(name: str) -> str:
    return digest("sym:" + name)


#: Connected graph over RANKING <-> QUERY so graph rerank/one-hop have edges.
GRAPH = FakeGraph(
    generation=_generation(),
    symbols_by_file={RANKING: (_sym("sR"),), QUERY: (_sym("sQ"),)},
    raw_edges={
        _sym("sR"): [("references", _sym("sQ"))],
        _sym("sQ"): [("references", _sym("sR"))],
    },
)


def _build_backend(*, dense: bool, file_fts: bool = False) -> DeterministicBackend:
    return DeterministicBackend(
        files=ALL_FILES,
        chunk_ids=CHUNK_IDS,
        file_terms=FILE_TERMS,
        path_terms=PATH_TERMS,
        symbol_index=SYMBOL_INDEX,
        dense_enabled=dense,
        file_fts=file_fts,
    )


def _build_reranker() -> ProjectGraphReranker:
    return ProjectGraphReranker(
        GRAPH,
        project_id="proj",
        source_revision="r1",
        tree_digest=TREE_DIGEST,
        chunk_file=CHUNK_TO_FILE.__getitem__,
    )


def _base_run(
    backend: DeterministicBackend, *, reranker: ProjectGraphReranker | None
) -> Callable[[str], tuple[str, ...]]:
    service = RetrievalService(backend=backend, reranker=reranker)

    def run(query: str) -> tuple[str, ...]:
        hits, _trace = service.search(query)
        return tuple(hit.chunk_id for hit in hits)

    return run


def _file_diversity_run(
    run: Callable[[str], tuple[str, ...]], chunk_file: dict[str, str], max_per_file: int
) -> Callable[[str], tuple[str, ...]]:
    def wrapped(query: str) -> tuple[str, ...]:
        per_file: dict[str, int] = {}
        out: list[str] = []
        for cid in run(query):
            path = chunk_file.get(cid)
            if path is None:
                out.append(cid)
                continue
            count = per_file.get(path, 0)
            if count < max_per_file:
                out.append(cid)
                per_file[path] = count + 1
        return tuple(out)

    return wrapped


def _one_hop_run(
    run: Callable[[str], tuple[str, ...]],
    chunk_file: dict[str, str],
    symbols_by_file: dict[str, tuple[str, ...]],
    chunk_ids: dict[str, tuple[str, ...]],
    *,
    max_expansion: int,
) -> Callable[[str], tuple[str, ...]]:
    def wrapped(query: str) -> tuple[str, ...]:
        base = tuple(run(query))
        added: list[str] = []
        present = set(base)
        for cid in base:
            path = chunk_file.get(cid)
            if path is None:
                continue
            for symbol in symbols_by_file.get(path, ()):
                for edge in GRAPH.neighbors(symbol):
                    neighbor = edge.target_symbol_id
                    if neighbor is None:
                        continue
                    neighbor_path = GRAPH.file_by_symbol.get(neighbor)
                    if neighbor_path is None or neighbor_path == path:
                        continue
                    for ncid in chunk_ids.get(neighbor_path, ()):
                        if ncid not in present and ncid not in added:
                            added.append(ncid)
                            if len(added) >= max_expansion:
                                return (*base, *added)
        return (*base, *added)

    return wrapped


def _variant_run(variant: int) -> Callable[[str], tuple[str, ...]]:
    if variant == 1:  # exact + FTS, dense kapali
        return _base_run(_build_backend(dense=False), reranker=None)
    if variant == 2:  # exact + FTS + vector (baseline, dense stub acik)
        return _base_run(_build_backend(dense=True), reranker=None)
    if variant == 3:  # +file diversity
        run = _base_run(_build_backend(dense=True), reranker=None)
        return _file_diversity_run(run, CHUNK_TO_FILE, max_per_file=2)
    if variant == 4:  # +graph rerank
        return _base_run(_build_backend(dense=True), reranker=_build_reranker())
    if variant == 5:  # +file FTS
        return _base_run(_build_backend(dense=True, file_fts=True), reranker=None)
    # variant 6: +bounded one-hop (graph neighbours of top chunks)
    run = _base_run(_build_backend(dense=True), reranker=_build_reranker())
    return _one_hop_run(run, CHUNK_TO_FILE, GRAPH.symbols_by_file, CHUNK_IDS, max_expansion=6)


# -- corpus shape ------------------------------------------------------------


def test_corpus_shape_and_categories() -> None:
    assert len(GOLDEN_CASES) == 20
    assert len({case.case_id for case in GOLDEN_CASES}) == 20
    by_category: dict[str, int] = {}
    for case in GOLDEN_CASES:
        assert case.expected_source
        for path in case.expected_source:
            assert ".." not in path
        by_category[case.category] = by_category.get(case.category, 0) + 1
    assert by_category["exact-identifier"] == 5
    assert by_category["semantic"] == 10
    assert by_category["graph-connectivity"] == 5


def test_relevant_ids_builder_is_deterministic() -> None:
    first = to_golden(GOLDEN_CASES)
    again = to_golden(GOLDEN_CASES)
    assert first == again
    ranking_only = [g for g in first if g.query == "ProjectGraphReranker"]
    assert len(ranking_only) == 1
    assert ranking_only[0].relevant_ids == frozenset(file_chunks(RANKING))


# -- metric-code behaviour ---------------------------------------------------


def test_metric_code_perfect_and_empty() -> None:
    case = GoldenCase(query="q1", relevant_ids=frozenset({"a", "b"}))
    perfect = evaluate((case,), run=lambda _q: ("a", "b", "c"), k=3)
    empty = evaluate((case,), run=lambda _q: ("x", "y"), k=3)
    assert perfect.recall_at_k == 1.0
    assert perfect.mrr == 1.0
    assert perfect.ndcg_at_k == 1.0
    assert empty.recall_at_k == 0.0
    assert empty.mrr == 0.0
    assert empty.ndcg_at_k == 0.0
    assert perfect.improves_on(empty) is True
    assert empty.improves_on(perfect) is False


def test_improves_on_rejects_regression_and_requires_gain() -> None:
    case = GoldenCase(query="q1", relevant_ids=frozenset({"a", "b"}))
    baseline = evaluate((case,), run=lambda _q: ("a", "b"), k=3)
    regression = evaluate((case,), run=lambda _q: ("a", "x"), k=3)
    equal = evaluate((case,), run=lambda _q: ("a", "b"), k=3)
    assert regression.improves_on(baseline) is False
    assert equal.improves_on(baseline) is False


def test_evaluate_guards_invalid_input() -> None:
    with pytest.raises(ValidationFailed):
        evaluate((), run=lambda _q: ("a",), k=3)
    case = GoldenCase(query="q1", relevant_ids=frozenset({"a"}))
    with pytest.raises(ValidationFailed):
        evaluate((case,), run=lambda _q: ("a",), k=0)


# -- varyant matrix ----------------------------------------------------------


def _in_range(value: float) -> bool:
    return 0.0 <= value <= 1.0


@pytest.mark.parametrize("variant", [1, 2, 3, 4, 5, 6])
def test_variant_runs_deterministically_in_range(variant: int) -> None:
    cases = to_golden(GOLDEN_CASES)
    run = _variant_run(variant)
    first = evaluate(cases, run=run, k=10)
    again = evaluate(cases, run=run, k=10)
    assert first.as_dict() == again.as_dict()
    assert first.case_count == 20
    assert _in_range(first.recall_at_k)
    assert _in_range(first.mrr)
    assert _in_range(first.ndcg_at_k)


def test_dense_variant_uses_stub_no_provider() -> None:
    """Proves the dense channel ran purely through the local stub (provider-free)."""
    backend = _build_backend(dense=True)
    run = _base_run(backend, reranker=None)
    cases = to_golden(GOLDEN_CASES)
    result = evaluate(cases, run=run, k=10)
    # Dense stub was actually exercised once per corpus query, with no provider.
    assert backend.dense_calls == len(cases) == 20
    # Local stub is deterministic: a second pass reproduces byte-identical metrics.
    rerun = evaluate(cases, run=run, k=10)
    assert rerun.as_dict() == result.as_dict()


def test_graph_reranker_wired_and_deterministic() -> None:
    backend = _build_backend(dense=True)
    reranker = _build_reranker()
    service = RetrievalService(backend=backend, reranker=reranker)
    first, trace_first = service.search("graph rerank seed connectivite")
    second, _trace_second = service.search("graph rerank seed connectivite")
    assert [hit.chunk_id for hit in first] == [hit.chunk_id for hit in second]
    assert trace_first.reranker_used is True
    assert trace_first.graph_used is True


def test_dense_disabled_variant_never_calls_stub() -> None:
    backend = _build_backend(dense=False)
    run = _base_run(backend, reranker=None)
    evaluate(to_golden(GOLDEN_CASES), run=run, k=10)
    assert backend.dense_calls == 0
