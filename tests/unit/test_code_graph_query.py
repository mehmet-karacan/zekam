"""G3: read-only structural code-graph query service tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from zekam.application.code_graph import apply_graph_build, plan_graph_build
from zekam.application.code_graph_python import PythonAstExtractor
from zekam.application.code_graph_query import (
    graph_find,
    graph_freshness,
    graph_impact,
    graph_map,
    graph_outline,
)
from zekam.domain.canonical import digest
from zekam.domain.code_graph import (
    GraphConfidence,
    GraphEdge,
    GraphFile,
    GraphGeneration,
    GraphNodeKind,
    GraphRelation,
    GraphSymbol,
    edge_identity,
    symbol_identity,
)
from zekam.infrastructure.sqlite.code_graph import SQLiteCodeGraphStore

pytestmark = pytest.mark.unit

_CREATED_AT = "2026-09-23T00:00:00Z"


class _FakeGraph:
    """In-memory GraphReadPort stub with controlled resolved dependency edges.

    Lets the traversal tests (direct/transitive/cycle) assert BFS semantics
    deterministically, independent of the Python AST extractor's unresolved
    cross-call edges.
    """

    def __init__(
        self,
        symbols: list[GraphSymbol],
        edges: list[GraphEdge],
        *,
        project_id: str = "proj",
    ) -> None:
        self._edges = edges
        self._symbols: dict[str, GraphSymbol] = {symbol.symbol_id: symbol for symbol in symbols}
        self._project_id = project_id
        self._generation = GraphGeneration(
            generation_digest=digest("gen"),
            project_id=project_id,
            source_revision="r1",
            tree_digest=digest("tree"),
            source_manifest_digest=digest("manifest"),
            extractor_profile_digest=digest("profile"),
            file_count=1,
            symbol_count=len(self._symbols),
            edge_count=len(edges),
            error_count=0,
            state="ready",
            created_at=_CREATED_AT,
        )

    def current_generation(self, project_id: str) -> GraphGeneration:
        if project_id != self._project_id:
            raise LookupError("missing")
        return self._generation

    def neighbors(self, symbol_id: str) -> tuple[GraphEdge, ...]:
        return tuple(
            edge
            for edge in self._edges
            if edge.source_symbol_id == symbol_id or edge.target_symbol_id == symbol_id
        )

    def symbols(self) -> tuple[GraphSymbol, ...]:
        return tuple(self._symbols.values())

    def files(self) -> tuple[GraphFile, ...]:
        return (
            GraphFile(
                relative_path="p.py",
                content_digest=digest("content"),
                parse_state="parsed",
                error_count=0,
                extractor_profile_digest=digest("profile"),
            ),
        )

    def file_for_symbol(self, symbol_id: str) -> str:
        return "p.py"

    def symbols_for_file(self, relative_path: str) -> tuple[str, ...]:
        return tuple(self._symbols)


def _fake_name(symbol_id: str) -> str:
    return f"sym.{symbol_id[-6:]}"


def _make_symbol(name: str) -> GraphSymbol:
    return GraphSymbol(
        symbol_id=symbol_identity(
            kind=GraphNodeKind.FUNCTION,
            qualified_name=name,
            file_relative_path="p.py",
            disambiguator=0,
        ),
        qualified_name=name,
        kind=GraphNodeKind.FUNCTION,
        file_relative_path="p.py",
        parent_symbol_id=None,
        body_digest=digest(name),
        start_line=1,
        end_line=2,
        confidence=GraphConfidence.EXTRACTED,
    )


def _make_edge(source: GraphSymbol, target: GraphSymbol) -> GraphEdge:
    return GraphEdge(
        edge_id=edge_identity(
            source_symbol_id=source.symbol_id,
            relation=GraphRelation.CALLS,
            target_symbol_id=target.symbol_id,
            target_qualified_name=target.qualified_name,
            confidence=GraphConfidence.EXTRACTED,
        ),
        source_symbol_id=source.symbol_id,
        relation=GraphRelation.CALLS,
        target_symbol_id=target.symbol_id,
        target_qualified_name=target.qualified_name,
        confidence=GraphConfidence.EXTRACTED,
        provenance="fake",
    )


def _pipeline(
    tmp_path: Path,
    files: dict[str, str],
    *,
    project_id: str = "proj",
    slug: str = "proj",
    revision: str = "r1",
) -> tuple[SQLiteCodeGraphStore, str, str, str]:
    """Create a source tree, build a ready graph and return store + identity.

    Returns ``(store, project_id, source_revision, tree_digest)``.
    """
    source_root = tmp_path / "srcroot"
    source_root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = source_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    store_path = tmp_path / "graph.sqlite3"
    store = SQLiteCodeGraphStore(store_path, create=True)
    extractor = PythonAstExtractor()
    plan = plan_graph_build(
        source_root,
        project_id=project_id,
        project_slug=slug,
        source_revision=revision,
        extractor=extractor,
        created_at=_CREATED_AT,
    )
    apply_graph_build(store, extractor, source_root, plan)
    return store, plan.project_id, plan.source_revision, plan.tree_digest


def test_graph_find_exact_and_fuzzy(tmp_path: Path) -> None:
    store, project_id, _rev, _tree = _pipeline(
        tmp_path,
        {
            "pkg/module_a.py": "def target_fn():\n    return 1\n",
            "pkg/util.py": "def other():\n    return 2\n",
        },
    )
    try:
        exact = graph_find(store, project_id, "pkg.module_a.target_fn")
        assert exact, "exact symbol match expected"
        assert exact[0]["confidence"] == "exact"
        assert exact[0]["match_type"] == "symbol"
        assert exact[0]["name"] == "pkg.module_a.target_fn"

        # Short query yields fuzzy symbol matches.
        fuzzy = graph_find(store, project_id, "target_fn")
        assert fuzzy
        assert all(e["confidence"] == "fuzzy" for e in fuzzy)
        assert any(e["name"] == "pkg.module_a.target_fn" for e in fuzzy)

        # Fuzzy file match (case-insensitive substring).
        fuzzy_file = graph_find(store, project_id, "module_")
        file_hits = [e for e in fuzzy_file if e["match_type"] == "file"]
        assert any(e["name"] == "pkg/module_a.py" for e in file_hits)

        # Unknown yields empty (generation exists but nothing matches).
        assert graph_find(store, project_id, "does_not_exist_123") == []
    finally:
        store.close()


def test_graph_find_unavailable_generation(tmp_path: Path) -> None:
    store = SQLiteCodeGraphStore(tmp_path / "graph.sqlite3", create=True)
    try:
        assert graph_find(store, "ghost", "anything") == []
        assert graph_map(store, "ghost") == []
        assert graph_outline(store, "ghost", "x.py") == []
    finally:
        store.close()


def test_graph_outline_hierarchy(tmp_path: Path) -> None:
    store, project_id, _rev, _tree = _pipeline(
        tmp_path,
        {
            "svc/calc.py": (
                "class Calculator:\n"
                "    def add(self, a, b):\n"
                "        return a + b\n"
                "    def sub(self, a, b):\n"
                "        return a - b\n"
                "def helper():\n"
                "    return 0\n"
            )
        },
    )
    try:
        outline = graph_outline(store, project_id, "svc/calc.py")
        assert outline, "outline expected"
        assert outline[0]["kind"] == "module"
        assert outline[0]["level"] == 0
        class_entry = next(e for e in outline if e["kind"] == "class")
        assert str(class_entry["name"]).endswith("Calculator")
        methods = [e for e in outline if e["kind"] == "method"]
        assert len(methods) >= 2
        # Methods nest under the class (level +1 of the class).
        assert all(
            int(cast(Any, m["level"])) == int(cast(Any, class_entry["level"])) + 1
            for m in methods
        )
        assert all(
            int(cast(Any, m["start_line"])) >= 1
            and int(cast(Any, m["end_line"])) >= int(cast(Any, m["start_line"]))
            for m in methods
        )
        # locator carries file + line range.
        assert str(class_entry["locator"]).startswith("svc/calc.py:")

        # Missing file -> empty outline.
        assert graph_outline(store, project_id, "svc/missing.py") == []
    finally:
        store.close()


def test_graph_impact_direct_and_transitive() -> None:
    a, b, c = _make_symbol("p.a"), _make_symbol("p.b"), _make_symbol("p.c")
    graph = _FakeGraph([a, b, c], [_make_edge(a, b), _make_edge(b, c)])
    impact = graph_impact(graph, "proj", "p.a")
    assert impact["found"] is True
    out_names = {
        str(h["other_name"])
        for h in cast(list[dict[str, object]], impact["direct_outgoing"])
    }
    assert "p.b" in out_names
    trans_names = {
        str(h["other_name"])
        for h in cast(list[dict[str, object]], impact["transitive_outgoing"])
    }
    assert "p.c" in trans_names
    assert impact["cycle_detected"] is False


def test_graph_impact_cycle_detected() -> None:
    a, b = _make_symbol("p.a"), _make_symbol("p.b")
    graph = _FakeGraph([a, b], [_make_edge(a, b), _make_edge(b, a)])
    impact = graph_impact(graph, "proj", "p.a")
    assert impact["found"] is True
    assert impact["cycle_detected"] is True
    out_names = {
        str(h["other_name"])
        for h in cast(list[dict[str, object]], impact["direct_outgoing"])
    }
    assert "p.b" in out_names


def test_graph_impact_on_real_store_found(tmp_path: Path) -> None:
    store, project_id, _rev, _tree = _pipeline(
        tmp_path,
        {
            "mutual.py": (
                "def a():\n    return b()\n"
                "def b():\n    return a()\n"
            )
        },
    )
    try:
        impact = graph_impact(store, project_id, "mutual.a")
        assert impact["found"] is True
        assert impact["symbol"] == "mutual.a"
        assert impact["relative_path"] == "mutual.py"
    finally:
        store.close()


def test_graph_impact_unknown_symbol(tmp_path: Path) -> None:
    store, project_id, _rev, _tree = _pipeline(
        tmp_path,
        {"a.py": "def real():\n    return 1\n"},
    )
    try:
        impact = graph_impact(store, project_id, "a.nonexistent_symbol")
        assert impact["found"] is False
    finally:
        store.close()


def test_graph_map_aggregation(tmp_path: Path) -> None:
    store, project_id, _rev, _tree = _pipeline(
        tmp_path,
        {
            "src/app.py": "def a():\n    return b()\n" "def b():\n    return 1\n",
            "tests/test_app.py": "def test_a():\n    return 1\n",
        },
    )
    try:
        rows = graph_map(store, project_id)
        by_path = {r["relative_path"]: r for r in rows}
        assert "src/app.py" in by_path
        src = by_path["src/app.py"]
        assert src["category"] == "source"
        assert int(cast(Any, src["symbol_count"])) >= 3  # module + a + b
        test_row = by_path["tests/test_app.py"]
        assert test_row["category"] == "test"
        assert test_row["scope"] == "tests"
    finally:
        store.close()


def test_graph_freshness_ready_stale_unavailable(tmp_path: Path) -> None:
    store, project_id, revision, tree = _pipeline(
        tmp_path,
        {"a.py": "def f():\n    return 1\n"},
        revision="abc123",
    )
    try:
        ready = graph_freshness(store, project_id, revision, tree)
        assert ready["state"] == "ready"
        assert ready["binding_match"] is True

        stale = graph_freshness(store, project_id, revision, digest("different-tree"))
        assert stale["state"] == "stale"
        assert stale["binding_match"] is False

        stale_rev = graph_freshness(store, project_id, "other-rev", tree)
        assert stale_rev["state"] == "stale"

        # No generation -> unavailable.
        unavailable = graph_freshness(store, "ghost", revision, tree)
        assert unavailable["state"] == "unavailable"
    finally:
        store.close()


def test_graph_find_binding_mismatch_no_generation(tmp_path: Path) -> None:
    store, _project_id, _rev, _tree = _pipeline(tmp_path, {"a.py": "def f():\n    return 1\n"})
    try:
        # A different project id (not in store) than the built one -> empty, no raise.
        assert graph_find(store, "other-project", "f") == []
        assert _rev  # generator identity is non-empty
    finally:
        store.close()
