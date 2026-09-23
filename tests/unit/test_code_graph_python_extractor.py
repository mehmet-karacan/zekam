"""Python AST structural extractor tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application.code_graph import GraphFileExtraction
from zekam.application.code_graph_python import PythonAstExtractor
from zekam.domain.canonical import parse_digest
from zekam.domain.code_graph import (
    GraphConfidence,
    GraphEdge,
    GraphNodeKind,
    GraphRelation,
    GraphSymbol,
)

pytestmark = pytest.mark.unit

_SAMPLE = '''\
"""module docstring"""
import os
import json as js
from collections.abc import Mapping

CONST = 1


class Base:
    def base_method(self):
        return self.helper()


class Child(Base):
    def method(self):
        return local_function(self.value)

    async def async_method(self):
        return 1


def local_function(arg):
    def nested():
        return arg
    return nested


async def async_top():
    return local_function()
'''


def _extract(tmp_path: Path, rel: str, content: str) -> GraphFileExtraction:
    source_root = tmp_path / "srcroot"
    source_root.mkdir(parents=True, exist_ok=True)
    extractor = PythonAstExtractor()
    return extractor.extract_file(source_root, rel, content.encode("utf-8"))


def _symbols(extraction: GraphFileExtraction) -> list[GraphSymbol]:
    return list(extraction.symbols)


def _edges(extraction: GraphFileExtraction) -> list[GraphEdge]:
    return list(extraction.edges)


def test_module_class_function_method_nested_async(tmp_path: Path) -> None:
    extraction = _extract(tmp_path, "pkg/mod.py", _SAMPLE)
    symbols = _symbols(extraction)
    kinds = {symbol.kind for symbol in symbols}
    assert GraphNodeKind.MODULE in kinds
    assert GraphNodeKind.CLASS in kinds
    assert GraphNodeKind.FUNCTION in kinds
    assert GraphNodeKind.METHOD in kinds
    assert GraphNodeKind.ASYNC_FUNCTION in kinds
    names = {symbol.qualified_name for symbol in symbols}
    assert "pkg.mod.Child.method" in names
    assert "pkg.mod.Child.async_method" in names
    assert "pkg.mod.local_function.nested" in names
    assert "pkg.mod.async_top" in names
    # Each symbol id is a valid deterministic digest.
    for symbol in symbols:
        parse_digest(symbol.symbol_id)
        parse_digest(symbol.body_digest)


def test_imports_and_from_import(tmp_path: Path) -> None:
    extraction = _extract(tmp_path, "pkg/mod.py", _SAMPLE)
    imports = {
        edge.target_qualified_name
        for edge in _edges(extraction)
        if edge.relation == GraphRelation.IMPORTS
    }
    assert "os" in imports
    assert "json" in imports
    assert "collections.abc.Mapping" in imports


def test_inheritance_extends(tmp_path: Path) -> None:
    extraction = _extract(tmp_path, "pkg/mod.py", _SAMPLE)
    extends_child = [
        edge
        for edge in _edges(extraction)
        if edge.relation == GraphRelation.EXTENDS
        and edge.target_qualified_name == "Base"
    ]
    assert len(extends_child) == 1
    edge = extends_child[0]
    assert edge.confidence == GraphConfidence.INFERRED  # Base is a local class
    assert edge.target_symbol_id is not None


def test_resolvable_call_inferred(tmp_path: Path) -> None:
    content = "def g():\n    return 1\n\ndef f():\n    return g()\n"
    extraction = _extract(tmp_path, "m.py", content)
    calls = {
        (edge.source_symbol_id, edge.target_symbol_id)
        for edge in _edges(extraction)
        if edge.relation == GraphRelation.CALLS and edge.target_symbol_id is not None
    }
    assert len(calls) >= 1


def test_unresolved_attribute_call(tmp_path: Path) -> None:
    content = "def f():\n    return self.helper()\n"
    extraction = _extract(tmp_path, "m.py", content)
    unresolved = [
        edge
        for edge in _edges(extraction)
        if edge.relation == GraphRelation.CALLS
        and edge.confidence == GraphConfidence.UNRESOLVED
    ]
    assert unresolved, "attribute dispatch must be marked unresolved"
    assert unresolved[0].target_symbol_id is None


def test_local_unresolved_call(tmp_path: Path) -> None:
    content = "def f():\n    return missing_function()\n"
    extraction = _extract(tmp_path, "m.py", content)
    unresolved = [
        edge
        for edge in _edges(extraction)
        if edge.relation == GraphRelation.CALLS
        and edge.confidence == GraphConfidence.UNRESOLVED
    ]
    assert unresolved
    assert unresolved[0].target_qualified_name == "missing_function"


def test_duplicate_symbol_names(tmp_path: Path) -> None:
    content = "class C:\n    pass\n\nclass C:\n    pass\n"
    extraction = _extract(tmp_path, "m.py", content)
    c_symbols = [
        symbol for symbol in _symbols(extraction) if symbol.qualified_name == "m.C"
    ]
    assert len(c_symbols) == 2
    assert c_symbols[0].symbol_id != c_symbols[1].symbol_id


def test_same_symbol_in_different_files(tmp_path: Path) -> None:
    a = _extract(tmp_path, "a.py", "def f():\n    return 1\n")
    b = _extract(tmp_path, "b.py", "def f():\n    return 1\n")
    a_f = next(s for s in _symbols(a) if s.qualified_name == "a.f")
    b_f = next(s for s in _symbols(b) if s.qualified_name == "b.f")
    assert a_f.symbol_id != b_f.symbol_id
    assert a_f.file_relative_path != b_f.file_relative_path


def test_syntax_error_behavior(tmp_path: Path) -> None:
    content = "def broken(:\n    return\n"
    extraction = _extract(tmp_path, "m.py", content)
    assert extraction.file.parse_state == "syntax-error"
    assert extraction.file.error_count == 1
    assert extraction.symbols == ()
    assert extraction.edges == ()


def test_extractor_profile_digest_reused(tmp_path: Path) -> None:
    extractor = PythonAstExtractor()
    assert extractor.profile_digest == PythonAstExtractor().profile_digest
    parse_digest(extractor.profile_digest)
    assert extractor.supports("src/a.py") is True
    assert extractor.supports("src/a.txt") is False
