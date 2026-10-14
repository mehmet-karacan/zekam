"""Bounded structural graph extraction for JavaScript-family and PL/SQL files.

The extractor intentionally has a small confidence surface.  Imports, symbol
declarations and containment are ``extracted`` from syntax.  A call is
``inferred`` only when an unqualified callee resolves uniquely to a symbol in
the same file; qualified or unknown calls remain ``unresolved``.  This keeps a
name or body resemblance from becoming a false call chain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from zekam.application.code_graph import MAX_GRAPH_SOURCE_BYTES, GraphFileExtraction
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
from zekam.domain.errors import ValidationFailed

EXTRACTOR_PROFILE_SCHEMA: Final = "zekam-polyglot-structural-extractor/v1"
EXTRACTOR_PROFILE_VERSION: Final = 1
_SUPPORTED_SUFFIXES = frozenset(
    {".py", ".java", ".js", ".jsx", ".ts", ".tsx", ".sql", ".pks", ".pkb", ".plsql"}
)
_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$#]*"
_CONTROL_WORDS = frozenset(
    {"if", "for", "while", "switch", "catch", "with", "return", "throw", "new", "function"}
)
_CLASS_RE = re.compile(
    rf"(?m)^[ \t]*(?:export\s+|public\s+|private\s+|protected\s+|abstract\s+|final\s+|"
    rf"sealed\s+|non-sealed\s+)*"
    rf"(?:class|interface|enum|record)\s+(?P<name>{_IDENTIFIER})"
    rf"(?:\s+extends\s+(?P<base>{_IDENTIFIER}))?[^{{\n]*{{"
)
_FUNCTION_RE = re.compile(
    rf"(?<![\w$])(?:(?:export|default|public|private|protected|static|final|async|override|abstract|synchronized)\s+)*"
    rf"(?:function\s+|(?:[A-Za-z_$][A-Za-z0-9_$<>\[\],.?]*\s+))?"
    rf"(?P<name>{_IDENTIFIER})\s*\([^;\n]*?\)\s*(?:throws\s+[^{{\n]+)?{{"
)
_ARROW_RE = re.compile(
    rf"(?m)^[ \t]*(?:export\s+)?(?:const|let|var)\s+"
    rf"(?P<name>{_IDENTIFIER})\s*=\s*(?:async\s*)?\([^\n]*\)\s*=>\s*{{"
)
_PLSQL_RE = re.compile(
    rf"(?im)^\s*create\s+(?:or\s+replace\s+)?(?:editionable\s+|noneditionable\s+)?"
    rf"(?P<kind>package(?:\s+body)?|procedure|function)\s+(?P<name>{_IDENTIFIER})"
)
_PLSQL_BLOCK_TOKEN = re.compile(
    r"(?im)\bBEGIN\b|\bIF\b|\bLOOP\b|\bCASE\b|"
    r"\bEND(?:\s+[A-Za-z_$][A-Za-z0-9_$#]*)?\s*;"
)


@dataclass(frozen=True, slots=True)
class _Declaration:
    name: str
    kind: GraphNodeKind
    start: int
    end: int
    body_start: int
    body_end: int
    base: str | None = None


def _mask_non_code(text: str) -> str:
    """Mask comments/literals while preserving offsets and newlines.

    JavaScript and TypeScript regular-expression literals can contain quote
    characters (for example ``['"]``).  Treating those quotes as the start of
    a string makes the later brace balance check report a valid file as a
    syntax error, so regex literals get masked as one lexical unit too.
    """

    def regex_start(position: int) -> bool:
        previous = position - 1
        while previous >= 0 and text[previous].isspace():
            previous -= 1
        if previous < 0 or text[previous] in "=([{,:;!&|?":
            return True
        word_match = re.search(r"[A-Za-z_$][A-Za-z0-9_$]*$", text[: previous + 1])
        return bool(
            word_match
            and word_match.group(0)
            in {
                "return",
                "case",
                "throw",
                "else",
                "do",
                "yield",
                "await",
                "typeof",
                "void",
                "delete",
            }
        )

    def regex_end(position: int) -> int | None:
        cursor = position + 1
        in_class = False
        while cursor < length:
            current_regex = text[cursor]
            if current_regex in {"\n", "\r"}:
                return None
            if current_regex == "\\":
                cursor += 2
                continue
            if current_regex == "[":
                in_class = True
            elif current_regex == "]":
                in_class = False
            elif current_regex == "/" and not in_class:
                cursor += 1
                while cursor < length and (text[cursor].isalpha() or text[cursor].isdigit()):
                    cursor += 1
                return cursor
            cursor += 1
        return None

    chars = list(text)
    index = 0
    length = len(chars)
    while index < length:
        current = chars[index]
        next_char = chars[index + 1] if index + 1 < length else ""
        if current == "/" and next_char in {"/", "*"}:
            if next_char == "/":
                end = text.find("\n", index)
                end = length if end < 0 else end
            else:
                end = text.find("*/", index + 2)
                end = length if end < 0 else end + 2
            for position in range(index, end):
                if chars[position] != "\n":
                    chars[position] = " "
            index = end
            continue
        if current == "-" and next_char == "-":
            end = text.find("\n", index)
            end = length if end < 0 else end
            for position in range(index, end):
                chars[position] = " "
            index = end
            continue
        if current == "/" and regex_start(index):
            end = regex_end(index)
            if end is not None:
                for position in range(index, end):
                    if chars[position] != "\n":
                        chars[position] = " "
                index = end
                continue
        if current in {"'", '"', "`"}:
            quote = current
            end = index + 1
            while end < length:
                if chars[end] == "\\":
                    end += 2
                    continue
                if chars[end] == quote:
                    end += 1
                    break
                end += 1
            for position in range(index, min(end, length)):
                if chars[position] != "\n":
                    chars[position] = " "
            index = end
            continue
        index += 1
    return "".join(chars)


def _line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _body_end(masked: str, opening: int) -> int:
    depth = 1
    cursor = opening + 1
    while cursor < len(masked) and depth:
        if masked[cursor] == "{":
            depth += 1
        elif masked[cursor] == "}":
            depth -= 1
        cursor += 1
    return cursor if depth == 0 else -1


def _plsql_end(masked: str, start: int) -> int:
    depth = 0
    for match in _PLSQL_BLOCK_TOKEN.finditer(masked, start):
        token = match.group(0).lstrip().upper()
        if token.startswith("END"):
            if depth == 0:
                return match.end()
            depth -= 1
        else:
            depth += 1
    return len(masked)


def _language(relative_path: str) -> str:
    suffix = Path(relative_path).suffix.casefold()
    return "plsql" if suffix in {".sql", ".pks", ".pkb", ".plsql"} else suffix.removeprefix(".")


class PolyglotStructuralExtractor:
    """Small, deterministic extractor for the WP-06 polyglot graph surface."""

    def __init__(self) -> None:
        self._python = PythonAstExtractor()
        self._profile_digest = digest(
            {
                "schema": EXTRACTOR_PROFILE_SCHEMA,
                "version": EXTRACTOR_PROFILE_VERSION,
                "python_profile": self._python.profile_digest,
            }
        )

    @property
    def profile_digest(self) -> str:
        return self._profile_digest

    def supports(self, relative_path: str) -> bool:
        return Path(relative_path).suffix.casefold() in _SUPPORTED_SUFFIXES

    def extract_file(
        self, source_root: Path, relative_path: str, content: bytes
    ) -> GraphFileExtraction:
        if not self.supports(relative_path):
            raise ValidationFailed("Polyglot graph dosya uzantisi desteklenmiyor")
        if len(content) > MAX_GRAPH_SOURCE_BYTES:
            raise ValidationFailed("Graph polyglot kaynak dosya boyut sinirini asiyor")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationFailed("Graph polyglot kaynak strict UTF-8 olmali") from exc
        if Path(relative_path).suffix.casefold() == ".py":
            extraction = self._python.extract_file(source_root, relative_path, content)
            return replace(
                extraction,
                file=replace(extraction.file, extractor_profile_digest=self.profile_digest),
            )
        return _extract(relative_path, text, digest_of_bytes(content), self.profile_digest)


def _extract(
    relative_path: str, text: str, content_digest: str, profile_digest: str
) -> GraphFileExtraction:
    masked = _mask_non_code(text)
    if masked.count("{") != masked.count("}"):
        return _syntax_error(relative_path, content_digest, profile_digest)
    language = _language(relative_path)
    declarations: list[_Declaration] = []
    if language == "plsql":
        for match in _PLSQL_RE.finditer(masked):
            kind = (
                GraphNodeKind.CLASS
                if match.group("kind").casefold().startswith("package")
                else GraphNodeKind.FUNCTION
            )
            declarations.append(
                _Declaration(
                    name=match.group("name"),
                    kind=kind,
                    start=match.start(),
                    end=_plsql_end(masked, match.start()),
                    body_start=match.end(),
                    body_end=_plsql_end(masked, match.start()),
                )
            )
    else:
        for match in _CLASS_RE.finditer(masked):
            opening = masked.find("{", match.end() - 1)
            end = _body_end(masked, opening)
            if end < 0:
                return _syntax_error(relative_path, content_digest, profile_digest)
            declarations.append(
                _Declaration(
                    name=match.group("name"),
                    kind=GraphNodeKind.CLASS,
                    start=match.start(),
                    end=end,
                    body_start=opening + 1,
                    body_end=end - 1,
                    base=match.group("base"),
                )
            )
        for regex in (_FUNCTION_RE, _ARROW_RE):
            for match in regex.finditer(masked):
                name = match.group("name")
                if name.casefold() in _CONTROL_WORDS:
                    continue
                opening = masked.find("{", match.end() - 1)
                end = _body_end(masked, opening)
                if end < 0:
                    return _syntax_error(relative_path, content_digest, profile_digest)
                declarations.append(
                    _Declaration(
                        name=name,
                        kind=GraphNodeKind.FUNCTION,
                        start=match.start(),
                        end=end,
                        body_start=opening + 1,
                        body_end=end - 1,
                    )
                )
    declarations.sort(key=lambda item: (item.start, item.end, item.name))
    # A function declaration inside a class is a method; nested declarations use
    # the smallest containing class, avoiding a guessed cross-file relationship.
    classes = [item for item in declarations if item.kind is GraphNodeKind.CLASS]
    parents: dict[int, int | None] = {}
    for index, declaration in enumerate(declarations):
        candidates = [
            (parent_index, parent)
            for parent_index, parent in enumerate(classes)
            if parent.start < declaration.start < parent.end
        ]
        parents[index] = (
            min(candidates, key=lambda item: item[1].end - item[1].start)[0]
            if candidates and declaration.kind is GraphNodeKind.FUNCTION
            else None
        )
    for index, declaration in enumerate(declarations):
        if parents[index] is not None and declaration.kind is GraphNodeKind.FUNCTION:
            declarations[index] = replace(declaration, kind=GraphNodeKind.METHOD)

    module_name = relative_path.rsplit(".", 1)[0].replace("/", ".").replace("\\", ".")
    file_symbol = GraphSymbol(
        symbol_id=symbol_identity(
            kind=GraphNodeKind.MODULE,
            qualified_name=module_name,
            file_relative_path=relative_path,
            disambiguator=0,
        ),
        qualified_name=module_name,
        kind=GraphNodeKind.MODULE,
        file_relative_path=relative_path,
        parent_symbol_id=None,
        body_digest=body_digest(text),
        start_line=1,
        end_line=max(1, len(text.splitlines())),
        confidence=GraphConfidence.EXTRACTED,
    )
    symbols: list[GraphSymbol] = [file_symbol]
    symbol_by_index: dict[int, GraphSymbol] = {}
    names: dict[str, list[GraphSymbol]] = {}
    counts: dict[str, int] = {}
    for index, declaration in enumerate(declarations):
        parent_index = parents[index]
        parent = file_symbol if parent_index is None else symbol_by_index[parent_index]
        qualified_name = f"{parent.qualified_name}.{declaration.name}"
        ordinal = counts.get(qualified_name, 0)
        counts[qualified_name] = ordinal + 1
        segment = text[declaration.start : declaration.end]
        symbol = GraphSymbol(
            symbol_id=symbol_identity(
                kind=declaration.kind,
                qualified_name=qualified_name,
                file_relative_path=relative_path,
                disambiguator=ordinal,
            ),
            qualified_name=qualified_name,
            kind=declaration.kind,
            file_relative_path=relative_path,
            parent_symbol_id=parent.symbol_id,
            body_digest=body_digest(segment),
            start_line=_line_number(text, declaration.start),
            end_line=_line_number(text, max(declaration.start, declaration.end - 1)),
            confidence=GraphConfidence.EXTRACTED,
        )
        symbols.append(symbol)
        symbol_by_index[index] = symbol
        names.setdefault(declaration.name, []).append(symbol)

    edges: list[GraphEdge] = []
    edge_ids: set[str] = set()

    def add_edge(
        source: GraphSymbol,
        relation: GraphRelation,
        target_name: str,
        *,
        target: GraphSymbol | None,
        confidence: GraphConfidence,
        provenance: str,
    ) -> None:
        edge = GraphEdge(
            edge_id=edge_identity(
                source_symbol_id=source.symbol_id,
                relation=relation,
                target_symbol_id=target.symbol_id if target is not None else None,
                target_qualified_name=target_name,
                confidence=confidence,
            ),
            source_symbol_id=source.symbol_id,
            relation=relation,
            target_symbol_id=target.symbol_id if target is not None else None,
            target_qualified_name=target_name,
            confidence=confidence,
            provenance=provenance,
        )
        if edge.edge_id not in edge_ids:
            edges.append(edge)
            edge_ids.add(edge.edge_id)

    provenance = f"{EXTRACTOR_PROFILE_SCHEMA}:{language}"
    for index, declaration in enumerate(declarations):
        source = symbol_by_index[index]
        parent_index = parents[index]
        parent = symbol_by_index[parent_index] if parent_index is not None else file_symbol
        add_edge(
            parent,
            GraphRelation.CONTAINS,
            source.qualified_name,
            target=source,
            confidence=GraphConfidence.EXTRACTED,
            provenance=provenance,
        )
        if declaration.base is not None:
            targets = names.get(declaration.base, [])
            target = targets[0] if len(targets) == 1 else None
            add_edge(
                source,
                GraphRelation.EXTENDS,
                declaration.base,
                target=target,
                confidence=(
                    GraphConfidence.INFERRED
                    if target is not None
                    else GraphConfidence.EXTERNAL
                ),
                provenance=provenance,
            )

    # Imports are explicit syntax.  For string-bearing JS/TS imports use the
    # original line only after the masked line proved that it is code, so a
    # comment cannot create a fabricated edge.
    for match in re.finditer(r"(?m)^[ \t]*import\b[^\n;]*(?:;|$)", masked):
        original = text[match.start() : match.end()]
        target_match = re.search(r"(?:from\s*)?[\"']([^\"']+)[\"']", original)
        target_name = (
            target_match.group(1)
            if target_match
            else original.strip().removeprefix("import").strip().rstrip(";")
        )
        if target_name:
            add_edge(
                file_symbol,
                GraphRelation.IMPORTS,
                target_name,
                target=None,
                confidence=GraphConfidence.EXTRACTED,
                provenance=provenance,
            )

    # Call resolution is intentionally conservative: only a unique unqualified
    # local symbol becomes inferred.  Qualified/ambiguous calls remain unresolved.
    for call in re.finditer(rf"\b(?P<name>{_IDENTIFIER})\s*\(", masked):
        name = call.group("name")
        if name.casefold() in _CONTROL_WORDS:
            continue
        if any(
            declaration.name == name
            and declaration.start <= call.start() < declaration.body_start
            for declaration in declarations
        ):
            # This is the declaration header's own parameter list, not a call.
            continue
        before = masked[max(0, call.start() - 1) : call.start()]
        qualified = before == "."
        target_name = name
        if qualified:
            prefix_start = call.start() - 1
            while prefix_start >= 0 and (
                masked[prefix_start].isalnum() or masked[prefix_start] in "_$#."
            ):
                prefix_start -= 1
            target_name = masked[prefix_start + 1 : call.end() - 1]
        owner = file_symbol
        owner_candidates = [
            (index, declaration)
            for index, declaration in enumerate(declarations)
            if declaration.body_start <= call.start() < declaration.body_end
        ]
        if owner_candidates:
            owner_index = min(
                owner_candidates, key=lambda item: item[1].end - item[1].start
            )[0]
            owner = symbol_by_index[owner_index]
        targets = names.get(name, [])
        target = targets[0] if len(targets) == 1 and not qualified else None
        if target is owner:
            continue
        add_edge(
            owner,
            GraphRelation.CALLS,
            target_name,
            target=target,
            confidence=(
                GraphConfidence.INFERRED
                if target is not None
                else GraphConfidence.UNRESOLVED
            ),
            provenance=provenance,
        )

    graph_file = GraphFile(
        relative_path=relative_path,
        content_digest=content_digest,
        parse_state="parsed",
        error_count=0,
        extractor_profile_digest=profile_digest,
    )
    return GraphFileExtraction(file=graph_file, symbols=tuple(symbols), edges=tuple(edges))


def _syntax_error(
    relative_path: str, content_digest: str, profile_digest: str
) -> GraphFileExtraction:
    return GraphFileExtraction(
        file=GraphFile(
            relative_path=relative_path,
            content_digest=content_digest,
            parse_state="syntax-error",
            error_count=1,
            extractor_profile_digest=profile_digest,
        ),
        symbols=(),
        edges=(),
    )
