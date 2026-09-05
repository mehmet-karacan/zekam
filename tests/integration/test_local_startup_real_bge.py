"""Explicit local BGE qualification, then provider-free startup on ONE real file."""

from __future__ import annotations

import os
import socket
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity_startup import (
    ROOT,
    SOURCE_REF,
    _receipts,
    _request,
    _stage_start,
)
from tests.unit.test_local_continuity_startup import startup as startup
from tests.unit.test_local_startup_retrieval_integration import (
    _attach,
    _build,
    _checkpoint,
    _records,
)

from zekam.application.local_continuity_service import LocalLifecycleContinuity
from zekam.application.local_continuity_startup import LocalStartupService
from zekam.application.local_embedding_composition import build_verified_mac_embedding
from zekam.domain.knowledge import UnitKind
from zekam.domain.retrieval import Chunk
from zekam.domain.security import DataClassification
from zekam.infrastructure.clients.local_continuity_decoder import validate_reviewed_control_entry
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex
from zekam.infrastructure.sqlite.local_continuity_startup import SQLiteStartupSourceResolver
from zekam.infrastructure.sqlite.local_startup_checkpoint import SQLiteStartupCheckpointSource

_SOCKET_CONNECT = socket.socket.connect
_CREATE_CONNECTION = socket.create_connection
pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("ZEKAM_RUN_LOCAL_STARTUP_BGE_E2E") != "1",
    reason="Real single-file local BGE startup evidence requires explicit activation",
)
def test_real_local_bge_index_then_provider_free_startup_and_restart(
    startup: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
) -> None:
    before = (ROOT / SOURCE_REF).read_bytes()
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    allowed_connections: list[tuple[str, int]] = []

    def connect(sock: socket.socket, address: Any) -> Any:
        assert isinstance(address, tuple) and address[:2] == ("127.0.0.1", 7997)
        allowed_connections.append(address[:2])
        return _SOCKET_CONNECT(sock, address)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        assert address == ("127.0.0.1", 7997)
        return _CREATE_CONNECTION(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    records = _records(startup)
    assert {record.source_path for record in records} == {SOURCE_REF}
    chunks = tuple(
        Chunk(
            chunk_id=r.chunk_id,
            document_id="real-health-source",
            text=r.text,
            locator=r.locator,
            kind=UnitKind.CODE,
            token_count=len(r.text.encode()),
            order=r.chunk_order,
        )
        for r in records
    )
    embedding = build_verified_mac_embedding(chunks, classification=DataClassification.LOCAL_ONLY)
    assert embedding.profile.exact_model_id == "BAAI/bge-m3"
    assert embedding.profile.dimension == 1024
    batch = embedding.provider.embed_documents(tuple(r.text for r in records), embedding.policy)
    assert len(batch.vectors) == batch.receipt.vector_count == len(records) == 2
    assert batch.receipt.profile_digest == embedding.profile.profile_digest
    for vector in batch.vectors:
        embedding.profile.validate_vector(vector)
    assert allowed_connections
    real_records = tuple(replace(r, vector=v) for r, v in zip(records, batch.vectors, strict=True))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("After real indexing, startup and restart must not call any provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    for name in ("describe", "health", "embed_query", "embed_documents"):
        monkeypatch.setattr(embedding.provider, name, forbidden)
    with SQLiteKnowledgeIndex(tmp_path / "real-bge-index.sqlite3", create=True) as index:
        value = _attach(startup, index)
        generation = _build(
            value,
            real_records,
            embedding_profile_digest=embedding.profile.profile_digest,
            provider_profile_digest=embedding.profile.profile_digest,
        )
        result = value["service"].hydrate(_request(retrieval_query="SaglikYaniti"))
        assert (
            result["retrieval"]["generation"]["provider_profile_digest"]
            == embedding.profile.profile_digest
        )
        assert result["retrieval"]["state"] == "source-verified-candidates"
        assert result["retrieval"]["fragment_count"] == 1
        assert result["retrieval"]["dense"] == "not-invoked"
        checkpoint = _checkpoint(value, result["manifest_digest"])
    with SQLiteKnowledgeIndex(tmp_path / "real-bge-index.sqlite3", read_only=True) as reopened:
        value = _attach(startup, reopened)
        resumed = value["base"].resume(value["binding"], checkpoint)
        assert resumed["context"]["context"]["grants_authority"] is False
        citations = [
            c
            for c in resumed["context"]["context"]["selected_provenance"]
            if c["kind"] == "citation"
        ]
        assert len(citations) == 1
        assert citations[0]["evidence_refs"][0]["digest"] == generation.generation_digest
        assert _receipts(startup) == (1, 1)
        next_binding = replace(
            value["binding"], session_id=str(uuid4()), external_session_id=str(uuid4())
        )
        value["base"].bind_session(next_binding)
        lifecycle = LocalLifecycleContinuity(
            value["base"],
            value["spool"],
            next_binding,
            source_probe=value["lifecycle"].source_probe,
            entry_validator=validate_reviewed_control_entry,
        )
        sources = SQLiteStartupSourceResolver(
            value["base"],
            value["project_sources"],
            retrieval=value["retrieval"],
            checkpoints=SQLiteStartupCheckpointSource(value["base"]),
        )
        value["base"].source_resolver = sources
        next_value = value | {"binding": next_binding, "lifecycle": lifecycle}
        _stage_start(next_value)
        next_result = LocalStartupService(lifecycle, sources).hydrate(
            _request(retrieval_query="SaglikYaniti", idempotency_key="next-session-real-bge")
        )
        assert next_result["prior_checkpoint"]["checkpoint_digest"] == checkpoint
        assert next_result["prior_checkpoint"]["selected_count"] == 1
        assert "prior-checkpoint" not in next_result["remaining_gates"]
        assert next_result["retrieval"]["source_bytes_verified"] is True
        next_checkpoint = _checkpoint(next_value, next_result["manifest_digest"])
        next_resumed = value["base"].resume(next_binding, next_checkpoint)
        context = next_resumed["context"]["context"]
        assert {c["kind"] for c in context["selected_provenance"]} >= {"checkpoint", "citation"}
        assert next_resumed["reacquire_required"] is True
        assert next_resumed["grants_authority"] is False
    assert _receipts(startup) == (2, 2)
    assert (ROOT / SOURCE_REF).read_bytes() == before
    record_property("real_embedding_model", embedding.profile.exact_model_id)
    record_property("real_embedding_profile", embedding.profile.profile_digest)
    record_property("actual_source_file_count", 1)
    record_property("actual_embedded_chunk_count", 2)
    record_property("provider_free_session_count", 2)
    record_property("startup_dense_channel", "not-invoked")
