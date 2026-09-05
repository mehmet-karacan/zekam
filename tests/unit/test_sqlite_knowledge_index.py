"""SQLite FTS5 + sqlite-vec generation, scope and recovery tests."""

from __future__ import annotations

import math
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
import sqlite_vec

from zekam.application.knowledge_index import KnowledgeIndexRecord
from zekam.application.technology_bakeoff import assess_sqlite_wal_safety
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.knowledge import Locator
from zekam.infrastructure.sqlite.knowledge_index import (
    VECTOR_DIMENSION,
    SQLiteKnowledgeIndex,
)

pytestmark = pytest.mark.unit


def _vector(primary: int, secondary: int | None = None) -> tuple[float, ...]:
    values = [0.0] * VECTOR_DIMENSION
    values[primary] = 1.0
    if secondary is not None:
        values[secondary] = 0.25
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


def _record(
    chunk_id: str,
    *,
    project_id: str = "akilli-kasa",
    revision: str = "rev-1",
    path: str = "belgeler/kararlar/ADR-0006.md",
    text: str = "ADR-0006 idempotent dosya ice aktarma SHA-256 ile tekrar engeller.",
    vector: tuple[float, ...] | None = None,
    order: int = 0,
) -> KnowledgeIndexRecord:
    return KnowledgeIndexRecord(
        chunk_id=chunk_id,
        project_id=project_id,
        source_revision=revision,
        source_path=path,
        source_digest=digest({"source": path, "revision": revision}),
        locator=Locator(relative_path=path, line_start=1, line_end=3),
        text=text,
        content_digest=digest_of_bytes(text.encode("utf-8")),
        chunk_order=order,
        vector=_vector(0) if vector is None else vector,
    )


def _build(
    index: SQLiteKnowledgeIndex,
    records: tuple[KnowledgeIndexRecord, ...],
    *,
    project_id: str = "akilli-kasa",
    revision: str = "rev-1",
    tree: str = "tree-1",
) -> str:
    generation = index.build_generation(
        records,
        project_id=project_id,
        source_revision=revision,
        tree_digest=digest(tree),
        source_manifest_digest=digest({"manifest": tree}),
        embedding_profile_digest=digest("embedding-profile"),
        provider_profile_digest=digest("provider-profile"),
        created_at="2026-09-02T00:00:00Z",
    )
    return generation.generation_digest


def test_generation_exact_lexical_dense_citation_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    records = (
        _record("chunk-idempotent", vector=_vector(0)),
        _record(
            "chunk-decimal",
            path="belgeler/kararlar/ADR-0005.md",
            text="ADR-0005 parasal tutarlarda Decimal ve Numeric kullanir; float reddedilir.",
            vector=_vector(1),
            order=1,
        ),
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation = _build(index, records)
        assert index.generation("akilli-kasa").generation_digest == generation
        assert index.exact("akilli-kasa", ("ADR-0006",), limit=5)[0].chunk_id == (
            "chunk-idempotent"
        )
        assert index.lexical("akilli-kasa", "parasal Decimal", limit=5)[0].chunk_id == (
            "chunk-decimal"
        )
        assert index.dense("akilli-kasa", _vector(0), limit=2)[0].chunk_id == ("chunk-idempotent")
        view = index.views("akilli-kasa", ("chunk-idempotent",))["chunk-idempotent"]
        assert view.locator.relative_path == "belgeler/kararlar/ADR-0006.md"
        assert index.integrity()["status"] == "passed"

    with SQLiteKnowledgeIndex(path) as restarted:
        assert restarted.generation("akilli-kasa").generation_digest == generation
        assert restarted.dense("akilli-kasa", _vector(1), limit=1)[0].chunk_id == ("chunk-decimal")


def test_journal_mode_follows_runtime_wal_safety_and_uses_writer_lock(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    expected = (
        "wal"
        if assess_sqlite_wal_safety(sqlite3.sqlite_version).safe_for_multi_connection_wal
        else "delete"
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        assert index._connection.execute("pragma journal_mode").fetchone()[0] == expected
        _build(index, (_record("policy-proof"),))
    lock = Path(str(path) + ".writer.lock")
    assert lock.read_bytes() == b"0"
    if expected == "delete":
        assert path.read_bytes()[18:20] == b"\x01\x01"
        assert not Path(str(path) + "-wal").exists()


def test_scope_filter_applies_before_limit_and_missing_project_has_no_generation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "knowledge.sqlite3"
    with SQLiteKnowledgeIndex(path, create=True) as index:
        _build(index, (_record("akilli-only"),))
        _build(
            index,
            (
                _record(
                    "other-only",
                    project_id="other",
                    revision="other-rev",
                    text="CROSS-PROJECT-ONLY exact forbidden content",
                ),
            ),
            project_id="other",
            revision="other-rev",
            tree="other-tree",
        )
        assert not index.exact("akilli-kasa", ("CROSS-PROJECT-ONLY",), limit=1)
        assert index.exact("other", ("CROSS-PROJECT-ONLY",), limit=1)[0].chunk_id == ("other-only")
        assert index.dense("akilli-kasa", _vector(0), limit=1)[0].chunk_id == "akilli-only"


def test_failed_new_generation_rolls_back_and_keeps_previous_current(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    with SQLiteKnowledgeIndex(path, create=True) as index:
        current = _build(index, (_record("stable-id"),))
        conflicting = _record(
            "stable-id",
            revision="rev-2",
            text="new content collides with an immutable old chunk id",
        )
        with pytest.raises(sqlite3.IntegrityError):
            _build(index, (conflicting,), revision="rev-2", tree="tree-2")
        assert index.generation("akilli-kasa").generation_digest == current
        assert index.exact("akilli-kasa", ("ADR-0006",), limit=1)[0].chunk_id == "stable-id"
        assert index.integrity()["status"] == "passed"


def test_successful_generation_supersedes_old_rows_and_restart_excludes_them(
    tmp_path: Path,
) -> None:
    path = tmp_path / "knowledge.sqlite3"
    with SQLiteKnowledgeIndex(path, create=True) as index:
        first = _build(
            index,
            (_record("old-id", text="OLD_ONLY ADR-0006", revision="rev-1"),),
        )
        second = _build(
            index,
            (
                _record(
                    "new-id",
                    text="NEW_ONLY ADR-0006",
                    revision="rev-2",
                ),
            ),
            revision="rev-2",
            tree="tree-2",
        )
        assert second != first
        assert index.generation("akilli-kasa").source_revision == "rev-2"
        assert index.exact("akilli-kasa", ("OLD_ONLY",), limit=5) == ()
        assert index.exact("akilli-kasa", ("NEW_ONLY",), limit=5)[0].chunk_id == "new-id"

    with SQLiteKnowledgeIndex(path) as restarted:
        assert restarted.generation("akilli-kasa").generation_digest == second
        assert restarted.exact("akilli-kasa", ("OLD_ONLY",), limit=5) == ()


def test_concurrent_builds_serialize_to_one_complete_current_generation(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    with SQLiteKnowledgeIndex(path, create=True):
        pass
    barrier = Barrier(2)

    def build(revision: str, marker: str, vector_slot: int) -> str:
        with SQLiteKnowledgeIndex(path) as index:
            barrier.wait(timeout=5)
            return _build(
                index,
                (
                    _record(
                        f"chunk-{revision}",
                        revision=revision,
                        text=f"{marker} complete generation",
                        vector=_vector(vector_slot),
                    ),
                ),
                revision=revision,
                tree=f"tree-{revision}",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        completed = set(
            pool.map(
                lambda args: build(*args),
                (("rev-a", "MARKER_A", 0), ("rev-b", "MARKER_B", 1)),
            )
        )
    assert len(completed) == 2
    with SQLiteKnowledgeIndex(path) as index:
        current = index.generation("akilli-kasa")
        assert current.generation_digest in completed
        present = sum(
            bool(index.exact("akilli-kasa", (marker,), limit=2))
            for marker in ("MARKER_A", "MARKER_B")
        )
        assert present == 1
        assert index.integrity()["status"] == "passed"


def test_logical_corruption_is_reported_and_replay_refuses_false_recovery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "knowledge.sqlite3"
    records = (_record("stable-id"),)
    with SQLiteKnowledgeIndex(path, create=True) as index:
        _build(index, records)
        index._connection.execute("delete from chunk_fts where id='stable-id'")
        assert index.integrity()["status"] == "failed"
        with pytest.raises(PolicyViolation, match=r"corrupt|recovery"):
            _build(index, records)


def test_malformed_persisted_locator_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    with SQLiteKnowledgeIndex(path, create=True) as index:
        _build(index, (_record("stable-id"),))
        index._connection.execute(
            "update chunk set locator_json=? where id=?",
            (
                '{"relative_path":"belgeler/kararlar/ADR-0006.md","line_start":"x","line_end":3}',
                "stable-id",
            ),
        )
        with pytest.raises(ValidationFailed, match="locator"):
            index.views("akilli-kasa", ("stable-id",))


def test_tampered_body_fails_integrity_views_identity_and_replay(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    records = (_record("stable-id"),)
    with SQLiteKnowledgeIndex(path, create=True) as index:
        _build(index, records)
        index._connection.execute(
            "update chunk set body='TAMPERED UNSUPPORTED CLAIM' where id='stable-id'"
        )
        assert index.integrity()["status"] == "failed"
        with pytest.raises(PolicyViolation, match="body/content digest drift"):
            index.views("akilli-kasa", ("stable-id",))
        with pytest.raises(PolicyViolation, match="identity/content digest drift"):
            index.source_identity("akilli-kasa", "stable-id")
        with pytest.raises(PolicyViolation, match=r"corrupt|recovery"):
            _build(index, records)


def test_tampered_vector_fails_integrity_and_replay(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    records = (_record("stable-id", vector=_vector(0)),)
    with SQLiteKnowledgeIndex(path, create=True) as index:
        _build(index, records)
        index._connection.execute(
            "update chunk_vector set embedding=? where id='stable-id'",
            (sqlite_vec.serialize_float32(_vector(1)),),
        )
        assert index.integrity()["status"] == "failed"
        with pytest.raises(PolicyViolation, match=r"corrupt|recovery"):
            _build(index, records)


def test_record_rejects_text_content_digest_mismatch() -> None:
    with pytest.raises(ValidationFailed, match="text/content digest drift"):
        KnowledgeIndexRecord(
            chunk_id="invalid-digest",
            project_id="akilli-kasa",
            source_revision="rev-1",
            source_path="service.py",
            source_digest=digest("source"),
            locator=Locator(relative_path="service.py", line_start=1, line_end=1),
            text="actual body",
            content_digest=digest_of_bytes(b"different body"),
            chunk_order=0,
            vector=_vector(0),
        )


@pytest.mark.parametrize(
    "vector",
    [(), (float("nan"),) * VECTOR_DIMENSION, (1.0,) * (VECTOR_DIMENSION - 1)],
)
def test_record_rejects_empty_nan_and_wrong_dimension(vector: tuple[float, ...]) -> None:
    with pytest.raises(ValidationFailed):
        _record("invalid", vector=vector)


def _filesystem_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    """Access time is excluded: ordinary reads may update it at the OS boundary."""
    result: dict[str, tuple[object, ...]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        info = path.lstat()
        result[str(path.relative_to(root))] = (
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            path.read_bytes() if stat.S_ISREG(info.st_mode) else None,
        )
    return result


def _offline_index(path: Path) -> None:
    with SQLiteKnowledgeIndex(path, create=True) as index:
        _build(index, (_record("stable-id"),))


def test_read_only_queries_keep_files_bytes_permissions_and_sidecars_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filename = (
        "knowledge #unicode-ü.sqlite3" if os.name == "nt" else "knowledge ?#unicode-ü.sqlite3"
    )
    path = tmp_path / filename
    with SQLiteKnowledgeIndex(path, create=True) as writer:
        first = _build(writer, (_record("old-id"),))
        second = _build(
            writer,
            (_record("new-id", revision="rev-2", text="NEW_ONLY decimal", vector=_vector(1)),),
            revision="rev-2",
            tree="tree-2",
        )
        expected_generation = writer.generation("akilli-kasa")
        expected_views = writer.views("akilli-kasa", ("old-id",), generation_digest=first)
        expected_identity = writer.source_identity("akilli-kasa", "new-id")
    path.chmod(0o444)
    before = _filesystem_snapshot(tmp_path)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("read-only opening must not mkdir or chmod")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(os, "chmod", forbidden)
    with SQLiteKnowledgeIndex(path, read_only=True) as reader:
        assert reader.read_only is True
        assert reader.generation("akilli-kasa") == expected_generation
        assert reader.generation("akilli-kasa").generation_digest == second
        assert reader.views("akilli-kasa", ("old-id",), generation_digest=first) == expected_views
        assert reader.source_identity("akilli-kasa", "new-id") == expected_identity
        assert reader.exact("akilli-kasa", ("NEW_ONLY",), limit=3)[0].chunk_id == "new-id"
        assert reader.lexical("akilli-kasa", "decimal", limit=3)[0].chunk_id == "new-id"
        assert reader.dense("akilli-kasa", _vector(1), limit=1)[0].chunk_id == "new-id"
        assert reader.integrity()["status"] == "passed"
        assert reader._connection.execute("pragma query_only").fetchone()[0] == 1
        assert _filesystem_snapshot(tmp_path) == before
    assert _filesystem_snapshot(tmp_path) == before


def test_read_only_mutators_reject_before_validation_or_idempotent_replay(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.db"
    _offline_index(path)
    before = _filesystem_snapshot(tmp_path)
    with SQLiteKnowledgeIndex(path, read_only=True) as reader:
        with pytest.raises(PolicyViolation, match="read-only"):
            _build(reader, ())
        with pytest.raises(PolicyViolation, match="read-only"):
            _build(reader, (_record("stable-id"),))
        with pytest.raises(PolicyViolation, match="read-only"):
            reader._create_schema()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader._connection.execute("update metadata set schema_version=99")
    assert _filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize("option", [None, 0, 1, "true", [], {}])
@pytest.mark.parametrize("name", ["create", "read_only"])
def test_read_only_constructor_flags_require_exact_boolean(
    tmp_path: Path, option: Any, name: str
) -> None:
    target = tmp_path / "never-created" / "index.db"
    with pytest.raises(ValidationFailed, match="booleans"):
        SQLiteKnowledgeIndex(target, **{name: option})
    assert not target.parent.exists()


@pytest.mark.parametrize("value", [None, 42, "/absolute/string.db", Path("relative.db")])
def test_read_only_path_wrong_types_and_relative_paths_are_typed_rejections(value: Any) -> None:
    with pytest.raises(ConfigurationError, match="path"):
        SQLiteKnowledgeIndex(value, read_only=True)


def test_read_only_missing_path_or_create_conflict_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "missing-parent" / "index.db"
    before = _filesystem_snapshot(tmp_path)
    with pytest.raises(ConfigurationError):
        SQLiteKnowledgeIndex(path, read_only=True)
    with pytest.raises(ConfigurationError, match="cannot create"):
        SQLiteKnowledgeIndex(path, read_only=True, create=True)
    assert _filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "kind", ["directory", "file-symlink", "parent-symlink", "ancestor-symlink"]
)
def test_read_only_nonregular_or_symlink_paths_never_touch_target(
    tmp_path: Path, kind: str
) -> None:
    if os.name == "nt" and kind != "directory":
        pytest.skip("Unprivileged Windows file symlink; junction coverage is separate")
    real = tmp_path / "real" / "nested" / "index.db"
    _offline_index(real)
    if kind == "directory":
        path = real.parent
    elif kind == "file-symlink":
        path = tmp_path / "index.db"
        path.symlink_to(real)
    elif kind == "parent-symlink":
        link = tmp_path / "linked"
        link.symlink_to(real.parent, target_is_directory=True)
        path = link / real.name
    else:
        link = tmp_path / "linked"
        link.symlink_to(real.parent.parent, target_is_directory=True)
        path = link / real.parent.name / real.name
    before = _filesystem_snapshot(tmp_path)
    with pytest.raises(ConfigurationError):
        SQLiteKnowledgeIndex(path, read_only=True)
    assert _filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "kind", ["corrupt", "empty", "version", "missing-table", "wrong-columns", "foreign-key"]
)
def test_read_only_invalid_database_rejects_without_schema_repair(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "invalid.db"
    if kind in {"corrupt", "empty"}:
        path.write_bytes(b"not a sqlite database" if kind == "corrupt" else b"")
    else:
        _offline_index(path)
        with sqlite3.connect(path) as db:
            if kind == "version":
                db.execute("update metadata set schema_version=99")
            elif kind in {"missing-table", "wrong-columns"}:
                db.execute("drop table current_generation")
                if kind == "wrong-columns":
                    db.execute("create table current_generation(unrelated text)")
            else:
                db.execute("insert into current_generation values('orphan','absent-generation')")
    before = _filesystem_snapshot(tmp_path)
    with pytest.raises(ConfigurationError):
        SQLiteKnowledgeIndex(path, read_only=True)
    assert _filesystem_snapshot(tmp_path) == before


def test_read_only_rejects_uncheckpointed_wal_instead_of_silently_reading_old_generation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "index.db"
    _offline_index(path)
    with SQLiteKnowledgeIndex(path) as writer:
        writer._connection.execute("pragma journal_mode=wal")
        current = _build(
            writer,
            (_record("new-id", revision="rev-2", text="ONLY_IN_WAL"),),
            revision="rev-2",
            tree="tree-2",
        )
        assert Path(str(path) + "-wal").stat().st_size > 0
        before = _filesystem_snapshot(tmp_path)
        with pytest.raises(ConfigurationError, match="offline checkpointed"):
            SQLiteKnowledgeIndex(path, read_only=True)
        assert _filesystem_snapshot(tmp_path) == before
    with SQLiteKnowledgeIndex(path, read_only=True) as reader:
        assert reader.generation("akilli-kasa").generation_digest == current


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_read_only_rejects_unsafe_sidecars_without_touching_them(
    tmp_path: Path, suffix: str, kind: str
) -> None:
    if os.name == "nt" and kind == "symlink":
        pytest.skip("Unprivileged Windows file symlink; reparse coverage is separate")
    path = tmp_path / "index.db"
    _offline_index(path)
    sidecar = Path(str(path) + suffix)
    if kind == "symlink":
        target = tmp_path / "unrelated-user-file"
        target.write_bytes(b"preserve")
        sidecar.symlink_to(target)
    else:
        sidecar.mkdir()
    before = _filesystem_snapshot(tmp_path)
    with pytest.raises(ConfigurationError, match="regular"):
        SQLiteKnowledgeIndex(path, read_only=True)
    assert _filesystem_snapshot(tmp_path) == before


def test_read_only_never_recovers_a_nonempty_rollback_journal(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    _offline_index(path)
    Path(str(path) + "-journal").write_bytes(b"pending rollback evidence")
    before = _filesystem_snapshot(tmp_path)
    with pytest.raises(ConfigurationError, match="offline checkpointed"):
        SQLiteKnowledgeIndex(path, read_only=True)
    assert _filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "operation",
    ["generation", "exact", "lexical", "dense", "views", "source_identity", "integrity"],
)
def test_read_only_every_public_query_rejects_source_drift(tmp_path: Path, operation: str) -> None:
    path = tmp_path / "index.db"
    _offline_index(path)
    with SQLiteKnowledgeIndex(path, read_only=True) as reader:
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        queries = {
            "generation": lambda: reader.generation("akilli-kasa"),
            "exact": lambda: reader.exact("akilli-kasa", ("ADR-0006",), limit=1),
            "lexical": lambda: reader.lexical("akilli-kasa", "dosya", limit=1),
            "dense": lambda: reader.dense("akilli-kasa", _vector(0), limit=1),
            "views": lambda: reader.views("akilli-kasa", ("stable-id",)),
            "source_identity": lambda: reader.source_identity("akilli-kasa", "stable-id"),
            "integrity": reader.integrity,
        }
        with pytest.raises(PolicyViolation, match="source fingerprint drift"):
            queries[operation]()


def test_read_only_rejects_source_change_during_query_before_returning_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "index.db"
    _offline_index(path)
    with SQLiteKnowledgeIndex(path, read_only=True) as reader:
        original = reader._pinned_generation

        def changed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            before = path.stat()
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            return result

        monkeypatch.setattr(reader, "_pinned_generation", changed)
        with pytest.raises(PolicyViolation, match="source fingerprint drift"):
            reader.exact("akilli-kasa", ("ADR-0006",), limit=1)


def test_read_only_rejects_wal_writer_started_after_reader_opened(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    _offline_index(path)
    with SQLiteKnowledgeIndex(path, read_only=True) as reader, SQLiteKnowledgeIndex(path) as writer:
        writer._connection.execute("pragma journal_mode=wal")
        _build(
            writer,
            (_record("new-id", revision="rev-2"),),
            revision="rev-2",
            tree="tree-2",
        )
        with pytest.raises(PolicyViolation, match="source drift"):
            reader.generation("akilli-kasa")


def test_read_only_constructor_drift_closes_the_failed_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "index.db"
    _offline_index(path)
    connections = []
    original_connect, original_load = sqlite3.connect, sqlite_vec.load

    def connected(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection: sqlite3.Connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    def altered(connection: sqlite3.Connection) -> None:
        original_load(connection)
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))

    monkeypatch.setattr(sqlite3, "connect", connected)
    monkeypatch.setattr(sqlite_vec, "load", altered)
    with pytest.raises(PolicyViolation, match="source fingerprint drift"):
        SQLiteKnowledgeIndex(path, read_only=True)
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("select 1")
