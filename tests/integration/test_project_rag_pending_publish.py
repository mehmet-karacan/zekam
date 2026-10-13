"""Dar offline entegrasyon testi: stage -> pending publish -> crash recovery.

Yalnizca yeni dosyadir; mevcut dosyalara dokunmaz. Gercek
``SQLiteKnowledgeIndex`` ve gecici dosyalar kullanilir; kaynak/provider canli
cagrisi yoktur:

* Eski hazir generation varken ``stage_generation`` current pointer'i
  degistirmemeli (``publish_staged_generation`` + ``generation``).
* ``_publish_rag_state_pending`` icinde state yazimi oncesi process crash
  taklidi: DB CAS (pointer) tamamlandigi halde state dokunulmaz, pending
  kanit dosyasi geriye kalmali.
* ``_recover_rag_state_pending`` DB'nin yeni/current nesilde oldugunu gorunce
  state'i pending result'dan tamamlayip pending'i temizlemeli.
* CAS oncesinde kesinti: eski hazir nesil ve eski state korunmali, recovery
  pending niyetini "unpublished" olarak temizlemeli.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.application import project_rag_runtime as runtime
from zekam.application.knowledge_index import KnowledgeGeneration, KnowledgeIndexRecord
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.knowledge import Locator
from zekam.infrastructure.local_file_security import restrict_private_tree
from zekam.infrastructure.sqlite.knowledge_index import (
    VECTOR_DIMENSION,
    SQLiteKnowledgeIndex,
)

pytestmark = pytest.mark.integration

PROJECT_ID = "pending-pub"
# Recovery kanit dogrulamasi source_revision/tree_digest'i sha256 digest olarak
# parse eder; gercek index planinin urettigi kimliklerle ayni sekilde digest ver.
SOURCE_REVISION = digest("source-revision")


def _vector(primary: int) -> tuple[float, ...]:
    values = [0.0] * VECTOR_DIMENSION
    values[primary] = 1.0
    return tuple(value for value in values)


def _record(chunk_id: str, *, text: str, order: int, path: str) -> KnowledgeIndexRecord:
    return KnowledgeIndexRecord(
        chunk_id=chunk_id,
        project_id=PROJECT_ID,
        source_revision=SOURCE_REVISION,
        source_path=path,
        source_digest=digest({"source": path, "revision": SOURCE_REVISION}),
        locator=Locator(relative_path=path, line_start=1, line_end=2),
        text=text,
        content_digest=digest_of_bytes(text.encode("utf-8")),
        chunk_order=order,
        vector=_vector(order),
    )


def _stage(
    index: SQLiteKnowledgeIndex,
    records: tuple[KnowledgeIndexRecord, ...],
    *,
    tree: str,
    manifest: str,
) -> str:
    generation = index.stage_generation(
        records,
        project_id=PROJECT_ID,
        source_revision=SOURCE_REVISION,
        tree_digest=digest(tree),
        source_manifest_digest=digest(manifest),
        embedding_profile_digest=digest("embedding-profile"),
        provider_profile_digest=digest("provider-profile"),
        created_at="2026-10-13T00:00:00Z",
    )
    return generation.generation_digest


class _CrashDuringStateWrite(RuntimeError):
    """Process crash taklidi: DB CAS sonrasi, rag-state.json yazimi oncesi kesinti."""

    def __init__(
        self, message: str = "simulated process crash after DB CAS, before state write"
    ) -> None:
        super().__init__(message)


def _runtime_paths(tmp_path: Path) -> dict[str, Path]:
    """Gecici ama gercek, private-ACL'li RAG runtime yollarini olusturur."""

    home = tmp_path / "home"
    project_root = home / "projeler" / PROJECT_ID
    index_root = home / "knowledge-index" / "vector" / "opencode-bge-m3" / PROJECT_ID
    manifest_root = home / "knowledge-index" / "manifests" / PROJECT_ID
    for directory in (project_root / "runtime", index_root, manifest_root):
        directory.mkdir(parents=True)
    for directory in (project_root, index_root, manifest_root):
        restrict_private_tree(directory)
    state = project_root / "runtime" / "rag-state.json"
    return {
        "home": home,
        "project_root": project_root,
        "index_root": index_root,
        "manifest_root": manifest_root,
        "state": state,
        "index": index_root / "knowledge.sqlite3",
    }


def _seed_old_generation(paths: dict[str, Path]) -> dict[str, str]:
    old = (
        _record(
            "old-1",
            text="Eski karar metni: hazir birincil nesil.",
            order=0,
            path="belgeler/ADR-0001.md",
        ),
        _record(
            "old-2",
            text="Eski not: idempotent yayin kaniti.",
            order=1,
            path="belgeler/ADR-0002.md",
        ),
    )
    with SQLiteKnowledgeIndex(paths["index"], create=True) as index:
        generation = index.build_generation(
            old,
            project_id=PROJECT_ID,
            source_revision=SOURCE_REVISION,
            tree_digest=digest("tree-old"),
            source_manifest_digest=digest("manifest-old"),
            embedding_profile_digest=digest("embedding-profile"),
            provider_profile_digest=digest("provider-profile"),
            created_at="2026-10-13T00:00:00Z",
        )
    return {
        "generation_digest": generation.generation_digest,
        "source_revision": generation.source_revision,
        "tree_digest": generation.tree_digest,
    }


def _stage_new(paths: dict[str, Path]) -> str:
    with SQLiteKnowledgeIndex(paths["index"]) as index:
        return _stage(
            index,
            (
                _record(
                    "new-1",
                    text="Yeni karar metni: ikinci nesil.",
                    order=0,
                    path="belgeler/ADR-0001.md",
                ),
            ),
            tree="tree-new",
            manifest="manifest-new",
        )


def _crash_before_state_write(monkeypatch: pytest.MonkeyPatch, state_path: Path) -> None:
    real_write = runtime._write_private

    def crash(path: Path, payload: bytes) -> None:
        if path == state_path:
            raise _CrashDuringStateWrite()
        real_write(path, payload)

    monkeypatch.setattr(runtime, "_write_private", crash)


def test_staging_does_not_change_current_pointer(tmp_path: Path) -> None:
    paths = _runtime_paths(tmp_path)
    state = _seed_old_generation(paths)
    staged = _stage_new(paths)

    with SQLiteKnowledgeIndex(paths["index"]) as index:
        current = index.generation(PROJECT_ID)
        assert current.generation_digest == state["generation_digest"]
        assert current.state == "ready"
        assert index.exact(PROJECT_ID, ("Eski",), limit=5)[0].chunk_id.startswith("old-")

    # Eski nesil haliyle pointer'da: staging tek basina yayin degildir.
    with SQLiteKnowledgeIndex(paths["index"]) as index:
        generation = index.publish_staged_generation(
            PROJECT_ID, staged, expected_current_digest=state["generation_digest"]
        )
        assert generation.generation_digest == staged
        assert generation.state == "ready"
        assert index.generation(PROJECT_ID).generation_digest == staged


def test_crash_after_db_cas_leaves_pending_and_state_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _runtime_paths(tmp_path)
    result_old = _seed_old_generation(paths)
    state_path = paths["state"]
    runtime._write_private(
        state_path,
        (
            canonical_json(
                {
                    "project_id": PROJECT_ID,
                    "generation_digest": result_old["generation_digest"],
                    "source_revision": result_old["source_revision"],
                    "tree_digest": result_old["tree_digest"],
                }
            )
            + "\n"
        ).encode("utf-8"),
    )
    state_before = state_path.read_bytes()
    staged = _stage_new(paths)
    result_new = {
        "project_id": PROJECT_ID,
        "generation_digest": staged,
        "source_revision": SOURCE_REVISION,
        "tree_digest": digest("tree-new"),
    }
    _crash_before_state_write(monkeypatch, state_path)

    with pytest.raises(_CrashDuringStateWrite):
        runtime._publish_rag_state_pending(
            paths, PROJECT_ID, result_new, result_old["generation_digest"], staged
        )

    # Crash taklidi state yazimina erisemedi: dosya bayt bayt ayni.
    assert state_path.read_bytes() == state_before
    # DB tarafinda CAS tamamlandi: current pointer yeni nesilde.
    with SQLiteKnowledgeIndex(paths["index"]) as index:
        assert index.generation(PROJECT_ID).generation_digest == staged
        assert index.exact(PROJECT_ID, ("Yeni",), limit=5)[0].chunk_id == "new-1"
    # Pending kanit dosyasi recovery icin geriye kaldi.
    assert runtime._rag_pending_path(state_path).exists()


def test_recovery_completes_state_from_db_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _runtime_paths(tmp_path)
    result_old = _seed_old_generation(paths)
    state_path = paths["state"]
    runtime._write_private(
        state_path,
        (
            canonical_json(
                {
                    "project_id": PROJECT_ID,
                    "generation_digest": result_old["generation_digest"],
                    "source_revision": result_old["source_revision"],
                    "tree_digest": result_old["tree_digest"],
                }
            )
            + "\n"
        ).encode("utf-8"),
    )
    staged = _stage_new(paths)
    result_new = {
        "project_id": PROJECT_ID,
        "generation_digest": staged,
        "source_revision": SOURCE_REVISION,
        "tree_digest": digest("tree-new"),
    }
    _crash_before_state_write(monkeypatch, state_path)
    with pytest.raises(_CrashDuringStateWrite):
        runtime._publish_rag_state_pending(
            paths, PROJECT_ID, result_new, result_old["generation_digest"], staged
        )
    monkeypatch.undo()
    # Crash taklidi geri alindi; recovery bu asamadan sonra calisir.

    recovery = runtime._recover_rag_state_pending(paths, PROJECT_ID)

    assert recovery["status"] == "completed"
    assert recovery["generation_digest"] == staged
    assert recovery["state_written"] is True
    assert recovery["pending_cleared"] is True
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["generation_digest"] == staged
    assert state["tree_digest"] == digest("tree-new")
    assert not runtime._rag_pending_path(state_path).exists()
    with SQLiteKnowledgeIndex(paths["index"]) as index:
        assert index.generation(PROJECT_ID).generation_digest == staged


def test_crash_before_db_cas_preserves_old_generation_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _runtime_paths(tmp_path)
    result_old = _seed_old_generation(paths)
    state_path = paths["state"]
    runtime._write_private(
        state_path,
        (
            canonical_json(
                {
                    "project_id": PROJECT_ID,
                    "generation_digest": result_old["generation_digest"],
                    "source_revision": result_old["source_revision"],
                    "tree_digest": result_old["tree_digest"],
                }
            )
            + "\n"
        ).encode("utf-8"),
    )
    state_before = state_path.read_bytes()
    staged = _stage_new(paths)
    result_new = {
        "project_id": PROJECT_ID,
        "generation_digest": staged,
        "source_revision": SOURCE_REVISION,
        "tree_digest": digest("tree-new"),
    }

    class _CrashBeforeCas(SQLiteKnowledgeIndex):
        def publish_staged_generation(
            self,
            project_id: str,
            generation_digest: str,
            expected_current_digest: str | None,
        ) -> KnowledgeGeneration:
            raise _CrashDuringStateWrite("simulated process crash before DB CAS")

    monkeypatch.setattr(runtime, "SQLiteKnowledgeIndex", _CrashBeforeCas)

    with pytest.raises(_CrashDuringStateWrite):
        runtime._publish_rag_state_pending(
            paths, PROJECT_ID, result_new, result_old["generation_digest"], staged
        )

    # CAS hic calismadi: eski nesil hazir, pointer ve state dokunulmaz.
    with SQLiteKnowledgeIndex(paths["index"]) as index:
        assert index.generation(PROJECT_ID).generation_digest == result_old["generation_digest"]
    assert state_path.read_bytes() == state_before
    assert runtime._rag_pending_path(state_path).exists()

    monkeypatch.undo()
    recovery = runtime._recover_rag_state_pending(paths, PROJECT_ID)
    assert recovery["status"] == "unpublished"
    assert recovery["generation_digest"] == result_old["generation_digest"]
    assert recovery["state_written"] is False
    assert recovery["pending_cleared"] is True
    assert state_path.read_bytes() == state_before
    assert not runtime._rag_pending_path(state_path).exists()
