from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from zekam.application import project_rag_runtime as runtime
from zekam.application.config import EmbeddingRoute, KnowledgeSettings
from zekam.application.embedding_routing import EmbeddingRouteCandidate, EmbeddingRouteKind
from zekam.application.model_health_service import ProbeUnavailable
from zekam.domain.canonical import digest, digest_of_bytes
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
        embedding_profile=SimpleNamespace(model_ref=runtime.MODEL_ID, dimension=1024),
        embedding_route=SimpleNamespace(
            sanitized=lambda: {"kind": "local-provider", "model_ref": runtime.MODEL_ID}
        ),
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


def test_runtime_plan_records_the_real_qualified_remote_route(tmp_path: Path) -> None:
    (tmp_path / "source.md").write_text("# source\nremote plan", encoding="utf-8")
    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.REMOTE)
    candidate = EmbeddingRouteCandidate(
        model_ref=knowledge.embedding_model_ref,
        dimension=knowledge.embedding_dimension,
        qualified=True,
        health_fresh=True,
        verified=True,
        latency_ms=1.0,
        semantic_margin=0.5,
        qualification_evidence_digest=digest("qualification"),
        probe_evidence_digest=digest("probe"),
    )

    _, plan = runtime._project_plan(
        tmp_path,
        project_id=uuid4(),
        project_slug="remote-project",
        knowledge=knowledge,
        embedding_candidates=(candidate,),
        allow_remote_source=True,
    )

    assert plan.embedding_route.kind is EmbeddingRouteKind.QUALIFIED_REMOTE
    assert plan.embedding_profile.model_ref == knowledge.embedding_model_ref


def test_remote_provider_refuses_calls_without_explicit_service_authorization(
    tmp_path: Path,
) -> None:
    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.REMOTE)

    with pytest.raises(PolicyViolation, match="explicit authorization"):
        runtime._provider(
            tmp_path,
            tmp_path / "ledger.sqlite3",
            None,
            uuid4(),
            (),
            knowledge,
            remote_authorized=False,
        )


def test_local_provider_route_does_not_require_remote_config_or_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = SimpleNamespace(
        exact_model_id="BAAI/bge-m3",
        dimension=1024,
        profile_digest=digest("local-profile"),
        probe_evidence_digest=digest("local-probe"),
    )
    local = SimpleNamespace(provider=object(), policy=object(), profile=profile)
    monkeypatch.setattr(runtime, "_runtime_platform", lambda: "darwin")
    monkeypatch.setattr(runtime, "build_verified_mac_embedding", lambda _chunks: local)
    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.LOCAL)

    binding = runtime._provider(
        tmp_path,
        tmp_path / "ledger.sqlite3",
        None,
        uuid4(),
        (),
        knowledge,
        remote_authorized=False,
    )

    assert binding.route is EmbeddingRoute.LOCAL
    assert binding.remote_provider_used is False


def test_local_provider_route_fails_closed_outside_macos(tmp_path: Path) -> None:
    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.LOCAL)

    with pytest.raises(PolicyViolation, match="yalniz macOS"):
        runtime._provider(
            tmp_path,
            tmp_path / "ledger.sqlite3",
            None,
            uuid4(),
            (),
            knowledge,
            remote_authorized=False,
        )


def test_remote_provider_uses_canonical_config_provider_and_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}
    knowledge = KnowledgeSettings(
        embedding_route=EmbeddingRoute.REMOTE,
        remote_provider_id="reviewed-provider",
        embedding_model_ref="reviewed/model",
    )

    def stop_after_config(  # type: ignore[no-untyped-def]
        _path, *, provider_id, selected_model_id, inventory
    ):
        observed.update(
            provider_id=provider_id,
            selected_model_id=selected_model_id,
            inventory=inventory,
        )
        raise ValidationFailed("stop-after-config-binding")

    monkeypatch.setattr(runtime, "load_opencode_embedding_configuration", stop_after_config)

    with pytest.raises(ValidationFailed, match="stop-after-config-binding"):
        runtime._provider(
            tmp_path,
            tmp_path / "ledger.sqlite3",
            tmp_path / "opencode.json",
            uuid4(),
            (),
            knowledge,
            remote_authorized=True,
        )

    assert observed["provider_id"] == "reviewed-provider"
    assert observed["selected_model_id"] == "reviewed/model"


def test_successful_query_persists_scoped_verification_timestamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.md").write_text("# verified query source", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    project_id = uuid4()
    repository_revision = runtime._git_source_state(source)[2]
    repository_tree = runtime.discover(source).tree_digest
    project_root = home / "project"
    state_path = project_root / "runtime" / "rag-state.json"
    index_path = home / "index" / "knowledge.sqlite3"
    state_path.parent.mkdir(parents=True)
    index_path.parent.mkdir()
    index_path.write_bytes(b"")
    state = {
        "project_id": str(project_id),
        "repository_source_revision": repository_revision,
        "repository_tree_digest": repository_tree,
        "odi_source_digest": None,
        "embedding_profile_id": "bge-m3-dense-v1",
        "embedding_route": "remote",
        "knowledge_binding_digest": runtime._knowledge_binding_digest(
            KnowledgeSettings(embedding_route=EmbeddingRoute.REMOTE)
        ),
        "generation_digest": digest("generation"),
        "source_revision": digest("combined-revision"),
        "tree_digest": digest("combined-tree"),
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    paths = {
        "home": home,
        "project_root": project_root,
        "index_root": index_path.parent,
        "manifest_root": home / "manifest",
        "state": state_path,
        "index": index_path,
        "ledger": project_root / "runtime" / "provider-ledger.sqlite3",
    }
    monkeypatch.setattr(runtime, "_existing_runtime_paths", lambda *_: paths)
    monkeypatch.setattr(runtime, "_rag_scope_is_private", lambda _paths: True)
    monkeypatch.setattr(runtime, "load_smart_binding", lambda *_: None)
    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.REMOTE)
    monkeypatch.setattr(
        runtime,
        "load_settings",
        lambda **_kwargs: SimpleNamespace(knowledge=knowledge),
    )
    profile = SimpleNamespace(profile_digest=digest("provider-profile"))
    provider = SimpleNamespace(describe=lambda: profile)
    binding = runtime._EmbeddingBinding(
        provider=cast(Any, provider),
        policy=cast(Any, object()),
        ledger={},
        probe={"probe_evidence_digest": digest("probe")},
        route=EmbeddingRoute.REMOTE,
        remote_provider_used=True,
        probe_call_count=2,
    )
    monkeypatch.setattr(runtime, "_provider", lambda *_args, **_kwargs: binding)

    class _Index:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args) -> None:  # type: ignore[no-untyped-def]
            pass

    monkeypatch.setattr(runtime, "SQLiteKnowledgeIndex", _Index)
    monkeypatch.setattr(
        runtime,
        "EmbeddedProjectRAG",
        lambda *_args: SimpleNamespace(
            query=lambda *_args, **_kwargs: {
                "state": "answered",
                "retrieval_digest": digest("retrieval"),
                "searched_channels": ["exact", "lexical", "dense"],
                "degraded_reason": None,
                "provider_profile_digest": digest("provider-profile"),
                "generation_digest": digest("generation"),
            }
        ),
    )

    result = runtime._query(
        source,
        home,
        project_id,
        "verified-project",
        tmp_path / "opencode.json",
        "where?",
        authorize_remote_query=True,
    )

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["query_verified_at"].endswith("Z")
    assert persisted["query_verified_at"] == result["query_verified_at"]
    assert persisted["query_verification"]["retrieval_digest"] == digest("retrieval")
    assert result["query_verification_recorded"] is True


def test_degraded_query_does_not_overwrite_verified_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.md").write_text("# degraded query source", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    project_id = uuid4()
    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.REMOTE)
    old_verified_at = "2026-09-01T00:00:00Z"
    project_root = home / "project"
    state_path = project_root / "runtime" / "rag-state.json"
    index_path = home / "index" / "knowledge.sqlite3"
    state_path.parent.mkdir(parents=True)
    index_path.parent.mkdir()
    index_path.write_bytes(b"")
    state = {
        "project_id": str(project_id),
        "repository_source_revision": runtime._git_source_state(source)[2],
        "repository_tree_digest": runtime.discover(source).tree_digest,
        "odi_source_digest": None,
        "embedding_profile_id": knowledge.embedding_profile_id,
        "embedding_route": knowledge.embedding_route.value,
        "knowledge_binding_digest": runtime._knowledge_binding_digest(knowledge),
        "generation_digest": digest("generation"),
        "source_revision": digest("combined-revision"),
        "tree_digest": digest("combined-tree"),
        "query_verified_at": old_verified_at,
        "query_verification": {"retrieval_digest": digest("old")},
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    paths = {
        "home": home,
        "project_root": project_root,
        "index_root": index_path.parent,
        "manifest_root": home / "manifest",
        "state": state_path,
        "index": index_path,
        "ledger": project_root / "runtime" / "provider-ledger.sqlite3",
    }
    monkeypatch.setattr(runtime, "_existing_runtime_paths", lambda *_: paths)
    monkeypatch.setattr(runtime, "_rag_scope_is_private", lambda _paths: True)
    monkeypatch.setattr(runtime, "load_smart_binding", lambda *_: None)
    monkeypatch.setattr(
        runtime, "load_settings", lambda **_kwargs: SimpleNamespace(knowledge=knowledge)
    )
    profile = SimpleNamespace(profile_digest=digest("provider-profile"))
    provider = SimpleNamespace(describe=lambda: profile)
    binding = runtime._EmbeddingBinding(
        provider=cast(Any, provider),
        policy=cast(Any, object()),
        ledger={},
        probe={"probe_evidence_digest": digest("probe")},
        route=EmbeddingRoute.REMOTE,
        remote_provider_used=True,
        probe_call_count=2,
    )
    monkeypatch.setattr(runtime, "_provider", lambda *_args, **_kwargs: binding)

    class _Index:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args) -> None:  # type: ignore[no-untyped-def]
            pass

    monkeypatch.setattr(runtime, "SQLiteKnowledgeIndex", _Index)
    monkeypatch.setattr(
        runtime,
        "EmbeddedProjectRAG",
        lambda *_args: SimpleNamespace(
            query=lambda *_args, **_kwargs: {
                "state": "lexical-only-degraded",
                "retrieval_digest": digest("degraded"),
                "searched_channels": ["exact", "lexical"],
                "degraded_reason": "provider-health-unavailable",
                "provider_profile_digest": digest("provider-profile"),
                "generation_digest": digest("generation"),
            }
        ),
    )

    result = runtime._query(
        source,
        home,
        project_id,
        "degraded-project",
        tmp_path / "opencode.json",
        "where?",
        authorize_remote_query=True,
    )

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted == state
    assert result["query_verified_at"] == old_verified_at
    assert result["query_verification_recorded"] is False


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


def test_ordinary_directory_uses_digest_bound_source_identity(tmp_path: Path) -> None:
    (tmp_path / "source.md").write_text("alpha", encoding="utf-8")

    head, status_digest, revision = runtime._git_source_state(tmp_path)

    assert head == ""
    assert len(status_digest) == 64
    assert revision.startswith("directory:sha256:")
    (tmp_path / "source.md").write_text("beta", encoding="utf-8")
    assert runtime._git_source_state(tmp_path)[2] != revision


def test_source_binding_preflight_validates_exact_directory_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "T\u00fcrk\u00e7e Proje"
    source.mkdir()
    (source / "README.md").write_text("# kaynak", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(runtime, "_registered_project", lambda *_: "project-1")

    plan = runtime.build_project_source_binding_plan(home, "ornek", source)

    assert plan["schema"] == "zekam-project-local-source-binding-plan/v1"
    assert plan["binding_schema"] == "zekam-project-local-source-binding/v1"
    assert plan["project_id"] == "project-1"
    assert plan["source_root"] == str(source.resolve())
    assert plan["source_kind"] == "directory"
    assert plan["source_revision"].startswith("directory:sha256:")
    assert plan["plan_digest"].startswith("sha256:")
    assert plan["apply"] is False
    assert not (home / "projeler" / "ornek" / "baglantilar" / "source.json").exists()


def test_unborn_git_source_is_controlled_validation_error(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    with pytest.raises(ValidationFailed, match="committed HEAD"):
        runtime._git_source_state(tmp_path)


def test_git_marker_never_falls_back_to_directory_when_git_probe_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git unavailable")),
    )

    with pytest.raises(ValidationFailed, match="identity probe"):
        runtime._git_source_state(tmp_path)


def test_broken_git_link_marker_never_downgrades_to_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git unavailable")),
    )
    monkeypatch.setattr(runtime.os, "lstat", lambda path: object())
    monkeypatch.setattr(
        runtime,
        "_directory_source_state",
        lambda _root: (_ for _ in ()).throw(AssertionError("must fail closed")),
    )

    with pytest.raises(ValidationFailed, match="identity probe"):
        runtime._git_source_state(tmp_path)


def test_git_file_marker_is_classified_consistently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime.os, "lstat", lambda path: object())

    assert runtime.classify_project_source(tmp_path) == "git"


def test_index_progress_is_jsonl_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    runtime._emit_index_progress(completed=2, total=3, provider_batches=1)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "progress": 2,
        "provider_batches": 1,
        "total": 3,
    }


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
        "index_readable": False,
        "provider_readiness": "unknown",
        "query_verified_at": None,
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

        def readiness(self, _project_id: str) -> dict[str, str]:
            return {"status": "passed"}

    state.write_text(
        json.dumps(
            {
                "generation_digest": "sha256:generation",
                "embedding_profile_id": "bge-m3-dense-v1",
                "embedding_route": "remote",
                "knowledge_binding_digest": runtime._knowledge_binding_digest(
                    KnowledgeSettings(embedding_route=EmbeddingRoute.REMOTE)
                ),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "SQLiteKnowledgeIndex", _Index)

    result = runtime.project_rag_status(tmp_path, "gpu-fusion")

    assert result["state"] == "ready"
    assert result["index_readable"] is True
    assert result["provider_readiness"] == "unknown"
    assert result["query_verified_at"] is None
    assert result["query_ready"] is False
    assert result["index_rebuild_required"] is False
    assert directory_checks == [
        tmp_path / "project",
        tmp_path / "index",
        tmp_path / "manifest",
    ]
    assert file_checks == [state, index]
    assert tmp_path not in directory_checks

    monkeypatch.setattr(
        runtime,
        "load_settings",
        lambda **_kwargs: SimpleNamespace(
            knowledge=KnowledgeSettings(
                embedding_route=EmbeddingRoute.REMOTE,
                remote_provider_id="different-provider",
            )
        ),
    )
    drifted = runtime.project_rag_status(tmp_path, "gpu-fusion")
    assert drifted["state"] == "index-rebuild-required"
    assert drifted["index_rebuild_required"] is True


def test_legacy_index_without_canonical_route_requires_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    state = project_root / "runtime" / "rag-state.json"
    index = tmp_path / "index" / "knowledge.sqlite3"
    manifest_root = tmp_path / "manifest"
    state.parent.mkdir(parents=True)
    index.parent.mkdir()
    manifest_root.mkdir()
    state.write_text(json.dumps({"generation_digest": "sha256:generation"}), encoding="utf-8")
    index.write_bytes(b"")
    paths = {
        "home": tmp_path,
        "project_root": project_root,
        "index_root": index.parent,
        "manifest_root": manifest_root,
        "state": state,
        "index": index,
    }
    monkeypatch.setattr(runtime, "_registered_project", lambda *_: "project-1")
    monkeypatch.setattr(runtime, "_existing_runtime_paths", lambda *_: paths)
    monkeypatch.setattr(runtime, "_rag_scope_is_private", lambda _paths: True)

    class _Index:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args) -> None:  # type: ignore[no-untyped-def]
            pass

        def generation(self, _project_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                generation_digest="sha256:generation",
                chunk_count=1,
                source_revision="revision",
                tree_digest="sha256:tree",
                provider_profile_digest="sha256:provider",
            )

        def readiness(self, _project_id: str) -> dict[str, str]:
            return {"status": "passed"}

    monkeypatch.setattr(runtime, "SQLiteKnowledgeIndex", _Index)

    result = runtime.project_rag_status(tmp_path, "gpu-fusion")

    assert result["state"] == "index-rebuild-required"
    assert result["index_readable"] is True
    assert result["index_rebuild_required"] is True


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
