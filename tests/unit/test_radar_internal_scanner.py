"""WP-05: RadarInternalScanner produces code-graph based internal gap cards."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from zekam.application.code_graph import apply_graph_build, graph_store_path, plan_graph_build
from zekam.application.code_graph_python import PythonAstExtractor
from zekam.application.code_graph_ranking import GraphReadPort
from zekam.application.radar_internal_scanner import RadarInternalScanner
from zekam.domain.code_graph import (
    GraphConfidence,
    GraphEdge,
    GraphFile,
    GraphGeneration,
    GraphNodeKind,
    GraphSymbol,
)
from zekam.infrastructure.sqlite.code_graph import SQLiteCodeGraphStore

pytestmark = pytest.mark.unit


REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_store(
    tmp_path: Path,
    source_root: Path,
    *,
    project_id: str = "p1",
    source_revision: str = "test-rev",
) -> SQLiteCodeGraphStore:
    """Build a fresh read-only graph store for the given source root."""
    extractor = PythonAstExtractor()
    plan = plan_graph_build(
        source_root,
        project_id=project_id,
        project_slug="demo",
        source_revision=source_revision,
        extractor=extractor,
        created_at="2026-01-01T00:00:00Z",
    )
    store_path = graph_store_path(tmp_path / "home", "demo")
    store = SQLiteCodeGraphStore(store_path, create=True, read_only=False)
    try:
        apply_graph_build(store, extractor, source_root, plan)
    except BaseException:
        store.close()
        raise
    return store


def _make_synthetic_project(tmp_path: Path) -> Path:
    root = tmp_path / "srcroot"
    root.mkdir(parents=True, exist_ok=True)
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text(
        "def helper():\n    return 1\n\ndef _private_unused():\n    return 2\n",
        encoding="utf-8",
    )
    (root / "pkg" / "consumer.py").write_text(
        "from internal_module.missing import required\n\ndef use():\n    return required()\n",
        encoding="utf-8",
    )
    (root / "pkg" / "broken.py").write_text("def broken(:\n    return\n", encoding="utf-8")
    return root


def _card_paths(cards: list[Any]) -> list[str]:
    return [card.production_call_path for card in cards]


def test_scan_detects_parse_error_file(tmp_path: Path) -> None:
    source_root = _make_synthetic_project(tmp_path)
    store = _build_store(tmp_path, source_root)
    try:
        scanner = RadarInternalScanner(store)
        cards = scanner.scan_for_gaps("p1")
        paths = _card_paths(cards)
        assert any("parse_error_file" in p and "broken.py" in p for p in paths)
    finally:
        store.close()


def test_scan_detects_import_orphan(tmp_path: Path) -> None:
    source_root = _make_synthetic_project(tmp_path)
    store = _build_store(tmp_path, source_root)
    try:
        scanner = RadarInternalScanner(store)
        cards = scanner.scan_for_gaps("p1")
        paths = _card_paths(cards)
        assert any("import_orphan" in p and "internal_module.missing" in p for p in paths)
    finally:
        store.close()


def test_scan_detects_unused_private_symbol(tmp_path: Path) -> None:
    source_root = _make_synthetic_project(tmp_path)
    store = _build_store(tmp_path, source_root)
    try:
        scanner = RadarInternalScanner(store)
        cards = scanner.scan_for_gaps("p1")
        paths = _card_paths(cards)
        assert any("unused_symbol" in p and "_private_unused" in p for p in paths)
    finally:
        store.close()


def test_scan_respects_symbol_limit(tmp_path: Path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    lines = [f"def f{i:04d}():\n    return {i}" for i in range(1_001)]
    (root / "many.py").write_text("\n\n".join(lines), encoding="utf-8")
    store = _build_store(tmp_path, root)
    try:
        scanner = RadarInternalScanner(store)
        cards = scanner.scan_for_gaps("p1")
        assert any("coverage_limit_exceeded" in c.production_call_path for c in cards)
    finally:
        store.close()


def test_scan_respects_file_limit(tmp_path: Path) -> None:
    root = tmp_path / "manyfiles"
    root.mkdir()
    for i in range(101):
        (root / f"m{i:03d}.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    store = _build_store(tmp_path, root)
    try:
        scanner = RadarInternalScanner(store)
        cards = scanner.scan_for_gaps("p1")
        assert any("coverage_limit_exceeded" in c.production_call_path for c in cards)
    finally:
        store.close()


def test_scan_real_zekam_domain_finds_at_least_one_gap(tmp_path: Path) -> None:
    source_root = REPO_ROOT / "src" / "zekam" / "domain"
    assert source_root.is_dir()
    store = _build_store(tmp_path, source_root)
    try:
        scanner = RadarInternalScanner(store)
        cards = scanner.scan_for_gaps("p1")
        assert len(cards) >= 1
        for card in cards:
            assert card.kind.value == "gap"
            assert card.current_baseline == "Zekam main"
            assert card.source_binding == "p1"
            assert card.test_measurement_evidence == "code-graph-only"
    finally:
        store.close()


class _FakeGraphReadPort:
    """In-memory read port to exercise scanner logic deterministically."""

    def __init__(
        self,
        files: tuple[GraphFile, ...],
        symbols: tuple[GraphSymbol, ...],
        edges: tuple[GraphEdge, ...],
    ) -> None:
        self._files = files
        self._symbols = symbols
        self._edges = edges
        self._symbol_index = {symbol.symbol_id: symbol for symbol in symbols}

    def current_generation(self, project_id: str) -> GraphGeneration:
        return GraphGeneration(
            generation_digest="sha256:" + "0" * 64,
            project_id=project_id,
            source_revision="test-rev",
            tree_digest="sha256:" + "1" * 64,
            source_manifest_digest="sha256:" + "2" * 64,
            extractor_profile_digest="sha256:" + "3" * 64,
            file_count=len(self._files),
            symbol_count=len(self._symbols),
            edge_count=len(self._edges),
            error_count=0,
            state="ready",
            created_at="2026-01-01T00:00:00Z",
        )

    def neighbors(self, symbol_id: str) -> tuple[GraphEdge, ...]:
        return tuple(edge for edge in self._edges if edge.source_symbol_id == symbol_id)

    def file_for_symbol(self, symbol_id: str) -> str:
        symbol = self._symbol_index[symbol_id]
        return symbol.file_relative_path

    def symbols_for_file(self, relative_path: str) -> tuple[str, ...]:
        return tuple(
            symbol.symbol_id
            for symbol in self._symbols
            if symbol.file_relative_path == relative_path
        )

    def symbols(self) -> tuple[GraphSymbol, ...]:
        return self._symbols

    def files(self) -> tuple[GraphFile, ...]:
        return self._files


def test_scan_detects_duplicate_qualified_name() -> None:
    profile_digest = "sha256:" + "0" * 64
    file_a = GraphFile(
        relative_path="a.py",
        content_digest="sha256:" + "a" * 64,
        parse_state="parsed",
        error_count=0,
        extractor_profile_digest=profile_digest,
    )
    file_b = GraphFile(
        relative_path="b.py",
        content_digest="sha256:" + "b" * 64,
        parse_state="parsed",
        error_count=0,
        extractor_profile_digest=profile_digest,
    )
    sym_a = GraphSymbol(
        symbol_id="sha256:" + "0" * 64,
        qualified_name="demo.helper",
        kind=GraphNodeKind.FUNCTION,
        file_relative_path="a.py",
        parent_symbol_id=None,
        body_digest="sha256:" + "c" * 64,
        start_line=1,
        end_line=2,
        confidence=GraphConfidence.EXTRACTED,
    )
    sym_b = GraphSymbol(
        symbol_id="sha256:" + "d" * 64,
        qualified_name="demo.helper",
        kind=GraphNodeKind.FUNCTION,
        file_relative_path="b.py",
        parent_symbol_id=None,
        body_digest="sha256:" + "e" * 64,
        start_line=1,
        end_line=2,
        confidence=GraphConfidence.EXTRACTED,
    )
    port: GraphReadPort = _FakeGraphReadPort(
        files=(file_a, file_b), symbols=(sym_a, sym_b), edges=()
    )
    scanner = RadarInternalScanner(port)
    cards = scanner.scan_for_gaps("p1")
    assert any("duplicate_qualified_name" in c.production_call_path for c in cards)
