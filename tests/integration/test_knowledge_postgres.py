"""P11 Knowledge Plane PostgreSQL kabul testleri."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import psycopg
import pytest

from zekam.application.knowledge_ingestion import IngestionService, pending_version
from zekam.application.knowledge_parsers import default_router
from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.knowledge import (
    Artifact,
    IngestionStage,
    NormalizedDocument,
    SourceFormat,
    VersionState,
)
from zekam.domain.retrieval import ChunkProfile, EmbeddingProfile, chunk_units
from zekam.infrastructure.postgres.knowledge_repository import KnowledgeRepository
from zekam.infrastructure.postgres.retrieval_repository import RetrievalRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

NOW = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)
PAYLOAD = b"# Baslik\n\nBirinci paragraf.\n\n## Alt\n\nIkinci paragraf.\n"


def _artifact(payload: bytes = PAYLOAD, name: str = "rapor.md") -> Artifact:
    return Artifact(
        artifact_id="a1",
        content_digest=digest_of_bytes(payload),
        byte_size=len(payload),
        media_type="text/markdown",
        original_name=name,
        stored_at=NOW,
    )


def _setup(connection: Any, realm: Any, tmp_path: Path) -> KnowledgeRepository:
    source = tmp_path / "kaynak"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    return KnowledgeRepository(connection, realm.id, project.id)


def _store_index_chain(
    connection: Any,
    realm: Any,
    document: Any,
    *,
    version_id: Any,
    now: dt.datetime = NOW,
) -> Any:
    """Normalize belgeyi ve aktivasyon icin exact profil zincirini yazar."""

    knowledge = KnowledgeRepository(connection, realm.id)
    retrieval = RetrievalRepository(connection, realm.id)
    document_id = knowledge.store_document(document, version_id=version_id, now=now)
    chunk_profile = ChunkProfile(name="integration-default")
    embedding_profile = EmbeddingProfile(
        model_ref="integration-placeholder",
        dimension=1024,
        distance="cosine",
    )
    chunk_profile_id = retrieval.store_chunk_profile(chunk_profile, now=now)
    embedding_profile_id = retrieval.store_embedding_profile(embedding_profile, now=now)
    chunks = chunk_units(document.units, document_id=str(document_id), profile=chunk_profile)
    retrieval.store_chunks(chunks, document_id=document_id, now=now)
    retrieval.store_document_profiles(
        document_id=document_id,
        chunk_profile_id=chunk_profile_id,
        embedding_profile_id=embedding_profile_id,
        now=now,
    )
    return document_id


def _markdown_document(
    artifact: Artifact, *, document_id: str, payload: bytes
) -> NormalizedDocument:
    parser = default_router().resolve(SourceFormat.MARKDOWN)
    return NormalizedDocument(
        document_id=document_id,
        artifact_digest=artifact.artifact_digest,
        source_format=SourceFormat.MARKDOWN,
        units=parser.parse(payload),
        parser_ref=parser.parser_ref,
        parser_version=parser.parser_version,
        parser_profile=parser.parser_profile,
    )


def test_tam_ingestion_akisi_kalicilasir(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    repository = _setup(connection, realm, tmp_path)
    service = IngestionService(default_router())

    artifact = _artifact()
    artifact_id = repository.store_artifact(artifact)
    assert repository.store_artifact(artifact) == artifact_id, "artifact idempotent olmali"
    source_id = repository.register_source("rapor", SourceFormat.MARKDOWN, now=NOW)

    job = service.start(job_id="j", source_id="rapor", artifact=artifact, idempotency_key="k-1")
    job_id = repository.start_job(job, source_id=source_id, artifact_id=artifact_id, now=NOW)
    assert (
        repository.start_job(job, source_id=source_id, artifact_id=artifact_id, now=NOW) == job_id
    ), "ayni idempotency anahtari ikinci is yaratmamali"

    job = service.store(job)
    repository.save_progress(job_id, job, now=NOW)
    job, document = service.parse(
        job, document_id="d1", source_format=SourceFormat.MARKDOWN, payload=PAYLOAD
    )
    repository.save_progress(job_id, job, now=NOW)
    job = service.index(job)
    repository.save_progress(job_id, job, now=NOW)

    version = pending_version(
        version_id="v1",
        source_id="rapor",
        revision=1,
        artifact=artifact,
        content_digest=document.content_digest,
        now=NOW,
    )
    version_id = repository.store_version(version, source_id=source_id, artifact_id=artifact_id)
    job, _ = service.activate(job, version)
    repository.save_progress(job_id, job, now=NOW)
    document_id = _store_index_chain(connection, realm, document, version_id=version_id)
    repository.activate_version(version_id)

    assert repository.unit_count(document_id) == document.unit_count
    assert repository.active_version(source_id) == version_id


def test_tamamlanmamis_ingestion_aktif_surum_uretemez(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    repository = _setup(connection, realm, tmp_path)
    artifact = _artifact()
    artifact_id = repository.store_artifact(artifact)
    source_id = repository.register_source("yarim", SourceFormat.MARKDOWN, now=NOW)
    service = IngestionService(default_router())
    job = service.start(job_id="j", source_id="yarim", artifact=artifact, idempotency_key="k-2")
    job_id = repository.start_job(job, source_id=source_id, artifact_id=artifact_id, now=NOW)
    repository.save_progress(job_id, service.store(job), now=NOW)

    version = pending_version(
        version_id="v1",
        source_id="yarim",
        revision=1,
        artifact=artifact,
        content_digest=digest("icerik"),
        now=NOW,
    )
    version_id = repository.store_version(version, source_id=source_id, artifact_id=artifact_id)
    with pytest.raises(psycopg.errors.CheckViolation):
        repository.activate_version(version_id)
    connection.rollback()


def test_asama_atlanamaz_ve_geri_alinamaz(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    repository = _setup(connection, realm, tmp_path)
    artifact = _artifact()
    artifact_id = repository.store_artifact(artifact)
    source_id = repository.register_source("asama", SourceFormat.TXT, now=NOW)
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into knowledge.ingestion_job"
            " (id, realm_id, source_id, artifact_id, idempotency_key, completed_stages, updated_at)"
            " values (gen_random_uuid(), %s, %s, %s, 'k-3', %s, now()) returning id",
            (realm.id, source_id, artifact_id, ["validated", "stored"]),
        )
        job_id = cursor.fetchone()[0]

    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "update knowledge.ingestion_job set completed_stages = %s where id = %s",
            (["validated", "stored", "indexed"], job_id),
        )
    connection.rollback()

    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "update knowledge.ingestion_job set completed_stages = %s where id = %s",
            (["validated"], job_id),
        )
    connection.rollback()


def test_kaynak_tek_aktif_surum_tasir(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    repository = _setup(connection, realm, tmp_path)
    service = IngestionService(default_router())
    source_id = repository.register_source("tek", SourceFormat.MARKDOWN, now=NOW)

    version_ids = []
    for revision, payload in enumerate((PAYLOAD, PAYLOAD + b"\nek satir\n"), start=1):
        artifact = _artifact(payload, name=f"rapor-{revision}.md")
        artifact_id = repository.store_artifact(artifact)
        job = service.start(
            job_id=f"j{revision}",
            source_id="tek",
            artifact=artifact,
            idempotency_key=f"k-{revision}",
        )
        job_id = repository.start_job(job, source_id=source_id, artifact_id=artifact_id, now=NOW)
        for stage in IngestionStage:
            if stage is not IngestionStage.VALIDATED:
                job = job.advance(stage)
        repository.save_progress(job_id, job, now=NOW)
        document = _markdown_document(
            artifact,
            document_id=f"tek-{revision}",
            payload=payload,
        )
        version = pending_version(
            version_id=f"v{revision}",
            source_id="tek",
            revision=revision,
            artifact=artifact,
            content_digest=document.content_digest,
            now=NOW,
        )
        version_id = repository.store_version(version, source_id=source_id, artifact_id=artifact_id)
        _store_index_chain(connection, realm, document, version_id=version_id)
        version_ids.append(version_id)

    repository.activate_version(version_ids[0])
    with pytest.raises(psycopg.errors.UniqueViolation):
        repository.activate_version(version_ids[1])
    connection.rollback()

    repository.activate_version(version_ids[0])
    repository.supersede_version(version_ids[0], version_ids[1])
    repository.activate_version(version_ids[1])
    assert repository.active_version(source_id) == version_ids[1]


def test_artifact_ve_icerik_degistirilemez(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    repository = _setup(connection, realm, tmp_path)
    repository.store_artifact(_artifact())
    for table in ("knowledge.artifact", "knowledge.normalized_document", "knowledge.content_unit"):
        for statement in (f"update {table} set realm_id = realm_id", f"delete from {table}"):
            with (
                pytest.raises(Exception, match=r"append-only|permission denied"),
                connection.cursor() as cursor,
            ):
                cursor.execute(statement)
            connection.rollback()


def _activated_document(connection: Any, realm: Any, tmp_path: Path, *, slug: str) -> Any:
    """Aktif surume bagli bos bir normalize belge olusturur."""

    repository = _setup(connection, realm, tmp_path)
    service = IngestionService(default_router())
    artifact = _artifact(PAYLOAD + slug.encode(), name=f"{slug}.md")
    artifact_id = repository.store_artifact(artifact)
    source_id = repository.register_source(slug, SourceFormat.MARKDOWN, now=NOW)
    job = service.start(job_id="j", source_id=slug, artifact=artifact, idempotency_key=f"k-{slug}")
    job_id = repository.start_job(job, source_id=source_id, artifact_id=artifact_id, now=NOW)
    for stage in IngestionStage:
        if stage is not IngestionStage.VALIDATED:
            job = job.advance(stage)
    repository.save_progress(job_id, job, now=NOW)
    document = _markdown_document(
        artifact,
        document_id=slug,
        payload=PAYLOAD + slug.encode(),
    )
    version = pending_version(
        version_id="v1",
        source_id=slug,
        revision=1,
        artifact=artifact,
        content_digest=document.content_digest,
        now=NOW,
    )
    version_id = repository.store_version(version, source_id=source_id, artifact_id=artifact_id)
    document_id = _store_index_chain(connection, realm, document, version_id=version_id)
    repository.activate_version(version_id)
    return document_id


def test_locatorsuz_birim_veritabanina_yazilamaz(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    document_id = _activated_document(connection, realm, tmp_path, slug="locator")
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into knowledge.content_unit"
            " (id, realm_id, document_id, unit_ref, kind, unit_order, body, locator,"
            "  unit_digest)"
            " values (gen_random_uuid(), %s, %s, 'u', 'paragraph', 0, 'metin',"
            "  '{}'::jsonb, %s)",
            (realm.id, document_id, digest("u")),
        )
    connection.rollback()


def test_ocr_birimi_confidence_olmadan_yazilamaz(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    document_id = _activated_document(connection, realm, tmp_path, slug="ocr")
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into knowledge.content_unit"
            " (id, realm_id, document_id, unit_ref, kind, unit_order, body, locator,"
            "  confidence, unit_digest)"
            " values (gen_random_uuid(), %s, %s, 'u', 'ocr-block', 0, 'metin',"
            " %s::jsonb, null, %s)",
            (realm.id, document_id, '{"page": 1}', digest("u")),
        )
    connection.rollback()


def test_versiyon_durumu_supersede_esleme_zorunlu(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    repository = _setup(connection, realm, tmp_path)
    artifact = _artifact()
    artifact_id = repository.store_artifact(artifact)
    source_id = repository.register_source("esleme", SourceFormat.TXT, now=NOW)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into knowledge.source_version"
            " (id, realm_id, source_id, revision, artifact_id, artifact_digest,"
            "  content_digest, state, superseded_by, created_at)"
            " values (gen_random_uuid(), %s, %s, 1, %s, %s, %s, 'superseded', null, now())",
            (realm.id, source_id, artifact_id, artifact.artifact_digest, digest("c")),
        )
    connection.rollback()


def test_versiyon_durumu_pending_kalabilir(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    repository = _setup(connection, realm, tmp_path)
    artifact = _artifact()
    artifact_id = repository.store_artifact(artifact)
    source_id = repository.register_source("pending", SourceFormat.TXT, now=NOW)
    version = pending_version(
        version_id="v1",
        source_id="pending",
        revision=1,
        artifact=artifact,
        content_digest=digest("c"),
        now=NOW,
    )
    version_id = repository.store_version(version, source_id=source_id, artifact_id=artifact_id)
    assert repository.active_version(source_id) is None
    assert version.state is VersionState.PENDING
    assert version_id is not None
