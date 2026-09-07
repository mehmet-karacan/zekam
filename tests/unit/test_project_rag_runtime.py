from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from zekam.application import project_rag_runtime as runtime
from zekam.application.model_health_service import ProbeUnavailable
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import Locator, UnitKind
from zekam.domain.retrieval import Chunk


class _TransientProvider:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.result = object()

    def embed_documents(self, _texts: object, _policy: object) -> object:
        self.calls += 1
        if self.calls <= self.failures:
            raise ProbeUnavailable("provider-transport-unavailable")
        return self.result


def test_plan_document_counts_and_labels_sanitized_odi_metadata() -> None:
    discovery = SimpleNamespace(file_count=2, secrets=(), truncated=False)
    plan = SimpleNamespace(
        project_id="project-1",
        project_slug="gpu-fusion",
        source_revision="source-revision",
        tree_digest="sha256:" + "a" * 64,
        plan_digest="sha256:" + "b" * 64,
        selected_file_count=2,
        chunks=(object(), object()),
        skipped_unsupported=0,
        skipped_encoding=0,
    )
    odi_plan = SimpleNamespace(
        plan_digest="sha256:" + "c" * 64,
        chunks=(object(), object(), object()),
    )

    result = runtime._plan_document(
        cast(Any, discovery), cast(Any, plan), odi_plan=cast(Any, odi_plan)
    )

    assert result["source_chunk_count"] == 2
    assert result["odi_chunk_count"] == 3
    assert result["chunk_count"] == 5
    assert result["odi_access"] == "sanitized-metadata"


def test_generation_bound_chunk_id_changes_with_combined_source_revision() -> None:
    first = runtime._generation_chunk_id("repo-chunk-1", "sha256:" + "a" * 64)
    second = runtime._generation_chunk_id("repo-chunk-1", "sha256:" + "b" * 64)
    assert first != second
    assert first == runtime._generation_chunk_id("repo-chunk-1", "sha256:" + "a" * 64)


def test_vector_cache_reuses_identical_content_across_stable_identity_change(
    tmp_path: Path,
) -> None:
    connection = runtime._cache(tmp_path / "vectors.sqlite3")
    text = "CREATE TABLE GPU_USER.CDR (ID NUMBER)"
    content_digest = digest_of_bytes(text.encode("utf-8"))
    vector = tuple(0.001 for _ in range(runtime.VECTOR_DIMENSION))
    blob = runtime._vector_blob(vector)
    jittered = tuple(0.00101 for _ in range(runtime.VECTOR_DIMENSION))
    jittered_blob = runtime._vector_blob(jittered)
    profile = "sha256:" + "a" * 64
    connection.execute(
        "insert into vector_cache values(?,?,?,?,?,?)",
        (
            "old-snapshot-id",
            content_digest,
            profile,
            blob,
            digest_of_bytes(blob),
            "2026-09-06T00:00:00Z",
        ),
    )
    connection.execute(
        "insert into vector_cache values(?,?,?,?,?,?)",
        (
            "other-snapshot-id",
            content_digest,
            profile,
            jittered_blob,
            digest_of_bytes(jittered_blob),
            "2026-09-06T00:01:00Z",
        ),
    )
    chunk = Chunk(
        chunk_id="stable-content-id",
        document_id="oracle",
        text=text,
        locator=Locator(object_name="GPU_USER.CDR:TABLE"),
        kind=UnitKind.DB_OBJECT,
        token_count=8,
        order=0,
    )
    try:
        result = runtime._cached_vectors(connection, (chunk,), profile)
        expected = vector if digest_of_bytes(blob) < digest_of_bytes(jittered_blob) else jittered
        assert result[chunk.chunk_id] == pytest.approx(expected, abs=1e-8)
        assert (
            connection.execute(
                "select count(*) from vector_cache where chunk_id='stable-content-id'"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_document_embedding_retries_only_bounded_transport_unavailability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _TransientProvider(failures=2)
    waits: list[int] = []
    monkeypatch.setattr(runtime.time, "sleep", waits.append)

    result = runtime._embed_documents_with_retry(cast(Any, provider), ("one",), cast(Any, object()))

    assert result is provider.result
    assert provider.calls == 3
    assert waits == [1, 2]


def test_document_embedding_retry_exhaustion_preserves_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _TransientProvider(failures=4)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    with pytest.raises(ProbeUnavailable):
        runtime._embed_documents_with_retry(cast(Any, provider), ("one",), cast(Any, object()))

    assert provider.calls == 4


def test_project_status_reports_scoped_acl_block_before_claiming_query_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime, "_registered_project", lambda *_: "project-1")
    state = tmp_path / "project" / "runtime" / "rag-state.json"
    index = tmp_path / "index" / "knowledge.sqlite3"
    state.parent.mkdir(parents=True)
    index.parent.mkdir(parents=True)
    (tmp_path / "manifest").mkdir()
    state.write_text("{}", encoding="utf-8")
    index.write_bytes(b"")
    monkeypatch.setattr(
        runtime,
        "_existing_runtime_paths",
        lambda *_: {
            "home": tmp_path,
            "project_root": tmp_path / "project",
            "index_root": tmp_path / "index",
            "manifest_root": tmp_path / "manifest",
            "state": state,
            "index": index,
        },
    )
    monkeypatch.setattr(runtime, "private_directory", lambda _path: False)

    result = runtime.project_rag_status(tmp_path, "gpu-fusion")

    assert result == {
        "schema": "zekam-project-rag-status/v1",
        "project_id": "project-1",
        "project_slug": "gpu-fusion",
        "state": "blocked",
        "query_ready": False,
        "blocked_reason": "knowledge-scope-acl-drift",
        "retryable": False,
        "provider_calls": 0,
    }


def test_project_status_does_not_require_private_home_when_scope_is_private(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime, "_registered_project", lambda *_: "project-1")
    state = tmp_path / "project" / "runtime" / "rag-state.json"
    index = tmp_path / "index" / "knowledge.sqlite3"
    state.parent.mkdir(parents=True)
    index.parent.mkdir(parents=True)
    (tmp_path / "manifest").mkdir()
    state.write_text("{}", encoding="utf-8")
    index.write_bytes(b"")
    monkeypatch.setattr(
        runtime,
        "_existing_runtime_paths",
        lambda *_: {
            "home": tmp_path,
            "project_root": tmp_path / "project",
            "index_root": tmp_path / "index",
            "manifest_root": tmp_path / "manifest",
            "state": state,
            "index": index,
        },
    )
    directory_checks: list[Path] = []

    def private_scope(path: Path) -> bool:
        directory_checks.append(path)
        return True

    monkeypatch.setattr(runtime, "private_directory", private_scope)
    file_checks: list[Path] = []

    def private_file(path: Path) -> bool:
        file_checks.append(path)
        return True

    monkeypatch.setattr(runtime, "private_regular", private_file)

    class _Index:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Index:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def generation(self, _project_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                generation_digest="sha256:generation",
                chunk_count=1,
                source_revision="revision",
                tree_digest="sha256:tree",
                provider_profile_digest="sha256:provider",
            )

        def integrity(self) -> dict[str, str]:
            return {"status": "passed"}

    state.write_text(
        json.dumps({"generation_digest": "sha256:generation"}), encoding="utf-8"
    )
    monkeypatch.setattr(runtime, "SQLiteKnowledgeIndex", _Index)

    result = runtime.project_rag_status(tmp_path, "gpu-fusion")

    assert result["state"] == "ready"
    assert result["query_ready"] is True
    assert directory_checks == [
        tmp_path / "project",
        tmp_path / "index",
        tmp_path / "manifest",
    ]
    assert file_checks == [state, index]
    assert tmp_path not in directory_checks


@pytest.mark.parametrize(
    ("kind", "name"),
    (
        ("directory", "project_root"),
        ("directory", "index_root"),
        ("directory", "manifest_root"),
        ("file", "state"),
        ("file", "index"),
    ),
)
def test_rag_scope_rejects_each_scoped_acl_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, name: str
) -> None:
    project_root = tmp_path / "project"
    index_root = tmp_path / "index"
    manifest_root = tmp_path / "manifest"
    state = project_root / "runtime" / "rag-state.json"
    index = index_root / "knowledge.sqlite3"
    state.parent.mkdir(parents=True)
    index_root.mkdir()
    manifest_root.mkdir()
    state.write_text("{}", encoding="utf-8")
    index.write_bytes(b"")
    paths = {
        "home": tmp_path,
        "project_root": project_root,
        "index_root": index_root,
        "manifest_root": manifest_root,
        "state": state,
        "index": index,
    }
    drifted = paths[name]
    monkeypatch.setattr(
        runtime, "private_directory", lambda path: not (kind == "directory" and path == drifted)
    )
    monkeypatch.setattr(
        runtime, "private_regular", lambda path: not (kind == "file" and path == drifted)
    )

    assert runtime._rag_scope_is_private(paths) is False


def test_scoped_path_rejects_missing_and_outside_home(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    missing = root / "missing"
    outside = tmp_path / "outside-rag-index"
    outside.write_bytes(b"")

    assert runtime._scoped_path_is_private(root, missing, directory=False) is False
    assert runtime._scoped_path_is_private(root, outside, directory=False) is False


def test_existing_paths_preserve_lexical_project_path_for_alias_detection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime.HomeLayout, "verify", lambda _self: [])
    monkeypatch.setattr(
        runtime.HomeLayout,
        "project_root",
        lambda *_: (_ for _ in ()).throw(AssertionError("must stay lexical")),
    )

    paths = runtime._existing_runtime_paths(tmp_path, "gpu-fusion")

    assert paths["project_root"] == tmp_path / "projeler" / "gpu-fusion"


def test_read_surfaces_reject_acl_drift_without_runtime_self_heal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    state = project_root / "runtime" / "rag-state.json"
    index = tmp_path / "index" / "knowledge.sqlite3"
    binding = project_root / "baglantilar" / "source.json"
    state.parent.mkdir(parents=True)
    index.parent.mkdir(parents=True)
    binding.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    index.write_bytes(b"")
    binding.write_text("{}", encoding="utf-8")
    paths = {
        "home": tmp_path,
        "project_root": project_root,
        "index_root": index.parent,
        "manifest_root": tmp_path / "manifest",
        "state": state,
        "index": index,
    }
    monkeypatch.setattr(runtime, "_registered_project", lambda *_: "project-1")
    monkeypatch.setattr(runtime, "_existing_runtime_paths", lambda *_: paths)
    monkeypatch.setattr(runtime, "_rag_scope_is_private", lambda _paths: False)
    monkeypatch.setattr(runtime, "_scoped_path_is_private", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        runtime,
        "_runtime_paths",
        lambda *_: (_ for _ in ()).throw(AssertionError("read path must not self-heal")),
    )

    with pytest.raises(ValidationFailed, match="source binding bulunamadi"):
        runtime.resolve_project_source(tmp_path, "gpu-fusion")
    with pytest.raises(PolicyViolation, match="scoped ACL/identity drift"):
        runtime.read_project_citation(tmp_path, "gpu-fusion", "chunk-1")
    with pytest.raises(PolicyViolation, match="scoped ACL/identity drift"):
        runtime._query(
            tmp_path,
            tmp_path,
            cast(Any, "project-1"),
            "gpu-fusion",
            tmp_path / "opencode.json",
            "question",
        )
