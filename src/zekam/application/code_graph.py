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

import os
import stat as stat_module
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

# Metadata discovery is bounded independently from source content reads.  The
# graph plan must never turn a large or hostile tree into an unbounded list.
MAX_GRAPH_METADATA_ENTRIES: int = 200_000
MAX_GRAPH_METADATA_DIR_ENTRIES: int = 10_000
MAX_GRAPH_METADATA_DEPTH: int = 100

# Next.js emits browser bundles under these directory names.  They are build
# output rather than authored source and can contain syntax that the bounded
# structural extractor cannot interpret reliably.  Keep the rule narrow so a
# regular ``build`` or ``static`` source directory remains eligible.
_GENERATED_GRAPH_DIRS = frozenset({".next", "_next"})
_DENIED_GRAPH_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        "node_modules",
        "target",
    }
)


def _is_link_or_reparse(path: Path) -> bool:
    """Do not let graph discovery follow symlink or Windows reparse paths."""

    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except FileNotFoundError:
        # A plan/apply race is classified by the typed source read below.
        return False
    except OSError:
        # Permission or metadata failures must not turn into an implicit
        # source traversal; treat the path as unsafe and exclude it.
        return True
    return bool(attributes & 0x400)


def _has_link_component(source_root: Path, relative: Path) -> bool:
    current = source_root
    for part in relative.parts:
        current /= part
        if _is_link_or_reparse(current):
            return True
    return False


def _read_source_bytes(source_root: Path, relative: Path) -> bytes:
    path = source_root / relative
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_GRAPH_SOURCE_BYTES + 1)
    except OSError as exc:
        raise ValidationFailed("Graph source okunamadi") from exc
    if len(payload) > MAX_GRAPH_SOURCE_BYTES:
        raise ValidationFailed("Graph source bounded boyutu asti")
    return payload


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
class GraphSourceMetadata:
    """Bounded, content-free metadata for one graph source-tree entry."""

    relative_path: str
    kind: str
    size_bytes: int | None
    status: str

    def __post_init__(self) -> None:
        _safe_relative(self.relative_path)
        if self.kind not in {"file", "directory", "symlink", "other"}:
            raise ValidationFailed("Graph metadata turu gecersiz")
        if self.status not in {
            "eligible",
            "ignored",
            "generated",
            "unsupported",
            "oversized",
            "symlink",
            "unreadable",
            "missing",
            "other",
        }:
            raise ValidationFailed("Graph metadata durumu gecersiz")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValidationFailed("Graph metadata boyutu negatif olamaz")

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.relative_path,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "status": self.status,
        }


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
    metadata_inventory: tuple[GraphSourceMetadata, ...] = ()

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
        metadata_seen: set[str] = set()
        for entry in self.metadata_inventory:
            if not isinstance(entry, GraphSourceMetadata):
                raise ValidationFailed("Graph metadata envanter girdisi gecersiz")
            if entry.relative_path in metadata_seen:
                raise ValidationFailed("Graph metadata duplicate yol iceriyor")
            metadata_seen.add(entry.relative_path)

    @property
    def metadata_inventory_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-graph-metadata-inventory/v1",
                "entries": [
                    entry.as_dict()
                    for entry in sorted(
                        self.metadata_inventory, key=lambda item: item.relative_path
                    )
                ],
            }
        )

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
                "metadata_inventory_digest": self.metadata_inventory_digest,
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
    if not source_root.is_dir() or _is_link_or_reparse(source_root):
        raise ValidationFailed("Graph source root dizin olmali")
    files: list[Path] = []
    pending: list[Path] = [source_root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError as exc:
            raise ValidationFailed("Graph source dizin okunamadi") from exc
        child_directories: list[Path] = []
        for entry in entries:
            path = Path(entry.path)
            try:
                relative = path.relative_to(source_root)
            except ValueError:
                continue
            if _is_link_or_reparse(path):
                continue
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                # A race or permission error must not turn into an implicit
                # source read; the inaccessible entry is excluded fail-closed.
                continue
            parts = tuple(part.casefold() for part in relative.parts)
            if any(part in _GENERATED_GRAPH_DIRS or part in _DENIED_GRAPH_DIRS for part in parts):
                continue
            if is_directory:
                child_directories.append(path)
            elif is_file:
                files.append(relative)
        # Reverse push preserves lexical traversal order without collecting a
        # potentially unbounded recursive rglob result first.
        pending.extend(reversed(child_directories))
    files.sort()
    return files


def _metadata_entry(
    relative_path: str,
    *,
    kind: str,
    size_bytes: int | None,
    status: str,
) -> GraphSourceMetadata:
    return GraphSourceMetadata(
        relative_path=relative_path,
        kind=kind,
        size_bytes=size_bytes,
        status=status,
    )


def _discover_graph_metadata(
    source_root: Path,
    extractor: CodeGraphExtractor,
) -> tuple[GraphSourceMetadata, ...]:
    """Collect bounded path/type/size metadata without reading file content.

    Denied and generated directories are represented by their own metadata
    entry and pruned before descent.  This keeps the graph's source ownership
    contract aligned with its deny-tree traversal while still making skipped
    input visible in the exact plan digest.
    """

    if not source_root.is_dir() or _is_link_or_reparse(source_root):
        raise ValidationFailed("Graph source root dizin olmali")
    inventory: list[GraphSourceMetadata] = []
    pending: list[tuple[Path, int]] = [(source_root, 0)]
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError as exc:
            raise ValidationFailed("Graph metadata dizin okunamadi") from exc
        if len(entries) > MAX_GRAPH_METADATA_DIR_ENTRIES:
            raise ValidationFailed("Graph metadata dizin siniri asildi")
        child_directories: list[Path] = []
        for entry in entries:
            if len(inventory) >= MAX_GRAPH_METADATA_ENTRIES:
                raise ValidationFailed("Graph metadata envanter siniri asildi")
            path = Path(entry.path)
            try:
                relative = path.relative_to(source_root).as_posix()
            except ValueError as exc:
                raise ValidationFailed("Graph metadata root disi yol") from exc
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                inventory.append(
                    _metadata_entry(
                        relative,
                        kind="other",
                        size_bytes=None,
                        status="missing",
                    )
                )
                continue
            except OSError:
                inventory.append(
                    _metadata_entry(
                        relative,
                        kind="other",
                        size_bytes=None,
                        status="unreadable",
                    )
                )
                continue

            is_link = entry.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)
            if is_link:
                inventory.append(
                    _metadata_entry(
                        relative,
                        kind="symlink",
                        size_bytes=None,
                        status="symlink",
                    )
                )
                continue

            is_directory = stat_module.S_ISDIR(info.st_mode)
            is_file = stat_module.S_ISREG(info.st_mode)
            parts = tuple(part.casefold() for part in PurePosixPath(relative).parts)
            ignored = any(part in _DENIED_GRAPH_DIRS for part in parts)
            generated = any(part in _GENERATED_GRAPH_DIRS for part in parts)
            if is_directory:
                status = "generated" if generated else "ignored" if ignored else "eligible"
                inventory.append(
                    _metadata_entry(
                        relative,
                        kind="directory",
                        size_bytes=None,
                        status=status,
                    )
                )
                if ignored or generated:
                    continue
                if depth + 1 > MAX_GRAPH_METADATA_DEPTH:
                    raise ValidationFailed("Graph metadata derinlik siniri asildi")
                child_directories.append(path)
                continue

            if not is_file:
                inventory.append(
                    _metadata_entry(
                        relative,
                        kind="other",
                        size_bytes=None,
                        status="other",
                    )
                )
                continue

            if ignored:
                status = "ignored"
            elif generated:
                status = "generated"
            elif not extractor.supports(relative):
                status = "unsupported"
            elif int(info.st_size) > MAX_GRAPH_SOURCE_BYTES:
                status = "oversized"
            else:
                status = "eligible"
            inventory.append(
                _metadata_entry(
                    relative,
                    kind="file",
                    size_bytes=int(info.st_size),
                    status=status,
                )
            )
        pending.extend((child, depth + 1) for child in reversed(child_directories))
    inventory.sort(key=lambda item: item.relative_path)
    return tuple(inventory)


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
    metadata_inventory = _discover_graph_metadata(source_root, extractor)
    manifests: list[tuple[str, str]] = []
    for relative in _discover_source_files(source_root):
        relative_path = relative.as_posix()
        if not extractor.supports(relative_path):
            continue
        content = _read_source_bytes(source_root, relative)
        if len(content) > MAX_GRAPH_SOURCE_BYTES:
            raise ValidationFailed("Graph source dosya boyut sinirini asiyor")
        manifests.append((relative_path, digest_of_bytes(content)))
        del content
    if not manifests:
        raise ValidationFailed("Graph plan desteklenen dosya icermiyor")
    tree_digest = digest({"schema": "zekam-graph-tree/v1", "files": sorted(manifests)})
    source_manifest_digest = digest(
        {
            "schema": "zekam-graph-source-manifest/v1",
            "files": sorted(manifests),
            "metadata_inventory_digest": digest(
                {
                    "schema": "zekam-graph-metadata-inventory/v1",
                    "entries": [entry.as_dict() for entry in metadata_inventory],
                }
            ),
        }
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
        metadata_inventory=metadata_inventory,
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
    if plan.metadata_inventory:
        current_metadata = _discover_graph_metadata(source_root, extractor)
        if current_metadata != plan.metadata_inventory:
            raise ValidationFailed("Graph plan/source metadata envanter drift")
    files: list[GraphFile] = []
    symbols: list[GraphSymbol] = []
    edges: list[GraphEdge] = []
    for relative_path, content_digest in plan.file_manifests:
        if _has_link_component(source_root, Path(relative_path)):
            raise ValidationFailed("Graph source link/reparse drift")
        content = _read_source_bytes(source_root, Path(relative_path))
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
