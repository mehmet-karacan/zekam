"""Tek kullanicili kurulumlar icin gercek SQLite minimum persistence profili.

Bu profil PostgreSQL'in RLS/queue/governance kapsamını taklit etmez. Project registry,
Work kaydi, knowledge chunk ve JSON-vector cosine aramasini yerel ve agsiz saglar.
Desteklenmeyen control-plane yuzeyleri sessizce PostgreSQL'e dusmez.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zekam.domain.canonical import parse_digest
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.domain.identifiers import assert_portable, new_uuid7, validate_slug

SCHEMA_VERSION = 1

_SCHEMA = """
create table if not exists zekam_meta (
    key text primary key,
    value text not null
) strict;
create table if not exists project (
    id text primary key,
    slug text not null unique,
    display_name text not null,
    source_ref text,
    created_at text not null
) strict;
create table if not exists work_item (
    id text primary key,
    project_id text not null references project(id),
    kind text not null,
    title text not null,
    state text not null check (state in ('ready', 'active', 'blocked', 'completed', 'cancelled')),
    revision integer not null default 1 check (revision > 0),
    evidence_digest text,
    created_at text not null,
    check (
        state <> 'completed' or (
            evidence_digest is not null
            and length(evidence_digest) = 71
            and substr(evidence_digest, 1, 7) = 'sha256:'
            and substr(evidence_digest, 8) not glob '*[^0-9a-f]*'
        )
    )
) strict;
create table if not exists knowledge_chunk (
    id text primary key,
    project_id text not null references project(id),
    source_ref text not null,
    body text not null,
    metadata_json text not null,
    created_at text not null
) strict;
create table if not exists knowledge_embedding (
    chunk_id text primary key references knowledge_chunk(id) on delete cascade,
    model_ref text not null,
    dimension integer not null check (dimension > 0),
    vector_json text not null,
    created_at text not null
) strict;
create index if not exists work_item_project_idx on work_item(project_id, state);
create index if not exists knowledge_chunk_project_idx on knowledge_chunk(project_id);
"""


@dataclass(frozen=True, slots=True)
class SQLiteStatus:
    exists: bool
    schema_version: int | None
    integrity_ok: bool
    schema_ok: bool


@dataclass(frozen=True, slots=True)
class ProjectRow:
    id: str
    slug: str
    display_name: str
    source_ref: str | None


@dataclass(frozen=True, slots=True)
class WorkRow:
    id: str
    project_id: str
    kind: str
    title: str
    state: str
    revision: int
    evidence_digest: str | None


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: str
    source_ref: str
    body: str
    score: float


def _now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def _connect(path: Path, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    if read_only:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma foreign_keys = on")
    connection.execute("pragma busy_timeout = 5000")
    try:
        yield connection
    finally:
        connection.close()


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "select type, name, tbl_name, sql from sqlite_master "
        "where name not like 'sqlite_%' "
        "order by type, name"
    ).fetchall()
    payload = json.dumps(
        [(row["type"], row["name"], row["tbl_name"], row["sql"]) for row in rows],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _expected_schema_fingerprint() -> str:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(_SCHEMA)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


SCHEMA_DIGEST = _expected_schema_fingerprint()


def bootstrap(path: Path) -> SQLiteStatus:
    """Minimum semayi atomik ve idempotent bicimde kurar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as connection:
        try:
            connection.executescript("begin immediate;\n" + _SCHEMA)
            row = connection.execute(
                "select value from zekam_meta where key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "insert into zekam_meta(key, value) values ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif str(row[0]) != str(SCHEMA_VERSION):
                raise ConfigurationError("SQLite schema version drift tespit edildi")
            observed_digest = _schema_fingerprint(connection)
            if observed_digest != SCHEMA_DIGEST:
                raise ConfigurationError("SQLite schema manifest drift tespit edildi")
            connection.execute(
                "insert into zekam_meta(key, value) values ('schema_digest', ?) "
                "on conflict(key) do update set value = excluded.value",
                (SCHEMA_DIGEST,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return status(path)


def status(path: Path) -> SQLiteStatus:
    """Dosya ve migration durumunu mutation olmadan okur."""
    if not path.is_file():
        return SQLiteStatus(exists=False, schema_version=None, integrity_ok=False, schema_ok=False)
    try:
        with _connect(path, read_only=True) as connection:
            row = connection.execute(
                "select value from zekam_meta where key = 'schema_version'"
            ).fetchone()
            integrity = connection.execute("pragma integrity_check").fetchone()
            digest_row = connection.execute(
                "select value from zekam_meta where key = 'schema_digest'"
            ).fetchone()
            observed_digest = _schema_fingerprint(connection)
            try:
                schema_version = int(row[0]) if row is not None else None
            except (TypeError, ValueError):
                schema_version = None
    except (sqlite3.DatabaseError, OSError):
        return SQLiteStatus(exists=True, schema_version=None, integrity_ok=False, schema_ok=False)
    return SQLiteStatus(
        exists=True,
        schema_version=schema_version,
        integrity_ok=bool(integrity and integrity[0] == "ok"),
        schema_ok=bool(
            digest_row and digest_row[0] == SCHEMA_DIGEST and observed_digest == SCHEMA_DIGEST
        ),
    )


class SQLitePersistence:
    """SQLite minimum profilinin explicit repository siniri."""

    def __init__(self, path: Path) -> None:
        current = status(path)
        if (
            not current.integrity_ok
            or not current.schema_ok
            or current.schema_version != SCHEMA_VERSION
        ):
            raise ConfigurationError("SQLite persistence bootstrap veya migration gerektiriyor")
        self._path = path

    def create_project(
        self, *, slug: str, display_name: str, source_ref: str | None = None
    ) -> ProjectRow:
        validate_slug(slug)
        if not display_name.strip():
            raise ValidationFailed("Proje gorunen adi bos olamaz")
        if source_ref is not None:
            source_ref = assert_portable(source_ref)
        with _connect(self._path) as connection:
            connection.execute("begin immediate")
            existing = connection.execute(
                "select id, slug, display_name, source_ref from project where slug = ?", (slug,)
            ).fetchone()
            if existing is None:
                project_id = str(new_uuid7())
                connection.execute(
                    "insert into project(id, slug, display_name, source_ref, created_at)"
                    " values (?, ?, ?, ?, ?)",
                    (project_id, slug, display_name, source_ref, _now()),
                )
                connection.commit()
                return ProjectRow(project_id, slug, display_name, source_ref)
            if existing["display_name"] != display_name or existing["source_ref"] != source_ref:
                connection.rollback()
                raise ValidationFailed("SQLite project slug replay payload drift")
            connection.commit()
            return ProjectRow(**dict(existing))

    def list_projects(self) -> tuple[ProjectRow, ...]:
        with _connect(self._path, read_only=True) as connection:
            rows = connection.execute(
                "select id, slug, display_name, source_ref from project order by slug"
            ).fetchall()
        return tuple(ProjectRow(**dict(row)) for row in rows)

    def get_project(self, reference: str) -> ProjectRow:
        with _connect(self._path, read_only=True) as connection:
            row = connection.execute(
                "select id, slug, display_name, source_ref from project where slug = ? or id = ?",
                (reference, reference),
            ).fetchone()
        if row is None:
            raise ValidationFailed(f"SQLite proje bulunamadi: {reference}")
        return ProjectRow(**dict(row))

    def create_work(
        self,
        *,
        project_id: str,
        kind: str,
        title: str,
        state: str = "ready",
        evidence_digest: str | None = None,
    ) -> WorkRow:
        if not kind.strip() or not title.strip():
            raise ValidationFailed("Work kind ve title bos olamaz")
        if evidence_digest is not None:
            parse_digest(evidence_digest)
        if state == "completed" and evidence_digest is None:
            raise ValidationFailed("Completed Work canonical evidence digest gerektirir")
        work_id = str(new_uuid7())
        try:
            with _connect(self._path) as connection:
                connection.execute(
                    "insert into work_item(id, project_id, kind, title, state, evidence_digest,"
                    " created_at) values (?, ?, ?, ?, ?, ?, ?)",
                    (work_id, project_id, kind, title, state, evidence_digest, _now()),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("SQLite Work kaydi constraint ihlali") from exc
        return WorkRow(work_id, project_id, kind, title, state, 1, evidence_digest)

    def list_work(self, *, project_id: str | None = None) -> tuple[WorkRow, ...]:
        with _connect(self._path, read_only=True) as connection:
            statement = (
                "select id, project_id, kind, title, state, revision, evidence_digest "
                "from work_item"
            )
            if project_id is None:
                rows = connection.execute(statement + " order by created_at, id").fetchall()
            else:
                rows = connection.execute(
                    statement + " where project_id = ? order by created_at, id",
                    (project_id,),
                ).fetchall()
        return tuple(WorkRow(**dict(row)) for row in rows)

    def index_chunk(
        self,
        *,
        project_id: str,
        source_ref: str,
        body: str,
        metadata: dict[str, Any],
        model_ref: str,
        vector: Sequence[float],
    ) -> str:
        if not body.strip():
            raise ValidationFailed("Knowledge chunk body bos olamaz")
        normalized = _normalized_vector(vector)
        source_ref = assert_portable(source_ref)
        model_ref = assert_portable(model_ref)
        chunk_id = str(new_uuid7())
        try:
            with _connect(self._path) as connection:
                connection.execute("begin immediate")
                connection.execute(
                    "insert into knowledge_chunk(id, project_id, source_ref, body, metadata_json,"
                    " created_at) values (?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        project_id,
                        source_ref,
                        body,
                        json.dumps(metadata, sort_keys=True, ensure_ascii=False, allow_nan=False),
                        _now(),
                    ),
                )
                connection.execute(
                    "insert into knowledge_embedding(chunk_id, model_ref, dimension, vector_json,"
                    " created_at) values (?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        model_ref,
                        len(normalized),
                        json.dumps(normalized, separators=(",", ":"), allow_nan=False),
                        _now(),
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValidationFailed("SQLite knowledge kaydi constraint ihlali") from exc
        return chunk_id

    def search(
        self,
        *,
        project_id: str,
        model_ref: str,
        query_vector: Sequence[float],
        limit: int = 10,
    ) -> tuple[SearchHit, ...]:
        query = _normalized_vector(query_vector)
        model_ref = assert_portable(model_ref)
        if limit < 1:
            raise ValidationFailed("Arama limiti pozitif olmali")
        with _connect(self._path, read_only=True) as connection:
            rows = connection.execute(
                "select c.id, c.source_ref, c.body, e.dimension, e.vector_json"
                " from knowledge_chunk c join knowledge_embedding e on e.chunk_id = c.id"
                " where c.project_id = ? and e.model_ref = ?",
                (project_id, model_ref),
            ).fetchall()
        hits: list[SearchHit] = []
        for row in rows:
            if int(row["dimension"]) != len(query):
                continue
            vector = tuple(float(value) for value in json.loads(row["vector_json"]))
            score = sum(left * right for left, right in zip(query, vector, strict=True))
            hits.append(SearchHit(row["id"], row["source_ref"], row["body"], score))
        hits.sort(key=lambda item: (-item.score, item.chunk_id))
        return tuple(hits[:limit])


def _normalized_vector(values: Sequence[float]) -> tuple[float, ...]:
    vector = tuple(float(value) for value in values)
    if not vector or not all(math.isfinite(value) for value in vector):
        raise ValidationFailed("Embedding sonlu ve bos olmayan sayilardan olusmali")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise ValidationFailed("Sifir embedding cosine aramasinda kullanilamaz")
    return tuple(value / norm for value in vector)
