"""Code graph domain contract, identity and validation tests."""

from __future__ import annotations

import pytest

from zekam.domain.canonical import digest, digest_of_bytes, parse_digest
from zekam.domain.code_graph import (
    ALL_RELATIONS,
    DEPENDENCY_RELATIONS,
    GraphConfidence,
    GraphEdge,
    GraphFile,
    GraphGeneration,
    GraphNodeKind,
    GraphRelation,
    GraphSymbol,
    body_digest,
    edge_identity,
    symbol_identity,
)
from zekam.domain.errors import ValidationFailed

pytestmark = pytest.mark.unit

_PROFILE = digest("test-extractor-profile")


def _file(
    path: str = "src/a.py",
    *,
    parse_state: str = "parsed",
    error_count: int = 0,
) -> GraphFile:
    return GraphFile(
        relative_path=path,
        content_digest=digest_of_bytes(path.encode("utf-8")),
        parse_state=parse_state,
        error_count=error_count,
        extractor_profile_digest=_PROFILE,
    )


def _symbol(
    qname: str = "src.a.f",
    *,
    kind: GraphNodeKind = GraphNodeKind.FUNCTION,
    file: str = "src/a.py",
    parent: str | None = None,
) -> GraphSymbol:
    return GraphSymbol(
        symbol_id=symbol_identity(
            kind=kind,
            qualified_name=qname,
            file_relative_path=file,
            disambiguator=0,
        ),
        qualified_name=qname,
        kind=kind,
        file_relative_path=file,
        parent_symbol_id=parent,
        body_digest=body_digest(qname),
        start_line=1,
        end_line=2,
        confidence=GraphConfidence.EXTRACTED,
    )


def _edge(source: GraphSymbol, *, target: GraphSymbol | None, relation: GraphRelation) -> GraphEdge:
    return GraphEdge(
        edge_id=edge_identity(
            source_symbol_id=source.symbol_id,
            relation=relation,
            target_symbol_id=target.symbol_id if target is not None else None,
            target_qualified_name=target.qualified_name if target is not None else "external.T",
            confidence=GraphConfidence.INFERRED if target is not None else GraphConfidence.EXTERNAL,
        ),
        source_symbol_id=source.symbol_id,
        relation=relation,
        target_symbol_id=target.symbol_id if target is not None else None,
        target_qualified_name=target.qualified_name if target is not None else "external.T",
        confidence=GraphConfidence.INFERRED if target is not None else GraphConfidence.EXTERNAL,
        provenance="python-ast",
    )


def test_relation_allowlist_excludes_contains() -> None:
    assert set(GraphRelation) == {
        GraphRelation.CONTAINS,
        GraphRelation.IMPORTS,
        GraphRelation.CALLS,
        GraphRelation.REFERENCES,
        GraphRelation.EXTENDS,
    }
    assert {
        GraphRelation.IMPORTS,
        GraphRelation.CALLS,
        GraphRelation.REFERENCES,
        GraphRelation.EXTENDS,
    } == DEPENDENCY_RELATIONS
    assert GraphRelation.CONTAINS not in DEPENDENCY_RELATIONS
    assert set(GraphRelation) == ALL_RELATIONS


def test_deterministic_symbol_id() -> None:
    left = symbol_identity(
        kind=GraphNodeKind.FUNCTION,
        qualified_name="src.a.f",
        file_relative_path="src/a.py",
        disambiguator=0,
    )
    right = symbol_identity(
        kind=GraphNodeKind.FUNCTION,
        qualified_name="src.a.f",
        file_relative_path="src/a.py",
        disambiguator=0,
    )
    assert left == right
    parse_digest(left)
    # Same qualified name in a different file is a different identity.
    other_file = symbol_identity(
        kind=GraphNodeKind.FUNCTION,
        qualified_name="src.a.f",
        file_relative_path="src/b.py",
        disambiguator=0,
    )
    assert other_file != left


def test_duplicate_qualified_name_disambiguated() -> None:
    first = symbol_identity(
        kind=GraphNodeKind.CLASS,
        qualified_name="m.C",
        file_relative_path="m.py",
        disambiguator=0,
    )
    second = symbol_identity(
        kind=GraphNodeKind.CLASS,
        qualified_name="m.C",
        file_relative_path="m.py",
        disambiguator=1,
    )
    assert first != second


def test_body_digest_stability_across_line_moves() -> None:
    a = body_digest("def f():\n    return 1\n")
    b = body_digest("def f():\n    return 1\n")
    assert a == b
    changed = body_digest("def f():\n    return 2\n")
    assert changed != a


def test_graph_file_validation() -> None:
    _file()
    with pytest.raises(ValidationFailed):
        _file(parse_state="corrupt")
    with pytest.raises(ValidationFailed):
        _file(error_count=-1)
    with pytest.raises(ValidationFailed):
        _file(path="../escape.py")
    with pytest.raises(ValidationFailed):
        _file(path="/absolute/posix/path.py")


def test_graph_symbol_line_numbers_are_not_identity() -> None:
    symbol = _symbol()
    assert symbol.symbol_id == symbol_identity(
        kind=GraphNodeKind.FUNCTION,
        qualified_name="src.a.f",
        file_relative_path="src/a.py",
        disambiguator=0,
    )


def test_symbol_validation_rejects_bad_range() -> None:
    with pytest.raises(ValidationFailed):
        GraphSymbol(
            symbol_id=_symbol().symbol_id,
            qualified_name="x",
            kind=GraphNodeKind.FUNCTION,
            file_relative_path="src/a.py",
            parent_symbol_id=None,
            body_digest=body_digest("x"),
            start_line=5,
            end_line=2,
            confidence=GraphConfidence.EXTRACTED,
        )


def test_edge_self_loop_rejected() -> None:
    symbol = _symbol()
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


def test_edge_deterministic_and_target_required() -> None:
    source = _symbol()
    target = _symbol(qname="src.a.g")
    left = _edge(source, target=target, relation=GraphRelation.CALLS)
    right = _edge(source, target=target, relation=GraphRelation.CALLS)
    assert left.edge_id == right.edge_id
    with pytest.raises(ValidationFailed):
        GraphEdge(
            edge_id=edge_identity(
                source_symbol_id=source.symbol_id,
                relation=GraphRelation.CALLS,
                target_symbol_id=target.symbol_id,
                target_qualified_name=target.qualified_name,
                confidence=GraphConfidence.INFERRED,
            ),
            source_symbol_id=source.symbol_id,
            relation=GraphRelation.CALLS,
            target_symbol_id=target.symbol_id,
            target_qualified_name=target.qualified_name,
            confidence=GraphConfidence.INFERRED,
            provenance="",
        )


def test_edge_unknown_relation_rejected() -> None:
    source = _symbol()
    target = _symbol(qname="src.a.g")
    with pytest.raises(ValidationFailed):
        GraphEdge(
            edge_id=edge_identity(
                source_symbol_id=source.symbol_id,
                relation=GraphRelation.CALLS,
                target_symbol_id=target.symbol_id,
                target_qualified_name=target.qualified_name,
                confidence=GraphConfidence.INFERRED,
            ),
            source_symbol_id=source.symbol_id,
            relation="totally-missing",  # type: ignore[arg-type]
            target_symbol_id=target.symbol_id,
            target_qualified_name=target.qualified_name,
            confidence=GraphConfidence.INFERRED,
            provenance="python-ast",
        )


def test_generation_validation() -> None:
    GraphGeneration(
        generation_digest=digest("g"),
        project_id="p",
        source_revision="r1",
        tree_digest=digest("tree"),
        source_manifest_digest=digest("manifest"),
        extractor_profile_digest=_PROFILE,
        file_count=1,
        symbol_count=0,
        edge_count=0,
        error_count=0,
        state="ready",
        created_at="2026-09-21T00:00:00Z",
    )
    with pytest.raises(ValidationFailed):
        GraphGeneration(
            generation_digest=digest("g"),
            project_id="p",
            source_revision="r1",
            tree_digest=digest("tree"),
            source_manifest_digest=digest("manifest"),
            extractor_profile_digest=_PROFILE,
            file_count=1,
            symbol_count=0,
            edge_count=0,
            error_count=0,
            state="on-fire",
            created_at="2026-09-21T00:00:00Z",
        )
    with pytest.raises(ValidationFailed):
        GraphGeneration(
            generation_digest="not-a-digest",
            project_id="p",
            source_revision="r1",
            tree_digest=digest("tree"),
            source_manifest_digest=digest("manifest"),
            extractor_profile_digest=_PROFILE,
            file_count=1,
            symbol_count=0,
            edge_count=0,
            error_count=0,
            state="ready",
            created_at="2026-09-21T00:00:00Z",
        )
