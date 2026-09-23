"""Code graph negative / adversarial tests (corrupt, symlink, cycles, stale)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zekam.application.code_graph import (
    GraphFileExtraction,
    apply_graph_build,
    plan_graph_build,
)
from zekam.application.code_graph_python import PythonAstExtractor
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.code_graph import (
    GraphConfidence,
    GraphEdge,
    GraphFile,
    GraphNodeKind,
    GraphRelation,
    GraphSymbol,
    body_digest,
    edge_identity,
    symbol_identity,
)
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.code_graph import SQLiteCodeGraphStore

pytestmark = pytest.mark.unit

_PROFILE = digest("neg-profile")
_CREATED_AT = "2026-09-21T00:00:00Z"


def _symbol(qname: str, file: str = "a.py") -> GraphSymbol:
    return GraphSymbol(
        symbol_id=symbol_identity(
            kind=GraphNodeKind.FUNCTION, qualified_name=qname, file_relative_path=file,
            disambiguator=0,
        ),
        qualified_name=qname,
        kind=GraphNodeKind.FUNCTION,
        file_relative_path=file,
        parent_symbol_id=None,
        body_digest=body_digest(qname),
        start_line=1,
        end_line=2,
        confidence=GraphConfidence.EXTRACTED,
    )


def _file(path: str = "a.py") -> GraphFile:
    return GraphFile(
        relative_path=path,
        content_digest=digest_of_bytes(b"x"),
        parse_state="parsed",
        error_count=0,
        extractor_profile_digest=_PROFILE,
    )


def test_corrupt_graph_opening(tmp_path: Path) -> None:
    path = tmp_path / "g.sqlite3"
    path.write_bytes(b"garbage-not-a-db")
    with pytest.raises(ConfigurationError):
        SQLiteCodeGraphStore(path)


def test_symlink_path_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.sqlite3"
    real.write_bytes(b"x")
    link = tmp_path / "link.sqlite3"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(ConfigurationError):
        SQLiteCodeGraphStore(link, create=True)


def test_relative_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        SQLiteCodeGraphStore(Path("relative/g.sqlite3"), create=True)


def test_duplicate_symbol_in_extraction_rejected() -> None:
    symbol = _symbol("a.f")
    source = _file()
    with pytest.raises(ValidationFailed):
        GraphFileExtraction(file=source, symbols=(symbol, symbol), edges=())


def test_extraction_edge_with_unknown_source_rejected() -> None:
    symbol = _symbol("a.f")
    other = _symbol("a.g")
    edge = GraphEdge(
        edge_id=edge_identity(
            source_symbol_id=symbol.symbol_id,
            relation=GraphRelation.REFERENCES,
            target_symbol_id=other.symbol_id,
            target_qualified_name=other.qualified_name,
            confidence=GraphConfidence.INFERRED,
        ),
        source_symbol_id=symbol.symbol_id,
        relation=GraphRelation.REFERENCES,
        target_symbol_id=other.symbol_id,
        target_qualified_name=other.qualified_name,
        confidence=GraphConfidence.INFERRED,
        provenance="python-ast",
    )
    with pytest.raises(ValidationFailed):
        GraphFileExtraction(file=_file(), symbols=(other,), edges=(edge,))


def test_self_loop_edge_rejected() -> None:
    symbol = _symbol("a.f")
    with pytest.raises(ValidationFailed):
        GraphEdge(
            edge_id=edge_identity(
                source_symbol_id=symbol.symbol_id,
                relation=GraphRelation.REFERENCES,
                target_symbol_id=symbol.symbol_id,
                target_qualified_name=symbol.qualified_name,
                confidence=GraphConfidence.INFERRED,
            ),
            source_symbol_id=symbol.symbol_id,
            relation=GraphRelation.REFERENCES,
            target_symbol_id=symbol.symbol_id,
            target_qualified_name=symbol.qualified_name,
            confidence=GraphConfidence.INFERRED,
            provenance="python-ast",
        )


def test_two_node_cycle_allowed_but_self_loop_not() -> None:
    """A legitimate 2-cycle (A->B, B->A) is representable; only self-loops are banned."""
    a = _symbol("a.f")
    b = _symbol("a.g")
    ab = GraphEdge(
        edge_id=edge_identity(
            source_symbol_id=a.symbol_id,
            relation=GraphRelation.CALLS,
            target_symbol_id=b.symbol_id,
            target_qualified_name=b.qualified_name,
            confidence=GraphConfidence.INFERRED,
        ),
        source_symbol_id=a.symbol_id,
        relation=GraphRelation.CALLS,
        target_symbol_id=b.symbol_id,
        target_qualified_name=b.qualified_name,
        confidence=GraphConfidence.INFERRED,
        provenance="python-ast",
    )
    ba = GraphEdge(
        edge_id=edge_identity(
            source_symbol_id=b.symbol_id,
            relation=GraphRelation.CALLS,
            target_symbol_id=a.symbol_id,
            target_qualified_name=a.qualified_name,
            confidence=GraphConfidence.INFERRED,
        ),
        source_symbol_id=b.symbol_id,
        relation=GraphRelation.CALLS,
        target_symbol_id=a.symbol_id,
        target_qualified_name=a.qualified_name,
        confidence=GraphConfidence.INFERRED,
        provenance="python-ast",
    )
    # Representable (no rejection); self-loop still rejected separately above.
    assert ab.source_symbol_id != ab.target_symbol_id
    assert ba.source_symbol_id != ba.target_symbol_id


def test_unresolved_call_confidence(tmp_path: Path) -> None:
    source_root = tmp_path / "srcroot"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "a.py").write_text("def f():\n    return obj.call()\n", encoding="utf-8")
    extractor = PythonAstExtractor()
    extraction = extractor.extract_file(source_root, "a.py", b"def f():\n    return obj.call()\n")
    unresolved = [
        e
        for e in extraction.edges
        if e.relation == GraphRelation.CALLS and e.confidence == GraphConfidence.UNRESOLVED
    ]
    assert unresolved
    assert unresolved[0].target_symbol_id is None


def test_concurrent_writer_rejected(tmp_path: Path) -> None:
    from zekam.domain.errors import ConcurrencyConflict

    source_root = tmp_path / "srcroot"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    store = SQLiteCodeGraphStore(tmp_path / "g.sqlite3", create=True)
    try:
        with store._single_writer(), pytest.raises(ConcurrencyConflict):
            SQLiteCodeGraphStore(tmp_path / "g.sqlite3")
    finally:
        store.close()


def test_stale_plan_digest_on_change(tmp_path: Path) -> None:
    source_root = tmp_path / "srcroot"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    extractor = PythonAstExtractor()
    plan_a = plan_graph_build(
        source_root,
        project_id="p",
        project_slug="p",
        source_revision="r1",
        extractor=extractor,
        created_at=_CREATED_AT,
    )
    (source_root / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    plan_b = plan_graph_build(
        source_root,
        project_id="p",
        project_slug="p",
        source_revision="r1",
        extractor=extractor,
        created_at=_CREATED_AT,
    )
    # A build activated with plan_a's digest after the source changed is stale.
    store = SQLiteCodeGraphStore(tmp_path / "g.sqlite3", create=True)
    try:
        apply_graph_build(store, extractor, source_root, plan_b)
        assert plan_a.plan_digest != plan_b.plan_digest
        # plan_a is now stale: rebuilding it would drift against disk content.
        with pytest.raises(ValidationFailed):
            apply_graph_build(store, extractor, source_root, plan_a)
    finally:
        store.close()


def test_wrong_project_graph_rejected(tmp_path: Path) -> None:
    source_root = tmp_path / "srcroot"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    extractor = PythonAstExtractor()
    store = SQLiteCodeGraphStore(tmp_path / "g.sqlite3", create=True)
    try:
        plan = plan_graph_build(
            source_root, project_id="alpha", project_slug="alpha", source_revision="r1",
            extractor=extractor, created_at=_CREATED_AT,
        )
        apply_graph_build(store, extractor, source_root, plan)
        with pytest.raises(ValidationFailed):
            store.generation("beta")
    finally:
        store.close()


def test_read_only_mutation_rejected(tmp_path: Path) -> None:
    source_root = tmp_path / "srcroot"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    extractor = PythonAstExtractor()
    store = SQLiteCodeGraphStore(tmp_path / "g.sqlite3", create=True)
    plan = plan_graph_build(
        source_root, project_id="p", project_slug="p", source_revision="r1",
        extractor=extractor, created_at=_CREATED_AT,
    )
    apply_graph_build(store, extractor, source_root, plan)
    store.close()
    read_only = SQLiteCodeGraphStore(tmp_path / "g.sqlite3", read_only=True)
    try:
        with pytest.raises(PolicyViolation):
            read_only.build_generation(
                project_id="p",
                source_revision="r1",
                tree_digest=digest("t"),
                source_manifest_digest=digest("m"),
                extractor_profile_digest=extractor.profile_digest,
                files=(),
                symbols=(),
                edges=(),
                created_at=_CREATED_AT,
            )
    finally:
        read_only.close()
