"""Polyglot code graph extraction keeps syntax and inference confidence separate."""

from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application.code_graph import GraphFileExtraction, apply_graph_build, plan_graph_build
from zekam.application.code_graph_polyglot import PolyglotStructuralExtractor
from zekam.domain.code_graph import GraphConfidence, GraphNodeKind, GraphRelation
from zekam.infrastructure.sqlite.code_graph import SQLiteCodeGraphStore

pytestmark = pytest.mark.unit


def _extract(tmp_path: Path, relative_path: str, source: str) -> GraphFileExtraction:
    root = tmp_path / "source"
    root.mkdir()
    return PolyglotStructuralExtractor().extract_file(
        root, relative_path, source.encode("utf-8")
    )


@pytest.mark.parametrize(
    ("relative_path", "source", "import_target", "local_name", "unknown_name"),
    [
        (
            "src/Example.java",
            """
            import java.util.List;
            class Base {}
            class Example extends Base {
                void helper() {}
                void run() { helper(); missing(); }
            }
            """,
            "java.util.List",
            "helper",
            "missing",
        ),
        (
            "src/example.js",
            """
            import value from './value.js';
            function helper() {}
            function run() { helper(); missing(); }
            """,
            "./value.js",
            "helper",
            "missing",
        ),
        (
            "src/example.ts",
            """
            import { value } from './value';
            export function helper() {}
            export function run() { helper(); missing(); }
            """,
            "./value",
            "helper",
            "missing",
        ),
        (
            "src/example.tsx",
            """
            import React from 'react';
            export function Component() { return renderValue(); }
            function renderValue() { return React.createElement('div'); }
            """,
            "react",
            "renderValue",
            "React.createElement",
        ),
        (
            "db/load_data.pkb",
            """
            CREATE OR REPLACE PROCEDURE save_data AS
            BEGIN NULL; END;
            /
            CREATE OR REPLACE PROCEDURE load_data AS
            BEGIN save_data(); missing_proc(); END;
            /
            """,
            "",
            "save_data",
            "missing_proc",
        ),
    ],
)
def test_polyglot_edges_separate_extracted_inferred_and_unresolved(
    tmp_path: Path,
    relative_path: str,
    source: str,
    import_target: str,
    local_name: str,
    unknown_name: str,
) -> None:
    extraction = _extract(tmp_path, relative_path, source)
    assert extraction.file.parse_state == "parsed"
    assert extraction.symbols

    imports = [
        edge
        for edge in extraction.edges
        if edge.relation is GraphRelation.IMPORTS and edge.target_qualified_name == import_target
    ]
    if import_target:
        assert imports and imports[0].confidence is GraphConfidence.EXTRACTED

    calls = [edge for edge in extraction.edges if edge.relation is GraphRelation.CALLS]
    local_calls = [edge for edge in calls if edge.target_qualified_name == local_name]
    assert local_calls and local_calls[0].confidence is GraphConfidence.INFERRED
    assert local_calls[0].target_symbol_id is not None

    unknown_calls = [edge for edge in calls if edge.target_qualified_name == unknown_name]
    assert unknown_calls and unknown_calls[0].confidence is GraphConfidence.UNRESOLVED
    assert unknown_calls[0].target_symbol_id is None


def test_polyglot_does_not_create_call_edges_from_comments_or_strings(tmp_path: Path) -> None:
    extraction = _extract(
        tmp_path,
        "src/example.ts",
        """
        // fake_call();
        function real() { const note = 'also_fake()'; }
        """,
    )
    assert not any(
        edge.target_qualified_name in {"fake_call", "also_fake"}
        for edge in extraction.edges
    )


def test_polyglot_masks_javascript_regex_literals_without_losing_braces(
    tmp_path: Path,
) -> None:
    extraction = _extract(
        tmp_path,
        "src/download.js",
        r'''
        function parseFilename(header) {
            const pattern = /filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/;
            return pattern.exec(header);
        }
        ''',
    )
    assert extraction.file.parse_state == "parsed"
    assert any(symbol.qualified_name.endswith("parseFilename") for symbol in extraction.symbols)


@pytest.mark.parametrize(
    "relative_path",
    ["src/Example.java", "src/example.js", "src/example.ts", "src/example.tsx"],
)
def test_polyglot_marks_class_members_as_methods(
    tmp_path: Path, relative_path: str
) -> None:
    extraction = _extract(
        tmp_path,
        relative_path,
        "class Example { void helper() {} void run() { helper(); } }",
    )

    members = {
        symbol.qualified_name.rsplit(".", 1)[-1]: symbol.kind
        for symbol in extraction.symbols
        if symbol.qualified_name.endswith((".helper", ".run"))
    }
    assert members == {"helper": GraphNodeKind.METHOD, "run": GraphNodeKind.METHOD}


def test_polyglot_extractor_runs_through_plan_and_sqlite_generation(tmp_path: Path) -> None:
    source_root = tmp_path / "project"
    source_root.mkdir()
    (source_root / "Service.java").write_text(
        "class Service { void run() { helper(); } void helper() {} }", encoding="utf-8"
    )
    extractor = PolyglotStructuralExtractor()
    plan = plan_graph_build(
        source_root,
        project_id="polyglot-project",
        project_slug="polyglot-project",
        source_revision="r1",
        extractor=extractor,
        created_at="2026-10-14T00:00:00Z",
    )
    store = SQLiteCodeGraphStore(tmp_path / "graph.sqlite3", create=True)
    try:
        generation = apply_graph_build(store, extractor, source_root, plan)
        assert generation.state == "ready"
        assert generation.file_count == 1
        assert generation.symbol_count >= 3
        assert generation.edge_count >= 3
    finally:
        store.close()


def test_polyglot_supports_only_declared_graph_languages() -> None:
    extractor = PolyglotStructuralExtractor()
    assert all(
        extractor.supports(path)
        for path in ("A.java", "a.js", "a.ts", "A.TSX", "x.pkb", "a.py")
    )
    assert not extractor.supports("a.rb")
