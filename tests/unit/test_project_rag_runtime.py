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


def test_generation_chunk_scope_changes_when_provider_profile_changes() -> None:
    common = {
        "project_id": "project-1",
        "source_revision": digest("source-revision"),
        "tree_digest": digest("tree-digest"),
        "source_manifest_digest": digest("source-manifest"),
        "embedding_profile_digest": digest("embedding-profile"),
        "chunk_fingerprints": (
            ("logical-chunk", digest("content"), digest("vector")),
        ),
    }

    first_scope = runtime._generation_chunk_scope_digest(
        **common,
        provider_profile_digest=digest("provider-profile-a"),
    )
    second_scope = runtime._generation_chunk_scope_digest(
        **common,
        provider_profile_digest=digest("provider-profile-b"),
    )

    assert first_scope != second_scope
    assert runtime._generation_chunk_id("logical-chunk", first_scope) != (
        runtime._generation_chunk_id("logical-chunk", second_scope)
    )
    assert first_scope == runtime._generation_chunk_scope_digest(
        **common,
        provider_profile_digest=digest("provider-profile-a"),
    )


def test_generation_chunk_scope_changes_when_vector_changes() -> None:
    common = {
        "project_id": "project-1",
        "source_revision": digest("source-revision"),
        "tree_digest": digest("tree-digest"),
        "source_manifest_digest": digest("source-manifest"),
        "embedding_profile_digest": digest("embedding-profile"),
        "provider_profile_digest": digest("provider-profile"),
    }

    first_scope = runtime._generation_chunk_scope_digest(
        **common,
        chunk_fingerprints=(
            ("logical-chunk", digest("content"), digest("vector-a")),
        ),
    )
    second_scope = runtime._generation_chunk_scope_digest(
        **common,
        chunk_fingerprints=(
            ("logical-chunk", digest("content"), digest("vector-b")),
        ),
    )

    assert first_scope != second_scope


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
    monkeypatch.setattr(runtime, "load_smart_binding", lambda *_, **__: None)
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

        def generation(self, _project_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                generation_digest=state["generation_digest"],
                source_revision=state["source_revision"],
                tree_digest=state["tree_digest"],
                provider_profile_digest=digest("provider-profile"),
            )

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
    assert persisted["query_verification"]["retrieval_digest"] == result["retrieval_digest"]
    assert result["query_verification_recorded"] is True


def test_wp7_query_runtime_propagates_retrieval_only_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """WP7-A-1 / C-1 (B08): ``_query`` passes the embedded retrieval-only answer
    semantics through to the consumer unchanged — generation_state stays
    not_generated and answer_kind stays retrieval_evidence, never a fabricated
    generated answer — while the legacy ``state`` field is preserved."""
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
    monkeypatch.setattr(runtime, "load_smart_binding", lambda *_, **__: None)
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

        def generation(self, _project_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                generation_digest=state["generation_digest"],
                source_revision=state["source_revision"],
                tree_digest=state["tree_digest"],
                provider_profile_digest=digest("provider-profile"),
            )

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
                # WP7 additive fields already present on the embedded result.
                "evidence_found": True,
                "retrieval_state": "answered-evidence",
                "generation_state": "not_generated",
                "answer_kind": "retrieval_evidence",
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

    # Legacy state preserved and WP7 additive fields pass through untouched.
    assert result["state"] == "answered"
    assert result["evidence_found"] is True
    assert result["retrieval_state"] == "answered-evidence"
    assert result["generation_state"] == "not_generated"
    assert result["answer_kind"] == "retrieval_evidence"
    assert result["answer_kind"] != "generated_answer"


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
    monkeypatch.setattr(runtime, "load_smart_binding", lambda *_, **__: None)
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

        def generation(self, _project_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                generation_digest=state["generation_digest"],
                source_revision=state["source_revision"],
                tree_digest=state["tree_digest"],
                provider_profile_digest=digest("provider-profile"),
            )

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


def test_stale_project_source_queries_pinned_snapshot_without_remote_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.md").write_text("new source", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    project_id = uuid4()
    project_root = home / "project"
    state_path = project_root / "runtime" / "rag-state.json"
    index_path = home / "index" / "knowledge.sqlite3"
    state_path.parent.mkdir(parents=True)
    index_path.parent.mkdir()
    index_path.write_bytes(b"")
    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.REMOTE)
    state_path.write_text(
        json.dumps(
            {
                "project_id": str(project_id),
                "repository_source_revision": "older-revision",
                "repository_tree_digest": digest("older-tree"),
                "odi_source_digest": None,
                "knowledge_binding_digest": runtime._knowledge_binding_digest(knowledge),
                "generation_digest": digest("generation"),
                "source_revision": digest("indexed-source"),
                "tree_digest": digest("indexed-tree"),
                "database_access": "disabled",
            }
        ),
        encoding="utf-8",
    )
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
    monkeypatch.setattr(runtime, "load_smart_binding", lambda *_, **__: None)
    monkeypatch.setattr(
        runtime, "load_settings", lambda **_: SimpleNamespace(knowledge=knowledge)
    )
    monkeypatch.setattr(
        runtime,
        "_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("remote provider must not be called")
        ),
    )

    class _Index:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Index:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def generation(self, _project_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                generation_digest=digest("generation"),
                source_revision=digest("indexed-source"),
                tree_digest=digest("indexed-tree"),
                provider_profile_digest=digest("provider-profile"),
            )

    observed: dict[str, object] = {}

    class _RAG:
        def __init__(self, *_args: object) -> None:
            pass

        def query(self, *_args: object, **kwargs: object) -> dict[str, object]:
            observed.update(kwargs)
            return {
                "state": "lexical-only-degraded",
                "searched_channels": ["exact", "lexical"],
                "degraded_reason": "remote-query-not-authorized",
                "generation_digest": digest("generation"),
                "provider_profile_digest": digest("provider-profile"),
                "stale_reasons": [],
            }

    monkeypatch.setattr(runtime, "SQLiteKnowledgeIndex", _Index)
    monkeypatch.setattr(runtime, "EmbeddedProjectRAG", _RAG)

    result = runtime._query(
        source,
        home,
        project_id,
        "stale-project",
        None,
        "where?",
        authorize_remote_query=False,
    )

    assert result["state"] == "lexical-only-degraded"
    assert result["index_freshness"] == "stale"
    assert result["snapshot_only"] is True
    assert result["remote_provider_used"] is False
    assert set(result["stale_reasons"]) == {
        "project-source-revision-stale",
        "project-source-tree-stale",
    }
    assert observed["dense_disabled_reason"] == "remote-query-not-authorized"


@pytest.mark.parametrize(
    "drifted_field",
    ["generation_digest", "source_revision", "tree_digest"],
)
def test_query_rejects_state_generation_identity_drift_before_provider_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, drifted_field: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.md").write_text("indexed source", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    project_id = uuid4()
    project_root = home / "project"
    state_path = project_root / "runtime" / "rag-state.json"
    index_path = home / "index" / "knowledge.sqlite3"
    state_path.parent.mkdir(parents=True)
    index_path.parent.mkdir()
    index_path.write_bytes(b"")
    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.REMOTE)
    generation_identity = {
        "generation_digest": digest("generation"),
        "source_revision": digest("indexed-source"),
        "tree_digest": digest("indexed-tree"),
    }
    state_identity = generation_identity | {drifted_field: digest("tampered")}
    state_path.write_text(
        json.dumps(
            {
                "project_id": str(project_id),
                "repository_source_revision": runtime._git_source_state(source)[2],
                "repository_tree_digest": runtime.discover(source).tree_digest,
                "odi_source_digest": None,
                "knowledge_binding_digest": runtime._knowledge_binding_digest(knowledge),
                **state_identity,
            }
        ),
        encoding="utf-8",
    )
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
    monkeypatch.setattr(runtime, "load_smart_binding", lambda *_, **__: None)
    monkeypatch.setattr(
        runtime, "load_settings", lambda **_: SimpleNamespace(knowledge=knowledge)
    )
    monkeypatch.setattr(
        runtime,
        "_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider must not be called on binding drift")
        ),
    )

    class _Index:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Index:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def generation(self, _project_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                **generation_identity,
                provider_profile_digest=digest("provider-profile"),
            )

    monkeypatch.setattr(runtime, "SQLiteKnowledgeIndex", _Index)

    with pytest.raises(PolicyViolation, match="state/index generation binding drift"):
        runtime._query(
            source,
            home,
            project_id,
            "bound-project",
            None,
            "where?",
            authorize_remote_query=True,
        )


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
        "query_available": False,
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


def test_runtime_paths_skips_rehardening_when_acl_already_private(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime.HomeLayout, "verify", lambda _self: [])
    project_root = tmp_path / "projeler" / "slug"
    monkeypatch.setattr(
        runtime.HomeLayout, "ensure_project", lambda _self, _slug: project_root
    )
    restricted: list[Path] = []
    monkeypatch.setattr(runtime, "restrict_private_tree", restricted.append)
    monkeypatch.setattr(runtime, "private_directory", lambda _path: True)

    paths = runtime._runtime_paths(tmp_path, "slug")

    assert restricted == []
    assert paths["project_root"] == project_root
    assert paths["index_root"] == (
        tmp_path / "knowledge-index" / "vector" / "opencode-bge-m3" / "slug"
    )
    assert (
        paths["manifest_root"] == tmp_path / "knowledge-index" / "manifests" / "slug"
    )


def test_runtime_paths_still_hardens_and_raises_when_acl_not_private(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime.HomeLayout, "verify", lambda _self: [])
    project_root = tmp_path / "projeler" / "slug"
    monkeypatch.setattr(
        runtime.HomeLayout, "ensure_project", lambda _self, _slug: project_root
    )
    restricted: list[Path] = []
    monkeypatch.setattr(runtime, "restrict_private_tree", restricted.append)
    monkeypatch.setattr(runtime, "private_directory", lambda path: path != project_root)

    with pytest.raises(PolicyViolation, match="private ACL"):
        runtime._runtime_paths(tmp_path, "slug")

    assert project_root in restricted


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


def test_warm_query_does_not_repeat_provider_qualification_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B01 WP1 regression: expected to FAIL on baseline, PASS after WP2.

    A warm, already-qualified provider identity must NOT re-run the probe for a
    second authorized query.  On the current baseline ``_provider`` calls
    ``provider.probe(fixture)`` unconditionally on every call, so a second warm
    query probes again.  WP2 separates qualification from query use; after WP2
    two warm ``_provider`` calls over the same accepted identity run the probe
    exactly once.
    """
    import zekam.infrastructure.query_measurement as qm
    from zekam.domain.security import DataClassification

    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.REMOTE)
    probe_runs: list[int] = []

    class _FakeRemoteProvider:
        def __init__(self, configuration, executor, *, dimension, max_batch_size):
            del executor, dimension, max_batch_size
            self._configuration = configuration
            self._profile_digest = digest("provider-profile")

        def probe(self, _fixture):
            probe_runs.append(1)
            return SimpleNamespace(
                profile=SimpleNamespace(
                    profile_digest=self._profile_digest,
                    model_revision_fingerprint=digest("revision"),
                    provider_identity_digest=digest("provider"),
                    exact_model_id=knowledge.embedding_model_ref,
                    dimension=knowledge.embedding_dimension,
                    vector_dtype="float32",
                    normalized=True,
                    distance_metric="cosine",
                    query_prefix="",
                    passage_prefix="",
                    preprocessor_digest=digest("pre"),
                    tokenizer_digest=digest("tok"),
                    batch_policy_digest=digest("batch"),
                    device_scope="windows:x64:opencode",
                    data_classification_allowlist=(DataClassification.PUBLIC,),
                    verified_at="2026-09-02T00:00:00Z",
                    probe_evidence_digest=digest("probe"),
                    validate_vector=lambda _vector: None,
                ),
                semantic_margin=0.4,
                positive_score=0.7,
                negative_score=0.3,
                max_repeat_delta=0.0001,
                max_batch_delta=0.0001,
                batch_cosine=0.9999,
                latency_ms=1,
                evidence_digest=digest("probe"),
                provider_call_count=2,
            )

    monkeypatch.setattr(runtime, "OpenCodeRemoteEmbeddingProvider", _FakeRemoteProvider)
    monkeypatch.setattr(runtime, "ProcessIsolatedJsonProviderTransport", lambda *_: object())
    monkeypatch.setattr(runtime, "LiveProcessClient", lambda *_a, **_k: object())
    monkeypatch.setattr(runtime, "RuntimeOpenCodeEmbeddingExecutor", lambda _invocation: object())
    monkeypatch.setattr(runtime, "RuntimeProviderContractRunner", lambda *a, **k: object())
    monkeypatch.setattr(runtime, "load_inventory", lambda *_: object())

    class _FakeHost:
        def register(self, _work):
            return None

        def summary(self):
            return {
                "schema": "zekam-local-provider-ledger-summary/v1",
                "provider_calls": 0,
                "durable_remote_effects": 0,
            }

    monkeypatch.setattr(runtime, "SQLiteProviderLedgerHost", lambda *_a, **_k: _FakeHost())

    def _load_config(*_args, **_kwargs):
        return SimpleNamespace(
            provider_id="litellm",
            canonical_model_id="openai/BAAI/bge-m3",
            selected_model_id=knowledge.embedding_model_ref,
            credential_locator="OPENCODE_LITELLM_KEY",
            embedding_endpoint="https://models.example.test/v1/embeddings",
            endpoint_identity=SimpleNamespace(identity_digest=digest("endpoint")),
        )

    monkeypatch.setattr(runtime, "load_opencode_embedding_configuration", _load_config)

    with qm.scope():
        runtime._provider(
            tmp_path,
            tmp_path / "ledger.sqlite3",
            tmp_path / "opencode.json",
            uuid4(),
            (),
            knowledge,
            remote_authorized=True,
        )
        # Second warm query over the same accepted provider identity.  A cached
        # qualification must NOT probe again.
        runtime._provider(
            tmp_path,
            tmp_path / "ledger2.sqlite3",
            tmp_path / "opencode.json",
            uuid4(),
            (),
            knowledge,
            remote_authorized=True,
        )

    # Regression: the warm second query must not repeat the probe.  On baseline
    # two probes ran (qualification ran twice); WP2 must make it exactly one.
    assert len(probe_runs) == 1


def _git_repo(root: Path, files: dict[str, str]) -> None:
    """Initialise a committed Git repo at ``root`` with the given files."""
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)


def _indexed_state_for_git(root: Path, head_rev: str) -> dict[str, Any]:
    """Build a state dict whose repository revision/head matches ``root`` and which
    records per-file indexed content digests (content of current working files)."""
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        digests[relative] = digest_of_bytes(path.read_bytes())
    return {
        "repository_source_revision": head_rev,
        "repository_tree_digest": digest("indexed-tree"),
        "source_files": [
            {"path": path, "content_digest": content} for path, content in digests.items()
        ],
    }


def test_wp3_same_status_changed_content_is_not_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """WP3-B-1 regression: expected to FAIL on baseline, PASS after WP3.

    Two states with the SAME ``git status --porcelain`` text but DIFFERENT dirty
    file content must be detected as a content change and NOT reported current.
    On the baseline the freshness keyed only on ``head:status:<status_digest>``,
    hashing the status bytes (not the dirty file content), so identical status
    text with different content collided and was treated as current.  WP3 hashes
    the on-disk content of exactly the changed indexed paths and compares against
    the indexed content digest, so the same status text with changed content must
    surface ``project-source-content-stale``.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git_repo(root, {"f.md": "original-committed\n"})

    # Indexed state corresponds to dirty content A (status text " M f.md").
    (root / "f.md").write_text("content-A dirty\n", encoding="utf-8")
    state = _indexed_state_for_git(root, runtime._git_source_state(root)[2])
    status_a = runtime._git_source_state(root)[2]

    # Baseline behaviour: only the status digest was compared against the recorded
    # revision.  With " M f.md" the status digest was the recorded one already, so
    # the result was "current" regardless of file content -- this is the collision.
    baseline_reasons_at_a, _ = runtime._source_freshness_for_query(state, root)
    # At the indexed content the query is current (no content change detected).
    assert "project-source-content-stale" not in baseline_reasons_at_a

    # Change the content WITHOUT changing the git status text (still " M f.md").
    (root / "f.md").write_text("content-B dirty, same status text\n", encoding="utf-8")
    status_b = runtime._git_source_state(root)[2]
    # Identical status text => identical revision (this was the baseline collision).
    assert status_b == status_a

    reasons, _obs = runtime._source_freshness_for_query(state, root)
    # WP3: identical status text but different content must NOT be "current".
    assert "project-source-content-stale" in reasons
    assert "project-source-freshness-unknown" not in reasons


def test_wp3_warm_local_query_skips_corpus_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """WP3-A-1 regression: expected to FAIL on baseline, PASS after WP3.

    A warm local-route answer query must NOT invoke ``_project_plan`` and must not
    build/hand the whole corpus plan chunks to the provider for qualification.
    On the baseline ``_query`` called ``_project_plan(source_root, ...)`` on every
    local query to rebuild the corpus plan and pass ``query_chunks`` into
    ``_provider``/``build_verified_mac_embedding``.  WP3 removes that: the local
    route qualifies from the accepted bounded synthetic fixture only.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.md").write_text("# warm local query source", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    project_id = uuid4()
    knowledge = KnowledgeSettings(embedding_route=EmbeddingRoute.LOCAL)
    project_root = home / "project"
    state_path = project_root / "runtime" / "rag-state.json"
    index_path = home / "index" / "knowledge.sqlite3"
    state_path.parent.mkdir(parents=True)
    index_path.parent.mkdir()
    index_path.write_bytes(b"")
    state_path.write_text(
        json.dumps(
            {
                "project_id": str(project_id),
                "repository_source_revision": runtime._git_source_state(source)[2],
                "repository_tree_digest": runtime.discover(source).tree_digest,
                "odi_source_digest": None,
                "knowledge_binding_digest": runtime._knowledge_binding_digest(knowledge),
                "generation_digest": digest("generation"),
                "source_revision": digest("indexed-source"),
                "tree_digest": digest("indexed-tree"),
                "database_access": "disabled",
                "source_files": [],
            }
        ),
        encoding="utf-8",
    )
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
    monkeypatch.setattr(runtime, "load_smart_binding", lambda *_, **__: None)
    monkeypatch.setattr(
        runtime, "load_settings", lambda **_: SimpleNamespace(knowledge=knowledge)
    )
    monkeypatch.setattr(runtime, "_runtime_platform", lambda: "darwin")

    class _LocalBinding:
        provider = SimpleNamespace(
            describe=lambda: SimpleNamespace(profile_digest=digest("local-profile"))
        )
        profile = SimpleNamespace(
            exact_model_id="BAAI/bge-m3",
            dimension=1024,
            profile_digest=digest("local-profile"),
            probe_evidence_digest=digest("local-probe"),
        )
        policy = object()

    monkeypatch.setattr(runtime, "_build_local_query_embedding", lambda: _LocalBinding())

    # The plan must NOT be built: make _project_plan fail loudly if called.
    monkeypatch.setattr(
        runtime,
        "_project_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("query path must not build the corpus plan (WP3-A)")
        ),
    )

    class _Index:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args) -> None:  # type: ignore[no-untyped-def]
            pass

        def generation(self, _project_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                generation_digest=digest("generation"),
                source_revision=digest("indexed-source"),
                tree_digest=digest("indexed-tree"),
                provider_profile_digest=digest("provider-profile"),
            )

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
        "warm-local",
        None,
        "where?",
    )

    assert result["index_freshness"] == "current"
    assert result["embedding_route"] == "local"


def test_wp3_deleted_source_is_freshness_unknown(
    tmp_path: Path,
) -> None:
    """WP3-B-2 regression: expected to degrade like the baseline and PASS after WP3.

    A deleted source must never be silently served as "current".  The query path
    reports an explicit ``project-source-freshness-unknown`` state (never a silent
    current) when an indexed path is deleted / inaccessible / symlink-junction.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git_repo(root, {"f.md": "original-committed\n"})
    head_rev = runtime._git_source_state(root)[2]
    state = _indexed_state_for_git(root, head_rev)

    # Baseline "current" (unchanged).
    reasons, _obs = runtime._source_freshness_for_query(state, root)
    assert reasons == []

    # Delete the indexed source file.
    (root / "f.md").unlink()
    reasons, obs = runtime._source_freshness_for_query(state, root)
    assert "project-source-freshness-unknown" in reasons
    assert obs.get("unknown") is True



