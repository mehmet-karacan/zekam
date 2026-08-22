"""Read-only observatory projections for the Zekam UI.

The observatory is deliberately a *projection*: canonical authority remains in the
Work Graph, runtime receipt/claim records and the other bounded contexts.  This
module never grants authority and never reads prompt/model-response payloads.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from zekam.application.opencode_lifecycle import resume_projection
from zekam.domain.canonical import digest
from zekam.domain.observability import (
    REQUIRED_TILES,
    DerivedGraph,
    GraphEdge,
    GraphNode,
    OperationsDashboard,
    ProjectionTile,
)

SNAPSHOT_SCHEMA = "zekam-observatory-snapshot/v1"
MAX_MARKDOWN_BYTES = 64 * 1024
MAX_DOCUMENT_NODES = 180
MAX_DOCUMENT_EDGES = 360
MAX_REPORTS = 12
MAX_EVENTS = 80
MAX_AGENTS = 32
MAX_LABEL_CHARS = 96
REPOSITORY_REFRESH_SECONDS = 15.0

_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+?\.md(?:#[^)]+)?)\)", re.IGNORECASE)
_WIKI_LINK = re.compile(r"\[\[([^\]]+?)\]\]")
_HEADING = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_UNSAFE_LABEL = re.compile(
    r"(?:-----BEGIN|Bearer\s+|(?:password|secret|token|api[_ -]?key)\s*[:=]|"
    r"https?://|[A-Za-z]:\\|/(?:home|Users|root)/)",
    re.IGNORECASE,
)
_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "yerel-referanslar",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
    }
)
_REPORT_HINTS = ("RAPOR", "REPORT", "DURUM", "CHECKPOINT", "KANIT")


@dataclass(frozen=True, slots=True)
class ObservatoryAgent:
    """Sanitized live ownership observation.

    ``canonical_ref`` points to the lease/job record.  The observation does not
    prove completion and does not carry an owner token, prompt or model output.
    """

    agent_id: str
    label: str
    client: str
    state: str
    canonical_ref: str
    job_id: str | None = None
    work_item_id: str | None = None
    step_id: str | None = None
    heartbeat_at: dt.datetime | None = None
    lease_expires_at: dt.datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "label": self.label,
            "client": self.client,
            "state": self.state,
            "canonical_ref": self.canonical_ref,
            "job_id": self.job_id,
            "work_item_id": self.work_item_id,
            "step_id": self.step_id,
            "heartbeat_at": _iso(self.heartbeat_at),
            "lease_expires_at": _iso(self.lease_expires_at),
        }


@dataclass(frozen=True, slots=True)
class ObservatoryEvent:
    """Content-free state transition shown in the live event rail."""

    event_id: str
    event_type: str
    source: str
    occurred_at: dt.datetime
    canonical_ref: str
    job_id: str | None = None
    agent_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source": self.source,
            "occurred_at": _iso(self.occurred_at),
            "canonical_ref": self.canonical_ref,
            "job_id": self.job_id,
            "agent_id": self.agent_id,
        }


@dataclass(frozen=True, slots=True)
class ObservatoryReport:
    """Repository-relative report link; report body is never copied into telemetry."""

    report_id: str
    title: str
    relative_path: str
    modified_at: dt.datetime
    canonical_ref: str

    def as_dict(self) -> dict[str, str]:
        return {
            "report_id": self.report_id,
            "title": self.title,
            "relative_path": self.relative_path,
            "modified_at": _iso(self.modified_at) or "",
            "canonical_ref": self.canonical_ref,
        }


@dataclass(frozen=True, slots=True)
class RepositoryProjection:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    reports: tuple[ObservatoryReport, ...]
    source_digest: str


@dataclass(frozen=True, slots=True)
class RuntimeProjection:
    """Realm-scoped runtime projection returned by an infrastructure reader."""

    generated_at: dt.datetime
    tiles: tuple[ProjectionTile, ...]
    nodes: tuple[GraphNode, ...] = field(default_factory=tuple)
    edges: tuple[GraphEdge, ...] = field(default_factory=tuple)
    agents: tuple[ObservatoryAgent, ...] = field(default_factory=tuple)
    events: tuple[ObservatoryEvent, ...] = field(default_factory=tuple)
    source_digest: str = field(default_factory=lambda: digest({"runtime": "empty"}))
    available: bool = False
    detail: str = "runtime-not-configured"

    def __post_init__(self) -> None:
        keys = {item.key for item in self.tiles}
        missing = tuple(item for item in REQUIRED_TILES if item not in keys)
        if missing:
            raise ValueError(f"runtime projection missing tiles: {', '.join(missing)}")


class RuntimeProjectionReader(Protocol):
    def read(self) -> RuntimeProjection:
        """Read a realm-scoped projection without mutating canonical state."""

        ...


@dataclass(frozen=True, slots=True)
class OpenCodeLifecycleProjectionReader:
    """Project the sanitized local OpenCode ledger into observatory cards."""

    home: Path

    def read(self) -> RuntimeProjection:
        projection = resume_projection(self.home, limit=MAX_AGENTS)
        sessions = tuple(item for item in projection["sessions"] if item.get("status") != "closed")
        root = GraphNode(
            node_id="client:opencode",
            kind="client",
            label="OpenCode",
            canonical_ref="runtime:opencode-lifecycle",
        )
        nodes: list[GraphNode] = [root]
        edges: list[GraphEdge] = []
        agents: list[ObservatoryAgent] = []
        events: list[ObservatoryEvent] = []
        for item in sessions:
            session_id = str(item["session_id"])
            node_id = f"opencode-session:{_short_id(session_id)}"
            agent_name = str(item.get("agent") or "OpenCode session")
            model_ref = item.get("model_ref")
            label = agent_name if model_ref is None else f"{agent_name} · {model_ref}"
            occurred_at = _parse_timestamp(item.get("updated_at"))
            canonical_ref = f"runtime:opencode-lifecycle/{session_id}"
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    kind="agent-session",
                    label=sanitize_observatory_label(label, fallback="OpenCode session"),
                    canonical_ref=canonical_ref,
                )
            )
            edges.append(GraphEdge("client:opencode", node_id, "runs-session"))
            agents.append(
                ObservatoryAgent(
                    agent_id=node_id,
                    label=sanitize_observatory_label(label, fallback="OpenCode session"),
                    client="opencode",
                    state=str(item.get("status") or "unknown"),
                    canonical_ref=canonical_ref,
                    heartbeat_at=occurred_at,
                )
            )
            event_key = session_id + str(item.get("updated_at"))
            events.append(
                ObservatoryEvent(
                    event_id=f"opencode-event:{_short_id(event_key)}",
                    event_type=str(item.get("last_event") or "session.status"),
                    source="opencode-lifecycle",
                    occurred_at=occurred_at,
                    canonical_ref=canonical_ref,
                    agent_id=node_id,
                )
            )
        source = digest(
            {
                "sessions": [
                    {
                        "session_id": item["session_id"],
                        "status": item.get("status"),
                        "updated_at": item.get("updated_at"),
                    }
                    for item in sessions
                ]
            }
        )
        return RuntimeProjection(
            generated_at=dt.datetime.now(dt.UTC),
            tiles=unavailable_runtime_projection("opencode-lifecycle").tiles,
            nodes=tuple(nodes),
            edges=tuple(edges),
            agents=tuple(agents),
            events=tuple(events),
            source_digest=source,
            available=bool(sessions),
            detail="opencode-lifecycle",
        )


@dataclass(frozen=True, slots=True)
class EmptyRuntimeProjectionReader:
    detail: str = "realm-id-required"

    def read(self) -> RuntimeProjection:
        return unavailable_runtime_projection(self.detail)


@dataclass(frozen=True, slots=True)
class ObservatorySnapshot:
    """Single UI snapshot.  It is read-only and cannot grant authority."""

    generated_at: dt.datetime
    dashboard: OperationsDashboard
    graph: DerivedGraph
    agents: tuple[ObservatoryAgent, ...]
    events: tuple[ObservatoryEvent, ...]
    reports: tuple[ObservatoryReport, ...]
    runtime_available: bool
    runtime_detail: str
    read_only: bool = True
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not self.read_only:
            raise ValueError("observatory must remain read-only")
        if self.grants_authority:
            raise ValueError("observatory cannot grant authority")

    def _material(self) -> dict[str, Any]:
        return {
            "schema": SNAPSHOT_SCHEMA,
            "generated_at": _iso(self.generated_at),
            "read_only": True,
            "grants_authority": False,
            "dashboard": self.dashboard.as_dict(),
            "graph": self.graph.as_dict(),
            "agents": [item.as_dict() for item in self.agents],
            "events": [item.as_dict() for item in self.events],
            "reports": [item.as_dict() for item in self.reports],
            "runtime": {
                "available": self.runtime_available,
                "detail": self.runtime_detail,
            },
            "safety": {
                "prompt_content": False,
                "model_response_content": False,
                "secret_values": False,
                "authority": False,
            },
        }

    @property
    def projection_digest(self) -> str:
        material = self._material()
        # Snapshot time is transport metadata, not projection state.  Excluding it
        # prevents the SSE endpoint from sending an unchanged graph every poll.
        material.pop("generated_at", None)
        return digest(material)

    def as_dict(self) -> dict[str, Any]:
        document = self._material()
        document["projection_digest"] = self.projection_digest
        return document


@dataclass(slots=True)
class ObservatoryService:
    """Build the repository + runtime observatory snapshot."""

    core_path: Path
    runtime_reader: RuntimeProjectionReader = field(default_factory=EmptyRuntimeProjectionReader)
    client_reader: RuntimeProjectionReader = field(default_factory=EmptyRuntimeProjectionReader)
    repository_refresh_seconds: float = REPOSITORY_REFRESH_SECONDS
    _repository_cache: RepositoryProjection | None = field(default=None, init=False, repr=False)
    _repository_cache_at: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.repository_refresh_seconds <= 0:
            raise ValueError("repository_refresh_seconds pozitif olmali")

    def snapshot(self) -> ObservatorySnapshot:
        generated_at = dt.datetime.now(dt.UTC)
        repository = self._repository_projection()
        runtime = self._safe_runtime_projection()
        clients = self._safe_projection(self.client_reader, "client-read-failed")

        nodes = _unique_nodes(repository.nodes + runtime.nodes + clients.nodes)
        known = {item.node_id for item in nodes}
        edges = _unique_edges(
            tuple(
                item
                for item in repository.edges + runtime.edges + clients.edges
                if item.source in known and item.target in known
            )
        )
        runtime_root = next(
            (node.node_id for node in runtime.nodes if node.kind == "runtime-root"),
            None,
        )
        if runtime_root is not None and "system:zekam" in known:
            edges = _unique_edges(
                (*edges, GraphEdge("system:zekam", runtime_root, "observes-runtime"))
            )
        if "client:opencode" in known and "system:zekam" in known:
            edges = _unique_edges(
                (*edges, GraphEdge("system:zekam", "client:opencode", "observes-client"))
            )

        graph = DerivedGraph(
            nodes=nodes,
            edges=edges,
            source_digest=digest(
                {
                    "repository": repository.source_digest,
                    "runtime": runtime.source_digest,
                    "clients": clients.source_digest,
                }
            ),
        )
        dashboard = OperationsDashboard(generated_at=generated_at, tiles=runtime.tiles)
        return ObservatorySnapshot(
            generated_at=generated_at,
            dashboard=dashboard,
            graph=graph,
            agents=(clients.agents + runtime.agents)[:MAX_AGENTS],
            events=tuple(
                sorted(
                    clients.events + runtime.events,
                    key=lambda item: item.occurred_at,
                    reverse=True,
                )[:MAX_EVENTS]
            ),
            reports=repository.reports[:MAX_REPORTS],
            runtime_available=runtime.available,
            runtime_detail=runtime.detail,
        )

    def _repository_projection(self) -> RepositoryProjection:
        observed_at = time.monotonic()
        cached = self._repository_cache
        if (
            cached is not None
            and observed_at - self._repository_cache_at < self.repository_refresh_seconds
        ):
            return cached
        try:
            projection = scan_repository(self.core_path)
        except OSError:
            if cached is not None:
                return cached
            projection = empty_repository_projection()
        self._repository_cache = projection
        self._repository_cache_at = observed_at
        return projection

    def _safe_runtime_projection(self) -> RuntimeProjection:
        return self._safe_projection(self.runtime_reader, "runtime-read-failed")

    @staticmethod
    def _safe_projection(reader: RuntimeProjectionReader, prefix: str) -> RuntimeProjection:
        try:
            return reader.read()
        except Exception as exc:
            return unavailable_runtime_projection(f"{prefix}:{type(exc).__name__}")


def unavailable_runtime_projection(detail: str) -> RuntimeProjection:
    now = dt.datetime.now(dt.UTC)
    tiles = tuple(
        ProjectionTile(
            key=key,
            title=_tile_title(key),
            value=0,
            drill_down=_tile_drill_down(key),
            detail=detail,
        )
        for key in REQUIRED_TILES
    )
    return RuntimeProjection(
        generated_at=now,
        tiles=tiles,
        source_digest=digest({"available": False, "detail": detail}),
        available=False,
        detail=detail,
    )


def empty_repository_projection() -> RepositoryProjection:
    node = GraphNode(
        node_id="system:zekam",
        kind="system",
        label="Zekam",
        canonical_ref="core:README.md",
    )
    return RepositoryProjection(
        nodes=(node,),
        edges=(),
        reports=(),
        source_digest=digest({"nodes": [node.as_dict()], "edges": []}),
    )


def scan_repository(core_path: Path) -> RepositoryProjection:
    """Build an Obsidian-like derived graph from bounded Markdown metadata.

    Only repository-relative paths, a sanitized first heading and Markdown link
    targets are read.  Document bodies are not returned by this projection.
    """

    root = core_path.resolve()
    candidates: list[Path] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories[:] = [name for name in directories if name not in _EXCLUDED_PARTS]
        current_path = Path(current)
        candidates.extend(
            path
            for name in files
            if name.lower().endswith(".md") and _is_safe_markdown(path := current_path / name, root)
        )
    candidates.sort(key=lambda path: _document_priority(path.relative_to(root)))
    selected = candidates[:MAX_DOCUMENT_NODES]
    selected_paths = {path.resolve() for path in selected}

    nodes: list[GraphNode] = [
        GraphNode(
            node_id="system:zekam",
            kind="system",
            label="Zekam",
            canonical_ref="core:README.md",
        )
    ]
    edges: list[GraphEdge] = []
    reports: list[ObservatoryReport] = []
    category_nodes: dict[str, str] = {}
    path_to_id: dict[Path, str] = {}
    link_targets: dict[Path, tuple[Path, ...]] = {}

    for path in selected:
        relative = path.relative_to(root).as_posix()
        node_id = f"doc:{_short_id(relative)}"
        path_to_id[path.resolve()] = node_id
        text = _read_prefix(path)
        title = _safe_title(text, path.stem)
        category = _document_category(Path(relative))
        category_id = category_nodes.get(category)
        if category_id is None:
            category_id = f"cluster:docs:{category}"
            category_nodes[category] = category_id
            nodes.append(
                GraphNode(
                    node_id=category_id,
                    kind="document-cluster",
                    label=_category_title(category),
                    canonical_ref=f"core:README.md#ui-{category}",
                )
            )
            edges.append(GraphEdge("system:zekam", category_id, "documents"))

        nodes.append(
            GraphNode(
                node_id=node_id,
                kind="report" if _is_report(Path(relative)) else "document",
                label=title,
                canonical_ref=f"core:{relative}",
            )
        )
        edges.append(GraphEdge(category_id, node_id, "contains-document"))
        link_targets[path.resolve()] = _markdown_targets(path, text, root)

        if _is_report(Path(relative)):
            try:
                modified_at = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
            except OSError:
                continue
            reports.append(
                ObservatoryReport(
                    report_id=f"report:{_short_id(relative)}",
                    title=title,
                    relative_path=relative,
                    modified_at=modified_at,
                    canonical_ref=f"core:{relative}",
                )
            )

    for source, targets in link_targets.items():
        source_id = path_to_id[source]
        for target in targets:
            if len(edges) >= MAX_DOCUMENT_EDGES:
                break
            resolved = target.resolve()
            if resolved not in selected_paths:
                continue
            target_id = path_to_id.get(resolved)
            if target_id is None or target_id == source_id:
                continue
            edges.append(GraphEdge(source_id, target_id, "markdown-link"))

    reports.sort(key=lambda item: item.modified_at, reverse=True)
    node_tuple = _unique_nodes(tuple(nodes))
    known = {item.node_id for item in node_tuple}
    edge_tuple = _unique_edges(
        tuple(item for item in edges if item.source in known and item.target in known)
    )
    return RepositoryProjection(
        nodes=node_tuple,
        edges=edge_tuple,
        reports=tuple(reports[:MAX_REPORTS]),
        source_digest=digest(
            {
                "nodes": [item.as_dict() for item in node_tuple],
                "edges": [item.as_dict() for item in edge_tuple],
            }
        ),
    )


def _read_prefix(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_MARKDOWN_BYTES)
    except OSError:
        return ""


def _parse_timestamp(value: Any) -> dt.datetime:
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(dt.UTC)
        except ValueError:
            pass
    return dt.datetime.now(dt.UTC)


def sanitize_observatory_label(
    value: str,
    *,
    fallback: str = "Kayıt",
    limit: int = MAX_LABEL_CHARS,
) -> str:
    """Return a bounded label that cannot carry obvious secret/path material."""

    def clean(candidate: str) -> str:
        candidate = re.sub(r"[`*_{}\[\]]", "", candidate)
        candidate = "".join(character for character in candidate if character.isprintable())
        return re.sub(r"\s+", " ", candidate).strip()

    candidate = clean(value)
    if not candidate or _UNSAFE_LABEL.search(candidate):
        candidate = clean(fallback)
    if not candidate or _UNSAFE_LABEL.search(candidate):
        candidate = "Kayıt"
    return candidate[:limit]


def _safe_title(text: str, fallback: str) -> str:
    match = _HEADING.search(text)
    candidate = match.group(1).strip() if match else fallback.replace("_", " ").strip()
    return sanitize_observatory_label(
        candidate,
        fallback=fallback.replace("_", " "),
        limit=MAX_LABEL_CHARS,
    )


def _markdown_targets(source: Path, text: str, root: Path) -> tuple[Path, ...]:
    targets: list[Path] = []
    raw_targets = list(_MARKDOWN_LINK.findall(text))
    raw_targets.extend(_WIKI_LINK.findall(text))
    for raw in raw_targets:
        relative = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if not relative or "://" in relative or relative.startswith("/"):
            continue
        relative_path = Path(relative)
        if relative_path.suffix == "":
            relative_path = relative_path.with_suffix(".md")
        if relative_path.suffix.lower() != ".md":
            continue
        candidates = (source.parent / relative_path, root / relative_path)
        for unresolved in candidates:
            candidate = unresolved.resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if not candidate.is_file():
                continue
            targets.append(candidate)
            break
    return tuple(targets)


def _document_priority(relative: Path) -> tuple[int, int, str]:
    name = relative.name.upper()
    pinned = {
        "README.MD": 0,
        "00_BASLA.MD": 1,
        "AKTIF_GOREV.YAML": 2,
        "SURUM_RAPORU.MD": 3,
    }
    if name in pinned:
        return (0, pinned[name], relative.as_posix())
    if _is_report(relative):
        return (1, len(relative.parts), relative.as_posix())
    return (2, len(relative.parts), relative.as_posix())


def _document_category(relative: Path) -> str:
    folded = "/".join(part.casefold() for part in relative.parts)
    if _is_report(relative):
        return "reports"
    if "mimari" in folded or "architecture" in folded:
        return "architecture"
    if "operasyon" in folded or "runbook" in folded:
        return "operations"
    if any(token in folded for token in ("bellek", "knowledge", "rag", "vektor", "obsidian")):
        return "knowledge"
    if any(token in folded for token in ("guvenlik", "harness", "model", "veri", "sozlesme")):
        return "contracts"
    if relative.parent.as_posix() == ".":
        return "core"
    return "docs"


def _category_title(category: str) -> str:
    return {
        "reports": "Raporlar ve Kanıt",
        "architecture": "Mimari",
        "operations": "Operasyon",
        "knowledge": "Bilgi ve Bellek",
        "contracts": "Sözleşmeler",
        "core": "Kanonik Çekirdek",
        "docs": "Belgeler",
    }.get(category, category.replace("-", " ").title())


def _is_report(relative: Path) -> bool:
    upper = relative.as_posix().upper()
    return any(hint in upper for hint in _REPORT_HINTS)


def _is_excluded(relative: Path) -> bool:
    return any(part in _EXCLUDED_PARTS for part in relative.parts)


def _is_safe_markdown(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError):
        return False
    if _UNSAFE_LABEL.search(relative.as_posix()):
        return False
    return path.is_file() and not path.is_symlink()


def _short_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _tile_title(key: str) -> str:
    return {
        "work": "Aktif İşler",
        "run": "Çalışan Run",
        "model": "Modeller",
        "knowledge": "Bilgi Kaynakları",
        "memory": "Aktif Bellek",
        "scheduler": "Zamanlayıcı",
    }[key]


def _tile_drill_down(key: str) -> str:
    return {
        "work": "db:work.work_item?state=open",
        "run": "db:runtime.job?state=active",
        "model": "db:models.model_inventory?enabled=true",
        "knowledge": "db:knowledge.source_version?state=active",
        "memory": "db:memory.record?state=active",
        "scheduler": "db:ops.job_definition?state=active",
    }[key]


def _unique_nodes(items: Iterable[GraphNode]) -> tuple[GraphNode, ...]:
    unique: dict[str, GraphNode] = {}
    for item in items:
        unique.setdefault(item.node_id, item)
    return tuple(unique.values())


def _unique_edges(items: Iterable[GraphEdge]) -> tuple[GraphEdge, ...]:
    unique: dict[tuple[str, str, str], GraphEdge] = {}
    for item in items:
        unique.setdefault((item.source, item.target, item.kind), item)
    return tuple(unique.values())


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def runtime_projection_digest(material: Mapping[str, Any]) -> str:
    """Public helper for infrastructure readers to bind their source snapshot."""

    return digest(material)
