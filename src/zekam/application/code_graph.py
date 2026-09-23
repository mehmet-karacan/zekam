"""Storage-neutral code graph port, extractor contract and build orchestration.

This module wires the immutable domain contracts to a ``CodeGraphPort`` (SQLite
today, maybe another engine later) without coupling to a provider or to the
retrieval runtime.  It defines:

- ``CodeGraphPort``   the persistence boundary (store).
- ``CodeGraphExtractor`` / ``GraphFileExtraction``   the parser boundary.
- ``CodeGraphBuildPlan``   a frozen, digest-bound plan of exactly what to build.
- ``plan_graph_build`` / ``apply_graph_build``   the read-only plan and the
  atomic apply orchestration.

Incremental semantics: file cache identity is
``relative_path + content_digest + extractor_profile_digest``; the store's replay
idempotency turns a zero-change rebuild into a no-op, and any content/profile
drift produces a new generation that supersedes the previous one atomically.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from zekam.domain.canonical import digest, digest_of_bytes, parse_digest
from zekam.domain.code_graph import (
    GENERATION_SCHEMA,
    GraphEdge,
    GraphFile,
    GraphGeneration,
    GraphSymbol,
)
from zekam.domain.errors import ValidationFailed

#: Maximum source file bytes the structural extractor will accept.
MAX_GRAPH_SOURCE_BYTES: int = 4 * 1024 * 1024


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value != path.as_posix()
        or path.is_absolute()
        or ".." in path.parts
        or "\x00" in value
    ):
        raise ValidationFailed("Graph source path portable relative olmali")
    return value


@dataclass(frozen=True, slots=True)
class GraphFileExtraction:
    """Result of extracting one supported file (storage-free)."""

    file: GraphFile
    symbols: tuple[GraphSymbol, ...]
    edges: tuple[GraphEdge, ...]

    def __post_init__(self) -> None:
        for symbol in self.symbols:
            if symbol.file_relative_path != self.file.relative_path:
                raise ValidationFailed("Graph extraction symbol/file path drift")
        for edge in self.edges:
            if not any(edge.source_symbol_id == symbol.symbol_id for symbol in self.symbols):
                raise ValidationFailed("Graph extraction edge kaynagi bilinmeyen symbol")
        seen: set[str] = set()
        for symbol in self.symbols:
            if symbol.symbol_id in seen:
                raise ValidationFailed("Graph extraction duplicate symbol id")
            seen.add(symbol.symbol_id)


class CodeGraphExtractor(Protocol):
    """Parser boundary; produces nodes/edges but never writes to storage."""

    @property
    def profile_digest(self) -> str: ...

    def supports(self, relative_path: str) -> bool: ...

    def extract_file(
        self, source_root: Path, relative_path: str, content: bytes
    ) -> GraphFileExtraction: ...


class CodeGraphPort(ABC):
    """Persistence boundary implemented by the SQLite graph store."""

    @property
    @abstractmethod
    def path(self) -> Path: ...

    @abstractmethod
    def build_generation(
        self,
        *,
        project_id: str,
        source_revision: str,
        tree_digest: str,
        source_manifest_digest: str,
        extractor_profile_digest: str,
        files: tuple[GraphFile, ...],
        symbols: tuple[GraphSymbol, ...],
        edges: tuple[GraphEdge, ...],
        created_at: str,
    ) -> GraphGeneration: ...

    @abstractmethod
    def generation(self, project_id: str) -> GraphGeneration: ...

    @abstractmethod
    def integrity(self) -> dict[str, object]: ...

    @abstractmethod
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CodeGraphBuildPlan:
    """Frozen plan binding a build to exact source digests."""

    schema: str
    project_id: str
    project_slug: str
    source_revision: str
    tree_digest: str
    source_manifest_digest: str
    extractor_profile_digest: str
    created_at: str
    file_manifests: tuple[tuple[str, str], ...]  # (relative_path, content_digest)

    def __post_init__(self) -> None:
        if self.schema != GENERATION_SCHEMA:
            raise ValidationFailed("Graph plan schema uyusmuyor")
        if not self.project_id or not self.project_slug or not self.source_revision:
            raise ValidationFailed("Graph plan exact kimlik/revision ister")
        for value in (
            self.tree_digest,
            self.source_manifest_digest,
            self.extractor_profile_digest,
        ):
            parse_digest(value)
        seen: set[str] = set()
        for relative_path, content_digest in self.file_manifests:
            normalized = _safe_relative(relative_path)
            parse_digest(content_digest)
            if normalized in seen:
                raise ValidationFailed("Graph plan duplicate file iceriyor")
            seen.add(normalized)

    @property
    def plan_digest(self) -> str:
        return digest(
            {
                "schema": self.schema,
                "project_id": self.project_id,
                "project_slug": self.project_slug,
                "source_revision": self.source_revision,
                "tree_digest": self.tree_digest,
                "source_manifest_digest": self.source_manifest_digest,
                "extractor_profile_digest": self.extractor_profile_digest,
                "files": sorted(self.file_manifests),
            }
        )


def graph_store_path(home: Path, project_slug: str) -> Path:
    """Logical store layout: ``ZEKAM_HOME/knowledge-index/graph/<slug>/code-graph.sqlite3``."""
    if (
        not isinstance(home, Path)
        or not home.is_absolute()
        or not project_slug
        or "/" in project_slug
        or "\\" in project_slug
        or project_slug in {".", ".."}
    ):
        raise ValidationFailed("Graph store home/slug gecersiz")
    return home / "knowledge-index" / "graph" / project_slug / "code-graph.sqlite3"


def _discover_source_files(source_root: Path) -> list[Path]:
    if not source_root.is_dir():
        raise ValidationFailed("Graph source root dizin olmali")
    files: list[Path] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(source_root)
        except ValueError:
            continue
        if any(part in {".git", "__pycache__", ".venv", ".mypy_cache", ".pytest_cache"}
               for part in relative.parts):
            continue
        files.append(relative)
    return files


def plan_graph_build(
    source_root: Path,
    *,
    project_id: str,
    project_slug: str,
    source_revision: str,
    extractor: CodeGraphExtractor,
    created_at: str,
) -> CodeGraphBuildPlan:
    """Read-only discovery: enumerate supported files and digest the tree."""
    manifests: list[tuple[str, str]] = []
    for relative in _discover_source_files(source_root):
        relative_path = relative.as_posix()
        if not extractor.supports(relative_path):
            continue
        content = (source_root / relative).read_bytes()
        if len(content) > MAX_GRAPH_SOURCE_BYTES:
            raise ValidationFailed(
                "Graph source dosya boyut sinirini asiyor"
            )
        manifests.append((relative_path, digest_of_bytes(content)))
        del content
    if not manifests:
        raise ValidationFailed("Graph plan desteklenen dosya icermiyor")
    tree_digest = digest(
        {"schema": "zekam-graph-tree/v1", "files": sorted(manifests)}
    )
    source_manifest_digest = digest(
        {"schema": "zekam-graph-source-manifest/v1", "files": sorted(manifests)}
    )
    manifests.sort()
    return CodeGraphBuildPlan(
        schema=GENERATION_SCHEMA,
        project_id=project_id,
        project_slug=project_slug,
        source_revision=source_revision,
        tree_digest=tree_digest,
        source_manifest_digest=source_manifest_digest,
        extractor_profile_digest=extractor.profile_digest,
        created_at=created_at,
        file_manifests=tuple(manifests),
    )


def apply_graph_build(
    port: CodeGraphPort,
    extractor: CodeGraphExtractor,
    source_root: Path,
    plan: CodeGraphBuildPlan,
) -> GraphGeneration:
    """Extract every planned file and atomically publish a new generation."""
    expected = plan.plan_digest
    parse_digest(expected)
    files: list[GraphFile] = []
    symbols: list[GraphSymbol] = []
    edges: list[GraphEdge] = []
    for relative_path, content_digest in plan.file_manifests:
        content = (source_root / relative_path).read_bytes()
        if len(content) > MAX_GRAPH_SOURCE_BYTES:
            raise ValidationFailed("Graph source dosya boyut sinirini asiyor")
        if digest_of_bytes(content) != content_digest:
            raise ValidationFailed("Graph plan/source content digest drift")
        extraction = extractor.extract_file(source_root, relative_path, content)
        files.append(extraction.file)
        symbols.extend(extraction.symbols)
        edges.extend(extraction.edges)
        del content
    return port.build_generation(
        project_id=plan.project_id,
        source_revision=plan.source_revision,
        tree_digest=plan.tree_digest,
        source_manifest_digest=plan.source_manifest_digest,
        extractor_profile_digest=plan.extractor_profile_digest,
        files=tuple(files),
        symbols=tuple(symbols),
        edges=tuple(edges),
        created_at=plan.created_at,
    )


def generation_digest(
    *,
    project_id: str,
    source_revision: str,
    tree_digest: str,
    source_manifest_digest: str,
    extractor_profile_digest: str,
    files: tuple[GraphFile, ...],
    symbols: tuple[GraphSymbol, ...],
    edges: tuple[GraphEdge, ...],
) -> str:
    """Deterministic generation digest from the planned inputs."""
    return digest(
        {
            "schema": GENERATION_SCHEMA,
            "project_id": project_id,
            "source_revision": source_revision,
            "tree_digest": tree_digest,
            "source_manifest_digest": source_manifest_digest,
            "extractor_profile_digest": extractor_profile_digest,
            "files": [
                {
                    "path": file.relative_path,
                    "content_digest": parse_digest(file.content_digest),
                    "parse_state": file.parse_state,
                    "error_count": file.error_count,
                }
                for file in files
            ],
            "symbols": [
                {
                    "symbol_id": parse_digest(symbol.symbol_id),
                    "body_digest": parse_digest(symbol.body_digest),
                    "parent": (
                        parse_digest(symbol.parent_symbol_id)
                        if symbol.parent_symbol_id is not None
                        else None
                    ),
                }
                for symbol in symbols
            ],
            "edges": [
                {
                    "edge_id": parse_digest(edge.edge_id),
                    "relation": edge.relation.value,
                }
                for edge in edges
            ],
        }
    )
