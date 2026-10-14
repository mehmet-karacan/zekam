"""Offline integration test for RAG state pending publish and crash recovery.

Uses the real :class:`SQLiteKnowledgeIndex` (real staged generation, real DB
pointer CAS, real pending evidence document) together with the real
``project_rag_runtime._publish_rag_state_pending`` /
``_recover_rag_state_pending`` pair.  A crash is simulated only at the exact
crash point; everything around it -- SQLite generation rows, the
``current_generation`` pointer, the rag-state document and the pending digest
evidence -- is real and durable.  Chunk vectors are deterministic, so no
provider or model call happens: the test is fully offline.
"""

from __future__ import annotations

import json
import math
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from zekam.application import project_rag_runtime as runtime
from zekam.application.config import EmbeddingRoute, KnowledgeSettings
from zekam.application.embedding_provider import (
    EmbeddingBatch,
    EmbeddingPolicy,
    EmbeddingProfile,
    EmbeddingProviderKind,
    EmbeddingPurpose,
    EmbeddingReceipt,
)
from zekam.application.home import HomeLayout
from zekam.application.knowledge_index import KnowledgeGeneration, KnowledgeIndexRecord
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.knowledge import Locator
from zekam.domain.security import DataClassification
from zekam.infrastructure.sqlite.knowledge_index import (
    VECTOR_DIMENSION,
    SQLiteKnowledgeIndex,
)

pytestmark = pytest.mark.integration

PROJECT_ID = "00000000-0000-0000-0000-00000000a100"
REV_A = digest("revision-a")
TREE_A = digest("tree-a")
REV_B = digest("revision-b")
TREE_B = digest("tree-b")


def _vector(primary: int) -> tuple[float, ...]:
    values = [0.0] * VECTOR_DIMENSION
    values[primary] = 1.0
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


def _record(chunk_id: str, *, path: str, text: str, revision: str) -> KnowledgeIndexRecord:
    return KnowledgeIndexRecord(
        chunk_id=chunk_id,
        project_id=PROJECT_ID,
        source_revision=revision,
        source_path=path,
        source_digest=digest({"source": path, "revision": revision}),
        locator=Locator(relative_path=path, line_start=1, line_end=2),
        text=text,
        content_digest=digest_of_bytes(text.encode("utf-8")),
        chunk_order=0,
        vector=_vector(0),
    )


def _records(revision: str, path: str, text: str) -> tuple[KnowledgeIndexRecord, ...]:
    # chunk.id is unique across the whole table, not per generation.
    chunk_id = f"chunk-{revision.split(':')[1][:12]}"
    return (_record(chunk_id, path=path, text=text, revision=revision),)


def _stage(
    index: SQLiteKnowledgeIndex,
    records: tuple[KnowledgeIndexRecord, ...],
    *,
    revision: str,
    tree: str,
) -> KnowledgeGeneration:
    """Commit one complete, unpublished 'building' generation (real staging)."""

    return index.stage_generation(
        records,
        project_id=PROJECT_ID,
        source_revision=revision,
        tree_digest=digest(tree),
        source_manifest_digest=digest({"manifest": tree}),
        embedding_profile_digest=digest("embedding-profile"),
        provider_profile_digest=digest("provider-profile"),
        created_at="2026-10-13T00:00:00Z",
    )


def _paths(tmp_path: Path) -> dict[str, Path]:
    """Real runtime path layout; every scoped directory pre-exists and is private."""

    home = tmp_path / "home"
    project_root = home / "projeler" / "rag-project"
    index_root = home / "knowledge-index" / "vector" / "opencode-bge-m3" / "rag-project"
    manifest_root = home / "knowledge-index" / "manifests" / "rag-project"
    state = project_root / "runtime" / "rag-state.json"
    for directory in (project_root / "runtime", index_root, manifest_root):
        directory.mkdir(parents=True, exist_ok=True)
    return {
        "home": home,
        "project_root": project_root,
        "index_root": index_root,
        "manifest_root": manifest_root,
        "index": index_root / "knowledge.sqlite3",
        "state": state,
    }


def _result(staged: KnowledgeGeneration) -> dict[str, Any]:
    return {
        "project_id": PROJECT_ID,
        "generation_digest": staged.generation_digest,
        "source_revision": staged.source_revision,
        "tree_digest": staged.tree_digest,
        "chunk_count": staged.chunk_count,
    }


def _write_state(paths: dict[str, Path], state: dict[str, Any]) -> None:
    runtime._write_private(paths["state"], (canonical_json(state) + "\n").encode("utf-8"))


def _ready_and_staged(
    paths: dict[str, Path],
) -> tuple[KnowledgeGeneration, KnowledgeGeneration]:
    """Publish ready generation A, then stage generation B on top of it."""

    records_a = _records(REV_A, "docs/karar/adr-0001.md", "eski uretilmis icerik")
    records_b = _records(REV_B, "docs/karar/adr-0002.md", "yeni uretilmis icerik")
    with SQLiteKnowledgeIndex(paths["index"], create=True) as index:
        staged_a = _stage(index, records_a, revision=REV_A, tree=TREE_A)
        index.publish_staged_generation(PROJECT_ID, staged_a.generation_digest, None)
        staged_b = _stage(index, records_b, revision=REV_B, tree=TREE_B)
    return staged_a, staged_b


def _read_only_connection(index_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{index_path.as_uri()}?mode=ro", uri=True, timeout=5)


def _init_git_source(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("ayni belge icerigi\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)


def _test_embedding_profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="local-bge-integration-test",
        display_name="Local BGE integration test",
        provider_kind=EmbeddingProviderKind.LOCAL,
        provider_identity_digest=digest("provider"),
        exact_model_id="BAAI/bge-m3",
        model_revision_fingerprint=digest("model-revision"),
        dimension=VECTOR_DIMENSION,
        vector_dtype="float32",
        normalized=True,
        distance_metric="cosine",
        query_prefix="",
        passage_prefix="",
        preprocessor_digest=digest("preprocessor"),
        tokenizer_digest=digest("tokenizer"),
        batch_policy_digest=digest("batch"),
        device_scope="test",
        data_classification_allowlist=(DataClassification.LOCAL_ONLY,),
        verified_at="2026-10-14T00:00:00Z",
        probe_evidence_digest=digest("probe"),
    )


def test_head_revision_change_reuses_document_embedding_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source HEAD-only revision must reuse vectors and create a new generation."""

    source_root = tmp_path / "source"
    _init_git_source(source_root)
    home = tmp_path / "home"
    HomeLayout(home).ensure()
    project_id = uuid4()
    project_slug = "head-cache-reuse"
    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.LOCAL)
    profile = _test_embedding_profile()

    class _CountingProvider:
        def __init__(self) -> None:
            self.document_calls = 0

        def describe(self) -> EmbeddingProfile:
            return profile

        def embed_documents(
            self, texts: tuple[str, ...], policy: EmbeddingPolicy
        ) -> EmbeddingBatch:
            self.document_calls += 1
            profile.assert_policy(policy)
            vectors = tuple((1.0,) + (0.0,) * (profile.dimension - 1) for _ in texts)
            receipt = EmbeddingReceipt(
                purpose=EmbeddingPurpose.DOCUMENT,
                profile_digest=profile.profile_digest,
                input_digest=digest(
                    {"texts": list(texts), "purpose": EmbeddingPurpose.DOCUMENT.value}
                ),
                output_digest=digest(vectors),
                vector_count=len(texts),
                dimension=profile.dimension,
                latency_ms=1,
                provider_call_count=1,
            )
            return EmbeddingBatch(vectors=vectors, receipt=receipt)

    provider = _CountingProvider()
    policy = EmbeddingPolicy(DataClassification.LOCAL_ONLY, profile.profile_digest)
    binding = runtime._EmbeddingBinding(
        provider=provider,
        policy=policy,
        ledger={"provider_calls": 0, "durable_remote_effects": 0},
        probe={"route": "local"},
        route=EmbeddingRoute.LOCAL,
        remote_provider_used=False,
        probe_call_count=0,
    )
    monkeypatch.setattr(
        runtime,
        "load_settings",
        lambda **_kwargs: SimpleNamespace(knowledge=knowledge),
    )
    monkeypatch.setattr(runtime, "load_smart_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_provider", lambda *_args, **_kwargs: binding)

    first = runtime._index(
        source_root,
        home,
        project_id,
        project_slug,
        None,
        None,
        batch_size=8,
    )
    _, plan = runtime._project_plan(
        source_root,
        project_id=project_id,
        project_slug=project_slug,
        knowledge=knowledge,
    )
    cache_scope = runtime._vector_cache_scope(
        str(project_id),
        runtime.DEFAULT_REALM_SLUG,
        plan.document.parser_ref,
        plan.document.parser_version,
        plan.chunks[0].profile_digest,
        profile.profile_digest,
    )

    subprocess.run(
        ["git", "-C", str(source_root), "commit", "--allow-empty", "-qm", "revision-only"],
        check=True,
    )
    second = runtime._index(
        source_root,
        home,
        project_id,
        project_slug,
        None,
        None,
        batch_size=8,
    )

    assert provider.document_calls == 1
    assert first["newly_embedded_chunks"] == 1
    assert first["provider_batches"] == 1
    assert second["cache_hits"] == 1
    assert second["newly_embedded_chunks"] == 0
    assert second["provider_batches"] == 0
    assert first["repository_source_revision"] != second["repository_source_revision"]
    assert first["repository_tree_digest"] == second["repository_tree_digest"]
    assert first["generation_digest"] != second["generation_digest"]
    assert first["embedding_profile_digest"] == second["embedding_profile_digest"]
    assert first["provider_profile_digest"] == second["provider_profile_digest"]

    paths = runtime._runtime_paths(home, project_slug)
    with sqlite3.connect(paths["cache"]) as connection:
        rows = connection.execute(
            "select scope_digest,chunk_id,content_digest from vector_cache_v2"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == cache_scope


def test_staged_generation_keeps_old_ready_pointer(tmp_path: Path) -> None:
    """Staging must not touch ``current_generation`` nor the ready generation."""

    paths = _paths(tmp_path)
    with SQLiteKnowledgeIndex(paths["index"], create=True) as index:
        staged_a = _stage(
            index, _records(REV_A, "docs/a.md", "eski icerik"), revision=REV_A, tree=TREE_A
        )
        index.publish_staged_generation(PROJECT_ID, staged_a.generation_digest, None)
        staged_b = _stage(
            index, _records(REV_B, "docs/b.md", "yeni icerik"), revision=REV_B, tree=TREE_B
        )
        assert staged_b.state == "building"
        current = index.generation(PROJECT_ID)
        assert current.generation_digest == staged_a.generation_digest
        assert current.state == "ready"

    connection = _read_only_connection(paths["index"])
    try:
        states = dict(
            connection.execute("select generation_digest,state from generation").fetchall()
        )
        pointer = connection.execute(
            "select generation_digest from current_generation where project_id=?",
            (PROJECT_ID,),
        ).fetchone()
    finally:
        connection.close()
    assert states[staged_a.generation_digest] == "ready"
    assert states[staged_b.generation_digest] == "building"
    assert pointer is not None and pointer[0] == staged_a.generation_digest


def test_crash_after_db_cas_is_recovered_from_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB CAS committed, state write crashed: recovery completes from pending."""

    paths = _paths(tmp_path)
    staged_a, staged_b = _ready_and_staged(paths)
    old_state = {
        "project_id": PROJECT_ID,
        "generation_digest": staged_a.generation_digest,
        "source_revision": REV_A,
        "tree_digest": TREE_A,
    }
    _write_state(paths, old_state)

    real_write = runtime._write_private
    crashed = {"once": False}

    def crash_on_state(path: Path, payload: bytes) -> None:
        if path == paths["state"] and not crashed["once"]:
            crashed["once"] = True
            raise OSError("simulated crash after DB CAS")
        real_write(path, payload)

    monkeypatch.setattr(runtime, "_write_private", crash_on_state)
    with pytest.raises(OSError, match="simulated crash after DB CAS"):
        runtime._publish_rag_state_pending(
            paths,
            PROJECT_ID,
            _result(staged_b),
            staged_a.generation_digest,
            staged_b.generation_digest,
        )

    # The DB CAS committed: the pointer advanced to the staged generation.
    connection = _read_only_connection(paths["index"])
    try:
        pointer = connection.execute(
            "select generation_digest from current_generation where project_id=?",
            (PROJECT_ID,),
        ).fetchone()
        states = dict(
            connection.execute("select generation_digest,state from generation").fetchall()
        )
    finally:
        connection.close()
    assert pointer is not None and pointer[0] == staged_b.generation_digest
    assert states[staged_b.generation_digest] == "ready"
    assert states[staged_a.generation_digest] == "superseded"

    # The old state document is untouched and the pending evidence is durable.
    pending_path = runtime._rag_pending_path(paths["state"])
    assert pending_path.is_file()
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["generation_digest"] == (
        staged_a.generation_digest
    )

    recovery = runtime._recover_rag_state_pending(paths, PROJECT_ID)
    assert recovery["status"] == "completed"
    assert recovery["generation_digest"] == staged_b.generation_digest
    assert recovery["state_written"] is True
    assert recovery["pending_cleared"] is True
    assert json.loads(paths["state"].read_text(encoding="utf-8")) == _result(staged_b)
    assert not pending_path.exists()
    with SQLiteKnowledgeIndex(paths["index"], read_only=True) as index:
        assert index.generation(PROJECT_ID).generation_digest == staged_b.generation_digest


def test_crash_before_cas_keeps_old_ready_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publish crashed before the DB CAS: old ready pointer and state survive."""

    paths = _paths(tmp_path)
    staged_a, staged_b = _ready_and_staged(paths)
    old_state = {
        "project_id": PROJECT_ID,
        "generation_digest": staged_a.generation_digest,
        "source_revision": REV_A,
        "tree_digest": TREE_A,
    }
    _write_state(paths, old_state)

    class _CrashBeforeCASIndex(SQLiteKnowledgeIndex):
        def publish_staged_generation(
            self, _project_id: str, _generation_digest: str, _expected_current_digest: str | None
        ) -> KnowledgeGeneration:
            raise OSError("simulated crash before CAS")

    monkeypatch.setattr(runtime, "SQLiteKnowledgeIndex", _CrashBeforeCASIndex)
    with pytest.raises(OSError, match="simulated crash before CAS"):
        runtime._publish_rag_state_pending(
            paths,
            PROJECT_ID,
            _result(staged_b),
            staged_a.generation_digest,
            staged_b.generation_digest,
        )
    monkeypatch.setattr(runtime, "SQLiteKnowledgeIndex", SQLiteKnowledgeIndex)

    # Crash evidence is left behind; the old ready pointer and state survive.
    pending_path = runtime._rag_pending_path(paths["state"])
    assert pending_path.is_file()
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["generation_digest"] == (
        staged_a.generation_digest
    )

    connection = _read_only_connection(paths["index"])
    try:
        pointer = connection.execute(
            "select generation_digest from current_generation where project_id=?",
            (PROJECT_ID,),
        ).fetchone()
        states = dict(
            connection.execute("select generation_digest,state from generation").fetchall()
        )
    finally:
        connection.close()
    assert pointer is not None and pointer[0] == staged_a.generation_digest
    assert states[staged_a.generation_digest] == "ready"
    assert states[staged_b.generation_digest] == "building"

    recovery = runtime._recover_rag_state_pending(paths, PROJECT_ID)
    assert recovery["status"] == "unpublished"
    assert recovery["generation_digest"] == staged_a.generation_digest
    assert recovery["state_written"] is False
    assert recovery["pending_cleared"] is True
    assert not pending_path.exists()
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["generation_digest"] == (
        staged_a.generation_digest
    )

    # The validated intent is cleared, so a retry publishes cleanly.
    published = runtime._publish_rag_state_pending(
        paths,
        PROJECT_ID,
        _result(staged_b),
        staged_a.generation_digest,
        staged_b.generation_digest,
    )
    assert published["state_written"] is True
    assert published["pending_cleared"] is True
    with SQLiteKnowledgeIndex(paths["index"], read_only=True) as index:
        assert index.generation(PROJECT_ID).generation_digest == staged_b.generation_digest
    assert json.loads(paths["state"].read_text(encoding="utf-8")) == _result(staged_b)
    assert not runtime._rag_pending_path(paths["state"]).exists()
