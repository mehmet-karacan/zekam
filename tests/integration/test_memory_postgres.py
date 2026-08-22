"""P13 bellek PostgreSQL kabul testleri.

Gercek FTS, gercek pgvector ve gercek RLS kullanilir.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Any

import psycopg
import pytest

from zekam.application.memory_service import NativeMemoryEngine, ReviewDecision
from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.canonical import digest
from zekam.domain.memory import (
    MemoryCandidate,
    MemoryClass,
    MemoryEvidence,
    MemoryKey,
    MemoryQuery,
    MemoryScope,
    MemoryState,
)
from zekam.infrastructure.postgres.memory_repository import MemoryRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

NOW = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)
PROFILE = digest("bge-m3-1024")
EVIDENCE = (MemoryEvidence(kind="test", reference="tests/x.py", digest_value=digest("e")),)


def _vector(seed: int) -> tuple[float, ...]:
    raw = [math.sin(seed * (index + 1) * 0.001) for index in range(1024)]
    norm = math.sqrt(sum(value * value for value in raw)) or 1.0
    return tuple(value / norm for value in raw)


def _repository(connection: Any, realm: Any, tmp_path: Path) -> MemoryRepository:
    source = tmp_path / "kaynak"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    return MemoryRepository(connection, realm.id, "varsayilan", project.id, "zekam")


def _key(project: str = "zekam") -> MemoryKey:
    return MemoryKey(scope=MemoryScope.PROJECT, realm_ref="varsayilan", project_ref=project)


def _candidate(content: str, **kwargs: Any) -> MemoryCandidate:
    defaults: dict[str, Any] = {
        "candidate_id": "c1",
        "key": _key(),
        "memory_class": MemoryClass.EPISODIC,
        "content": content,
        "author_ref": "agent-a",
        "observed_at": NOW,
        "evidence": EVIDENCE,
    }
    defaults.update(kwargs)
    return MemoryCandidate(**defaults)


def test_aday_ve_kayit_kalicilasir(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    repository = _repository(connection, realm, tmp_path)
    engine = NativeMemoryEngine()

    candidate = _candidate("pgvector HNSW indeksi cosine kullanir")
    repository.store_candidate(candidate)
    record = engine.write(candidate, now=NOW)
    record_id = repository.store_record(record)
    assert repository.store_record(record) == record_id, "kayit idempotent olmali"

    active = repository.active_records()
    assert len(active) == 1
    assert active[0].content == candidate.content
    assert active[0].state is MemoryState.ACTIVE


def test_fts_ve_vektor_arama_calisir(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    repository = _repository(connection, realm, tmp_path)
    engine = NativeMemoryEngine()

    first = engine.write(_candidate("pgvector HNSW indeksi cosine kullanir"), now=NOW)
    second = engine.write(
        _candidate("Migration drift kontrolu checksum ile yapilir", candidate_id="c2"), now=NOW
    )
    first_id = repository.store_record(first)
    second_id = repository.store_record(second)
    repository.store_embedding(first_id, PROFILE, _vector(1), now=NOW)
    repository.store_embedding(second_id, PROFILE, _vector(2), now=NOW)

    lexical = repository.lexical_search("pgvector")
    assert first.record_digest in lexical
    assert second.record_digest not in lexical

    ranks = repository.vector_ranks(_vector(1), PROFILE)
    assert ranks[first.record_digest] == 1

    hits = engine.search(
        MemoryQuery(text="pgvector", key=_key()),
        records=repository.active_records(),
        lexical_hits=lexical,
        vector_ranks=ranks,
        now=NOW,
    )
    assert hits
    assert hits[0].record.content == first.content
    assert any("FTS" in reason for reason in hits[0].reasons)


def test_temporal_sorgu_gecerli_kayitlari_dondurur(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    repository = _repository(connection, realm, tmp_path)
    engine = NativeMemoryEngine()
    record = engine.write(_candidate("Gecerli bilgi"), now=NOW)
    record_id = repository.store_record(record)

    assert len(repository.valid_at(NOW)) == 1
    assert repository.valid_at(NOW - dt.timedelta(days=1)) == ()

    assert record_id is not None


def test_supersede_iliski_kurar_ve_icerigi_korur(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    repository = _repository(connection, realm, tmp_path)
    engine = NativeMemoryEngine()

    original = engine.write(_candidate("Retry stratejisi kullanilir"), now=NOW)
    original_id = repository.store_record(original)
    later = NOW + dt.timedelta(days=1)
    _, successor = engine.revise(
        original, "Retry stratejisi kullanilmaz", memory_id="m2", now=later
    )
    successor_id = repository.store_record(successor)
    repository.supersede(original_id, successor_id, now=later)

    with connection.cursor() as cursor:
        cursor.execute(
            "select state, superseded_by, content from memory.record where id = %s",
            (original_id,),
        )
        state, superseded_by, content = cursor.fetchone()
        assert state == "superseded"
        assert str(superseded_by) == str(successor_id)
        assert content == "Retry stratejisi kullanilir", "eski icerik korunmali"
        cursor.execute(
            "select kind from memory.relation where from_id = %s and to_id = %s",
            (successor_id, original_id),
        )
        assert cursor.fetchone()[0] == "supersedes"


def test_kanitsiz_kayit_veritabanina_yazilamaz(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    _repository(connection, realm, tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into memory.record"
            " (id, realm_id, scope, memory_class, content, state, revision, evidence,"
            "  record_digest, created_at)"
            " values (gen_random_uuid(), %s, 'project', 'episodic', 'metin', 'active', 1,"
            "  '[]'::jsonb, %s, now())",
            (realm.id, digest("r")),
        )
    connection.rollback()


def test_review_olmadan_semantic_kayit_yazilamaz(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    _repository(connection, realm, tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into memory.record"
            " (id, realm_id, scope, memory_class, content, state, revision, evidence,"
            "  author_ref, reviewed_by, record_digest, created_at)"
            " values (gen_random_uuid(), %s, 'project', 'semantic', 'metin', 'active', 1,"
            "  %s::jsonb, 'agent-a', 'agent-a', %s, now())",
            (realm.id, '[{"kind": "test"}]', digest("r2")),
        )
    connection.rollback()


def test_gecici_kapsam_aktif_kayit_yazamaz(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _repository(connection, realm, tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into memory.record"
            " (id, realm_id, scope, memory_class, content, state, revision, evidence,"
            "  record_digest, created_at)"
            " values (gen_random_uuid(), %s, 'agent', 'episodic', 'metin', 'active', 1,"
            "  %s::jsonb, %s, now())",
            (realm.id, '[{"kind": "test"}]', digest("r3")),
        )
    connection.rollback()


def test_bellek_authority_alani_zorlanir(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _repository(connection, realm, tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into memory.record"
            " (id, realm_id, scope, memory_class, content, state, revision, evidence,"
            "  record_digest, grants_authority, created_at)"
            " values (gen_random_uuid(), %s, 'project', 'episodic', 'metin', 'candidate', 1,"
            "  '[]'::jsonb, %s, true, now())",
            (realm.id, digest("r4")),
        )
    connection.rollback()


def test_icerik_degistirilemez(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    repository = _repository(connection, realm, tmp_path)
    record = NativeMemoryEngine().write(_candidate("Degismez icerik"), now=NOW)
    record_id = repository.store_record(record)
    # Iki katmanli koruma: sutun yetkisi content guncellemesini hic vermez, trigger
    # ise yetkili bir yoldan gelen degisikligi reddeder.
    with (
        pytest.raises(Exception, match=r"permission denied|degistirilemez"),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "update memory.record set state = 'revoked', content = 'degistirildi' where id = %s",
            (record_id,),
        )
    connection.rollback()


def test_kayit_silinemez(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    repository = _repository(connection, realm, tmp_path)
    repository.store_record(NativeMemoryEngine().write(_candidate("Kalici kayit"), now=NOW))
    with (
        pytest.raises(Exception, match=r"append-only|permission denied"),
        connection.cursor() as cursor,
    ):
        cursor.execute("delete from memory.record")
    connection.rollback()


def test_ayni_kapsamda_ayni_icerik_iki_kez_aktif_olamaz(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    repository = _repository(connection, realm, tmp_path)
    engine = NativeMemoryEngine()
    first = engine.write(_candidate("Tekil icerik"), now=NOW)
    repository.store_record(first)
    duplicate = engine.write(
        _candidate("Tekil icerik", candidate_id="c2", author_ref="agent-b"), now=NOW
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        repository.store_record(duplicate)
    connection.rollback()


def test_review_kayidi_bagimsiz_kimlik_ile_yazilir(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    repository = _repository(connection, realm, tmp_path)
    candidate = _candidate("Dogrulanmis proje bilgisi", memory_class=MemoryClass.SEMANTIC)
    decision = ReviewDecision(approved=True, reviewer_ref="reviewer-b", reason="dogrulandi")
    record = NativeMemoryEngine().write(candidate, now=NOW, decision=decision)
    record_id = repository.store_record(record)
    with connection.cursor() as cursor:
        cursor.execute(
            "select author_ref, reviewed_by from memory.record where id = %s", (record_id,)
        )
        author, reviewer = cursor.fetchone()
    assert author == "agent-a"
    assert reviewer == "reviewer-b"
