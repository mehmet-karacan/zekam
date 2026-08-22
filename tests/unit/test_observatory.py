from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from zekam.application.observatory import (
    ObservatoryService,
    OpenCodeLifecycleProjectionReader,
    sanitize_observatory_label,
    scan_repository,
)
from zekam.application.opencode_lifecycle import record_event
from zekam.domain.observability import REQUIRED_TILES


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
        event_type="tool.execute.before",
        session_id="ses_live",
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
    assert any(agent.state == "interrupted" for agent in snapshot.agents)
    assert any(agent.task_label == "Sky 11267 task detaylarini al" for agent in snapshot.agents)
    assert any(event.source == "opencode" for event in snapshot.events)
    assert any(event.event_type == "tool.execute.before · bash" for event in snapshot.events)
    assert any(node.node_id == "client:opencode" for node in snapshot.graph.nodes)


def test_opencode_title_is_backfilled_from_read_only_session_metadata(tmp_path: Path) -> None:
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
    assert session.task_label == "Sky 11267 task details"
    assert session.model_ref == "litellm/Qwen/Qwen3.5-27B-FP8"
