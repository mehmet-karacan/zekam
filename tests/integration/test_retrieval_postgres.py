"""P12 hibrit retrieval PostgreSQL kabul testleri.

Gercek pgvector HNSW, gercek FTS ve gercek trigram indeksleri kullanilir.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Any

import psycopg
import pytest

from zekam.application.knowledge_ingestion import IngestionService, pending_version
from zekam.application.knowledge_parsers import default_router
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.retrieval_service import GoldenCase, RetrievalService, evaluate
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import ValidationFailed
from zekam.domain.knowledge import Artifact, IngestionStage, SourceFormat
from zekam.domain.retrieval import (
    ChunkProfile,
    RetrievalChannel,
    ScoredHit,
    bge_m3_profile,
    chunk_units,
)
from zekam.infrastructure.postgres.knowledge_repository import KnowledgeRepository
from zekam.infrastructure.postgres.retrieval_repository import RetrievalRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

NOW = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)
DOCUMENT = b"""# Filtreli recall

HNSW indeksinde filtreli arama recall dususu yasatabilir.

## ZEKAM-P12-T04 fusion

RRF ham skorlari toplamaz; yalniz sira kullanilir.

## app.musteri tablosu

Musteri tablosu kimlik ve ad sutunlarini tasir.
"""


def _vector(seed: int, dimension: int = 1024) -> tuple[float, ...]:
    """Deterministik, normalize edilmis sahte vektor."""

    raw = [math.sin(seed * (index + 1) * 0.001) for index in range(dimension)]
    norm = math.sqrt(sum(value * value for value in raw)) or 1.0
    return tuple(value / norm for value in raw)


@pytest.fixture
def indexed(realm_session: tuple[Any, Any], tmp_path: Path) -> dict[str, Any]:
    """Gercek belgeyi ingest edip chunk ve vektorlerini indeksler."""

    realm, connection = realm_session
    source = tmp_path / "kaynak"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    knowledge = KnowledgeRepository(connection, realm.id, project.id)
    retrieval = RetrievalRepository(connection, realm.id)

    artifact = Artifact(
        artifact_id="a1",
        content_digest=digest_of_bytes(DOCUMENT),
        byte_size=len(DOCUMENT),
        media_type="text/markdown",
        original_name="recall.md",
        stored_at=NOW,
    )
    artifact_id = knowledge.store_artifact(artifact)
    source_id = knowledge.register_source("recall", SourceFormat.MARKDOWN, now=NOW)
    service = IngestionService(default_router())
    job = service.start(job_id="j", source_id="recall", artifact=artifact, idempotency_key="k")
    job_id = knowledge.start_job(job, source_id=source_id, artifact_id=artifact_id, now=NOW)
    job = service.store(job)
    job, normalized = service.parse(
        job, document_id="recall", source_format=SourceFormat.MARKDOWN, payload=DOCUMENT
    )
    job = service.index(job)
    for stage in (IngestionStage.ACTIVATED,):
        job = job.advance(stage)
    knowledge.save_progress(job_id, job, now=NOW)
    version = pending_version(
        version_id="v1",
        source_id="recall",
        revision=1,
        artifact=artifact,
        content_digest=normalized.content_digest,
        now=NOW,
    )
    version_id = knowledge.store_version(version, source_id=source_id, artifact_id=artifact_id)
    document_id = knowledge.store_document(normalized, version_id=version_id, now=NOW)

    chunk_profile = ChunkProfile(name="varsayilan", max_tokens=64, overlap_tokens=8)
    retrieval.store_chunk_profile(chunk_profile, now=NOW)
    chunks = chunk_units(normalized.units, document_id="recall", profile=chunk_profile)
    mapping = retrieval.store_chunks(chunks, document_id=document_id, now=NOW)

    embedding_profile = bge_m3_profile(query_prefix="query: ", passage_prefix="passage: ")
    profile_id = retrieval.store_embedding_profile(embedding_profile, now=NOW)
    retrieval.store_document_profiles(
        document_id=document_id,
        chunk_profile_id=retrieval.store_chunk_profile(chunk_profile, now=NOW),
        embedding_profile_id=profile_id,
        now=NOW,
    )
    knowledge.activate_version(version_id)
    for index, chunk in enumerate(chunks, start=1):
        retrieval.store_embedding(
            mapping[chunk.chunk_id], profile_id, embedding_profile, _vector(index), now=NOW
        )

    return {
        "realm": realm,
        "connection": connection,
        "retrieval": retrieval,
        "chunks": chunks,
        "mapping": mapping,
        "profile_id": profile_id,
        "profile": embedding_profile,
    }


def test_document_profile_pending_to_ready_replay_fails_closed(
    indexed: dict[str, Any],
) -> None:
    connection = indexed["connection"]
    retrieval = indexed["retrieval"]
    with connection.cursor() as cursor:
        cursor.execute(
            "select document_id, chunk_profile_id, embedding_profile_id, embedding_state "
            "from knowledge.document_index_profile where realm_id = %s",
            (indexed["realm"].id,),
        )
        document_id, chunk_profile_id, embedding_profile_id, initial_state = cursor.fetchone()
    assert initial_state == "pending"

    with pytest.raises(ValidationFailed, match="replay payload drift"):
        retrieval.store_document_profiles(
            document_id=document_id,
            chunk_profile_id=chunk_profile_id,
            embedding_profile_id=embedding_profile_id,
            embedding_state="ready",
            now=NOW,
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "select embedding_state from knowledge.document_index_profile "
            "where realm_id = %s and document_id = %s",
            (indexed["realm"].id, document_id),
        )
        assert cursor.fetchone()[0] == "pending"


def test_fts_teknik_sorguyu_bulur(indexed: dict[str, Any]) -> None:
    repository: RetrievalRepository = indexed["retrieval"]
    hits = repository.lexical("recall", limit=10)
    assert hits, "FTS sonuc dondurmeli"
    assert all(hit.channel is RetrievalChannel.LEXICAL for hit in hits)
    assert [hit.rank for hit in hits] == list(range(1, len(hits) + 1))


def test_exact_kimlik_kanali_calisir(indexed: dict[str, Any]) -> None:
    repository: RetrievalRepository = indexed["retrieval"]
    hits = repository.exact(("ZEKAM-P12-T04",), limit=10)
    assert hits, "exact kimlik bulunmali"
    bodies = repository.views(tuple(hit.chunk_id for hit in hits))
    assert any("ZEKAM-P12-T04" in view.text for view in bodies.values())


def test_dense_arama_profil_kapsaminda_calisir(indexed: dict[str, Any]) -> None:
    repository: RetrievalRepository = indexed["retrieval"]
    hits = repository.dense(_vector(1), indexed["profile_id"], limit=5)
    assert hits, "dense arama sonuc dondurmeli"
    assert all(hit.raw_score >= 0 for hit in hits), "cosine mesafesi negatif olamaz"
    assert [hit.rank for hit in hits] == list(range(1, len(hits) + 1))


def test_hibrit_arama_exact_kimligi_one_alir(indexed: dict[str, Any]) -> None:
    repository: RetrievalRepository = indexed["retrieval"]
    profile_id = indexed["profile_id"]

    class Backend:
        def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
            return repository.exact(identifiers, limit=limit)

        def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
            return repository.lexical(query, limit=limit)

        def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
            # Sorgu vektoru kasitli olarak alakasiz; exact yine de one gecmeli.
            return repository.dense(_vector(99), profile_id, limit=limit)

    service = RetrievalService(Backend())
    hits, trace = service.search("ZEKAM-P12-T04 fusion nasil calisir")
    assert hits[0].exact_match is True
    assert "ZEKAM-P12-T04" in trace.identifiers

    views = repository.views(tuple(hit.chunk_id for hit in hits))
    answer = service.build_answer(
        "ZEKAM-P12-T04 fusion nasil calisir", hits, trace, views=views, token_budget=500
    )
    assert answer.is_answered is True
    assert answer.tokens_used <= 500
    assert all(not citation.locator.is_empty for citation in answer.citations)


def test_sonuc_bulunmayan_sorgu_abstain_eder(indexed: dict[str, Any]) -> None:
    repository: RetrievalRepository = indexed["retrieval"]

    class EmptyBackend:
        def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
            return ()

        def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
            return repository.lexical(query, limit=limit)

        def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
            return ()

    service = RetrievalService(EmptyBackend())
    hits, trace = service.search("kuantum kriptografi anahtar dagitimi")
    answer = service.build_answer(
        "kuantum kriptografi anahtar dagitimi", hits, trace, views={}, token_budget=100
    )
    assert answer.is_answered is False
    assert answer.state.value.startswith("abstained")


def test_yanlis_boyutlu_vektor_reddedilir(indexed: dict[str, Any]) -> None:
    repository: RetrievalRepository = indexed["retrieval"]
    connection = indexed["connection"]
    realm = indexed["realm"]
    chunk_row_id = next(iter(indexed["mapping"].values()))
    with pytest.raises(psycopg.errors.DataException), connection.cursor() as cursor:
        cursor.execute(
            "insert into knowledge.chunk_embedding"
            " (id, realm_id, chunk_id, profile_id, profile_digest, embedding, created_at)"
            " values (gen_random_uuid(), %s, %s, %s, %s, %s::vector, now())",
            (
                realm.id,
                chunk_row_id,
                indexed["profile_id"],
                indexed["profile"].profile_digest,
                "[0.1,0.2,0.3]",
            ),
        )
    connection.rollback()
    assert repository.realm_id == realm.id


def test_profil_digest_uyusmazligi_reddedilir(indexed: dict[str, Any]) -> None:
    connection = indexed["connection"]
    realm = indexed["realm"]
    chunk_row_id = next(iter(indexed["mapping"].values()))
    literal = "[" + ",".join(repr(value) for value in _vector(7)) + "]"
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into knowledge.chunk_embedding"
            " (id, realm_id, chunk_id, profile_id, profile_digest, embedding, created_at)"
            " values (gen_random_uuid(), %s, %s, %s, %s, %s::vector, now())",
            (
                realm.id,
                chunk_row_id,
                indexed["profile_id"],
                digest("baska-profil"),
                literal,
            ),
        )
    connection.rollback()


def test_chunk_ve_vektor_degistirilemez(indexed: dict[str, Any]) -> None:
    connection = indexed["connection"]
    for table in ("knowledge.chunk", "knowledge.chunk_embedding"):
        for statement in (f"update {table} set realm_id = realm_id", f"delete from {table}"):
            with (
                pytest.raises(Exception, match=r"append-only|permission denied"),
                connection.cursor() as cursor,
            ):
                cursor.execute(statement)
            connection.rollback()


def test_golden_degerlendirme_baseline_uzerinde_calisir(indexed: dict[str, Any]) -> None:
    """Hibrit fusion, yalniz dense kanaldan olusan baseline'i geriletmemelidir."""

    repository: RetrievalRepository = indexed["retrieval"]
    profile_id = indexed["profile_id"]
    exact_hits = repository.exact(("ZEKAM-P12-T04",), limit=5)
    assert exact_hits, "golden ornegi icin exact sonuc gerekiyor"
    relevant = frozenset({exact_hits[0].chunk_id})
    cases = (GoldenCase(query="ZEKAM-P12-T04 fusion", relevant_ids=relevant),)

    def dense_only(query: str) -> tuple[str, ...]:
        return tuple(hit.chunk_id for hit in repository.dense(_vector(99), profile_id, limit=10))

    class Hybrid:
        def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
            return repository.exact(identifiers, limit=limit)

        def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
            return repository.lexical(query, limit=limit)

        def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
            return repository.dense(_vector(99), profile_id, limit=limit)

    service = RetrievalService(Hybrid())

    def hybrid(query: str) -> tuple[str, ...]:
        hits, _ = service.search(query)
        return tuple(hit.chunk_id for hit in hits)

    baseline = evaluate(cases, run=dense_only, k=3)
    fused = evaluate(cases, run=hybrid, k=3)
    assert fused.mrr >= baseline.mrr
    assert fused.recall_at_k >= baseline.recall_at_k
