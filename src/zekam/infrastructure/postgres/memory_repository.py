"""PostgreSQL native bellek repository."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json
from zekam.domain.errors import NotFound, PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.memory import (
    MemoryCandidate,
    MemoryClass,
    MemoryEvidence,
    MemoryKey,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemoryState,
)
from zekam.domain.retrieval import extract_identifiers

_RECORD_COLUMNS = (
    "r.id, r.logical_memory_id, r.scope, r.project_id, r.work_item_id,"
    " r.project_ref, r.work_ref, r.memory_class, r.content, r.state, r.revision,"
    " r.evidence, r.entities, r.valid_from, r.valid_until, r.author_ref, r.reviewed_by,"
    " successor.logical_memory_id, r.last_used_at, r.record_digest, r.created_at"
)

_RECORD_JOIN = (
    " from memory.record r left join memory.record successor"
    " on successor.realm_id = r.realm_id and successor.id = r.superseded_by"
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
    work_item_id: UUID | None = None
    #: Portable is referansi; storage UUID domain kimligi olarak dondurulmez.
    work_ref: str | None = None

    def _binding_for(
        self, key: MemoryKey
    ) -> tuple[UUID | None, str | None, UUID | None, str | None]:
        if key.scope is MemoryScope.GLOBAL_USER:
            return None, None, None, None
        if key.scope is MemoryScope.PROJECT:
            if self.project_id is None or self.project_ref is None:
                raise PolicyViolation("project memory exact proje binding ister")
            if key.project_ref != self.project_ref:
                raise PolicyViolation("project memory logical proje binding ile eslesmiyor")
            return self.project_id, self.project_ref, None, None
        if key.scope is MemoryScope.WORK_ITEM:
            if (
                self.project_id is None
                or self.project_ref is None
                or self.work_item_id is None
                or self.work_ref is None
            ):
                raise PolicyViolation("work-item memory exact proje ve is binding ister")
            if key.work_ref != self.work_ref:
                raise PolicyViolation("work-item memory logical is binding ile eslesmiyor")
            if key.project_ref is not None and key.project_ref != self.project_ref:
                raise PolicyViolation("work-item memory logical proje binding ile eslesmiyor")
            return self.project_id, self.project_ref, self.work_item_id, self.work_ref
        return None, None, None, None

    def store_candidate(self, candidate: MemoryCandidate) -> UUID:
        record_id = new_uuid7(now=candidate.observed_at)
        project_id, project_ref, work_item_id, work_ref = self._binding_for(candidate.key)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into memory.candidate"
                " (id, realm_id, logical_candidate_id, scope, project_id, work_item_id,"
                "  project_ref, work_ref, memory_class, content, author_ref,"
                "  evidence, occurrence_key, observation_count, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)"
                " returning id",
                (
                    record_id,
                    self.realm_id,
                    candidate.candidate_id,
                    str(candidate.key.scope),
                    project_id,
                    work_item_id,
                    project_ref,
                    work_ref,
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
        project_id, project_ref, work_item_id, work_ref = self._binding_for(record.key)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into memory.record"
                " (id, realm_id, logical_memory_id, scope, project_id, work_item_id,"
                "  project_ref, work_ref, memory_class, content, state, revision,"
                "  evidence, entities, valid_from, valid_until, author_ref, reviewed_by,"
                "  superseded_by, record_digest, grants_authority, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s,"
                "  %s, %s, %s, %s, null, %s, false, %s)"
                " on conflict (realm_id, record_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    record.memory_id,
                    str(record.key.scope),
                    project_id,
                    work_item_id,
                    project_ref,
                    work_ref,
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
        """Hijyen/yonetim gorunumu; retrieval icin ``retrieval_records`` kullanilir."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_RECORD_COLUMNS}{_RECORD_JOIN}"
                " where r.realm_id = %s and r.state = 'active'"
                " order by r.created_at desc limit %s",
                (self.realm_id, limit),
            )
            rows = cursor.fetchall()
        return tuple(self._from_row(row) for row in rows)

    def retrieval_records_by_ids(
        self,
        memory_ids: tuple[str, ...],
        query: MemoryQuery,
        *,
        at: dt.datetime,
    ) -> tuple[MemoryRecord, ...]:
        """Kanallarin ranked union sonucunu ayni eligible relation ile hydrate eder."""

        if not memory_ids:
            return ()

        where_sql, params = self._retrieval_where(query, at=at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_RECORD_COLUMNS}{_RECORD_JOIN} where {where_sql}"
                " and r.logical_memory_id = any (%s) order by r.logical_memory_id",
                (*params, list(memory_ids)),
            )
            rows = cursor.fetchall()
        return tuple(self._from_row(row) for row in rows)

    def retrieval_exact_ranks(
        self, text: str, query: MemoryQuery, *, at: dt.datetime, limit: int = 20
    ) -> dict[str, int]:
        """Exact metin/varlik adaylarini eligible relation icinde siralar."""

        where_sql, params = self._retrieval_where(query, at=at)
        predicates: list[str] = []
        exact_params: list[Any] = []
        terms = tuple(dict.fromkeys((text.strip(), *extract_identifiers(text))))
        for term in terms:
            if term:
                predicates.append("position(lower(%s) in lower(r.content)) > 0")
                exact_params.append(term)
        if query.entities:
            predicates.append("r.entities && %s")
            exact_params.append(list(query.entities))
        if not predicates:
            return {}
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select r.logical_memory_id from memory.record r"
                f" where {where_sql} and ({' or '.join(predicates)})"
                " order by r.logical_memory_id limit %s",
                (*params, *exact_params, limit),
            )
            return {str(row[0]): index for index, row in enumerate(cursor.fetchall(), start=1)}

    def get_by_logical_id(self, logical_memory_id: str) -> MemoryRecord:
        """Storage UUID veya digest kabul etmeden exact domain kimligiyle okur."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_RECORD_COLUMNS}{_RECORD_JOIN}"
                " where r.realm_id = %s and r.logical_memory_id = %s"
                " order by r.revision desc limit 1",
                (self.realm_id, logical_memory_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound(f"Bellek kaydi bulunamadi: {logical_memory_id}")
        return self._from_row(row)

    def lexical_search(self, text: str, *, limit: int = 20) -> frozenset[str]:
        """FTS eslesen kayitlarin logical memory kimliklerini dondurur."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select logical_memory_id from memory.record"
                " where realm_id = %s and state = 'active'"
                "   and search_vector @@ plainto_tsquery('simple', %s)"
                " order by ts_rank(search_vector, plainto_tsquery('simple', %s)) desc"
                " limit %s",
                (self.realm_id, text, text, limit),
            )
            return frozenset(str(row[0]) for row in cursor.fetchall())

    def retrieval_lexical_ranks(
        self, text: str, query: MemoryQuery, *, at: dt.datetime, limit: int = 20
    ) -> dict[str, int]:
        """Scope/gecerlilik/review filtresini FTS LIMIT'inden once uygular."""

        where_sql, params = self._retrieval_where(query, at=at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select r.logical_memory_id from memory.record r"
                f" where {where_sql}"
                " and r.search_vector @@ plainto_tsquery('simple', %s)"
                " order by ts_rank(r.search_vector, plainto_tsquery('simple', %s)) desc,"
                " r.logical_memory_id limit %s",
                (*params, text, text, limit),
            )
            return {str(row[0]): index for index, row in enumerate(cursor.fetchall(), start=1)}

    def valid_at(self, moment: dt.datetime, *, limit: int = 200) -> tuple[MemoryRecord, ...]:
        """Temporal sorgu: verilen anda gecerli aktif kayitlar."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_RECORD_COLUMNS}{_RECORD_JOIN}"
                " where r.realm_id = %s and r.state = 'active'"
                "   and (r.valid_from is null or r.valid_from <= %s)"
                "   and (r.valid_until is null or r.valid_until > %s)"
                " order by r.created_at desc limit %s",
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
        """Logical memory kimliginden vektor sirasina esleme."""

        literal = "[" + ",".join(repr(float(value)) for value in vector) + "]"
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select r.logical_memory_id from memory.embedding e"
                " join memory.record r on r.realm_id = e.realm_id and r.id = e.record_id"
                " where e.realm_id = %s and e.profile_digest = %s and r.state = 'active'"
                " order by e.embedding <=> %s::vector asc limit %s",
                (self.realm_id, profile_digest, literal, limit),
            )
            return {str(row[0]): index for index, row in enumerate(cursor.fetchall(), start=1)}

    def retrieval_vector_ranks(
        self,
        vector: tuple[float, ...],
        profile_digest: str,
        query: MemoryQuery,
        *,
        at: dt.datetime,
        limit: int = 20,
    ) -> dict[str, int]:
        """Scope/gecerlilik/review filtresini dense LIMIT'inden once uygular."""

        literal = "[" + ",".join(repr(float(value)) for value in vector) + "]"
        where_sql, params = self._retrieval_where(query, at=at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select r.logical_memory_id from memory.embedding e"
                " join memory.record r on r.realm_id = e.realm_id and r.id = e.record_id"
                f" where {where_sql} and e.profile_digest = %s"
                " order by e.embedding <=> %s::vector asc, r.logical_memory_id limit %s",
                (*params, profile_digest, literal, limit),
            )
            return {str(row[0]): index for index, row in enumerate(cursor.fetchall(), start=1)}

    def _retrieval_where(
        self, query: MemoryQuery, *, at: dt.datetime
    ) -> tuple[str, tuple[Any, ...]]:
        """Uc retrieval kanali icin tek fail-closed eligible relation'i."""

        if query.key.realm_ref != self.realm_ref:
            raise PolicyViolation("memory retrieval realm binding ile eslesmiyor")
        if (
            not query.allow_cross_project
            and query.key.scope in {MemoryScope.PROJECT, MemoryScope.WORK_ITEM}
            and query.key.project_ref != self.project_ref
        ):
            raise PolicyViolation("memory retrieval project binding ile eslesmiyor")
        if query.key.scope is MemoryScope.WORK_ITEM and query.key.work_ref != self.work_ref:
            raise PolicyViolation("memory retrieval work binding ile eslesmiyor")
        clauses = [
            "r.realm_id = %s",
            "r.state = 'active'",
            "r.scope not in ('run', 'agent')",
            "(r.valid_from is null or r.valid_from <= %s)",
            "(r.valid_until is null or r.valid_until > %s)",
            "(r.memory_class not in ('semantic', 'procedural', 'failure')"
            " or (r.reviewed_by is not null and r.reviewed_by <> r.author_ref))",
        ]
        params: list[Any] = [self.realm_id, at, at]
        if query.classes:
            clauses.append("r.memory_class = any (%s)")
            params.append([str(item) for item in sorted(query.classes, key=str)])

        scope_clauses = ["r.scope = 'global-user'"]
        if query.allow_cross_project:
            scope_clauses.append("r.scope = 'project'")
        elif (
            query.key.scope in {MemoryScope.PROJECT, MemoryScope.WORK_ITEM}
            and self.project_id is not None
        ):
            scope_clauses.append("(r.scope = 'project' and r.project_id = %s)")
            params.append(self.project_id)
        if (
            query.key.scope is MemoryScope.WORK_ITEM
            and self.project_id is not None
            and self.work_item_id is not None
        ):
            scope_clauses.append(
                "(r.scope = 'work-item' and r.project_id = %s and r.work_item_id = %s)"
            )
            params.extend((self.project_id, self.work_item_id))
        clauses.append("(" + " or ".join(scope_clauses) + ")")
        return " and ".join(clauses), tuple(params)

    def _from_row(self, row: Any) -> MemoryRecord:
        scope = MemoryScope(str(row[2]))
        key = MemoryKey(
            scope=scope,
            realm_ref=self.realm_ref,
            project_ref=str(row[5]) if row[5] else None,
            work_ref=str(row[6]) if row[6] else None,
        )
        return MemoryRecord(
            memory_id=str(row[1]),
            key=key,
            memory_class=MemoryClass(str(row[7])),
            content=str(row[8]),
            state=MemoryState(str(row[9])),
            revision=int(row[10]),
            created_at=row[20],
            evidence=tuple(
                MemoryEvidence(
                    kind=item["kind"], reference=item["reference"], digest_value=item["digest"]
                )
                for item in (row[11] or [])
            ),
            entities=tuple(row[12] or ()),
            valid_from=row[13],
            valid_until=row[14],
            author_ref=row[15],
            reviewed_by=row[16],
            superseded_by=str(row[17]) if row[17] else None,
            last_used_at=row[18],
        )
