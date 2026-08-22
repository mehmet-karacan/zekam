"""PostgreSQL chunk, embedding ve hibrit arama altyapisi."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.application.retrieval_service import ChunkView
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.identifiers import new_uuid7
from zekam.domain.knowledge import Locator
from zekam.domain.retrieval import (
    Chunk,
    ChunkProfile,
    EmbeddingProfile,
    RetrievalChannel,
    ScoredHit,
)


def _locator_from(payload: dict[str, Any]) -> Locator:
    raw = payload.get("bbox")
    bbox: tuple[float, float, float, float] | None = None
    if raw:
        values = [float(value) for value in raw]
        bbox = (values[0], values[1], values[2], values[3])
    return Locator(
        page=payload.get("page"),
        bbox=bbox,
        heading_path=tuple(payload.get("heading_path", ())),
        block_index=payload.get("block_index"),
        line_start=payload.get("line_start"),
        line_end=payload.get("line_end"),
        symbol=payload.get("symbol"),
        object_name=payload.get("object_name"),
        relative_path=payload.get("relative_path"),
    )


@dataclass(frozen=True, slots=True)
class RetrievalRepository:
    """Chunk yazimi ve uc kanalli arama."""

    connection: Any
    realm_id: UUID

    # -- profiller ------------------------------------------------------------

    def store_chunk_profile(self, profile: ChunkProfile, *, now: dt.datetime) -> UUID:
        record_id = new_uuid7(now=now)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into knowledge.chunk_profile"
                " (id, realm_id, name, max_tokens, overlap_tokens, keep_tables_whole,"
                "  keep_code_whole, profile_digest, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, profile_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    profile.name,
                    profile.max_tokens,
                    profile.overlap_tokens,
                    profile.keep_tables_whole,
                    profile.keep_code_whole,
                    profile.profile_digest,
                    now,
                ),
            )
            return self._resolve(
                cursor, "knowledge.chunk_profile", "profile_digest", profile.profile_digest
            )

    def store_embedding_profile(self, profile: EmbeddingProfile, *, now: dt.datetime) -> UUID:
        record_id = new_uuid7(now=now)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into knowledge.embedding_profile"
                " (id, realm_id, model_ref, dimension, distance, query_prefix, passage_prefix,"
                "  profile_digest, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, profile_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    profile.model_ref,
                    profile.dimension,
                    profile.distance,
                    profile.query_prefix,
                    profile.passage_prefix,
                    profile.profile_digest,
                    now,
                ),
            )
            return self._resolve(
                cursor, "knowledge.embedding_profile", "profile_digest", profile.profile_digest
            )

    def store_document_profiles(
        self,
        *,
        document_id: UUID,
        chunk_profile_id: UUID,
        embedding_profile_id: UUID,
        embedding_state: str = "pending",
        now: dt.datetime,
    ) -> UUID:
        """Belgenin exact chunk/embedding profil zincirini kaydeder.

        Embedding vektoru provider calistirilana kadar ``pending`` kalir;
        lexical chunk indeksi gercekten yazildiktan sonra kayit olusturulur.
        """

        if embedding_state not in {"pending", "ready"}:
            raise ValueError("embedding state pending veya ready olmali")

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, chunk_profile_id, embedding_profile_id, embedding_state "
                "from knowledge.document_index_profile "
                "where realm_id = %s and document_id = %s",
                (self.realm_id, document_id),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if (
                    UUID(str(existing[1])) != chunk_profile_id
                    or UUID(str(existing[2])) != embedding_profile_id
                    or str(existing[3]) != embedding_state
                ):
                    raise ValidationFailed("document index profile replay payload drift")
                return UUID(str(existing[0]))
            record_id = new_uuid7(now=now)
            cursor.execute(
                "insert into knowledge.document_index_profile"
                " (id, realm_id, document_id, chunk_profile_id, embedding_profile_id,"
                "  lexical_state, embedding_state, created_at)"
                " values (%s, %s, %s, %s, %s, 'ready', %s, %s)",
                (
                    record_id,
                    self.realm_id,
                    document_id,
                    chunk_profile_id,
                    embedding_profile_id,
                    embedding_state,
                    now,
                ),
            )
            return record_id

    # -- chunk ve vektor ------------------------------------------------------

    def store_chunks(
        self, chunks: tuple[Chunk, ...], *, document_id: UUID, now: dt.datetime
    ) -> dict[str, UUID]:
        """Chunk'lari yazar ve mantiksal kimlikten satir kimligine esleme dondurur."""

        mapping: dict[str, UUID] = {}
        with self.connection.cursor() as cursor:
            for chunk in chunks:
                record_id = new_uuid7(now=now)
                cursor.execute(
                    "insert into knowledge.chunk"
                    " (id, realm_id, document_id, chunk_ref, parent_id, kind, chunk_order,"
                    "  body, locator, token_count, content_digest, chunk_digest, profile_digest)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)"
                    " on conflict (realm_id, chunk_ref) do nothing returning id",
                    (
                        record_id,
                        self.realm_id,
                        document_id,
                        chunk.chunk_id,
                        mapping.get(chunk.parent_id) if chunk.parent_id else None,
                        str(chunk.kind),
                        chunk.order,
                        chunk.text,
                        canonical_json(chunk.locator.as_dict()),
                        chunk.token_count,
                        digest({"body": chunk.text}),
                        chunk.chunk_digest,
                        chunk.profile_digest,
                    ),
                )
                mapping[chunk.chunk_id] = self._resolve(
                    cursor, "knowledge.chunk", "chunk_ref", chunk.chunk_id
                )
        return mapping

    def store_embedding(
        self,
        chunk_id: UUID,
        profile_id: UUID,
        profile: EmbeddingProfile,
        vector: tuple[float, ...],
        *,
        now: dt.datetime,
    ) -> None:
        """Vektoru yazar. Boyut ve sonluluk once alan katmaninda dogrulanir."""

        profile.validate_vector(vector)
        literal = "[" + ",".join(repr(float(value)) for value in vector) + "]"
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into knowledge.chunk_embedding"
                " (id, realm_id, chunk_id, profile_id, profile_digest, embedding, created_at)"
                " values (%s, %s, %s, %s, %s, %s::vector, %s)"
                " on conflict (realm_id, chunk_id, profile_id) do nothing",
                (
                    new_uuid7(now=now),
                    self.realm_id,
                    chunk_id,
                    profile_id,
                    profile.profile_digest,
                    literal,
                    now,
                ),
            )

    # -- kanallar -------------------------------------------------------------

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
        """Exact teknik kimlik eslesmesi. Dense skor bunu eleyemez."""

        if not identifiers:
            return ()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select chunk_ref from knowledge.chunk"
                " where realm_id = %s and body like any (%s)"
                " order by chunk_ref limit %s",
                (self.realm_id, [f"%{item}%" for item in identifiers], limit),
            )
            rows = cursor.fetchall()
        return tuple(
            ScoredHit(
                chunk_id=str(row[0]),
                channel=RetrievalChannel.EXACT,
                rank=index,
                raw_score=1.0,
            )
            for index, row in enumerate(rows, start=1)
        )

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        """PostgreSQL FTS; teknik kimligi bozmamak icin 'simple' sozlugu."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select chunk_ref, ts_rank(search_vector, plainto_tsquery('simple', %s)) as score"
                " from knowledge.chunk"
                " where realm_id = %s and search_vector @@ plainto_tsquery('simple', %s)"
                " order by score desc, chunk_ref limit %s",
                (query, self.realm_id, query, limit),
            )
            rows = cursor.fetchall()
        return tuple(
            ScoredHit(
                chunk_id=str(row[0]),
                channel=RetrievalChannel.LEXICAL,
                rank=index,
                raw_score=float(row[1]),
            )
            for index, row in enumerate(rows, start=1)
        )

    def dense(
        self, vector: tuple[float, ...], profile_id: UUID, *, limit: int
    ) -> tuple[ScoredHit, ...]:
        """Cosine mesafesiyle dense arama; profil disi vektor karismaz."""

        literal = "[" + ",".join(repr(float(value)) for value in vector) + "]"
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select c.chunk_ref, e.embedding <=> %s::vector as distance"
                " from knowledge.chunk_embedding e"
                " join knowledge.chunk c on c.realm_id = e.realm_id and c.id = e.chunk_id"
                " where e.realm_id = %s and e.profile_id = %s"
                " order by distance asc, c.chunk_ref limit %s",
                (literal, self.realm_id, profile_id, limit),
            )
            rows = cursor.fetchall()
        return tuple(
            ScoredHit(
                chunk_id=str(row[0]),
                channel=RetrievalChannel.DENSE,
                rank=index,
                raw_score=float(row[1]),
            )
            for index, row in enumerate(rows, start=1)
        )

    def views(self, chunk_refs: tuple[str, ...]) -> dict[str, ChunkView]:
        """Baglam kurulumu icin chunk govdesi ve locator'i."""

        if not chunk_refs:
            return {}
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select c.chunk_ref, c.document_id, c.body, c.locator, c.content_digest,"
                "  p.chunk_ref"
                " from knowledge.chunk c"
                " left join knowledge.chunk p on p.realm_id = c.realm_id and p.id = c.parent_id"
                " where c.realm_id = %s and c.chunk_ref = any (%s)",
                (self.realm_id, list(chunk_refs)),
            )
            rows = cursor.fetchall()
        return {
            str(row[0]): ChunkView(
                chunk_id=str(row[0]),
                document_id=str(row[1]),
                text=str(row[2]),
                locator=_locator_from(row[3]),
                content_digest=str(row[4]),
                parent_id=str(row[5]) if row[5] else None,
            )
            for row in rows
        }

    @staticmethod
    def _resolve(cursor: Any, table: str, column: str, value: str) -> UUID:
        row = cursor.fetchone()
        if row is not None:
            return UUID(str(row[0]))
        cursor.execute(f"select id from {table} where {column} = %s", (value,))
        return UUID(str(cursor.fetchone()[0]))
