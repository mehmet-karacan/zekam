"""SQLite code graph store schema, atomicity, recovery and security tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application.code_graph import (
    apply_graph_build,
    plan_graph_build,
)
from zekam.application.code_graph_python import PythonAstExtractor
from zekam.domain.canonical import digest
from zekam.domain.errors import (
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.infrastructure.sqlite.code_graph import SQLiteCodeGraphStore

pytestmark = pytest.mark.unit

_CREATED_AT = "2026-09-21T00:00:00Z"


def _pipeline(
    tmp_path: Path,
    files: dict[str, str],
    *,
    project_id: str = "proj",
    slug: str = "proj",
    revision: str = "r1",
) -> tuple[SQLiteCodeGraphStore, Path]:
    source_root = tmp_path / "srcroot"
    source_root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = source_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    store_path = tmp_path / "graph.sqlite3"
    store = SQLiteCodeGraphStore(store_path, create=True)
    extractor = PythonAstExtractor()
    plan = plan_graph_build(
        source_root,
        project_id=project_id,
        project_slug=slug,
        source_revision=revision,
        extractor=extractor,
        created_at=_CREATED_AT,
    )
    apply_graph_build(store, extractor, source_root, plan)
    return store, source_root


def _build_with(
    store: SQLiteCodeGraphStore,
    source_root: Path,
    *,
    project_id: str = "proj",
    revision: str = "r1",
) -> str:
    extractor = PythonAstExtractor()
    plan = plan_graph_build(
        source_root,
        project_id=project_id,
        project_slug="proj",
        source_revision=revision,
        extractor=extractor,
        created_at=_CREATED_AT,
    )
    return apply_graph_build(store, extractor, source_root, plan).generation_digest


def test_schema_and_integrity(tmp_path: Path) -> None:
    store, _ = _pipeline(tmp_path, {"mod.py": "def f():\n    return 1\n"})
    try:
        generation = store.generation("proj")
        assert generation.state == "ready"
        assert generation.file_count == 1
        assert generation.symbol_count >= 2  # module + function
        assert store.integrity()["status"] == "passed"
        assert store.integrity()["foreign_key_check"] == "passed"
    finally:
        store.close()


def test_required_tables_exist(tmp_path: Path) -> None:
    store, _ = _pipeline(tmp_path, {"mod.py": "x = 1\n"})
    try:
        rows = store._connection.execute(
            "select name from sqlite_master where type in ('table','view')"
        ).fetchall()
        names = {str(row[0]) for row in rows}
        for required in (
            "metadata",
            "graph_generation",
            "current_graph_generation",
            "graph_file",
            "graph_symbol",
            "graph_edge",
            "graph_file_fts",
            "graph_chunk_link",
            "graph_annotation",
        ):
            assert required in names
    finally:
        store.close()


def test_atomic_supersede_current_pointer(tmp_path: Path) -> None:
    store, source_root = _pipeline(tmp_path, {"mod.py": "def f():\n    return 1\n"})
    try:
        first = store.generation("proj").generation_digest
        # Edit content -> new generation, previous becomes superseded.
        (source_root / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        second_gen = _build_with(store, source_root)
        assert second_gen != first
        current = store.generation("proj")
        assert current.generation_digest == second_gen
        row = store._connection.execute(
            "select state from graph_generation where generation_digest=?", (first,)
        ).fetchone()
        assert row is not None and row[0] == "superseded"
        assert store.integrity()["status"] == "passed"
    finally:
        store.close()


def test_replay_idempotency(tmp_path: Path) -> None:
    store, source_root = _pipeline(tmp_path, {"mod.py": "def f():\n    return 1\n"})
    try:
        first = store.generation("proj").generation_digest
        replay = _build_with(store, source_root)
        assert replay == first  # zero-change rebuild is a no-op
        assert store.generation("proj").generation_digest == first
        assert store.integrity()["status"] == "passed"
    finally:
        store.close()


def test_read_only_mutation_rejected(tmp_path: Path) -> None:
    store, _ = _pipeline(tmp_path, {"mod.py": "def f():\n    return 1\n"})
    store.close()
    read_only = SQLiteCodeGraphStore(tmp_path / "graph.sqlite3", read_only=True)
    try:
        assert read_only.read_only is True
        with pytest.raises(PolicyViolation):
            read_only.build_generation(
                project_id="proj",
                source_revision="r1",
                tree_digest=digest("tree"),
                source_manifest_digest=digest("manifest"),
                extractor_profile_digest=PythonAstExtractor().profile_digest,
                files=(),
                symbols=(),
                edges=(),
                created_at=_CREATED_AT,
            )
    finally:
        read_only.close()


def test_source_drift_detected_on_read_only(tmp_path: Path) -> None:
    store, source_root = _pipeline(tmp_path, {"mod.py": "def f():\n    return 1\n"})
    store.close()
    read_only = SQLiteCodeGraphStore(tmp_path / "graph.sqlite3", read_only=True)
    try:
        # Publish a new generation on the file *after* opening the read-only store.
        (source_root / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        with SQLiteCodeGraphStore(tmp_path / "graph.sqlite3") as writer:
            _build_with(writer, source_root)
        with pytest.raises(PolicyViolation):
            read_only.generation("proj")
    finally:
        read_only.close()


def test_corruption_detected_on_open(tmp_path: Path) -> None:
    store, _ = _pipeline(tmp_path, {"mod.py": "def f():\n    return 1\n"})
    store.close()
    path = tmp_path / "graph.sqlite3"
    path.write_bytes(b"this is not a sqlite database at all")
    with pytest.raises(ConfigurationError):
        SQLiteCodeGraphStore(path)


def test_partial_build_rollback_on_drift(tmp_path: Path) -> None:
    source_root = tmp_path / "srcroot"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    store = SQLiteCodeGraphStore(tmp_path / "graph.sqlite3", create=True)
    try:
        extractor = PythonAstExtractor()
        plan = plan_graph_build(
            source_root,
            project_id="proj",
            project_slug="proj",
            source_revision="r1",
            extractor=extractor,
            created_at=_CREATED_AT,
        )
        # Change content on disk *after* planning -> digest drift -> no publish.
        (source_root / "mod.py").write_text("def f():\n    return 99\n", encoding="utf-8")
        with pytest.raises(ValidationFailed):
            apply_graph_build(store, extractor, source_root, plan)
        with pytest.raises(ValidationFailed):
            store.generation("proj")
    finally:
        store.close()


def test_wrong_project_generation_rejected(tmp_path: Path) -> None:
    store, _ = _pipeline(tmp_path, {"mod.py": "def f():\n    return 1\n"}, project_id="alpha")
    try:
        with pytest.raises(ValidationFailed):
            store.generation("beta")
    finally:
        store.close()


def test_concurrent_writer_rejected(tmp_path: Path) -> None:
    from zekam.domain.errors import ConcurrencyConflict

    store, _ = _pipeline(tmp_path, {"mod.py": "def f():\n    return 1\n"})
    try:
        with store._single_writer(), pytest.raises(ConcurrencyConflict):
            # A second writable store construction must fail to acquire the lock.
            SQLiteCodeGraphStore(tmp_path / "graph.sqlite3")
    finally:
        store.close()
