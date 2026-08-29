from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from zekam.application.observatory import (
    CompositeRuntimeProjectionReader,
    LocalSessionFileProjectionReader,
    ObservatoryService,
    OpenCodeLifecycleProjectionReader,
    sanitize_observatory_label,
    scan_repository,
)
from zekam.application.opencode_lifecycle import record_event
from zekam.domain.observability import REQUIRED_TILES
from zekam.domain.process_observation import (
    ObservedClient,
    ProcessIdentity,
    ProcessObservation,
    ProcessObservationSnapshot,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_markdown_projection_is_bounded_linked_and_repository_relative(tmp_path: Path) -> None:
    root = tmp_path / "core"
    _write(
        root / "README.md",
        "# Zekam\n\n[Architecture](docs/ARCH.md)\n[[docs/DECISION]]\n",
    )
    _write(root / "docs" / "ARCH.md", "# Architecture\n\n[Report](../SURUM_RAPORU.md)\n")
    _write(root / "docs" / "DECISION.md", "# Decision\n")
    _write(root / "SURUM_RAPORU.md", "# Release report\n")
    _write(root / "yerel-referanslar" / "PRIVATE.md", "# Should not appear\n")

    projection = scan_repository(root)
    node_refs = {node.canonical_ref for node in projection.nodes}

    assert "core:README.md" in node_refs
    assert "core:docs/ARCH.md" in node_refs
    assert "core:docs/DECISION.md" in node_refs
    assert "core:SURUM_RAPORU.md" in node_refs
    assert all("yerel-referanslar" not in reference for reference in node_refs)
    document_links = [edge for edge in projection.edges if edge.kind == "markdown-link"]
    assert len(document_links) >= 3
    assert projection.reports[0].relative_path == "SURUM_RAPORU.md"
    assert all(not Path(reference.removeprefix("core:")).is_absolute() for reference in node_refs)


def test_unsafe_heading_falls_back_to_filename_and_body_is_not_exposed(tmp_path: Path) -> None:
    root = tmp_path / "core"
    _write(root / "README.md", "# Zekam\n")
    _write(
        root / "SECRET_NOTE.md",
        "# password=super-secret\n\nThis body must never be copied into the snapshot.\n",
    )

    document = ObservatoryService(root).snapshot().as_dict()
    encoded = json.dumps(document, ensure_ascii=False)

    assert "password=super-secret" not in encoded
    assert "This body must never be copied" not in encoded
    assert "SECRET NOTE" in encoded
    assert document["read_only"] is True
    assert document["grants_authority"] is False
    assert document["graph"]["derived"] is True
    assert document["schema"] == "zekam-observatory-snapshot/v2"
    assert document["causal"]["schema"] == "zekam-causal-projection/v1"
    assert document["causal"]["grants_authority"] is False
    schema_path = Path("schemas/observatory_snapshot.schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert set(schema["required"]) <= set(document)
    assert set(schema["properties"]["causal"]["required"]) <= set(document["causal"])


def test_unconfigured_runtime_keeps_six_required_tiles(tmp_path: Path) -> None:
    root = tmp_path / "core"
    _write(root / "README.md", "# Zekam\n")

    snapshot = ObservatoryService(root).snapshot()
    keys = {tile.key for tile in snapshot.dashboard.tiles}

    assert keys == set(REQUIRED_TILES)
    assert snapshot.runtime_available is False
    assert snapshot.runtime_detail == "realm-id-required"
    assert all(tile.value == 0 for tile in snapshot.dashboard.tiles)


def test_runtime_failure_is_sanitized(tmp_path: Path) -> None:
    root = tmp_path / "core"
    _write(root / "README.md", "# Zekam\n")

    class BrokenReader:
        def read(self) -> object:
            raise RuntimeError("password=must-not-leak")

    service = ObservatoryService(
        root,
        runtime_reader=BrokenReader(),  # type: ignore[arg-type]
    )
    snapshot = service.snapshot()
    encoded = json.dumps(snapshot.as_dict(), ensure_ascii=False)

    assert snapshot.runtime_detail == "runtime-read-failed:RuntimeError"
    assert "must-not-leak" not in encoded


def test_projection_label_sanitizer_rejects_secret_and_absolute_path_material() -> None:
    assert sanitize_observatory_label("password=super-secret", fallback="iş") == "iş"
    assert sanitize_observatory_label("/home/mehmet/private", fallback="kayıt") == "kayıt"
    assert sanitize_observatory_label("  güvenli   etiket  ") == "güvenli etiket"


def test_repository_graph_is_cached_between_runtime_polls(tmp_path: Path) -> None:
    root = tmp_path / "core"
    _write(root / "README.md", "# Zekam\n")
    service = ObservatoryService(root, repository_refresh_seconds=60)

    first = service.snapshot()
    _write(root / "NEW_REPORT.md", "# New report\n")
    second = service.snapshot()
    refreshed = ObservatoryService(root, repository_refresh_seconds=60).snapshot()

    assert first.graph.source_digest == second.graph.source_digest
    assert all(report.relative_path != "NEW_REPORT.md" for report in second.reports)
    assert any(report.relative_path == "NEW_REPORT.md" for report in refreshed.reports)


def test_opencode_lifecycle_is_visible_without_postgresql(tmp_path: Path) -> None:
    root = tmp_path / "core"
    home = tmp_path / "home"
    _write(root / "README.md", "# Zekam\n")
    record_event(
        home,
        event_type="session.status",
        session_id="ses_parent",
        agent="zekam-coordinator",
        model_ref="provider/coordinator",
        status="busy",
    )
    record_event(
        home,
        event_type="tool.execute.before",
        session_id="ses_live",
        parent_session_id="ses_parent",
        agent="zekam-builder",
        model_ref="provider/model",
        tool="bash",
        task_label="Sky 11267 task detaylarini al",
    )

    snapshot = ObservatoryService(
        root,
        client_reader=OpenCodeLifecycleProjectionReader(home),
    ).snapshot()

    assert any(agent.client == "opencode" for agent in snapshot.agents)
    assert any(agent.state == "recent" for agent in snapshot.agents)
    assert any(agent.active_tool == "bash" for agent in snapshot.agents)
    assert all("Sky 11267" not in (agent.task_label or "") for agent in snapshot.agents)
    assert any(event.source == "opencode" for event in snapshot.events)
    assert any(event.event_type == "tool.execute.before · bash" for event in snapshot.events)
    assert any(node.node_id == "client:opencode" for node in snapshot.graph.nodes)
    assert any(edge.kind == "delegates" for edge in snapshot.graph.edges)
    assert any(
        edge.source == "client:opencode"
        and edge.target == "system:zekam"
        and edge.kind == "reports-observation"
        for edge in snapshot.graph.edges
    )
    tiles = {tile.key: tile.value for tile in snapshot.dashboard.tiles}
    assert tiles["work"] == 0
    assert tiles["run"] == 0
    assert tiles["model"] == 0


def test_opencode_prompt_derived_title_is_not_exposed_from_session_metadata(tmp_path: Path) -> None:
    root = tmp_path / "core"
    home = tmp_path / "home"
    database = tmp_path / "opencode.db"
    _write(root / "README.md", "# Zekam\n")
    record_event(home, event_type="session.status", session_id="ses_sky", status="busy")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "create table session (id text primary key, title text, agent text, model text, "
            "time_created integer, time_updated integer)"
        )
        connection.execute(
            "insert into session values (?, ?, ?, ?, ?, ?)",
            (
                "ses_sky",
                "Sky 11267 task details",
                "zekam-coordinator",
                '{"providerID":"litellm","id":"Qwen/Qwen3.5-27B-FP8"}',
                1787437970556,
                1787438343171,
            ),
        )

    snapshot = ObservatoryService(
        root,
        client_reader=OpenCodeLifecycleProjectionReader(home, metadata_path=database),
    ).snapshot()

    session = next(agent for agent in snapshot.agents if agent.client == "opencode")
    assert session.task_label is not None
    assert session.task_label.startswith("OpenCode session ")
    assert "Sky 11267" not in json.dumps(snapshot.as_dict(), ensure_ascii=False)
    assert session.model_ref == "litellm/Qwen/Qwen3.5-27B-FP8"


def test_codex_and_claude_file_heartbeats_are_client_scoped(tmp_path: Path) -> None:
    root = tmp_path / "core"
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    _write(root / "README.md", "# Zekam\n")
    _write(codex_root / "rollout-01a02b31-a697-7553-8a72-c5ba348997a2.jsonl", "")
    _write(claude_root / "b463564c-3715-4532-8130-609a54c5d9b0.jsonl", "")

    reader = CompositeRuntimeProjectionReader(
        (
            LocalSessionFileProjectionReader("codex", codex_root),
            LocalSessionFileProjectionReader("claude", claude_root),
        )
    )
    snapshot = ObservatoryService(root, client_reader=reader).snapshot()

    clients = {agent.client for agent in snapshot.agents}
    assert clients == {"codex", "claude"}
    assert all(agent.state == "recent" for agent in snapshot.agents)
    assert {event.source for event in snapshot.events} == {"codex", "claude"}


def test_client_without_observed_session_does_not_create_graph_node(tmp_path: Path) -> None:
    projection = LocalSessionFileProjectionReader("claude", tmp_path / "missing").read()

    assert projection.available is False
    assert projection.nodes == ()
    assert projection.agents == ()


def test_fresh_session_without_os_process_is_stale_not_open_cli(tmp_path: Path) -> None:
    root = tmp_path / "core"
    codex_root = tmp_path / "codex"
    _write(root / "README.md", "# Zekam\n")
    _write(codex_root / "rollout-01a02b31-a697-7553-8a72-c5ba348997a2.jsonl", "")

    class NoProcesses:
        def read(self) -> ProcessObservationSnapshot:
            return ProcessObservationSnapshot(dt.datetime.now(dt.UTC), (), True, "os-process-scan")

    reader = CompositeRuntimeProjectionReader(
        (LocalSessionFileProjectionReader("codex", codex_root),),
        process_reader=NoProcesses(),
    )
    snapshot = ObservatoryService(root, client_reader=reader).snapshot()

    assert snapshot.agents[0].availability == "stale"
    assert snapshot.agents[0].process_id is None
    tiles = {tile.key: tile.value for tile in snapshot.dashboard.tiles}
    assert tiles["work"] == 0


def test_process_and_session_are_heuristically_bound_without_claiming_canonical_ownership(
    tmp_path: Path,
) -> None:
    root = tmp_path / "core"
    codex_root = tmp_path / "codex"
    _write(root / "README.md", "# Zekam\n")
    _write(codex_root / "rollout-01a02b31-a697-7553-8a72-c5ba348997a2.jsonl", "")
    now = dt.datetime.now(dt.UTC)

    class OneProcess:
        def read(self) -> ProcessObservationSnapshot:
            process = ProcessObservation(
                identity=ProcessIdentity(4242, 1_700_000_000_000_000),
                parent_pid=1,
                client=ObservedClient.CODEX,
                executable="codex.exe",
                status="running",
                started_at=now,
                cpu_percent=2.0,
                rss_bytes=8192,
            )
            return ProcessObservationSnapshot(now, (process,), True, "os-process-scan")

    reader = CompositeRuntimeProjectionReader(
        (LocalSessionFileProjectionReader("codex", codex_root),),
        process_reader=OneProcess(),
    )
    snapshot = ObservatoryService(root, client_reader=reader).snapshot()
    session = next(agent for agent in snapshot.agents if agent.client == "codex")

    assert session.availability == "live"
    assert session.binding_confidence == "heuristic"
    assert session.process_id == "process:4242:1700000000000000"
    assert session.canonical_ref.startswith("runtime:codex-sessions/")
    assert any(edge.kind == "heuristic-session-bind" for edge in snapshot.graph.edges)


def test_open_process_without_session_is_unbound(tmp_path: Path) -> None:
    root = tmp_path / "core"
    _write(root / "README.md", "# Zekam\n")
    now = dt.datetime.now(dt.UTC)

    class OneProcess:
        def read(self) -> ProcessObservationSnapshot:
            process = ProcessObservation(
                identity=ProcessIdentity(5151, 1_700_000_000_000_000),
                parent_pid=1,
                client=ObservedClient.CLAUDE,
                executable="claude.exe",
                status="sleeping",
                started_at=now,
            )
            return ProcessObservationSnapshot(now, (process,), True, "os-process-scan")

    reader = CompositeRuntimeProjectionReader((), process_reader=OneProcess())
    snapshot = ObservatoryService(root, client_reader=reader).snapshot()

    assert snapshot.agents[0].state == "unbound"
    assert snapshot.agents[0].binding_confidence == "unbound"
    assert snapshot.agents[0].session_id is None
