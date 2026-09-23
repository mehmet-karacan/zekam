"""Python stdlib ``ast`` structural extractor for the Code Graph Engine.

Only the standard library is used; no new runtime dependency is introduced.  The
extractor is deterministic: for identical file content and profile digest it
always produces the same symbols and edges.  Parser identity and body digests
follow the domain rules (line numbers are never identity).

Confidence policy
- module/class/function/import/inheritance clauses literally present -> EXTRACTED
- call/reference resolved to a symbol visible in the same file -> INFERRED
- name that points outside the file (dotted import, builtin base) -> EXTERNAL
- local-unresolvable call / cross-module attribute dispatch -> UNRESOLVED
  (compiler-grade cross-module dispatch is intentionally out of V1 scope).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

from zekam.application.code_graph import MAX_GRAPH_SOURCE_BYTES, GraphFileExtraction
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
from zekam.domain.errors import ValidationFailed

EXTRACTOR_PROFILE_SCHEMA: Final = "zekam-python-ast-extractor/v1"
EXTRACTOR_PROFILE_VERSION: Final = 1
_EXTRACTOR_PROFILE_DIGEST = digest(
    {"schema": EXTRACTOR_PROFILE_SCHEMA, "version": EXTRACTOR_PROFILE_VERSION}
)


class PythonAstExtractor:
    """Deterministic Python structural extractor using the stdlib parser."""

    @property
    def profile_digest(self) -> str:
        return _EXTRACTOR_PROFILE_DIGEST

    def supports(self, relative_path: str) -> bool:
        return relative_path.endswith(".py")

    def extract_file(
        self, source_root: Path, relative_path: str, content: bytes
    ) -> GraphFileExtraction:
        del source_root  # structural extraction needs only the single file content
        if len(content) > MAX_GRAPH_SOURCE_BYTES:
            raise ValidationFailed("Graph Python kaynak dosya boyut sinirini asiyor")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationFailed("Graph Python kaynak strict UTF-8 olmali") from exc
        content_digest = digest_of_bytes(content)
        try:
            tree = ast.parse(text, filename=relative_path)
        except SyntaxError:
            return GraphFileExtraction(
                file=GraphFile(
                    relative_path=relative_path,
                    content_digest=content_digest,
                    parse_state="syntax-error",
                    error_count=1,
                    extractor_profile_digest=self.profile_digest,
                ),
                symbols=(),
                edges=(),
            )
        return _build(relative_path, text, tree, content_digest, self.profile_digest)


def _module_qualified_name(relative_path: str) -> str:
    module = relative_path[:-3] if relative_path.endswith(".py") else relative_path
    module = module.replace("/", ".").replace("\\", ".")
    return module.strip(".") or "module"


def _build(
    relative_path: str,
    text: str,
    tree: ast.Module,
    content_digest: str,
    profile_digest: str,
) -> GraphFileExtraction:
    module_qname = _module_qualified_name(relative_path)
    file_symbol = GraphSymbol(
        symbol_id=symbol_identity(
            kind=GraphNodeKind.MODULE,
            qualified_name=module_qname,
            file_relative_path=relative_path,
            disambiguator=0,
        ),
        qualified_name=module_qname,
        kind=GraphNodeKind.MODULE,
        file_relative_path=relative_path,
        parent_symbol_id=None,
        body_digest=body_digest(text),
        start_line=1,
        end_line=max(1, len(text.splitlines())),
        confidence=GraphConfidence.EXTRACTED,
    )

    symbols: list[GraphSymbol] = [file_symbol]
    edges: list[GraphEdge] = []
    qname_count: dict[str, int] = {}
    # simple-name -> symbol_id for local resolution (last definition wins).
    local_names: dict[str, str] = {}

    def create_symbol(
        qname: str, kind: GraphNodeKind, node: ast.AST, parent_symbol_id: str | None
    ) -> GraphSymbol:
        count = qname_count.get(qname, 0)
        qname_count[qname] = count + 1
        segment = ast.get_source_segment(text, node)
        source_segment = segment if segment is not None else ""
        return GraphSymbol(
            symbol_id=symbol_identity(
                kind=kind,
                qualified_name=qname,
                file_relative_path=relative_path,
                disambiguator=count,
            ),
            qualified_name=qname,
            kind=kind,
            file_relative_path=relative_path,
            parent_symbol_id=parent_symbol_id,
            body_digest=body_digest(source_segment),
            start_line=getattr(node, "lineno", 1),
            end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
            confidence=GraphConfidence.EXTRACTED,
        )

    def add_edge(
        *,
        source_symbol_id: str,
        relation: GraphRelation,
        target_symbol_id: str | None,
        target_qualified_name: str,
        confidence: GraphConfidence,
        provenance: str = "python-ast",
    ) -> None:
        edge = GraphEdge(
            edge_id=edge_identity(
                source_symbol_id=source_symbol_id,
                relation=relation,
                target_symbol_id=target_symbol_id,
                target_qualified_name=target_qualified_name,
                confidence=confidence,
            ),
            source_symbol_id=source_symbol_id,
            relation=relation,
            target_symbol_id=target_symbol_id,
            target_qualified_name=target_qualified_name,
            confidence=confidence,
            provenance=provenance,
        )
        if all(existing.edge_id != edge.edge_id for existing in edges):
            edges.append(edge)

    def know_local(symbol: GraphSymbol) -> None:
        local_names[symbol.qualified_name.rsplit(".", 1)[-1]] = symbol.symbol_id

    def resolve_local(name: str) -> str | None:
        return local_names.get(name)

    def reference_edges(
        owner_symbol_id: str, node: ast.AST, confidence: GraphConfidence
    ) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                target = resolve_local(child.id)
                if target is not None and target != owner_symbol_id:
                    add_edge(
                        source_symbol_id=owner_symbol_id,
                        relation=GraphRelation.REFERENCES,
                        target_symbol_id=target,
                        target_qualified_name=child.id,
                        confidence=GraphConfidence.INFERRED,
                    )
            elif isinstance(child, ast.Attribute):
                add_edge(
                    source_symbol_id=owner_symbol_id,
                    relation=GraphRelation.REFERENCES,
                    target_symbol_id=None,
                    target_qualified_name=_attribute_dotted(child),
                    confidence=GraphConfidence.UNRESOLVED,
                )

    def call_edges(owner_symbol_id: str, node: ast.AST) -> None:
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if isinstance(func, ast.Name):
                target = resolve_local(func.id)
                if target is not None and target != owner_symbol_id:
                    add_edge(
                        source_symbol_id=owner_symbol_id,
                        relation=GraphRelation.CALLS,
                        target_symbol_id=target,
                        target_qualified_name=func.id,
                        confidence=GraphConfidence.INFERRED,
                    )
                else:
                    add_edge(
                        source_symbol_id=owner_symbol_id,
                        relation=GraphRelation.CALLS,
                        target_symbol_id=None,
                        target_qualified_name=func.id,
                        confidence=GraphConfidence.UNRESOLVED,
                    )
            elif isinstance(func, ast.Attribute):
                add_edge(
                    source_symbol_id=owner_symbol_id,
                    relation=GraphRelation.CALLS,
                    target_symbol_id=None,
                    target_qualified_name=_attribute_dotted(func),
                    confidence=GraphConfidence.UNRESOLVED,
                )

    def class_extends(class_symbol: GraphSymbol, node: ast.ClassDef) -> None:
        for base in node.bases:
            if isinstance(base, ast.Name):
                target = resolve_local(base.id)
                confidence = (
                    GraphConfidence.INFERRED if target is not None else GraphConfidence.EXTERNAL
                )
                add_edge(
                    source_symbol_id=class_symbol.symbol_id,
                    relation=GraphRelation.EXTENDS,
                    target_symbol_id=target,
                    target_qualified_name=base.id,
                    confidence=confidence,
                )
            elif isinstance(base, ast.Attribute):
                add_edge(
                    source_symbol_id=class_symbol.symbol_id,
                    relation=GraphRelation.EXTENDS,
                    target_symbol_id=None,
                    target_qualified_name=_attribute_dotted(base),
                    confidence=GraphConfidence.EXTERNAL,
                )

    def process_class(
        class_symbol: GraphSymbol, node: ast.ClassDef, qname: str
    ) -> None:
        know_local(class_symbol)
        class_extends(class_symbol, node)
        reference_edges(class_symbol.symbol_id, node, GraphConfidence.INFERRED)
        for stmt in node.body:
            if isinstance(stmt, ast.ClassDef):
                child_qname = f"{qname}.{stmt.name}"
                child = create_symbol(
                    child_qname, GraphNodeKind.CLASS, stmt, class_symbol.symbol_id
                )
                symbols.append(child)
                add_edge(
                    source_symbol_id=class_symbol.symbol_id,
                    relation=GraphRelation.CONTAINS,
                    target_symbol_id=child.symbol_id,
                    target_qualified_name=child_qname,
                    confidence=GraphConfidence.EXTRACTED,
                )
                process_class(child, stmt, child_qname)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child = process_function(stmt, qname, class_symbol.symbol_id, inside_class=True)
                add_edge(
                    source_symbol_id=class_symbol.symbol_id,
                    relation=GraphRelation.CONTAINS,
                    target_symbol_id=child.symbol_id,
                    target_qualified_name=child.qualified_name,
                    confidence=GraphConfidence.EXTRACTED,
                )

    def process_function(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        qname: str,
        parent_symbol_id: str | None,
        *,
        inside_class: bool,
    ) -> GraphSymbol:
        is_async = isinstance(node, ast.AsyncFunctionDef)
        kind = (
            GraphNodeKind.ASYNC_FUNCTION
            if is_async
            else (GraphNodeKind.METHOD if inside_class else GraphNodeKind.FUNCTION)
        )
        child_qname = f"{qname}.{node.name}"
        symbol = create_symbol(child_qname, kind, node, parent_symbol_id)
        symbols.append(symbol)
        know_local(symbol)
        for stmt in node.body:
            if isinstance(stmt, ast.ClassDef):
                nested_qname = f"{child_qname}.{stmt.name}"
                nested = create_symbol(
                    nested_qname, GraphNodeKind.CLASS, stmt, symbol.symbol_id
                )
                symbols.append(nested)
                add_edge(
                    source_symbol_id=symbol.symbol_id,
                    relation=GraphRelation.CONTAINS,
                    target_symbol_id=nested.symbol_id,
                    target_qualified_name=nested_qname,
                    confidence=GraphConfidence.EXTRACTED,
                )
                process_class(nested, stmt, nested_qname)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nested = process_function(stmt, child_qname, symbol.symbol_id, inside_class=False)
                add_edge(
                    source_symbol_id=symbol.symbol_id,
                    relation=GraphRelation.CONTAINS,
                    target_symbol_id=nested.symbol_id,
                    target_qualified_name=nested.qualified_name,
                    confidence=GraphConfidence.EXTRACTED,
                )
        call_edges(symbol.symbol_id, node)
        reference_edges(symbol.symbol_id, node, GraphConfidence.INFERRED)
        return symbol

    # Top-level scope
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            qname = f"{module_qname}.{stmt.name}"
            symbol = create_symbol(qname, GraphNodeKind.CLASS, stmt, file_symbol.symbol_id)
            symbols.append(symbol)
            add_edge(
                source_symbol_id=file_symbol.symbol_id,
                relation=GraphRelation.CONTAINS,
                target_symbol_id=symbol.symbol_id,
                target_qualified_name=qname,
                confidence=GraphConfidence.EXTRACTED,
            )
            process_class(symbol, stmt, qname)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbol = process_function(
                stmt, module_qname, file_symbol.symbol_id, inside_class=False
            )
            add_edge(
                source_symbol_id=file_symbol.symbol_id,
                relation=GraphRelation.CONTAINS,
                target_symbol_id=symbol.symbol_id,
                target_qualified_name=symbol.qualified_name,
                confidence=GraphConfidence.EXTRACTED,
            )

    # Whole-file imports (module scope).
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add_edge(
                    source_symbol_id=file_symbol.symbol_id,
                    relation=GraphRelation.IMPORTS,
                    target_symbol_id=None,
                    target_qualified_name=alias.name or "",
                    confidence=GraphConfidence.EXTRACTED,
                )
        elif isinstance(node, ast.ImportFrom):
            root_module = node.module or ""
            for alias in node.names:
                full = f"{root_module}.{alias.name}" if root_module else alias.name
                add_edge(
                    source_symbol_id=file_symbol.symbol_id,
                    relation=GraphRelation.IMPORTS,
                    target_symbol_id=None,
                    target_qualified_name=full,
                    confidence=GraphConfidence.EXTRACTED,
                )

    graph_file = GraphFile(
        relative_path=relative_path,
        content_digest=content_digest,
        parse_state="parsed",
        error_count=0,
        extractor_profile_digest=profile_digest,
    )
    return GraphFileExtraction(file=graph_file, symbols=tuple(symbols), edges=tuple(edges))


def _attribute_dotted(node: ast.Attribute) -> str:
    parts: list[str] = [node.attr]
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        current = current.value
        if isinstance(current, ast.Attribute):
            parts.append(current.attr)
        elif isinstance(current, ast.Name):
            parts.append(current.id)
        else:
            break
    return ".".join(reversed(parts))
