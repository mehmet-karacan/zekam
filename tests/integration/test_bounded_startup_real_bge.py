"""Opt-in single-file BGE evidence through the real existing-state composition."""

from __future__ import annotations

import os
import socket
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity_environment import environment as environment
from tests.unit.test_local_continuity_startup import (
    ROOT,
    SOURCE_REF,
    _receipts,
    _request,
    _stage_start,
)
from tests.unit.test_local_startup_composition import composition as composition
from tests.unit.test_local_startup_retrieval_integration import _build, _checkpoint, _records

from zekam.application.local_embedding_composition import build_verified_mac_embedding
from zekam.domain.knowledge import UnitKind
from zekam.domain.retrieval import Chunk
from zekam.domain.security import DataClassification
from zekam.infrastructure.local_startup_composition import compose_local_startup
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex

_SOCKET_CONNECT = socket.socket.connect
_CREATE_CONNECTION = socket.create_connection
pytestmark = pytest.mark.integration


def _composed(value: dict[str, Any], index: SQLiteKnowledgeIndex) -> dict[str, Any]:
    composed = compose_local_startup(value["gate"], value["binding"], value["source"], index=index)
    return value | {
        "composed": composed,
        "base": composed.lifecycle.store,
        "sources": composed.sources,
        "lifecycle": composed.lifecycle,
        "spool": composed.lifecycle.spool,
    }


@pytest.mark.skipif(
    os.environ.get("ZEKAM_RUN_LOCAL_STARTUP_BGE_E2E") != "1",
    reason="Real bounded local BGE composition requires explicit activation",
)
def test_actual_source_plan_bge_readonly_composition_and_two_session_resume(
    composition: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
) -> None:
    before = (ROOT / SOURCE_REF).read_bytes()
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    connections: list[tuple[str, int]] = []

    def connect(sock: socket.socket, address: Any) -> Any:
        assert isinstance(address, tuple) and address[:2] == ("127.0.0.1", 7997)
        connections.append(address[:2])
        return _SOCKET_CONNECT(sock, address)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        assert address == ("127.0.0.1", 7997)
        return _CREATE_CONNECTION(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    records = _records(composition)
    chunks = tuple(
        Chunk(
            chunk_id=record.chunk_id,
            document_id="actual-akilli-health",
            text=record.text,
            locator=record.locator,
            kind=UnitKind.CODE,
            token_count=len(record.text.encode()),
            order=record.chunk_order,
        )
        for record in records
    )
    embedding = build_verified_mac_embedding(chunks, classification=DataClassification.LOCAL_ONLY)
    assert embedding.profile.exact_model_id == "BAAI/bge-m3"
    assert embedding.profile.dimension == 1024
    batch = embedding.provider.embed_documents(
        tuple(record.text for record in records), embedding.policy
    )
    assert len(batch.vectors) == batch.receipt.vector_count == len(records) == 2
    assert batch.receipt.profile_digest == embedding.profile.profile_digest
    assert connections
    for vector in batch.vectors:
        embedding.profile.validate_vector(vector)
    actual_records = tuple(
        replace(record, vector=vector)
        for record, vector in zip(records, batch.vectors, strict=True)
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Composed startup/restart may not call any network or provider")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    for name in ("describe", "health", "embed_query", "embed_documents"):
        monkeypatch.setattr(embedding.provider, name, forbidden)
    index_path = tmp_path / "actual-bge-index.sqlite3"
    with SQLiteKnowledgeIndex(index_path, create=True) as index:
        generation = _build(
            composition | {"index": index},
            actual_records,
            tree_digest=composition["plan"].tree_digest,
            embedding_profile_digest=embedding.profile.profile_digest,
            provider_profile_digest=embedding.profile.profile_digest,
        )
    with SQLiteKnowledgeIndex(index_path, read_only=True) as index:
        first = _composed(composition, index)
        _stage_start(first, drain=False)
        assert first["composed"].drain() == 1
        result = first["composed"].hydrate(_request(retrieval_query="SaglikYaniti"))
        assert result["environment"]["source_capture_digest"] == composition["plan"].content_digest
        assert (
            result["retrieval"]["generation"]["generation_digest"] == generation.generation_digest
        )
        assert result["retrieval"]["state"] == "source-verified-candidates"
        assert result["retrieval"]["fragment_count"] == 1
        assert result["prior_checkpoint"]["selected_count"] == 0
        assert result["remaining_gates"] == ["installed-client-lifecycle"]
        checkpoint = _checkpoint(first, result["manifest_digest"])
    with SQLiteKnowledgeIndex(index_path, read_only=True) as index:
        reopened = _composed(composition, index)
        resumed = reopened["base"].resume(reopened["binding"], checkpoint)
        assert resumed["reacquire_required"] is True
        assert resumed["grants_authority"] is False
        next_binding = replace(
            reopened["binding"], session_id=str(uuid4()), external_session_id=str(uuid4())
        )
        reopened["base"].bind_session(next_binding)
        second = _composed(composition | {"binding": next_binding}, index)
        _stage_start(second, drain=False)
        assert second["composed"].drain() == 1
        next_result = second["composed"].hydrate(
            _request(retrieval_query="SaglikYaniti", idempotency_key="composed-second-start")
        )
        assert next_result["prior_checkpoint"]["checkpoint_digest"] == checkpoint
        assert next_result["prior_checkpoint"]["selected_count"] == 1
        assert next_result["retrieval"]["source_bytes_verified"] is True
        assert next_result["remaining_gates"] == ["installed-client-lifecycle"]
        second_checkpoint = _checkpoint(second, next_result["manifest_digest"])
        next_resumed = second["base"].resume(next_binding, second_checkpoint)
        provenance = next_resumed["context"]["context"]["selected_provenance"]
        assert {item["kind"] for item in provenance} >= {"checkpoint", "citation", "source-slice"}
        citations = [item for item in provenance if item["kind"] == "citation"]
        assert len(citations) == 1
        assert citations[0]["evidence_refs"][0]["digest"] == generation.generation_digest
        assert next_resumed["grants_authority"] is False
    assert _receipts(composition) == (2, 2)
    assert composition["source"].capture() == composition["plan"]
    assert (ROOT / SOURCE_REF).read_bytes() == before
    record_property("actual_source_file_count", 1)
    record_property("actual_embedded_chunk_count", 2)
    record_property("real_embedding_model", embedding.profile.exact_model_id)
    record_property("real_embedding_profile", embedding.profile.profile_digest)
    record_property("source_plan_digest", composition["plan"].content_digest)
    record_property("provider_free_composed_session_count", 2)
    record_property("native_lifecycle_proven", False)
