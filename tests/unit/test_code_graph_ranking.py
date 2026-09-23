"""Graph-aware reranker (G2) unit tests.

Covers PPR determinism, connectivity-driven promotion, ``contains`` exclusion,
exact-hit preservation, same-file sibling diversity, cyclic bounds, fused set
equality, binding/fallback behaviour and configuration validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from tests.unit.test_embedded_project_rag import (
    PROJECT,
    REVISION,
    TREE,
    QueryProvider,
    _vector,
)

from zekam.application.code_graph_ranking import (
    GraphRankingConfig,
    GraphSeedMapper,
    ProjectGraphReranker,
    approximate_pagerank,
)
from zekam.application.embedded_project_rag import (
    EmbeddedProjectRAG,
    compose_graph_reranker,
)
from zekam.application.embedding_provider import EmbeddingPolicy
from zekam.application.knowledge_index import KnowledgeIndexRecord
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.code_graph import (
    GraphConfidence,
    GraphEdge,
    GraphFile,
    GraphGeneration,
    GraphRelation,
    GraphSymbol,
)
from zekam.domain.errors import ValidationFailed
from zekam.domain.knowledge import Locator
from zekam.domain.retrieval import FusedHit
from zekam.domain.security import DataClassification
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex

pytestmark = pytest.mark.unit


def _id(name: str) -> str:
    """Deterministic, valid symbol id (must satisfy digest validation)."""
    return digest("sym:" + name)


CH = GraphConfidence.EXTRACTED

TREE_DIGEST = digest("tree1")


def _generation(
    *,
    project_id: str = "proj",
    revision: str = "r1",
    tree_digest: str | None = None,
    state: str = "ready",
) -> GraphGeneration:
    return GraphGeneration(
        generation_digest=digest("generation"),
        project_id=project_id,
        source_revision=revision,
        tree_digest=tree_digest if tree_digest is not None else TREE_DIGEST,
        source_manifest_digest=digest("manifest"),
        extractor_profile_digest=digest("profile"),
        file_count=1,
        symbol_count=0,
        edge_count=0,
        error_count=0,
        state=state,
        created_at="2026-09-21T00:00:00Z",
    )


@dataclass
class FakeGraph:
    """Scriptable read-only graph for ranking tests.  Endpoints are digests."""

    generation: GraphGeneration
    symbols_by_file: dict[str, tuple[str, ...]]
    file_by_symbol: dict[str, str]
    # source_symbol -> list of (relation, target_symbol)
    raw_edges: dict[str, list[tuple[str, str]]]

    def __post_init__(self) -> None:
        for path, symbols in self.symbols_by_file.items():
            for symbol in symbols:
                self.file_by_symbol.setdefault(symbol, path)

    def current_generation(self, project_id: str) -> GraphGeneration:
        return self.generation

    def neighbors(self, symbol_id: str) -> tuple[GraphEdge, ...]:
        out: list[GraphEdge] = []
        for relation, target in self.raw_edges.get(symbol_id, []):
            out.append(
                GraphEdge(
                    edge_id=digest(f"edge-{symbol_id}-{relation}-{target}"),
                    source_symbol_id=symbol_id,
                    relation=GraphRelation(relation),
                    target_symbol_id=target,
                    target_qualified_name=target,
                    confidence=CH,
                    provenance="f",
                )
            )
        return tuple(out)

    def file_for_symbol(self, symbol_id: str) -> str:
        return self.file_by_symbol[symbol_id]

    def symbols_for_file(self, relative_path: str) -> tuple[str, ...]:
        return self.symbols_by_file.get(relative_path, ())

    def symbols(self) -> tuple[GraphSymbol, ...]:
        # Ranking never consumes the full symbol/file listing; empty is
        # sufficient to satisfy the read port for query/map composition.
        return ()

    def files(self) -> tuple[GraphFile, ...]:
        return ()


def _hit(chunk_id: str, exact: bool = False) -> FusedHit:
    return FusedHit(chunk_id, score=1.0, channels=(), exact_match=exact)


def _reranker(
    graph: FakeGraph,
    chunk_files: dict[str, str],
    *,
    project_id: str = "proj",
    revision: str = "r1",
    tree_digest: str | None = None,
    config: GraphRankingConfig | None = None,
) -> ProjectGraphReranker:
    return ProjectGraphReranker(
        graph,
        project_id=project_id,
        source_revision=revision,
        tree_digest=tree_digest if tree_digest is not None else TREE_DIGEST,
        chunk_file=chunk_files.__getitem__,
        config=config,
    )


# -- PPR ---------------------------------------------------------------------


def test_ppr_is_deterministic_and_bounded() -> None:
    seeds = {_id("a"): 1.0, _id("b"): 2.0}
    adjacency = {_id("a"): frozenset({_id("b")}), _id("b"): frozenset({_id("a")})}
    first = approximate_pagerank(seeds, adjacency, alpha=0.25, iterations=25)
    again = approximate_pagerank(seeds, adjacency, alpha=0.25, iterations=25)
    assert first == again
    assert all(value >= 0.0 for value in first.values())


def test_ppr_empty_seeds_returns_empty() -> None:
    assert approximate_pagerank({}, {_id("a"): frozenset()}, alpha=0.25, iterations=10) == {}


def test_contains_edge_never_enters_config() -> None:
    with pytest.raises(ValueError):
        GraphRankingConfig(dependency_relations=frozenset({GraphRelation.CONTAINS}))


def test_config_validation_rejects_invalid_alpha() -> None:
    with pytest.raises(ValueError):
        GraphRankingConfig(alpha=0.0)
    with pytest.raises(ValueError):
        GraphRankingConfig(alpha=1.5)


# -- GraphSeedMapper ---------------------------------------------------------


def test_seed_mapper_weights_top_and_exact_higher() -> None:
    graph = FakeGraph(
        _generation(),
        symbols_by_file={"a.py": (_id("a1"), _id("a2")), "b.py": (_id("b1"),)},
        file_by_symbol={},
        raw_edges={},
    )
    fused = (
        _hit("c-b", exact=True),
        _hit("c-a1"),
        _hit("c-a2"),
    )
    mapping = {"c-b": "b.py", "c-a1": "a.py", "c-a2": "a.py"}
    seed_map = GraphSeedMapper(
        graph, config=GraphRankingConfig()
    ).map_hits(fused, mapping.__getitem__)
    weight_b = sum(seed_map.weights.get(s, 0.0) for s in (_id("b1"),))
    weight_a = sum(seed_map.weights.get(s, 0.0) for s in (_id("a1"), _id("a2")))
    assert weight_b > weight_a


# -- Reranker: connectivity --------------------------------------------------


def _connected_graph() -> FakeGraph:
    a1, a2, a3, b1 = _id("a1"), _id("a2"), _id("a3"), _id("b1")
    return FakeGraph(
        _generation(),
        symbols_by_file={"file_a.py": (a1, a2, a3), "file_b.py": (b1,)},
        file_by_symbol={},
        raw_edges={
            a1: [("references", a2)],
            a2: [("references", a3)],
            a3: [("references", a1)],
        },
        # b1 intentionally has no dependency edges.
    )


def _three_file_graph() -> FakeGraph:
    a = tuple(_id(f"a{i}") for i in range(6))
    b1 = _id("b1")
    c1, c2 = _id("c1"), _id("c2")
    raw_edges: dict[str, list[tuple[str, str]]] = {}
    for i in range(6):
        raw_edges.setdefault(a[i], []).append(("references", a[(i + 1) % 6]))
    raw_edges.setdefault(c1, []).append(("references", a[0]))
    raw_edges.setdefault(c2, []).append(("references", a[0]))
    return FakeGraph(
        _generation(),
        symbols_by_file={"file_a.py": a, "file_b.py": (b1,), "file_c.py": (c1, c2)},
        file_by_symbol={},
        raw_edges=raw_edges,
        # file_b (b1) is fully isolated.
    )


def test_strong_connected_candidate_promoted() -> None:
    graph = _three_file_graph()
    # Original order puts the isolated file_b second; the connected file_c is
    # promoted above it even though it started lower.
    fused = (_hit("c-a1"), _hit("c-b"), _hit("c-c"))
    chunk_files = {"c-a1": "file_a.py", "c-b": "file_b.py", "c-c": "file_c.py"}
    reranker = _reranker(graph, chunk_files)
    result = reranker("query", fused)
    ids = [hit.chunk_id for hit in result]
    assert ids[0] == "c-a1"  # original top hit preserved
    assert ids.index("c-c") < ids.index("c-b")
    assert reranker.last_state.used is True


def test_disconnected_lexical_collision_deranked() -> None:
    graph = _three_file_graph()
    fused = (_hit("c-a1"), _hit("c-b"), _hit("c-c"))
    chunk_files = {"c-a1": "file_a.py", "c-b": "file_b.py", "c-c": "file_c.py"}
    reranker = _reranker(graph, chunk_files)
    result = reranker("query", fused)
    ids = [hit.chunk_id for hit in result]
    # The isolated file_b sinks below the connected file_c.
    assert ids.index("c-b") > ids.index("c-c")
    assert ids[0] == "c-a1"


def test_exact_top_hit_preserved_and_front() -> None:
    graph = _connected_graph()
    fused = (_hit("c-b", exact=True), _hit("c-a1"))
    chunk_files = {"c-b": "file_b.py", "c-a1": "file_a.py"}
    reranker = _reranker(graph, chunk_files)
    result = reranker("query", fused)
    assert result[0].chunk_id == "c-b"
    assert result[0].exact_match is True
    assert {hit.chunk_id for hit in result} == {hit.chunk_id for hit in fused}


def test_same_file_sibling_diversity() -> None:
    graph = FakeGraph(
        _generation(),
        symbols_by_file={"one.py": (_id("a"),), "two.py": (_id("b"),)},
        file_by_symbol={},
        raw_edges={_id("a"): [("references", _id("b"))], _id("b"): [("references", _id("a"))]},
    )
    fused = (_hit("one-1"), _hit("one-2"), _hit("one-3"), _hit("two-1"))
    chunk_files = {
        "one-1": "one.py",
        "one-2": "one.py",
        "one-3": "one.py",
        "two-1": "two.py",
    }
    reranker = _reranker(graph, chunk_files)
    result = reranker("query", fused)
    ids = [hit.chunk_id for hit in result]
    assert ids.index("two-1") < max(ids.index("one-2"), ids.index("one-3"))
    assert set(ids) == {hit.chunk_id for hit in fused}


def test_reranker_preserves_fused_set_identity() -> None:
    graph = _connected_graph()
    fused = tuple(_hit(f"c-{i}") for i in range(6))
    chunk_files = {f"c-{i}": ("file_a.py" if i % 2 == 0 else "file_b.py") for i in range(6)}
    reranker = _reranker(graph, chunk_files)
    result = reranker("query", fused)
    assert {hit.chunk_id for hit in result} == {hit.chunk_id for hit in fused}
    assert len(result) == len(fused)


def test_cyclic_graph_bounded() -> None:
    symbols = tuple(_id(f"s{i}") for i in range(4))
    graph = FakeGraph(
        _generation(),
        symbols_by_file={"f.py": symbols},
        file_by_symbol={},
        raw_edges={symbols[i]: [("references", symbols[(i + 1) % 4])] for i in range(4)},
    )
    fused = tuple(_hit(f"c-{i}") for i in range(4))
    chunk_files = {f"c-{i}": "f.py" for i in range(4)}
    reranker = _reranker(graph, chunk_files, config=GraphRankingConfig(max_iterations=50))
    result = reranker("query", fused)
    assert len(result) == len(fused)
    assert reranker.last_state.used is True


# -- Binding / fallback ------------------------------------------------------


def test_binding_mismatch_falls_back_with_trace() -> None:
    graph = _connected_graph()
    fused = (_hit("c-a1"), _hit("c-b"))
    chunk_files = {"c-a1": "file_a.py", "c-b": "file_b.py"}
    reranker = ProjectGraphReranker(
        graph,
        project_id="proj",
        source_revision="DIFFERENT",
        tree_digest=TREE_DIGEST,
        chunk_file=chunk_files.__getitem__,
    )
    result = reranker("query", fused)
    assert [hit.chunk_id for hit in result] == [hit.chunk_id for hit in fused]
    assert reranker.last_state.bypass == "binding-mismatch"
    assert reranker.last_state.used is False


def test_graph_missing_falls_back() -> None:
    class MissingGraph(FakeGraph):
        def current_generation(self, project_id: str) -> GraphGeneration:
            raise ValidationFailed("yok")

    graph = MissingGraph(
        _generation(),
        symbols_by_file={"f.py": (_id("a"),)},
        file_by_symbol={},
        raw_edges={},
    )
    fused = (_hit("c-a"),)
    chunk_files = {"c-a": "f.py"}
    reranker = _reranker(graph, chunk_files)
    result = reranker("query", fused)
    assert [hit.chunk_id for hit in result] == ["c-a"]
    assert reranker.last_state.graph_state == "unavailable"


def test_stale_graph_state_falls_back() -> None:
    graph = FakeGraph(
        _generation(state="building"),
        symbols_by_file={"f.py": (_id("a"),)},
        file_by_symbol={},
        raw_edges={},
    )
    fused = (_hit("c-a"),)
    chunk_files = {"c-a": "f.py"}
    reranker = _reranker(graph, chunk_files)
    result = reranker("query", fused)
    assert [hit.chunk_id for hit in result] == ["c-a"]
    assert reranker.last_state.bypass == "graph-not-ready"


def test_no_seed_symbols_falls_back() -> None:
    graph = FakeGraph(
        _generation(),
        symbols_by_file={"other.py": (_id("a"),)},
        file_by_symbol={},
        raw_edges={},
    )
    fused = (_hit("c-x"),)
    chunk_files = {"c-x": "unmapped.py"}
    reranker = _reranker(graph, chunk_files)
    result = reranker("query", fused)
    assert [hit.chunk_id for hit in result] == ["c-x"]
    assert reranker.last_state.bypass == "no-seed-symbols"
    assert reranker.last_state.used is True


# -- Real SQLite store read helpers ------------------------------------------


def test_sqlite_neighbors_exclude_contains_and_roundtrip(tmp_path: Path) -> None:
    from zekam.application.code_graph import apply_graph_build, plan_graph_build
    from zekam.application.code_graph_python import PythonAstExtractor
    from zekam.infrastructure.sqlite.code_graph import SQLiteCodeGraphStore

    source_root = tmp_path / "src"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "m.py").write_text(
        "class Base:\n    def greet(self) -> str:\n        return 'hi'\n"
        "def caller():\n    return Base().greet()\n",
        encoding="utf-8",
    )
    store = SQLiteCodeGraphStore(tmp_path / "g.sqlite3", create=True)
    extractor = PythonAstExtractor()
    plan = plan_graph_build(
        source_root,
        project_id="proj",
        project_slug="proj",
        source_revision="r1",
        extractor=extractor,
        created_at="2026-09-21T00:00:00Z",
    )
    apply_graph_build(store, extractor, source_root, plan)
    gen = store.current_generation("proj")
    assert gen.state == "ready"

    symbols_in_file = store.symbols_for_file("m.py")
    assert symbols_in_file, "extractor should produce symbols"
    for symbol in symbols_in_file:
        for edge in store.neighbors(symbol):
            assert edge.relation is not GraphRelation.CONTAINS
        assert store.file_for_symbol(symbol) == "m.py"
    store.close()


# -- Graph-off / baseline regression (task #21, item-5) -----------------------


def _graph_off_rag(
    tmp_path: Path,
) -> tuple[SQLiteKnowledgeIndex, QueryProvider, EmbeddedProjectRAG]:
    provider = QueryProvider()
    index = SQLiteKnowledgeIndex(tmp_path / "knowledge.sqlite3", create=True)
    records = tuple(
        KnowledgeIndexRecord(
            chunk_id=f"chunk-{i}",
            project_id=PROJECT,
            source_revision=REVISION,
            source_path=f"src/mod_{i}.py",
            source_digest=digest({"source": i}),
            locator=Locator(relative_path=f"src/mod_{i}.py", line_start=1, line_end=3),
            text=f"ADR-0006 idempotent dosya ice aktarma modulu {i}",
            content_digest=digest_of_bytes(
                f"ADR-0006 idempotent dosya ice aktarma modulu {i}".encode()
            ),
            chunk_order=i,
            vector=_vector(),
        )
        for i in range(3)
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
    policy = EmbeddingPolicy(DataClassification.LOCAL_ONLY, provider.profile.profile_digest)
    return index, provider, EmbeddedProjectRAG(index, provider, policy)


def test_graph_off_baseline_regression_result_is_byte_for_byte(tmp_path: Path) -> None:
    """Graph-disabled (default-off) composition keeps the baseline RAG identical.

    Single, dedicated gate for the "graph-off baseline regression" criterion:
    with the graph simply not wired in, the high-level retrieval result must be
    byte-for-byte identical to running with no reranker at all.  The enabler
    flag is the only difference between the two composed paths; no provider or
    model call happens anywhere (pure stub backend + fake graph).
    """
    index, provider, rag = _graph_off_rag(tmp_path)

    # Fake graph whose binding matches the generation identity, so the enabler
    # flag is the sole switch between baseline and graph composition.
    graph = FakeGraph(
        _generation(),
        symbols_by_file={"src/mod_0.py": (_id("a"),)},
        file_by_symbol={},
        raw_edges={},
    )

    def chunk_file(chunk_id: str) -> str | None:
        return f"src/mod_{chunk_id[-1]}.py" if chunk_id.startswith("chunk-") else None

    try:
        # 1. Default-off composition yields no reranker at all.
        off = compose_graph_reranker(
            graph,
            project_id=PROJECT,
            source_revision=REVISION,
            tree_digest=TREE,
            chunk_file=chunk_file,
            enabled=False,
        )
        assert off is None

        # The flag genuinely toggles composition (guards a vacuous pass): with
        # the same graph enabled, a reranker is produced.
        on = compose_graph_reranker(
            graph,
            project_id=PROJECT,
            source_revision=REVISION,
            tree_digest=TREE,
            chunk_file=chunk_file,
            enabled=True,
        )
        assert on is not None

        baseline = rag.query(
            "ADR-0006",
            project_id=PROJECT,
            expected_source_revision=REVISION,
            expected_tree_digest=TREE,
        )
        composed = EmbeddedProjectRAG(
            rag.index, provider, rag.embedding_policy, reranker=off
        ).query(
            "ADR-0006",
            project_id=PROJECT,
            expected_source_revision=REVISION,
            expected_tree_digest=TREE,
        )

        # 2. Baseline (reranker=None) == graph-wired-off composition, byte-for-byte:
        # fused-order / used chunk ids / citations / answer all identical, and no
        # graph_reranker trace leaks into the result.
        assert composed == baseline
        assert composed["used_chunk_ids"] == baseline["used_chunk_ids"]
        assert composed["citations"] == baseline["citations"]
        assert composed["retrieval_digest"] == baseline["retrieval_digest"]
        assert composed["graph_reranker"] is None
        assert baseline["graph_reranker"] is None
    finally:
        index.close()
