"""PostgreSQL native bellek repository."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json
from zekam.domain.identifiers import new_uuid7
from zekam.domain.memory import (
    MemoryCandidate,
    MemoryClass,
    MemoryEvidence,
    MemoryKey,
    MemoryRecord,
    MemoryScope,
    MemoryState,
)

_COLUMNS = (
    "id, scope, project_id, work_item_id, memory_class, content, state, revision,"
    " evidence, entities, valid_from, valid_until, author_ref, reviewed_by,"
    " superseded_by, last_used_at, record_digest, created_at"
)


@dataclass(frozen=True, slots=True)
class MemoryRepository:
    """Realm kapsamli bellek kayitlari. Native motor kanoniktir."""

    connection: Any
    realm_id: UUID
    realm_ref: str
    project_id: UUID | None = None
    #: Portable proje referansi; UUID logical kimlige sizmaz.
    project_ref: str | None = None

    def store_candidate(self, candidate: MemoryCandidate) -> UUID:
        record_id = new_uuid7(now=candidate.observed_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into memory.candidate"
                " (id, realm_id, scope, project_id, memory_class, content, author_ref,"
                "  evidence, occurrence_key, observation_count, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s) returning id",
                (
                    record_id,
                    self.realm_id,
                    str(candidate.key.scope),
                    self.project_id,
                    str(candidate.memory_class),
                    candidate.content,
                    candidate.author_ref,
                    canonical_json([item.as_dict() for item in candidate.evidence]),
                    candidate.occurrence_key,
                    candidate.observation_count,
                    candidate.observed_at,
                ),
            )
            return UUID(str(cursor.fetchone()[0]))

    def store_record(self, record: MemoryRecord) -> UUID:
        record_id = new_uuid7(now=record.created_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into memory.record"
                " (id, realm_id, scope, project_id, memory_class, content, state, revision,"
                "  evidence, entities, valid_from, valid_until, author_ref, reviewed_by,"
                "  superseded_by, record_digest, grants_authority, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,"
                "  null, %s, false, %s)"
                " on conflict (realm_id, record_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    str(record.key.scope),
                    self.project_id,
                    str(record.memory_class),
                    record.content,
                    str(record.state),
                    record.revision,
                    canonical_json([item.as_dict() for item in record.evidence]),
                    list(record.entities),
                    record.valid_from,
                    record.valid_until,
                    record.author_ref,
                    record.reviewed_by,
                    record.record_digest,
                    record.created_at,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0]))
            cursor.execute(
                "select id from memory.record where realm_id = %s and record_digest = %s",
                (self.realm_id, record.record_digest),
            )
            return UUID(str(cursor.fetchone()[0]))

    def supersede(self, current_id: UUID, successor_id: UUID, *, now: dt.datetime) -> None:
        """Eski kaydi emekliye ayirir ve iliskiyi kurar. Icerik degismez."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "update memory.record set state = 'superseded', superseded_by = %s,"
                " valid_until = %s where realm_id = %s and id = %s and state = 'active'",
                (successor_id, now, self.realm_id, current_id),
            )
            cursor.execute(
                "insert into memory.relation (id, realm_id, from_id, to_id, kind, created_at)"
                " values (%s, %s, %s, %s, 'supersedes', %s)"
                " on conflict (realm_id, from_id, to_id, kind) do nothing",
                (new_uuid7(now=now), self.realm_id, successor_id, current_id, now),
            )

    def active_records(self, *, limit: int = 200) -> tuple[MemoryRecord, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_COLUMNS} from memory.record"
                " where realm_id = %s and state = 'active'"
                " order by created_at desc limit %s",
                (self.realm_id, limit),
            )
            rows = cursor.fetchall()
        return tuple(self._from_row(row) for row in rows)

    def lexical_search(self, text: str, *, limit: int = 20) -> frozenset[str]:
        """FTS eslesen kayitlarin digest'lerini dondurur."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select record_digest from memory.record"
                " where realm_id = %s and state = 'active'"
                "   and search_vector @@ plainto_tsquery('simple', %s)"
                " order by ts_rank(search_vector, plainto_tsquery('simple', %s)) desc"
                " limit %s",
                (self.realm_id, text, text, limit),
            )
            return frozenset(str(row[0]) for row in cursor.fetchall())

    def valid_at(self, moment: dt.datetime, *, limit: int = 200) -> tuple[MemoryRecord, ...]:
        """Temporal sorgu: verilen anda gecerli aktif kayitlar."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_COLUMNS} from memory.record"
                " where realm_id = %s and state = 'active'"
                "   and (valid_from is null or valid_from <= %s)"
                "   and (valid_until is null or valid_until > %s)"
                " order by created_at desc limit %s",
                (self.realm_id, moment, moment, limit),
            )
            rows = cursor.fetchall()
        return tuple(self._from_row(row) for row in rows)

    def store_embedding(
        self, record_id: UUID, profile_digest: str, vector: tuple[float, ...], *, now: dt.datetime
    ) -> None:
        literal = "[" + ",".join(repr(float(value)) for value in vector) + "]"
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into memory.embedding"
                " (id, realm_id, record_id, profile_digest, embedding, created_at)"
                " values (%s, %s, %s, %s, %s::vector, %s)"
                " on conflict (realm_id, record_id, profile_digest) do nothing",
                (new_uuid7(now=now), self.realm_id, record_id, profile_digest, literal, now),
            )

    def vector_ranks(
        self, vector: tuple[float, ...], profile_digest: str, *, limit: int = 20
    ) -> dict[str, int]:
        """Kayit digest'inden vektor sirasina esleme."""

        literal = "[" + ",".join(repr(float(value)) for value in vector) + "]"
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select r.record_digest from memory.embedding e"
                " join memory.record r on r.realm_id = e.realm_id and r.id = e.record_id"
                " where e.realm_id = %s and e.profile_digest = %s and r.state = 'active'"
                " order by e.embedding <=> %s::vector asc limit %s",
                (self.realm_id, profile_digest, literal, limit),
            )
            return {str(row[0]): index for index, row in enumerate(cursor.fetchall(), start=1)}

    def _from_row(self, row: Any) -> MemoryRecord:
        scope = MemoryScope(str(row[1]))
        key = MemoryKey(
            scope=scope,
            realm_ref=self.realm_ref,
            project_ref=self.project_ref if row[2] else None,
            work_ref=str(row[3]) if row[3] else None,
        )
        return MemoryRecord(
            memory_id=str(row[16]),
            key=key,
            memory_class=MemoryClass(str(row[4])),
            content=str(row[5]),
            state=MemoryState(str(row[6])),
            revision=int(row[7]),
            created_at=row[17],
            evidence=tuple(
                MemoryEvidence(
                    kind=item["kind"], reference=item["reference"], digest_value=item["digest"]
                )
                for item in (row[8] or [])
            ),
            entities=tuple(row[9] or ()),
            valid_from=row[10],
            valid_until=row[11],
            author_ref=row[12],
            reviewed_by=row[13],
            superseded_by=str(row[14]) if row[14] else None,
            last_used_at=row[15],
        )
