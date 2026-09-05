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
        entry_path=payload.get("entry_path"),
        timestamp_start_ms=payload.get("timestamp_start_ms"),
        timestamp_end_ms=payload.get("timestamp_end_ms"),
        video_id=payload.get("video_id"),
    )


@dataclass(frozen=True, slots=True)
class RetrievalRepository:
    """Chunk yazimi ve uc kanalli arama."""

    connection: Any
    realm_id: UUID
    project_id: UUID | None = None

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
            scope_sql, scope_params = self._project_scope("c")
            cursor.execute(
                "select c.chunk_ref from knowledge.chunk c"
                " where c.realm_id = %s and c.body like any (%s)"
                + scope_sql
                + " order by c.chunk_ref limit %s",
                (self.realm_id, [f"%{item}%" for item in identifiers], *scope_params, limit),
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
            scope_sql, scope_params = self._project_scope("c")
            cursor.execute(
                "select c.chunk_ref, ts_rank(c.search_vector, plainto_tsquery('simple', %s))"
                " as score from knowledge.chunk c"
                " where c.realm_id = %s"
                " and c.search_vector @@ plainto_tsquery('simple', %s)"
                + scope_sql
                + " order by score desc, c.chunk_ref limit %s",
                (query, self.realm_id, query, *scope_params, limit),
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
            if self.project_id is not None:
                # HNSW post-filtering can return zero rows when the global nearest
                # neighbours belong to another project. Materialize the exact
                # project corpus first, then rank it; project isolation and recall
                # are both deterministic.
                cursor.execute(
                    "with project_chunks as materialized ("
                    " select c.chunk_ref,e.embedding from knowledge.chunk_embedding e"
                    " join knowledge.chunk c on c.realm_id=e.realm_id and c.id=e.chunk_id"
                    " join knowledge.normalized_document d on d.realm_id=c.realm_id"
                    "  and d.id=c.document_id"
                    " join knowledge.source_version v on v.realm_id=d.realm_id"
                    "  and v.id=d.version_id and v.state='active'"
                    " join knowledge.source s on s.realm_id=v.realm_id and s.id=v.source_id"
                    " where e.realm_id=%s and e.profile_id=%s and s.project_id=%s)"
                    " select chunk_ref,embedding <=> %s::vector as distance"
                    " from project_chunks order by distance asc,chunk_ref limit %s",
                    (self.realm_id, profile_id, self.project_id, literal, limit),
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
            scope_sql, scope_params = self._project_scope("c")
            cursor.execute(
                "select c.chunk_ref, e.embedding <=> %s::vector as distance"
                " from knowledge.chunk_embedding e"
                " join knowledge.chunk c on c.realm_id = e.realm_id and c.id = e.chunk_id"
                " where e.realm_id = %s and e.profile_id = %s"
                + scope_sql
                + " order by distance asc, c.chunk_ref limit %s",
                (literal, self.realm_id, profile_id, *scope_params, limit),
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
            scope_sql, scope_params = self._project_scope("c")
            cursor.execute(
                "select c.chunk_ref, c.document_id, c.body, c.locator, c.content_digest,"
                "  p.chunk_ref"
                " from knowledge.chunk c"
                " left join knowledge.chunk p on p.realm_id = c.realm_id and p.id = c.parent_id"
                " where c.realm_id = %s and c.chunk_ref = any (%s)"
                + scope_sql,
                (self.realm_id, list(chunk_refs), *scope_params),
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

    def active_project_embedding_profile(self) -> dict[str, Any] | None:
        """Exact projenin aktif repository index profilini salt okunur yukler."""

        if self.project_id is None:
            raise ValidationFailed("project embedding profili exact project_id ister")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select dip.embedding_profile_id,ep.model_ref,ep.dimension,ep.query_prefix,"
                " ep.profile_digest,v.revision,v.content_digest,d.id"
                " from knowledge.source s"
                " join knowledge.source_version v on v.realm_id=s.realm_id"
                "  and v.source_id=s.id and v.state='active'"
                " join knowledge.normalized_document d on d.realm_id=v.realm_id"
                "  and d.version_id=v.id"
                " join knowledge.document_index_profile dip on dip.realm_id=d.realm_id"
                "  and dip.document_id=d.id and dip.embedding_state='ready'"
                " join knowledge.embedding_profile ep on ep.realm_id=dip.realm_id"
                "  and ep.id=dip.embedding_profile_id"
                " where s.realm_id=%s and s.project_id=%s and s.source_format='repository'"
                " order by v.created_at desc,d.created_at desc limit 1",
                (self.realm_id, self.project_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "profile_id": UUID(str(row[0])),
            "model_ref": str(row[1]),
            "dimension": int(row[2]),
            "query_prefix": str(row[3]),
            "profile_digest": str(row[4]),
            "source_revision": int(row[5]),
            "source_content_digest": str(row[6]),
            "document_id": str(row[7]),
        }

    def _project_scope(self, chunk_alias: str) -> tuple[str, tuple[UUID, ...]]:
        if self.project_id is None:
            return "", ()
        return (
            " and exists (select 1 from knowledge.normalized_document scope_document"
            " join knowledge.source_version scope_version"
            " on scope_version.realm_id=scope_document.realm_id"
            " and scope_version.id=scope_document.version_id"
            " join knowledge.source scope_source"
            " on scope_source.realm_id=scope_version.realm_id"
            " and scope_source.id=scope_version.source_id"
            f" where scope_document.realm_id={chunk_alias}.realm_id"
            f" and scope_document.id={chunk_alias}.document_id"
            " and scope_version.state='active' and scope_source.project_id=%s)",
            (self.project_id,),
        )

    @staticmethod
    def _resolve(cursor: Any, table: str, column: str, value: str) -> UUID:
        row = cursor.fetchone()
        if row is not None:
            return UUID(str(row[0]))
        cursor.execute(f"select id from {table} where {column} = %s", (value,))
        return UUID(str(cursor.fetchone()[0]))
