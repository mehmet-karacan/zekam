"""Read-only observatory projections for the Zekam UI.

The observatory is deliberately a *projection*: canonical authority remains in the
Work Graph, runtime receipt/claim records and the other bounded contexts.  This
module never grants authority and never reads prompt/model-response payloads.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from zekam.application.opencode_lifecycle import recent_events, resume_projection
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
_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f-]{27,36}", re.IGNORECASE)


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
    task_label: str | None = None
    model_ref: str | None = None
    current_tool: str | None = None
    active_tool: str | None = None
    parent_agent_id: str | None = None
    started_at: dt.datetime | None = None

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
            "task_label": self.task_label,
            "model_ref": self.model_ref,
            "current_tool": self.current_tool,
            "active_tool": self.active_tool,
            "parent_agent_id": self.parent_agent_id,
            "started_at": _iso(self.started_at),
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
    metadata_path: Path | None = None

    def read(self) -> RuntimeProjection:
        projection = resume_projection(self.home, limit=MAX_AGENTS)
        sessions = tuple(item for item in projection["sessions"] if item.get("status") != "closed")
        metadata = _opencode_session_metadata(
            self.metadata_path,
            tuple(str(item["session_id"]) for item in sessions),
        )
        root = GraphNode(
            node_id="client:opencode",
            kind="client",
            label="OpenCode",
            canonical_ref="runtime:opencode-lifecycle",
        )
        nodes: list[GraphNode] = [root] if sessions else []
        edges: list[GraphEdge] = []
        agents: list[ObservatoryAgent] = []
        events: list[ObservatoryEvent] = []
        session_nodes = {
            str(item["session_id"]): f"opencode-session:{_short_id(str(item['session_id']))}"
            for item in sessions
        }
        for item in sessions:
            session_id = str(item["session_id"])
            session_metadata = metadata.get(session_id, {})
            node_id = session_nodes[session_id]
            agent_name = str(
                item.get("agent") or session_metadata.get("agent") or "OpenCode session"
            )
            model_ref = item.get("model_ref") or session_metadata.get("model_ref")
            occurred_at = _parse_timestamp(item.get("updated_at"))
            if session_metadata.get("updated_at"):
                occurred_at = max(
                    occurred_at,
                    _parse_timestamp(session_metadata["updated_at"]),
                )
            started_at = _parse_timestamp(
                session_metadata.get("created_at") or item.get("created_at")
            )
            age = dt.datetime.now(dt.UTC) - occurred_at
            observed_state = str(item.get("status") or "unknown")
            if age <= dt.timedelta(seconds=45) and observed_state in {
                "active",
                "busy",
                "claimed",
                "executing",
                "in_progress",
                "running",
            }:
                observed_state = "active"
            canonical_ref = f"runtime:opencode-lifecycle/{session_id}"
            task_label = sanitize_observatory_label(
                str(item.get("task_label") or session_metadata.get("title") or agent_name),
                fallback="OpenCode session",
            )
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    kind="agent-session",
                    label=task_label,
                    canonical_ref=canonical_ref,
                )
            )
            parent_id = item.get("parent_session_id")
            parent_node = session_nodes.get(str(parent_id)) if parent_id else None
            edges.append(
                GraphEdge(
                    parent_node or "client:opencode",
                    node_id,
                    "delegates" if parent_node else "runs-session",
                )
            )
            agents.append(
                ObservatoryAgent(
                    agent_id=node_id,
                    label=task_label,
                    client="opencode",
                    state=observed_state,
                    canonical_ref=canonical_ref,
                    heartbeat_at=occurred_at,
                    task_label=task_label,
                    model_ref=None if model_ref is None else str(model_ref),
                    current_tool=None if item.get("last_tool") is None else str(item["last_tool"]),
                    active_tool=(
                        None if item.get("active_tool") is None else str(item["active_tool"])
                    ),
                    parent_agent_id=(
                        None
                        if item.get("parent_session_id") is None
                        else f"opencode-session:{_short_id(str(item['parent_session_id']))}"
                    ),
                    started_at=started_at,
                )
            )
        for item in recent_events(self.home, limit=MAX_EVENTS):
            session_id = str(item["session_id"])
            event_type = str(item.get("event_type") or "session.status")
            if item.get("tool"):
                event_type = f"{event_type} · {item['tool']}"
            events.append(
                ObservatoryEvent(
                    event_id=f"opencode-event:{item['event_id']}",
                    event_type=event_type,
                    source="opencode",
                    occurred_at=_parse_timestamp(item.get("occurred_at")),
                    canonical_ref=f"runtime:opencode-lifecycle/{session_id}",
                    agent_id=f"opencode-session:{_short_id(session_id)}",
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
class LocalSessionFileProjectionReader:
    """Observe Codex/Claude session file heartbeats without reading conversation content."""

    client: str
    root: Path
    index_path: Path | None = None
    active_seconds: int = 45
    recent_seconds: int = 300

    def read(self) -> RuntimeProjection:
        now = dt.datetime.now(dt.UTC)
        titles = _session_title_index(self.index_path)
        candidates: list[tuple[Path, dt.datetime]] = []
        if self.root.is_dir():
            for path in self.root.rglob("*.jsonl"):
                if "tool-results" in path.parts:
                    continue
                try:
                    updated_at = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
                except OSError:
                    continue
                if (now - updated_at).total_seconds() <= self.recent_seconds:
                    candidates.append((path, updated_at))
        candidates.sort(key=lambda item: item[1], reverse=True)
        nodes: list[GraphNode] = []
        if candidates:
            nodes.append(
                GraphNode(
                    node_id=f"client:{self.client}",
                    kind="client",
                    label=self.client.title(),
                    canonical_ref=f"runtime:{self.client}-sessions",
                )
            )
        edges: list[GraphEdge] = []
        agents: list[ObservatoryAgent] = []
        events: list[ObservatoryEvent] = []
        for path, updated_at in candidates[:8]:
            session_id = _session_id_from_path(path)
            node_id = f"{self.client}-session:{_short_id(session_id)}"
            title = titles.get(session_id, f"{self.client.title()} session {session_id[-8:]}")
            label = sanitize_observatory_label(title, fallback=f"{self.client.title()} session")
            age_seconds = (now - updated_at).total_seconds()
            state = "active" if age_seconds <= self.active_seconds else "checkpointed"
            canonical_ref = f"runtime:{self.client}-sessions/{session_id}"
            nodes.append(GraphNode(node_id, "agent-session", label, canonical_ref))
            edges.append(GraphEdge(f"client:{self.client}", node_id, "runs-session"))
            agents.append(
                ObservatoryAgent(
                    agent_id=node_id,
                    label=label,
                    client=self.client,
                    state=state,
                    canonical_ref=canonical_ref,
                    heartbeat_at=updated_at,
                    task_label=label,
                    started_at=updated_at,
                )
            )
            events.append(
                ObservatoryEvent(
                    event_id=(
                        f"{self.client}-event:{_short_id(session_id + updated_at.isoformat())}"
                    ),
                    event_type="session.activity",
                    source=self.client,
                    occurred_at=updated_at,
                    canonical_ref=canonical_ref,
                    agent_id=node_id,
                )
            )
        return RuntimeProjection(
            generated_at=now,
            tiles=unavailable_runtime_projection(f"{self.client}-sessions").tiles,
            nodes=tuple(nodes),
            edges=tuple(edges),
            agents=tuple(agents),
            events=tuple(events),
            source_digest=digest(
                {"client": self.client, "sessions": [agent.as_dict() for agent in agents]}
            ),
            available=bool(agents),
            detail=f"{self.client}-sessions",
        )


@dataclass(frozen=True, slots=True)
class CompositeRuntimeProjectionReader:
    readers: tuple[RuntimeProjectionReader, ...]

    def read(self) -> RuntimeProjection:
        projections = tuple(reader.read() for reader in self.readers)
        return RuntimeProjection(
            generated_at=dt.datetime.now(dt.UTC),
            tiles=unavailable_runtime_projection("local-client-sessions").tiles,
            nodes=_unique_nodes(tuple(node for item in projections for node in item.nodes)),
            edges=_unique_edges(tuple(edge for item in projections for edge in item.edges)),
            agents=tuple(agent for item in projections for agent in item.agents)[:MAX_AGENTS],
            events=tuple(
                sorted(
                    (event for item in projections for event in item.events),
                    key=lambda event: event.occurred_at,
                    reverse=True,
                )[:MAX_EVENTS]
            ),
            source_digest=digest([item.source_digest for item in projections]),
            available=any(item.available for item in projections),
            detail="local-client-sessions",
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
        if "system:zekam" in known:
            client_edges = tuple(
                GraphEdge(node_id, "system:zekam", "reports-observation")
                for node_id in ("client:opencode", "client:codex", "client:claude")
                if node_id in known
            )
            edges = _unique_edges((*edges, *client_edges))

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
        dashboard_tiles = runtime.tiles
        if not runtime.available and clients.available:
            dashboard_tiles = _client_observation_tiles(runtime.tiles, clients.agents)
        dashboard = OperationsDashboard(generated_at=generated_at, tiles=dashboard_tiles)
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


def _client_observation_tiles(
    tiles: tuple[ProjectionTile, ...],
    agents: tuple[ObservatoryAgent, ...],
) -> tuple[ProjectionTile, ...]:
    active_states = {"active", "running", "claimed", "executing", "in_progress"}
    active = tuple(agent for agent in agents if agent.state in active_states)
    observed = {
        "work": sum(agent.parent_agent_id is None for agent in active),
        "run": len(active),
        "model": len({agent.model_ref for agent in active if agent.model_ref}),
    }
    return tuple(
        ProjectionTile(
            key=tile.key,
            title=tile.title,
            value=observed.get(tile.key, tile.value),
            drill_down=tile.drill_down,
            detail=(
                "istemci gozlemi · authority degil"
                if tile.key in observed
                else "canli realm baglanmadi"
            ),
        )
        for tile in tiles
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


def _opencode_session_metadata(
    database: Path | None,
    session_ids: tuple[str, ...],
) -> dict[str, dict[str, str]]:
    if database is None or not database.is_file() or not session_ids:
        return {}
    placeholders = ",".join("?" for _ in session_ids)
    uri = f"file:{database.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=0.2) as connection:
            connection.execute("pragma query_only = on")
            rows = connection.execute(
                "select id, title, agent, model, time_created, time_updated "
                f"from session where id in ({placeholders})",
                session_ids,
            ).fetchall()
    except sqlite3.Error:
        return {}
    result: dict[str, dict[str, str]] = {}
    for session_id, title, agent, model, created_at, updated_at in rows:
        model_ref = ""
        try:
            document = json.loads(str(model))
            model_ref = f"{document.get('providerID', '')}/{document.get('id', '')}".strip("/")
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        result[str(session_id)] = {
            "title": sanitize_observatory_label(str(title), fallback="OpenCode session"),
            "agent": sanitize_observatory_label(str(agent), fallback="OpenCode session"),
            "model_ref": model_ref,
            "created_at": dt.datetime.fromtimestamp(int(created_at) / 1000, tz=dt.UTC).isoformat(),
            "updated_at": dt.datetime.fromtimestamp(int(updated_at) / 1000, tz=dt.UTC).isoformat(),
        }
    return result


def _session_id_from_path(path: Path) -> str:
    matches = _SESSION_ID.findall(path.stem)
    return matches[-1] if matches else path.stem[-80:]


def _session_title_index(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return {}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    titles: dict[str, str] = {}
    for line in lines[-2000:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = item.get("id")
        title = item.get("thread_name")
        if isinstance(session_id, str) and isinstance(title, str):
            titles[session_id] = sanitize_observatory_label(title, fallback="Session")
    return titles


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
