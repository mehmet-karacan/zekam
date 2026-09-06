from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from zekam.application import project_rag_runtime as runtime
from zekam.application.model_health_service import ProbeUnavailable
from zekam.domain.canonical import digest_of_bytes
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
