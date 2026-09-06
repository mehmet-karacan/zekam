"""Rebuildable SQLite FTS5 + sqlite-vec knowledge index.

The database is a derived projection. Source manifests and provider profile
digests remain authoritative; a generation becomes visible only after every
chunk, lexical row and real vector has been committed in one transaction.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate

import sqlite_vec

from zekam.application.knowledge_index import (
    KNOWLEDGE_VECTOR_DIMENSION,
    KnowledgeGeneration,
    KnowledgeIndexRecord,
)
from zekam.application.retrieval_service import ChunkView
from zekam.application.technology_bakeoff import assess_sqlite_wal_safety
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes, parse_digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.domain.knowledge import Locator
from zekam.domain.retrieval import RetrievalChannel, ScoredHit
from zekam.infrastructure.local_file_security import private_regular, restrict_private_file

SCHEMA_VERSION = 2
VECTOR_DIMENSION = KNOWLEDGE_VECTOR_DIMENSION
# A real medium-sized repository can exceed 10k source chunks. Keep a hard
# per-generation bound for memory/disk safety, but size it to the Windows
# acceptance workload (sky-microservis currently produces about 20.5k chunks).
MAX_RECORDS_PER_GENERATION = 50_000
MAX_QUERY_BYTES = 16 * 1024
_TOKEN = re.compile(r"\w+", re.UNICODE)
_FileIdentity = tuple[int, int, int, int, int, int]
_SourceIdentity = tuple[_FileIdentity | None, ...]


def _stable_read[**P, R](
    method: Callable[Concatenate[SQLiteKnowledgeIndex, P], R],
) -> Callable[Concatenate[SQLiteKnowledgeIndex, P], R]:
    @wraps(method)
    def checked(self: SQLiteKnowledgeIndex, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._read_boundary():
            return method(self, *args, **kwargs)

    return checked


def _exact_json(value: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValidationFailed("Knowledge locator duplicate JSON key tasiyor")
            result[key] = item
        return result

    try:
        document = json.loads(value, object_pairs_hook=object_pairs)
    except json.JSONDecodeError as exc:
        raise ValidationFailed("Knowledge locator strict JSON olmali") from exc
    if not isinstance(document, dict):
        raise ValidationFailed("Knowledge locator JSON object olmali")
    return document


def _locator(value: str) -> Locator:
    document = _exact_json(value)
    allowed = {
        "page",
        "bbox",
        "heading_path",
        "block_index",
        "line_start",
        "line_end",
        "symbol",
        "object_name",
        "relative_path",
        "entry_path",
        "timestamp_start_ms",
        "timestamp_end_ms",
        "video_id",
    }
    if set(document) - allowed:
        raise ValidationFailed("Knowledge locator bilinmeyen alan tasiyor")
    try:
        return Locator(
            page=document.get("page"),
            bbox=tuple(document["bbox"]) if document.get("bbox") is not None else None,
            heading_path=tuple(document.get("heading_path") or ()),
            block_index=document.get("block_index"),
            line_start=document.get("line_start"),
            line_end=document.get("line_end"),
            symbol=document.get("symbol"),
            object_name=document.get("object_name"),
            relative_path=document.get("relative_path"),
            entry_path=document.get("entry_path"),
            timestamp_start_ms=document.get("timestamp_start_ms"),
            timestamp_end_ms=document.get("timestamp_end_ms"),
            video_id=document.get("video_id"),
        )
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("Knowledge locator alan tipleri gecersiz") from exc


class SQLiteKnowledgeIndex:
    """Persistent hybrid index; read-only mode requires an offline checkpointed file.

    Read-only opening never creates files, changes permissions or checkpoints a WAL.
    Immutable SQLite reads avoid shared-memory writes; file/sidecar identities are
    checked before and after each query so concurrent publication fails closed.
    An active WAL/journal must be resolved by its writer, never by this reader.
    """

    def __init__(self, path: Path, *, create: bool = False, read_only: bool = False) -> None:
        if type(create) is not bool or type(read_only) is not bool:
            raise ValidationFailed("Knowledge index create/read_only must be exact booleans")
        if read_only and create:
            raise ConfigurationError("Knowledge read-only index cannot create a schema")
        if not isinstance(path, Path) or not path.is_absolute() or path.name in {"", ".", ".."}:
            raise ConfigurationError("Knowledge index path absolute file olmali")
        self.path = path
        self._read_only = read_only
        self._journal_mode = (
            "wal"
            if assess_sqlite_wal_safety(sqlite3.sqlite_version).safe_for_multi_connection_wal
            else "delete"
        )
        self._source_identity_at_open: _SourceIdentity | None = None
        if read_only:
            self._source_identity_at_open = self._source_file_identity()
        else:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.is_symlink() or path.parent.is_symlink():
                raise ConfigurationError("Knowledge index path symlink olamaz")
        connection: sqlite3.Connection | None = None
        try:
            connection = (
                sqlite3.connect(
                    f"{path.as_uri()}?mode=ro&immutable=1", uri=True, isolation_level=None
                )
                if read_only
                else sqlite3.connect(path, isolation_level=None)
            )
            self._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute("pragma foreign_keys=on")
            connection.execute("pragma trusted_schema=off")
            connection.execute("pragma busy_timeout=5000")
            if read_only:
                connection.execute("pragma query_only=on")
            connection.enable_load_extension(True)
            try:
                sqlite_vec.load(connection)
            finally:
                connection.enable_load_extension(False)
            if not read_only:
                with self._single_writer():
                    actual_mode = str(
                        connection.execute(f"pragma journal_mode={self._journal_mode}").fetchone()[
                            0
                        ]
                    ).casefold()
                    if actual_mode != self._journal_mode:
                        raise ConfigurationError("Knowledge index journal policy uygulanamadi")
                    connection.execute("pragma synchronous=full")
                    if create:
                        self._create_schema()
            self._validate_schema()
            if not read_only:
                os.chmod(path, 0o600)
        except (OSError, sqlite3.DatabaseError) as exc:
            if connection is not None:
                connection.close()
            raise ConfigurationError(
                "Knowledge index existing file/schema could not be read"
            ) from exc
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    @property
    def read_only(self) -> bool:
        return self._read_only

    def _require_writable(self) -> None:
        if self._read_only:
            raise PolicyViolation("Knowledge read-only index cannot mutate or perform maintenance")

    @contextmanager
    def _single_writer(self) -> Iterator[None]:
        self._require_writable()
        lock_path = Path(str(self.path) + ".writer.lock")
        if lock_path.is_symlink():
            raise ConfigurationError("Knowledge writer lock symlink olamaz")
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        acquired = False
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
                os.fsync(descriptor)
            restrict_private_file(lock_path)
            identity = lock_path.lstat()
            opened = os.fstat(descriptor)
            if not private_regular(lock_path) or (identity.st_dev, identity.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise ConfigurationError("Knowledge writer lock identity/ACL drift")
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    if os.name == "nt":
                        msvcrt = importlib.import_module("msvcrt")
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl = importlib.import_module("fcntl")
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ConcurrencyConflict("Knowledge index writer already active") from exc
                    time.sleep(0.01)
            acquired = True
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    msvcrt = importlib.import_module("msvcrt")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl = importlib.import_module("fcntl")
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _source_file_identity(self) -> _SourceIdentity:
        """Metadata identity plus WAL/journal admission; never follows symlink ancestors."""
        try:
            for parent in self.path.parents:
                if not stat.S_ISDIR(parent.lstat().st_mode):
                    raise ConfigurationError(
                        "Knowledge read-only path ancestor must be a directory"
                    )
            identities: list[_FileIdentity | None] = []
            for suffix in ("", "-wal", "-journal", "-shm"):
                candidate = Path(str(self.path) + suffix)
                try:
                    info = candidate.lstat()
                except FileNotFoundError:
                    if not suffix:
                        raise ConfigurationError(
                            "Knowledge read-only existing file is missing"
                        ) from None
                    identities.append(None)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ConfigurationError(
                        "Knowledge read-only file/sidecar must be regular, not symlink"
                    )
                if suffix in {"-wal", "-journal"} and info.st_size:
                    raise ConfigurationError(
                        "Knowledge read-only requires offline checkpointed index"
                    )
                identities.append(
                    (
                        info.st_dev,
                        info.st_ino,
                        info.st_mode,
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                    )
                )
            return tuple(identities)
        except OSError as exc:
            raise ConfigurationError("Knowledge read-only existing path is unavailable") from exc

    def _assert_stable_source(self) -> None:
        try:
            current = self._source_file_identity()
        except ConfigurationError as exc:
            raise PolicyViolation(
                "Knowledge read-only source drift; offline checkpointed index required"
            ) from exc
        if current != self._source_identity_at_open:
            raise PolicyViolation("Knowledge read-only source fingerprint drift")

    @contextmanager
    def _read_boundary(self) -> Iterator[None]:
        if not self._read_only:
            yield
            return
        self._assert_stable_source()
        try:
            yield
        finally:
            self._assert_stable_source()

    def _create_schema(self) -> None:
        self._require_writable()
        self._connection.executescript(
            f"""
            create table if not exists metadata (
                singleton integer primary key check(singleton=1),
                schema_version integer not null,
                engine text not null,
                vector_dimension integer not null
            ) strict;
            insert or ignore into metadata values (1,{SCHEMA_VERSION},'sqlite-fts5+sqlite-vec',
                                                   {VECTOR_DIMENSION});
            create table if not exists generation (
                generation_digest text primary key,
                project_id text not null,
                source_revision text not null,
                tree_digest text not null,
                source_manifest_digest text not null,
                embedding_profile_digest text not null,
                provider_profile_digest text not null,
                chunk_count integer not null check(chunk_count > 0),
                state text not null check(state in ('building','ready','superseded')),
                created_at text not null
            ) strict;
            create unique index if not exists generation_ready_source
                on generation(project_id,source_revision,tree_digest,provider_profile_digest)
                where state='ready';
            create table if not exists current_generation (
                project_id text primary key,
                generation_digest text not null references generation(generation_digest)
            ) strict;
            create table if not exists chunk (
                rowid integer primary key,
                id text not null unique,
                generation_digest text not null references generation(generation_digest),
                project_id text not null,
                source_revision text not null,
                source_path text not null,
                source_digest text not null,
                locator_json text not null,
                body text not null,
                content_digest text not null,
                vector_digest text not null,
                chunk_order integer not null check(chunk_order >= 0),
                unique(generation_digest,source_path,chunk_order)
            ) strict;
            create index if not exists chunk_scope
                on chunk(project_id,generation_digest,source_path,chunk_order);
            create virtual table if not exists chunk_fts using fts5(
                id unindexed,
                generation_digest unindexed,
                project_id unindexed,
                source_path,
                body,
                tokenize='unicode61 remove_diacritics 2'
            );
            create virtual table if not exists chunk_vector using vec0(
                id text primary key,
                embedding float[{VECTOR_DIMENSION}],
                generation_digest text partition key,
                project_id text partition key
            );
            """
        )

    @_stable_read
    def _validate_schema(self) -> None:
        row = self._connection.execute(
            "select schema_version,engine,vector_dimension from metadata where singleton=1"
        ).fetchone()
        if row is None or tuple(row) != (
            SCHEMA_VERSION,
            "sqlite-fts5+sqlite-vec",
            VECTOR_DIMENSION,
        ):
            raise ConfigurationError("Knowledge index schema/engine drift")
        integrity = self._connection.execute("pragma quick_check").fetchall()
        if [str(item[0]) for item in integrity] != ["ok"]:
            raise ConfigurationError("Knowledge index integrity check gecemedi")
        if self._connection.execute("pragma foreign_key_check").fetchone() is not None:
            raise ConfigurationError("Knowledge index foreign key integrity check failed")
        required_columns = {
            "generation": (
                "generation_digest,project_id,source_revision,tree_digest,source_manifest_digest,"
                "embedding_profile_digest,provider_profile_digest,chunk_count,state,created_at"
            ),
            "current_generation": "project_id,generation_digest",
            "chunk": (
                "rowid,id,generation_digest,project_id,source_revision,source_path,source_digest,"
                "locator_json,body,content_digest,vector_digest,chunk_order"
            ),
            "chunk_fts": "rowid,id,generation_digest,project_id,source_path,body",
            "chunk_vector": "id,embedding,generation_digest,project_id",
        }
        for table, columns in required_columns.items():
            self._connection.execute(f"select {columns} from {table} limit 0")

    def _content_rows_consistent(self, generation_digest: str | None = None) -> bool:
        select_rows = (
            "select c.body,c.content_digest,c.vector_digest,c.locator_json,c.source_path,"
            " v.embedding vector_embedding,f.body fts_body,f.source_path fts_source_path"
            " from chunk c left join chunk_vector v on v.id=c.id"
            " left join chunk_fts f on f.rowid=c.rowid"
        )
        if generation_digest is None:
            rows = self._connection.execute(select_rows).fetchall()
        else:
            rows = self._connection.execute(
                select_rows + " where c.generation_digest=?",
                (generation_digest,),
            ).fetchall()
        for row in rows:
            try:
                locator = _locator(str(row["locator_json"]))
            except (PolicyViolation, ValidationFailed):
                return False
            if (
                str(row["content_digest"]) != digest_of_bytes(str(row["body"]).encode("utf-8"))
                or locator.relative_path != str(row["source_path"])
                or row["vector_embedding"] is None
                or str(row["vector_digest"]) != digest_of_bytes(bytes(row["vector_embedding"]))
                or str(row["fts_body"]) != str(row["body"])
                or str(row["fts_source_path"]) != str(row["source_path"])
            ):
                return False
        return True

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteKnowledgeIndex:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _generation_digest(
        records: tuple[KnowledgeIndexRecord, ...],
        *,
        project_id: str,
        source_revision: str,
        tree_digest: str,
        source_manifest_digest: str,
        embedding_profile_digest: str,
        provider_profile_digest: str,
    ) -> str:
        return digest(
            {
                "schema": "zekam-knowledge-generation/v1",
                "project_id": project_id,
                "source_revision": source_revision,
                "tree_digest": tree_digest,
                "source_manifest_digest": source_manifest_digest,
                "embedding_profile_digest": embedding_profile_digest,
                "provider_profile_digest": provider_profile_digest,
                "chunks": [
                    {
                        "id": record.chunk_id,
                        "source_path": record.source_path,
                        "source_digest": record.source_digest,
                        "content_digest": record.content_digest,
                        "vector_digest": digest_of_bytes(
                            sqlite_vec.serialize_float32(record.vector)
                        ),
                        "order": record.chunk_order,
                    }
                    for record in records
                ],
            }
        )

    def build_generation(
        self,
        records: tuple[KnowledgeIndexRecord, ...],
        *,
        project_id: str,
        source_revision: str,
        tree_digest: str,
        source_manifest_digest: str,
        embedding_profile_digest: str,
        provider_profile_digest: str,
        created_at: str,
    ) -> KnowledgeGeneration:
        with self._single_writer():
            return self._build_generation_locked(
                records,
                project_id=project_id,
                source_revision=source_revision,
                tree_digest=tree_digest,
                source_manifest_digest=source_manifest_digest,
                embedding_profile_digest=embedding_profile_digest,
                provider_profile_digest=provider_profile_digest,
                created_at=created_at,
            )

    def _build_generation_locked(
        self,
        records: tuple[KnowledgeIndexRecord, ...],
        *,
        project_id: str,
        source_revision: str,
        tree_digest: str,
        source_manifest_digest: str,
        embedding_profile_digest: str,
        provider_profile_digest: str,
        created_at: str,
    ) -> KnowledgeGeneration:
        self._require_writable()
        if not records:
            raise ValidationFailed("Knowledge generation en az bir kayit ister")
        if len(records) > MAX_RECORDS_PER_GENERATION:
            raise ValidationFailed(
                "Knowledge generation kayit sinirini asiyor: "
                f"{len(records)} > {MAX_RECORDS_PER_GENERATION}"
            )
        if any(
            record.project_id != project_id or record.source_revision != source_revision
            for record in records
        ):
            raise ValidationFailed("Knowledge generation tek project/revision ister")
        if len({record.chunk_id for record in records}) != len(records):
            raise ValidationFailed("Knowledge generation duplicate chunk id tasiyor")
        for value in (
            tree_digest,
            source_manifest_digest,
            embedding_profile_digest,
            provider_profile_digest,
        ):
            parse_digest(value)
        generation_digest = self._generation_digest(
            records,
            project_id=project_id,
            source_revision=source_revision,
            tree_digest=tree_digest,
            source_manifest_digest=source_manifest_digest,
            embedding_profile_digest=embedding_profile_digest,
            provider_profile_digest=provider_profile_digest,
        )
        existing = self._connection.execute(
            "select chunk_count,state from generation where generation_digest=?",
            (generation_digest,),
        ).fetchone()
        if existing is not None:
            current = self._connection.execute(
                "select generation_digest from current_generation where project_id=?",
                (project_id,),
            ).fetchone()
            counts = self._connection.execute(
                "select (select count(*) from chunk where generation_digest=?),"
                " (select count(*) from chunk_fts where generation_digest=?),"
                " (select count(*) from chunk_vector where generation_digest=?)",
                (generation_digest, generation_digest, generation_digest),
            ).fetchone()
            if (
                str(existing[1]) != "ready"
                or int(existing[0]) != len(records)
                or counts is None
                or tuple(int(value) for value in counts)
                != (len(records), len(records), len(records))
                or not self._content_rows_consistent(generation_digest)
                or current is None
                or str(current[0]) != generation_digest
            ):
                raise PolicyViolation("Knowledge generation replay state corrupt/recovery-required")
            return self.generation(project_id)
        try:
            self._connection.execute("begin immediate")
            self._connection.execute(
                "insert into generation values (?,?,?,?,?,?,?,?,?,?)",
                (
                    generation_digest,
                    project_id,
                    source_revision,
                    tree_digest,
                    source_manifest_digest,
                    embedding_profile_digest,
                    provider_profile_digest,
                    len(records),
                    "building",
                    created_at,
                ),
            )
            previous = self._connection.execute(
                "select generation_digest from current_generation where project_id=?",
                (project_id,),
            ).fetchone()
            for record in records:
                locator_json = canonical_json(record.locator.as_dict())
                serialized_vector = sqlite_vec.serialize_float32(record.vector)
                cursor = self._connection.execute(
                    "insert into chunk(id,generation_digest,project_id,source_revision,"
                    "source_path,source_digest,locator_json,body,content_digest,vector_digest,"
                    "chunk_order) values (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.chunk_id,
                        generation_digest,
                        project_id,
                        source_revision,
                        record.source_path,
                        record.source_digest,
                        locator_json,
                        record.text,
                        record.content_digest,
                        digest_of_bytes(serialized_vector),
                        record.chunk_order,
                    ),
                )
                if cursor.lastrowid is None:
                    raise PolicyViolation("Knowledge chunk rowid uretilmedi")
                rowid = int(cursor.lastrowid)
                self._connection.execute(
                    "insert into chunk_fts(rowid,id,generation_digest,project_id,source_path,body)"
                    " values (?,?,?,?,?,?)",
                    (
                        rowid,
                        record.chunk_id,
                        generation_digest,
                        project_id,
                        record.source_path,
                        record.text,
                    ),
                )
                self._connection.execute(
                    "insert into chunk_vector(id,embedding,generation_digest,project_id)"
                    " values (?,?,?,?)",
                    (
                        record.chunk_id,
                        serialized_vector,
                        generation_digest,
                        project_id,
                    ),
                )
            counts = self._connection.execute(
                "select (select count(*) from chunk where generation_digest=?),"
                " (select count(*) from chunk_fts where generation_digest=?),"
                " (select count(*) from chunk_vector where generation_digest=?)",
                (generation_digest, generation_digest, generation_digest),
            ).fetchone()
            if counts is None or tuple(int(value) for value in counts) != (
                len(records),
                len(records),
                len(records),
            ):
                raise PolicyViolation("Knowledge generation partial build")
            if previous is not None:
                self._connection.execute(
                    "update generation set state='superseded' where generation_digest=?",
                    (str(previous[0]),),
                )
            self._connection.execute(
                "update generation set state='ready' where generation_digest=?",
                (generation_digest,),
            )
            self._connection.execute(
                "insert into current_generation(project_id,generation_digest) values (?,?)"
                " on conflict(project_id) do update set"
                " generation_digest=excluded.generation_digest",
                (project_id, generation_digest),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return self.generation(project_id)

    @staticmethod
    def _generation_from_row(row: sqlite3.Row) -> KnowledgeGeneration:
        return KnowledgeGeneration(
            generation_digest=str(row["generation_digest"]),
            project_id=str(row["project_id"]),
            source_revision=str(row["source_revision"]),
            tree_digest=str(row["tree_digest"]),
            source_manifest_digest=str(row["source_manifest_digest"]),
            embedding_profile_digest=str(row["embedding_profile_digest"]),
            provider_profile_digest=str(row["provider_profile_digest"]),
            chunk_count=int(row["chunk_count"]),
            state=str(row["state"]),
        )

    @_stable_read
    def generation(self, project_id: str) -> KnowledgeGeneration:
        row = self._connection.execute(
            "select g.* from current_generation c join generation g"
            " on g.generation_digest=c.generation_digest where c.project_id=?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ValidationFailed("Project current knowledge generation bulunamadi")
        return self._generation_from_row(row)

    def _pinned_generation(
        self, project_id: str, generation_digest: str | None
    ) -> KnowledgeGeneration:
        if generation_digest is None:
            return self.generation(project_id)
        parse_digest(generation_digest)
        row = self._connection.execute(
            "select * from generation where project_id=? and generation_digest=?"
            " and state in ('ready','superseded')",
            (project_id, generation_digest),
        ).fetchone()
        if row is None:
            raise PolicyViolation("Knowledge pinned generation unavailable")
        return self._generation_from_row(row)

    @staticmethod
    def _fts_expression(query: str) -> str:
        tokens = tuple(dict.fromkeys(item.casefold() for item in _TOKEN.findall(query)))
        if not tokens:
            return ""
        return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    @_stable_read
    def exact(
        self,
        project_id: str,
        identifiers: tuple[str, ...],
        *,
        limit: int,
        generation_digest: str | None = None,
    ) -> tuple[ScoredHit, ...]:
        if not identifiers or limit < 1:
            return ()
        generation = self._pinned_generation(project_id, generation_digest)
        hits: list[ScoredHit] = []
        seen: set[str] = set()
        for identifier in identifiers:
            if not identifier or len(identifier.encode("utf-8")) > MAX_QUERY_BYTES:
                continue
            normalized = identifier.casefold()
            rows = self._connection.execute(
                "select id from chunk where project_id=? and generation_digest=?"
                " and ("
                " lower(coalesce(json_extract(locator_json,'$.object_name'),''))=?"
                " or instr(lower(coalesce(json_extract(locator_json,'$.object_name'),'')),?)>0"
                " or instr(lower(source_path),?)>0"
                " or instr(lower(body),?)>0"
                ")"
                " order by case"
                " when lower(coalesce(json_extract(locator_json,'$.object_name'),''))=? then 0"
                " when instr(lower(coalesce(json_extract(locator_json,'$.object_name'),'')),?)>0"
                " then 1"
                " when instr(lower(source_path),?)>0 then 2"
                " else 3 end,chunk_order,id limit ?",
                (
                    project_id,
                    generation.generation_digest,
                    normalized,
                    normalized,
                    normalized,
                    normalized,
                    normalized,
                    normalized,
                    normalized,
                    limit,
                ),
            ).fetchall()
            for row in rows:
                chunk_id = str(row[0])
                if chunk_id not in seen:
                    seen.add(chunk_id)
                    hits.append(ScoredHit(chunk_id, RetrievalChannel.EXACT, len(hits) + 1, 1.0))
                    if len(hits) == limit:
                        return tuple(hits)
        return tuple(hits)

    @_stable_read
    def lexical(
        self,
        project_id: str,
        query: str,
        *,
        limit: int,
        generation_digest: str | None = None,
    ) -> tuple[ScoredHit, ...]:
        if not query.strip() or len(query.encode("utf-8")) > MAX_QUERY_BYTES or limit < 1:
            return ()
        expression = self._fts_expression(query)
        if not expression:
            return ()
        generation = self._pinned_generation(project_id, generation_digest)
        rows = self._connection.execute(
            "select f.id,bm25(chunk_fts) score from chunk_fts f"
            " where chunk_fts match ? and f.project_id=? and f.generation_digest=?"
            " order by score,f.rowid limit ?",
            (expression, project_id, generation.generation_digest, limit),
        ).fetchall()
        return tuple(
            ScoredHit(str(row[0]), RetrievalChannel.LEXICAL, rank, float(row[1]))
            for rank, row in enumerate(rows, start=1)
        )

    @_stable_read
    def dense(
        self,
        project_id: str,
        vector: tuple[float, ...],
        *,
        limit: int,
        generation_digest: str | None = None,
    ) -> tuple[ScoredHit, ...]:
        if limit < 1:
            return ()
        if len(vector) != VECTOR_DIMENSION or any(not math.isfinite(value) for value in vector):
            raise ValidationFailed("Knowledge dense query vector invalid")
        generation = self._pinned_generation(project_id, generation_digest)
        rows = self._connection.execute(
            "select id,distance from chunk_vector where embedding match ? and k=?"
            " and generation_digest=? and project_id=? order by distance",
            (
                sqlite_vec.serialize_float32(vector),
                min(limit, generation.chunk_count),
                generation.generation_digest,
                project_id,
            ),
        ).fetchall()
        return tuple(
            ScoredHit(
                str(row[0]),
                RetrievalChannel.DENSE,
                rank,
                max(-1.0, min(1.0, 1.0 - float(row[1]) ** 2 / 2.0)),
            )
            for rank, row in enumerate(rows, start=1)
        )

    @_stable_read
    def views(
        self,
        project_id: str,
        chunk_refs: tuple[str, ...],
        *,
        generation_digest: str | None = None,
    ) -> dict[str, ChunkView]:
        if not chunk_refs:
            return {}
        generation = self._pinned_generation(project_id, generation_digest)
        placeholders = ",".join("?" for _ in chunk_refs)
        rows = self._connection.execute(
            "select id,generation_digest,source_revision,source_path,source_digest,"
            " locator_json,body,content_digest from chunk where project_id=?"
            " and generation_digest=? and id in (" + placeholders + ")",
            (project_id, generation.generation_digest, *chunk_refs),
        ).fetchall()
        result: dict[str, ChunkView] = {}
        for row in rows:
            locator = _locator(str(row["locator_json"]))
            if locator.relative_path != str(row["source_path"]):
                raise PolicyViolation("Knowledge citation locator/source drift")
            if str(row["content_digest"]) != digest_of_bytes(str(row["body"]).encode("utf-8")):
                raise PolicyViolation("Knowledge citation body/content digest drift")
            result[str(row["id"])] = ChunkView(
                chunk_id=str(row["id"]),
                document_id=f"{row['generation_digest']}:{row['source_revision']}",
                text=str(row["body"]),
                locator=locator,
                content_digest=str(row["content_digest"]),
            )
        return result

    @_stable_read
    def source_identity(
        self,
        project_id: str,
        chunk_id: str,
        *,
        generation_digest: str | None = None,
    ) -> dict[str, str]:
        generation = self._pinned_generation(project_id, generation_digest)
        row = self._connection.execute(
            "select source_revision,source_path,source_digest,content_digest,body"
            " from chunk where project_id=? and generation_digest=? and id=?",
            (project_id, generation.generation_digest, chunk_id),
        ).fetchone()
        if row is None:
            raise PolicyViolation("Citation chunk active generation disinda")
        if str(row["content_digest"]) != digest_of_bytes(str(row["body"]).encode("utf-8")):
            raise PolicyViolation("Knowledge citation identity/content digest drift")
        return {
            "source_id": digest(
                {
                    "schema": "zekam-project-source-identity/v1",
                    "project_id": project_id,
                    "source_ref": str(row["source_path"]),
                }
            ),
            "source_revision": str(row["source_revision"]),
            "source_ref": str(row["source_path"]),
            "source_digest": str(row["source_digest"]),
            "content_digest": str(row["content_digest"]),
        }

    @_stable_read
    def integrity(self) -> dict[str, object]:
        quick = self._connection.execute("pragma quick_check").fetchone()
        projects = self._connection.execute(
            "select c.project_id,g.chunk_count,"
            " (select count(*) from chunk x where x.generation_digest=g.generation_digest),"
            " (select count(*) from chunk_fts f where f.generation_digest=g.generation_digest),"
            " (select count(*) from chunk_vector v where v.generation_digest=g.generation_digest)"
            " from current_generation c join generation g"
            " on g.generation_digest=c.generation_digest order by c.project_id"
        ).fetchall()
        consistent = all(
            len({int(row[1]), int(row[2]), int(row[3]), int(row[4])}) == 1 for row in projects
        )
        content_consistent = self._content_rows_consistent()
        return {
            "quick_check": str(quick[0]) if quick else "missing",
            "project_count": len(projects),
            "generation_counts_consistent": consistent,
            "content_digests_consistent": content_consistent,
            "status": (
                "passed"
                if quick and quick[0] == "ok" and consistent and content_consistent
                else "failed"
            ),
        }
