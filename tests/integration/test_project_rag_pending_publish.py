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
from zekam.application.project_knowledge_index import LOCAL_E5_DIMENSION
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.knowledge import Locator
from zekam.domain.security import DataClassification
from zekam.infrastructure.sqlite.knowledge_index import (
    SQLiteKnowledgeIndex,
)

pytestmark = pytest.mark.integration

PROJECT_ID = "00000000-0000-0000-0000-00000000a100"
REV_A = digest("revision-a")
TREE_A = digest("tree-a")
REV_B = digest("revision-b")
TREE_B = digest("tree-b")


def _vector(primary: int) -> tuple[float, ...]:
    values = [0.0] * LOCAL_E5_DIMENSION
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
        vector_dimension=LOCAL_E5_DIMENSION,
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
    index_root = (
        home
        / "knowledge-index"
        / "vector"
        / "multilingual-e5-small-onnx-int8"
        / "rag-project"
    )
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
        "source_identity_digest": runtime.source_identity_digest(
            PROJECT_ID, staged.source_revision, staged.tree_digest
        ),
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
    with SQLiteKnowledgeIndex(
        paths["index"], create=True, dimension=LOCAL_E5_DIMENSION
    ) as index:
        staged_a = _stage(index, records_a, revision=REV_A, tree=TREE_A)
        index.publish_staged_generation(PROJECT_ID, staged_a.generation_digest, None)
        staged_b = _stage(index, records_b, revision=REV_B, tree=TREE_B)
    return staged_a, staged_b


def _read_only_connection(index_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{index_path.as_uri()}?mode=ro", uri=True, timeout=5)


def _init_git_source(root: Path, files: dict[str, str] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in (files or {"README.md": "ayni belge icerigi\n"}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)


def _test_embedding_profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="local-e5-integration-test",
        display_name="Local multilingual-e5-small integration test",
        provider_kind=EmbeddingProviderKind.LOCAL,
        provider_identity_digest=digest("provider"),
        exact_model_id="intfloat/multilingual-e5-small",
        model_revision_fingerprint=digest("model-revision"),
        dimension=384,
        vector_dtype="float32",
        normalized=True,
        distance_metric="cosine",
        query_prefix="query: ",
        passage_prefix="passage: ",
        preprocessor_digest=digest("preprocessor"),
        tokenizer_digest=digest("tokenizer"),
        batch_policy_digest=digest("batch"),
        device_scope="test",
        data_classification_allowlist=(DataClassification.LOCAL_ONLY,),
        verified_at="2026-10-14T00:00:00Z",
        probe_evidence_digest=digest("probe"),
    )


class _CountingDocumentProvider:
    def __init__(self, profile: EmbeddingProfile) -> None:
        self.profile = profile
        self.document_calls = 0

    def describe(self) -> EmbeddingProfile:
        return self.profile

    def embed_documents(self, texts: tuple[str, ...], policy: EmbeddingPolicy) -> EmbeddingBatch:
        self.document_calls += 1
        self.profile.assert_policy(policy)
        vectors = tuple((1.0,) + (0.0,) * (self.profile.dimension - 1) for _ in texts)
        receipt = EmbeddingReceipt(
            purpose=EmbeddingPurpose.DOCUMENT,
            profile_digest=self.profile.profile_digest,
            input_digest=digest({"texts": list(texts), "purpose": EmbeddingPurpose.DOCUMENT.value}),
            output_digest=digest(vectors),
            vector_count=len(texts),
            dimension=self.profile.dimension,
            latency_ms=1,
            provider_call_count=1,
        )
        return EmbeddingBatch(vectors=vectors, receipt=receipt)


def _bind_counting_local_provider(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: KnowledgeSettings,
    *,
    canonical_references: bool = False,
) -> tuple[_CountingDocumentProvider, EmbeddingProfile]:
    profile = _test_embedding_profile()
    provider = _CountingDocumentProvider(profile)
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
    if not canonical_references:
        from zekam.application.reference_corpus import ReferenceCorpus

        # These repository/cache fixtures have no operational project registry.
        monkeypatch.setattr(
            runtime,
            "collect_reference_corpus",
            lambda home, project_id, slug, **kwargs: ReferenceCorpus(str(project_id), ()),
        )
    return provider, profile


def _run_index(
    source_root: Path,
    home: Path,
    project_id: Any,
    project_slug: str,
) -> dict[str, Any]:
    return runtime._index(
        source_root,
        home,
        project_id,
        project_slug,
        None,
        None,
        batch_size=4,
    )


def test_runtime_recovers_completed_slot_before_rebatching_after_cache_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _init_git_source(source)
    home = tmp_path / "home"
    HomeLayout(home).ensure()
    project_id = uuid4()
    provider, profile = _bind_counting_local_provider(
        monkeypatch,
        KnowledgeSettings(embedding_route=EmbeddingRoute.LOCAL),
    )
    completed = {}
    sent = []
    original_embed = provider.embed_documents

    def embed(texts, policy):
        sent.append(texts)
        batch = original_embed(texts, policy)
        completed.update(zip(texts, batch.vectors, strict=True))
        return batch

    def recover(texts, policy):
        if any(text not in completed for text in texts):
            return None
        vectors = tuple(completed[text] for text in texts)
        return EmbeddingBatch(
            vectors,
            EmbeddingReceipt(
                purpose=EmbeddingPurpose.DOCUMENT,
                profile_digest=profile.profile_digest,
                input_digest=digest({"texts": list(texts), "purpose": "document"}),
                output_digest=digest(vectors),
                vector_count=len(texts),
                dimension=profile.dimension,
                latency_ms=0,
                provider_call_count=0,
                reconciliation_evidence_digest=digest("fixture-terminal-evidence"),
            ),
        )

    monkeypatch.setattr(provider, "embed_documents", embed)
    monkeypatch.setattr(provider, "recover_documents", recover, raising=False)
    blob = runtime._vector_blob

    def crash(_vector):
        raise OSError("fixture cache crash after completed provider result")

    monkeypatch.setattr(runtime, "_vector_blob", crash)
    with pytest.raises(OSError, match="fixture cache crash"):
        _run_index(source, home, project_id, "regroup-recovery")
    assert len(sent) == 1
    monkeypatch.setattr(runtime, "_vector_blob", blob)
    (source / "new.md").write_text("new document after crash\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "new.md"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "new source"], check=True)
    result = _run_index(source, home, project_id, "regroup-recovery")
    assert len(sent) == 2
    assert not set(sent[0]).intersection(sent[1])
    assert result["cache_hits"] == 0
    assert result["reconciled_chunks"] == 1
    assert result["newly_embedded_chunks"] == 1
    assert result["provider_batches"] == 1


def _ready_chunk_paths(index_path: Path, project_id: Any) -> tuple[str, ...]:
    with SQLiteKnowledgeIndex(
        index_path, read_only=True, dimension=LOCAL_E5_DIMENSION
    ) as index:
        generation = index.generation(str(project_id))
        connection = _read_only_connection(index_path)
        try:
            rows = connection.execute(
                "select source_path from chunk where project_id=? and generation_digest=?"
                " order by chunk_order, id",
                (str(project_id), generation.generation_digest),
            ).fetchall()
        finally:
            connection.close()
    return tuple(str(row[0]) for row in rows)


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
    provider, profile = _bind_counting_local_provider(monkeypatch, knowledge)

    first = runtime._index(
        source_root,
        home,
        project_id,
        project_slug,
        None,
        None,
        batch_size=4,
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
        batch_size=4,
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


def test_source_rename_reuses_vector_and_rebinds_locator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rename with unchanged text reuses the vector and owns the new source path."""

    source_root = tmp_path / "source"
    _init_git_source(source_root, {"docs/eski.md": "ayni rename icerigi\n"})
    home = tmp_path / "home"
    HomeLayout(home).ensure()
    project_id = uuid4()
    project_slug = "rename-cache-reuse"
    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.LOCAL)
    provider, _profile = _bind_counting_local_provider(monkeypatch, knowledge)

    first = _run_index(source_root, home, project_id, project_slug)
    subprocess.run(
        ["git", "-C", str(source_root), "mv", "docs/eski.md", "docs/yeni.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source_root), "commit", "-qm", "rename-source"],
        check=True,
    )
    second = _run_index(source_root, home, project_id, project_slug)

    assert provider.document_calls == 1
    assert first["newly_embedded_chunks"] == 1
    assert second["cache_hits"] == 1
    assert second["newly_embedded_chunks"] == 0
    assert second["provider_batches"] == 0
    assert first["repository_tree_digest"] != second["repository_tree_digest"]
    assert first["generation_digest"] != second["generation_digest"]
    assert _ready_chunk_paths(runtime._runtime_paths(home, project_slug)["index"], project_id) == (
        "docs/yeni.md",
    )

    with sqlite3.connect(runtime._runtime_paths(home, project_slug)["index"]) as connection:
        row = connection.execute(
            "select source_path,locator_json,body from chunk where source_path=?",
            ("docs/yeni.md",),
        ).fetchone()
    assert row is not None
    assert json.loads(row[1])["relative_path"] == "docs/yeni.md"
    assert row[2] == "ayni rename icerigi"


def test_source_delete_removes_old_chunk_from_ready_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting one source removes its chunk while the remaining vector is reused."""

    source_root = tmp_path / "source"
    _init_git_source(
        source_root,
        {
            "docs/eski.md": "silinen icerik\n",
            "docs/keep.md": "kalacak icerik\n",
        },
    )
    home = tmp_path / "home"
    HomeLayout(home).ensure()
    project_id = uuid4()
    project_slug = "delete-cache-reuse"
    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.LOCAL)
    provider, _profile = _bind_counting_local_provider(monkeypatch, knowledge)

    first = _run_index(source_root, home, project_id, project_slug)
    subprocess.run(
        ["git", "-C", str(source_root), "rm", "docs/eski.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source_root), "commit", "-qm", "delete-source"],
        check=True,
    )
    second = _run_index(source_root, home, project_id, project_slug)

    assert first["selected_file_count"] == 2
    assert second["selected_file_count"] == 1
    assert provider.document_calls == 1
    assert first["provider_batches"] == 1
    assert second["cache_hits"] == 1
    assert second["newly_embedded_chunks"] == 0
    assert second["provider_batches"] == 0
    assert first["generation_digest"] != second["generation_digest"]
    assert _ready_chunk_paths(runtime._runtime_paths(home, project_slug)["index"], project_id) == (
        "docs/keep.md",
    )

    with sqlite3.connect(runtime._runtime_paths(home, project_slug)["index"]) as connection:
        deleted = connection.execute(
            "select 1 from chunk c join current_generation g"
            " on g.project_id=c.project_id and g.generation_digest=c.generation_digest"
            " where c.project_id=? and c.source_path=?",
            (str(project_id), "docs/eski.md"),
        ).fetchone()
    assert deleted is None


def test_staged_generation_keeps_old_ready_pointer(tmp_path: Path) -> None:
    """Staging must not touch ``current_generation`` nor the ready generation."""

    paths = _paths(tmp_path)
    with SQLiteKnowledgeIndex(
        paths["index"], create=True, dimension=LOCAL_E5_DIMENSION
    ) as index:
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
    with SQLiteKnowledgeIndex(
        paths["index"], read_only=True, dimension=LOCAL_E5_DIMENSION
    ) as index:
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
    with SQLiteKnowledgeIndex(
        paths["index"], read_only=True, dimension=LOCAL_E5_DIMENSION
    ) as index:
        assert index.generation(PROJECT_ID).generation_digest == staged_b.generation_digest
    assert json.loads(paths["state"].read_text(encoding="utf-8")) == _result(staged_b)
    assert not runtime._rag_pending_path(paths["state"]).exists()


@pytest.mark.parametrize("crash_before_cas", [True, False])
def test_manifest_and_projection_publication_recovers_with_database_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_before_cas: bool,
) -> None:
    paths = _paths(tmp_path)
    staged_a, staged_b = _ready_and_staged(paths)
    _write_state(paths, _result(staged_a))
    projection_path = paths["project_root"] / "PROJECT.yaml"
    old_projection = runtime.ProjectProjection.create(
        project_id=PROJECT_ID,
        slug="rag-project",
        display_name="RAG",
        status="active",
        source_bindings=("source:rag-project",),
        last_source_snapshot=digest({"source_revision": staged_a.source_revision}),
    )
    runtime.KnowledgeFileStore(paths["home"]).publish_project_projection(old_projection)
    old_bytes = projection_path.read_bytes()
    runtime._write_private(paths["manifest_root"] / "combined.json", b'{"old":true}')
    projection = runtime.ProjectProjection.create(
        project_id=PROJECT_ID,
        slug="rag-project",
        display_name="RAG",
        status="active",
        source_bindings=("source:rag-project",),
        last_source_snapshot=digest({"source_revision": staged_b.source_revision}),
    )
    manifests = {"repository.json": b'{"files":[]}', "combined.json": b'{"new":true}'}
    publication = runtime._stage_rag_publication(
        paths,
        staged_b.generation_digest,
        manifests,
        projection,
    )
    result = _result(staged_b) | {
        "combined_manifest_digest": digest_of_bytes(manifests["combined.json"])
    }
    real_publish = runtime.SQLiteKnowledgeIndex.publish_staged_generation
    real_complete = runtime._complete_rag_publication

    def crash(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("publication interrupted")

    if crash_before_cas:
        monkeypatch.setattr(runtime.SQLiteKnowledgeIndex, "publish_staged_generation", crash)
    else:
        monkeypatch.setattr(runtime, "_complete_rag_publication", crash)
    with pytest.raises(OSError, match="publication interrupted"):
        runtime._publish_rag_state_pending(
            paths,
            PROJECT_ID,
            result,
            staged_a.generation_digest,
            staged_b.generation_digest,
            publication=publication,
        )
    assert projection_path.read_bytes() == old_bytes
    assert (paths["manifest_root"] / "combined.json").read_bytes() == b'{"old":true}'
    monkeypatch.setattr(runtime.SQLiteKnowledgeIndex, "publish_staged_generation", real_publish)
    monkeypatch.setattr(runtime, "_complete_rag_publication", real_complete)
    recovery = runtime._recover_rag_state_pending(paths, PROJECT_ID)
    assert recovery["status"] == ("unpublished" if crash_before_cas else "completed")
    if crash_before_cas:
        assert projection_path.read_bytes() == old_bytes
    else:
        assert projection_path.read_bytes() == projection.render()
        assert (paths["manifest_root"] / "combined.json").read_bytes() == manifests["combined.json"]


def test_staged_manifest_tampering_prevents_database_publication(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    staged_a, staged_b = _ready_and_staged(paths)
    _write_state(paths, _result(staged_a))
    projection = runtime.ProjectProjection.create(
        project_id=PROJECT_ID,
        slug="rag-project",
        display_name="RAG",
        status="active",
        source_bindings=("source:rag-project",),
        last_source_snapshot=digest({"source_revision": staged_b.source_revision}),
    )
    manifests = {"repository.json": b"{}", "combined.json": b"{}"}
    publication = runtime._stage_rag_publication(
        paths, staged_b.generation_digest, manifests, projection
    )
    staged_root = paths["manifest_root"] / "generations" / staged_b.generation_digest.split(":")[1]
    runtime._write_private(staged_root / "repository.json", b'{"tampered":true}')
    result = _result(staged_b) | {"combined_manifest_digest": digest_of_bytes(b"{}")}
    with pytest.raises(runtime.PolicyViolation, match="digest drift"):
        runtime._publish_rag_state_pending(
            paths,
            PROJECT_ID,
            result,
            staged_a.generation_digest,
            staged_b.generation_digest,
            publication=publication,
        )
    with SQLiteKnowledgeIndex(
        paths["index"], read_only=True, dimension=LOCAL_E5_DIMENSION
    ) as index:
        assert index.generation(PROJECT_ID).generation_digest == staged_a.generation_digest


@pytest.mark.parametrize(
    "existing", [b"user owned content", b'{"schema":"foreign","project_id":"other"}']
)
def test_foreign_project_projection_rejects_before_cas(tmp_path: Path, existing: bytes) -> None:
    paths = _paths(tmp_path)
    staged_a, staged_b = _ready_and_staged(paths)
    _write_state(paths, _result(staged_a))
    projection = runtime.ProjectProjection.create(
        project_id=PROJECT_ID,
        slug="rag-project",
        display_name="RAG",
        status="active",
        source_bindings=("source:rag-project",),
        last_source_snapshot=digest({"source_revision": staged_b.source_revision}),
    )
    publication = runtime._stage_rag_publication(
        paths,
        staged_b.generation_digest,
        {"repository.json": b"{}", "combined.json": b"{}"},
        projection,
    )
    target = paths["project_root"] / "PROJECT.yaml"
    target.write_bytes(existing)
    result = _result(staged_b) | {"combined_manifest_digest": digest_of_bytes(b"{}")}
    with pytest.raises(runtime.PolicyViolation):
        runtime._publish_rag_state_pending(
            paths,
            PROJECT_ID,
            result,
            staged_a.generation_digest,
            staged_b.generation_digest,
            publication=publication,
        )
    assert target.read_bytes() == existing
    with SQLiteKnowledgeIndex(
        paths["index"], read_only=True, dimension=LOCAL_E5_DIMENSION
    ) as index:
        assert index.generation(PROJECT_ID).generation_digest == staged_a.generation_digest


@pytest.mark.parametrize("failure", [runtime.ProviderCallCancelled, runtime.ProviderDeadlineExceeded])
def test_cancel_or_deadline_after_lock_wait_keeps_ready_generation(
    tmp_path: Path, failure: type[Exception]
) -> None:
    paths = _paths(tmp_path)
    ready, staged = _ready_and_staged(paths)
    _write_state(paths, _result(ready))
    old_state = paths["state"].read_bytes()
    checks = 0

    def budget() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise failure("budget expired while waiting for publication")

    with pytest.raises(failure):
        runtime._publish_rag_state_pending(
            paths, PROJECT_ID, _result(staged), ready.generation_digest,
            staged.generation_digest, check_budget=budget,
        )
    assert checks == 2
    assert paths["state"].read_bytes() == old_state
    assert not runtime._rag_pending_path(paths["state"]).exists()
    with _read_only_connection(paths["index"]) as connection:
        row = connection.execute(
            "select generation_digest from current_generation where project_id=?", (PROJECT_ID,)
        ).fetchone()
        assert row[0] == ready.generation_digest
