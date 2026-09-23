"""Immutable context-graph contracts for the Zekam Code Graph Engine.

This module is deliberately provider-free and structural.  It defines the value
objects and enums that the Python AST extractor, the SQLite graph store and any
future Tree-sitter adapter share.  No runtime dependency beyond the standard
library is introduced here.

Deterministic identity rules (documented, versioned and exposed in signatures)

- A *line number* is **never** a persistent symbol identity.  Line numbers are
  only kept as a locator so that a symbol can be shown to a human; they are not
  part of ``symbol_id``/``edge_id`` digests.
- ``symbol_id`` is ``digest({schema, kind, qualified_name, file, disambiguator})``.
  The ``disambiguator`` is a deterministic per-file encounter ordinal used only
  when the same qualified name appears more than once inside one file; identical
  content yields an identical ``disambiguator``.
- ``body_digest`` is the SHA-256 of the source segment of the node.  Moving lines
  without changing content therefore keeps ``body_digest`` stable.
- ``edge_id`` is ``digest({schema, source, relation, target_symbol_id or
  target_qualified_name, confidence})``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from zekam.domain.canonical import digest, digest_of_bytes, parse_digest
from zekam.domain.errors import ValidationFailed

#: Schema identifiers used inside every symbol/edge/generation digest.
SYMBOL_IDENTITY_SCHEMA: str = "zekam-graph-symbol-id/v1"
EDGE_IDENTITY_SCHEMA: str = "zekam-graph-edge-id/v1"
BODY_DIGEST_PROFILE: str = "zekam-graph-body/v1"
GENERATION_SCHEMA: str = "zekam-code-graph-generation/v1"


class GraphNodeKind(StrEnum):
    """Extensible node kinds.  V2 may add ``interface``, ``enum`` and so on."""

    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    ASYNC_FUNCTION = "async_function"


class GraphRelation(StrEnum):
    """V1 relation set.

    ``contains`` is a structural/ranking-excluded edge; the dependency edges that
    participate in traversal/PageRank are ``imports``, ``calls``, ``references``
    and ``extends``.
    """

    CONTAINS = "contains"
    IMPORTS = "imports"
    CALLS = "calls"
    REFERENCES = "references"
    EXTENDS = "extends"


class GraphConfidence(StrEnum):
    """How much the parser trusts the produced symbol or edge.

    ``extracted``   the clause/node is literally present in the source.
    ``inferred``    resolved against a symbol visible in the same extraction unit.
    ``external``    the name points outside the extraction unit (import, builtin).
    ``unresolved``  the parser could not resolve the target (independent of
                    ``inferred``; cross-module method dispatch stays unresolved).
    """

    EXTRACTED = "extracted"
    INFERRED = "inferred"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"


#: Relations that are allowed inside traversal / ranking dependency edge sets.
#: ``contains`` is deliberately excluded (structural hierarchy edge only).
DEPENDENCY_RELATIONS: frozenset[GraphRelation] = frozenset(
    {
        GraphRelation.IMPORTS,
        GraphRelation.CALLS,
        GraphRelation.REFERENCES,
        GraphRelation.EXTENDS,
    }
)

#: All relations allowed in V1.
ALL_RELATIONS: frozenset[GraphRelation] = frozenset(
    {*DEPENDENCY_RELATIONS, GraphRelation.CONTAINS}
)

NODE_KINDS: frozenset[GraphNodeKind] = frozenset(GraphNodeKind)

_PARSE_STATES: frozenset[str] = frozenset({"parsed", "syntax-error"})


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or path.is_absolute()
        or ".." in path.parts
        or "\x00" in value
    ):
        raise ValidationFailed("Graph relative path portable olmali")
    return value


def _line_no(value: int) -> int:
    if value < 1:
        raise ValidationFailed("Graph satir numarasi pozitif tamsayi olmali")
    return value


def symbol_identity(
    *,
    kind: GraphNodeKind,
    qualified_name: str,
    file_relative_path: str,
    disambiguator: int,
) -> str:
    """Deterministic symbol identity; contains no line numbers."""
    if disambiguator < 0:
        raise ValidationFailed("Graph symbol disambiguator non-negative integer olmali")
    return digest(
        {
            "schema": SYMBOL_IDENTITY_SCHEMA,
            "kind": kind.value,
            "qualified_name": qualified_name,
            "file": _safe_relative(file_relative_path),
            "disambiguator": disambiguator,
        }
    )


def body_digest(source: str) -> str:
    """Digest of the raw structural source segment (stable across line moves)."""
    return digest_of_bytes((BODY_DIGEST_PROFILE + "\x00" + source).encode("utf-8"))


def edge_identity(
    *,
    source_symbol_id: str,
    relation: GraphRelation,
    target_symbol_id: str | None,
    target_qualified_name: str,
    confidence: GraphConfidence,
) -> str:
    """Deterministic edge identity; no line numbers."""
    parse_digest(source_symbol_id)
    return digest(
        {
            "schema": EDGE_IDENTITY_SCHEMA,
            "source": parse_digest(source_symbol_id),
            "relation": relation.value,
            "target": (
                parse_digest(target_symbol_id)
                if target_symbol_id is not None
                else _safe_relative_or_name(target_qualified_name)
            ),
            "confidence": confidence.value,
        }
    )


def _safe_relative_or_name(value: str) -> str:
    if not value or "\x00" in value:
        raise ValidationFailed("Graph target adi bos olamaz")
    return value


@dataclass(frozen=True, slots=True)
class GraphFile:
    """A single source file admitted into a graph generation.

    ``parse_state`` and ``error_count`` let a syntax-erroneous file be recorded
    instead of silently treating the whole generation as successful.
    """

    relative_path: str
    content_digest: str
    parse_state: str
    error_count: int
    extractor_profile_digest: str

    def __post_init__(self) -> None:
        _safe_relative(self.relative_path)
        parse_digest(self.content_digest)
        parse_digest(self.extractor_profile_digest)
        if self.parse_state not in _PARSE_STATES:
            raise ValidationFailed("Graph file parse_state bilinmeyen durum tasiyor")
        if self.error_count < 0:
            raise ValidationFailed("Graph file error_count non-negative integer olmali")


@dataclass(frozen=True, slots=True)
class GraphSymbol:
    """An immutable symbol node.

    ``start_line``/``end_line`` are locators, **not** identity.  ``symbol_id`` is
    the deterministic digest identity (see module docstring).
    """

    symbol_id: str
    qualified_name: str
    kind: GraphNodeKind
    file_relative_path: str
    parent_symbol_id: str | None
    body_digest: str
    start_line: int
    end_line: int
    confidence: GraphConfidence

    def __post_init__(self) -> None:
        parse_digest(self.symbol_id)
        _safe_relative(self.file_relative_path)
        if not self.qualified_name:
            raise ValidationFailed("Graph symbol qualified_name bos olamaz")
        if self.parent_symbol_id is not None:
            parse_digest(self.parent_symbol_id)
        parse_digest(self.body_digest)
        _line_no(self.start_line)
        _line_no(self.end_line)
        if self.end_line < self.start_line:
            raise ValidationFailed("Graph symbol satir araligi gecersiz")
        if self.kind not in NODE_KINDS:
            raise ValidationFailed("Graph symbol kind bilinmeyen durum tasiyor")


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """An immutable directed edge.

    A self-loop (``source_symbol_id == target_symbol_id``) is rejected.  A symbol
    must use ``target_symbol_id`` when the target is resolvable; otherwise only
    ``target_qualified_name`` is acceptable.
    """

    edge_id: str
    source_symbol_id: str
    relation: GraphRelation
    target_symbol_id: str | None
    target_qualified_name: str
    confidence: GraphConfidence
    provenance: str

    def __post_init__(self) -> None:
        parse_digest(self.edge_id)
        parse_digest(self.source_symbol_id)
        if self.relation not in ALL_RELATIONS:
            raise ValidationFailed("Graph edge relation bilinmeyen durum tasiyor")
        if self.target_symbol_id is not None:
            parse_digest(self.target_symbol_id)
            if self.target_symbol_id == self.source_symbol_id:
                raise ValidationFailed("Graph edge self-loop kabul edilmez")
            if self.target_symbol_id is not None and not self.target_qualified_name:
                raise ValidationFailed("Graph edge target adi bos olamaz")
        if not self.target_qualified_name:
            raise ValidationFailed("Graph edge target_qualified_name bos olamaz")
        if not self.provenance:
            raise ValidationFailed("Graph edge provenance bos olamaz")
        if self.source_symbol_id == self.target_symbol_id:
            raise ValidationFailed("Graph edge self-loop kabul edilmez")


@dataclass(frozen=True, slots=True)
class GraphGeneration:
    """The immutable header of one graph generation (building/ready/superseded)."""

    generation_digest: str
    project_id: str
    source_revision: str
    tree_digest: str
    source_manifest_digest: str
    extractor_profile_digest: str
    file_count: int
    symbol_count: int
    edge_count: int
    error_count: int
    state: str
    created_at: str

    def __post_init__(self) -> None:
        for value in (
            self.generation_digest,
            self.tree_digest,
            self.source_manifest_digest,
            self.extractor_profile_digest,
        ):
            parse_digest(value)
        if not self.project_id or not self.source_revision:
            raise ValidationFailed("Graph generation exact kimlik/revision ister")
        if self.state not in {"building", "ready", "superseded"}:
            raise ValidationFailed("Graph generation state bilinmeyen durum tasiyor")
        for field, count in (
            ("file_count", self.file_count),
            ("symbol_count", self.symbol_count),
            ("edge_count", self.edge_count),
            ("error_count", self.error_count),
        ):
            if count < 0:
                raise ValidationFailed(f"Graph generation {field} non-negative integer olmali")
        if self.file_count < 1:
            raise ValidationFailed("Graph generation en az bir dosya ister")


@dataclass(frozen=True, slots=True)
class GraphImpactHit:
    """One blast-radius hit produced by impact traversal."""

    target_symbol_id: str
    relation: GraphRelation
    weight: float
    hit_type: str

    def __post_init__(self) -> None:
        parse_digest(self.target_symbol_id)
        if self.relation not in ALL_RELATIONS:
            raise ValidationFailed("Graph impact relation bilinmeyen durum tasiyor")
        if not isinstance(self.weight, float) or self.weight < 0:
            raise ValidationFailed("Graph impact weight non-negative float olmali")
        if not self.hit_type:
            raise ValidationFailed("Graph impact hit_type bos olamaz")
