"""SQLite code graph store with the same security posture as SQLiteKnowledgeIndex.

The graph store is a derived, rebuildable projection.  A generation becomes the
``current_graph_generation`` only after every file, symbol, edge and FTS row has
been committed in a single transaction; the previous generation is superseded in
the same transaction.

Security posture (mirrors ``SQLiteKnowledgeIndex``)
- absolute private path required,
- symlink / reparse ancestor rejection,
- single writer through an advisory ``.writer.lock`` file,
- read-only immutable ``mode=ro&immutable=1`` opening that never mutates,
- integrity checks (``quick_check`` + ``foreign_key_check``),
- atomic generation publication with prior supersede + current pointer update.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Concatenate

from zekam.application.code_graph import CodeGraphPort, generation_digest
from zekam.domain.canonical import parse_digest
from zekam.domain.code_graph import (
    DEPENDENCY_RELATIONS,
    GraphEdge,
    GraphFile,
    GraphGeneration,
    GraphSymbol,
)
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.infrastructure.local_file_security import private_regular, restrict_private_file

SCHEMA_VERSION = 1
ENGINE = "sqlite-code-graph/v1"
MAX_FILES_PER_GENERATION = 200_000
MAX_SYMBOLS_PER_GENERATION = 2_000_000
MAX_EDGES_PER_GENERATION = 5_000_000
MAX_BODY_BYTES = 512 * 1024

_ParseState = str
_FileIdentity = tuple[int, int, int, int, int, int]
_SourceIdentity = tuple[_FileIdentity | None, ...]


def _stable_read[**P, R](
    method: Callable[Concatenate[SQLiteCodeGraphStore, P], R],
) -> Callable[Concatenate[SQLiteCodeGraphStore, P], R]:
    @wraps(method)
    def checked(self: SQLiteCodeGraphStore, /, *args: P.args, **kwargs: P.kwargs) -> R:
        if self._read_only:
            with self._read_boundary():
                return method(self, *args, **kwargs)
        return method(self, *args, **kwargs)

    return checked


class SQLiteCodeGraphStore(CodeGraphPort):
    """Rebuildable, atomic code graph store."""

    def __init__(self, path: Path, *, create: bool = False, read_only: bool = False) -> None:
        if type(create) is not bool or type(read_only) is not bool:
            raise ValidationFailed("Graph store create/read_only exact boolean olmali")
        if read_only and create:
            raise ConfigurationError("Graph read-only store cannot create a schema")
        if not isinstance(path, Path) or not path.is_absolute() or path.name in {"", ".", ".."}:
            raise ConfigurationError("Graph store path absolute file olmali")
        self._path = path
        self._read_only = read_only
        self._source_identity_at_open: _SourceIdentity | None = None
        if read_only:
            self._source_identity_at_open = self._source_file_identity()
        else:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.is_symlink() or path.parent.is_symlink():
                raise ConfigurationError("Graph store path symlink olamaz")
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
            if not read_only:
                with self._single_writer():
                    actual_mode = str(
                        connection.execute("pragma journal_mode=delete").fetchone()[0]
                    ).casefold()
                    if actual_mode != "delete":
                        raise ConfigurationError("Graph store journal policy uygulanamadi")
                    connection.execute("pragma synchronous=full")
                    if create:
                        self._create_schema()
            self._validate_schema()
            if not read_only and create:
                os.chmod(path, 0o600)
        except (OSError, sqlite3.DatabaseError) as exc:
            if connection is not None:
                connection.close()
            raise ConfigurationError("Graph store existing file/schema could not be read") from exc
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def path(self) -> Path:
        return self._path

    def _require_writable(self) -> None:
        if self._read_only:
            raise PolicyViolation("Graph read-only store cannot mutate or perform maintenance")

    @contextmanager
    def _single_writer(self) -> Iterator[None]:
        self._require_writable()
        lock_path = Path(str(self.path) + ".writer.lock")
        if lock_path.is_symlink():
            raise ConfigurationError("Graph writer lock symlink olamaz")
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
                raise ConfigurationError("Graph writer lock identity/ACL drift")
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    if os.name == "nt":
                        import importlib

                        msvcrt = importlib.import_module("msvcrt")
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import importlib

                        fcntl = importlib.import_module("fcntl")
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ConcurrencyConflict("Graph store writer already active") from exc
                    time.sleep(0.01)
            acquired = True
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    import importlib

                    msvcrt = importlib.import_module("msvcrt")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import importlib

                    fcntl = importlib.import_module("fcntl")
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _source_file_identity(self) -> _SourceIdentity:
        try:
            for parent in self.path.parents:
                if not stat.S_ISDIR(parent.lstat().st_mode):
                    raise ConfigurationError(
                        "Graph read-only path ancestor must be a directory"
                    )
            identities: list[_FileIdentity | None] = []
            for suffix in ("", "-wal", "-journal", "-shm"):
                candidate = Path(str(self.path) + suffix)
                try:
                    info = candidate.lstat()
                except FileNotFoundError:
                    if not suffix:
                        raise ConfigurationError(
                            "Graph read-only existing file is missing"
                        ) from None
                    identities.append(None)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ConfigurationError(
                        "Graph read-only file/sidecar must be regular, not symlink"
                    )
                if suffix in {"-wal", "-journal"} and info.st_size:
                    raise ConfigurationError(
                        "Graph read-only requires offline checkpointed index"
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
            raise ConfigurationError("Graph read-only existing path is unavailable") from exc

    def _assert_stable_source(self) -> None:
        try:
            current = self._source_file_identity()
        except ConfigurationError as exc:
            raise PolicyViolation(
                "Graph read-only source drift; offline checkpointed index required"
            ) from exc
        if current != self._source_identity_at_open:
            raise PolicyViolation("Graph read-only source fingerprint drift")

    @contextmanager
    def _read_boundary(self) -> Iterator[None]:
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
                engine text not null
            ) strict;
            insert or ignore into metadata values (1,{SCHEMA_VERSION},'{ENGINE}');
            create table if not exists graph_generation (
                generation_digest text primary key,
                project_id text not null,
                source_revision text not null,
                tree_digest text not null,
                source_manifest_digest text not null,
                extractor_profile_digest text not null,
                file_count integer not null check(file_count > 0),
                symbol_count integer not null check(symbol_count >= 0),
                edge_count integer not null check(edge_count >= 0),
                error_count integer not null check(error_count >= 0),
                state text not null check(state in ('building','ready','superseded')),
                created_at text not null
            ) strict;
            create unique index if not exists graph_generation_ready_source
                on graph_generation(project_id,source_revision,tree_digest,
                                    extractor_profile_digest) where state='ready';
            create table if not exists current_graph_generation (
                project_id text primary key,
                generation_digest text not null references graph_generation(generation_digest)
            ) strict;
            create table if not exists graph_file (
                rowid integer primary key,
                generation_digest text not null references graph_generation(generation_digest),
                relative_path text not null,
                content_digest text not null,
                parse_state text not null check(parse_state in ('parsed','syntax-error')),
                error_count integer not null check(error_count >= 0),
                symbol_count integer not null check(symbol_count >= 0),
                unique(generation_digest,relative_path)
            ) strict;
            create table if not exists graph_symbol (
                rowid integer primary key,
                generation_digest text not null references graph_generation(generation_digest),
                symbol_id text not null,
                file_rowid integer not null references graph_file(rowid),
                qualified_name text not null,
                kind text not null,
                parent_symbol_id text,
                body_digest text not null,
                confidence text not null,
                start_line integer not null check(start_line >= 1),
                end_line integer not null check(end_line >= 1),
                unique(generation_digest,symbol_id)
            ) strict;
            create index if not exists graph_symbol_file
                on graph_symbol(generation_digest,file_rowid,symbol_id);
            create table if not exists graph_edge (
                rowid integer primary key,
                generation_digest text not null references graph_generation(generation_digest),
                edge_id text not null,
                source_symbol_id text not null,
                relation text not null,
                target_symbol_id text,
                target_qualified_name text not null,
                confidence text not null,
                provenance text not null,
                unique(generation_digest,edge_id)
            ) strict;
            create index if not exists graph_edge_source
                on graph_edge(generation_digest,source_symbol_id,relation);
            create virtual table if not exists graph_file_fts using fts5(
                file_rowid unindexed,
                generation_digest unindexed,
                project_id unindexed,
                relative_path,
                symbol_names,
                tokenize='unicode61 remove_diacritics 2'
            );
            create table if not exists graph_chunk_link (
                rowid integer primary key,
                generation_digest text not null references graph_generation(generation_digest),
                graph_file_rowid integer not null references graph_file(rowid),
                knowledge_generation_digest text not null,
                chunk_id text not null,
                unique(generation_digest,graph_file_rowid,knowledge_generation_digest,chunk_id)
            ) strict;
            create table if not exists graph_annotation (
                rowid integer primary key,
                generation_digest text not null references graph_generation(generation_digest),
                symbol_id text not null,
                annotation_kind text not null,
                source text not null check(source in ('human','generated')),
                content text not null,
                content_digest text not null,
                created_at text not null
            ) strict;
            """
        )

    @_stable_read
    def _validate_schema(self) -> None:
        row = self._connection.execute(
            "select schema_version,engine from metadata where singleton=1"
        ).fetchone()
        if row is None or tuple(row) != (SCHEMA_VERSION, ENGINE):
            raise ConfigurationError("Graph store schema/engine drift")
        quick = self._connection.execute("pragma quick_check").fetchall()
        if [str(item[0]) for item in quick] != ["ok"]:
            raise ConfigurationError("Graph store integrity check gecemedi")
        if self._connection.execute("pragma foreign_key_check").fetchone() is not None:
            raise ConfigurationError("Graph store foreign key integrity check failed")
        required_columns = {
            "graph_generation": (
                "generation_digest,project_id,source_revision,tree_digest,"
                "source_manifest_digest,extractor_profile_digest,file_count,symbol_count,"
                "edge_count,error_count,state,created_at"
            ),
            "current_graph_generation": "project_id,generation_digest",
            "graph_file": (
                "rowid,generation_digest,relative_path,content_digest,parse_state,"
                "error_count,symbol_count"
            ),
            "graph_symbol": (
                "rowid,generation_digest,symbol_id,file_rowid,qualified_name,kind,"
                "parent_symbol_id,body_digest,confidence,start_line,end_line"
            ),
            "graph_edge": (
                "rowid,generation_digest,edge_id,source_symbol_id,relation,target_symbol_id,"
                "target_qualified_name,confidence,provenance"
            ),
            "graph_file_fts": "file_rowid,generation_digest,project_id,relative_path,symbol_names",
            "graph_chunk_link": (
                "rowid,generation_digest,graph_file_rowid,knowledge_generation_digest,chunk_id"
            ),
            "graph_annotation": (
                "rowid,generation_digest,symbol_id,annotation_kind,source,content,"
                "content_digest,created_at"
            ),
        }
        for table, columns in required_columns.items():
            self._connection.execute(f"select {columns} from {table} limit 0")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteCodeGraphStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _generation_payload(
        *,
        project_id: str,
        source_revision: str,
        tree_digest: str,
        source_manifest_digest: str,
        extractor_profile_digest: str,
        files: tuple[GraphFile, ...],
        symbols: tuple[GraphSymbol, ...],
        edges: tuple[GraphEdge, ...],
    ) -> str:
        return generation_digest(
            project_id=project_id,
            source_revision=source_revision,
            tree_digest=tree_digest,
            source_manifest_digest=source_manifest_digest,
            extractor_profile_digest=extractor_profile_digest,
            files=files,
            symbols=symbols,
            edges=edges,
        )

    def build_generation(
        self,
        *,
        project_id: str,
        source_revision: str,
        tree_digest: str,
        source_manifest_digest: str,
        extractor_profile_digest: str,
        files: tuple[GraphFile, ...],
        symbols: tuple[GraphSymbol, ...],
        edges: tuple[GraphEdge, ...],
        created_at: str,
    ) -> GraphGeneration:
        with self._single_writer():
            return self._build_generation_locked(
                project_id=project_id,
                source_revision=source_revision,
                tree_digest=tree_digest,
                source_manifest_digest=source_manifest_digest,
                extractor_profile_digest=extractor_profile_digest,
                files=files,
                symbols=symbols,
                edges=edges,
                created_at=created_at,
            )

    def _build_generation_locked(
        self,
        *,
        project_id: str,
        source_revision: str,
        tree_digest: str,
        source_manifest_digest: str,
        extractor_profile_digest: str,
        files: tuple[GraphFile, ...],
        symbols: tuple[GraphSymbol, ...],
        edges: tuple[GraphEdge, ...],
        created_at: str,
    ) -> GraphGeneration:
        self._require_writable()
        if not files:
            raise ValidationFailed("Graph generation en az bir dosya ister")
        if len(files) > MAX_FILES_PER_GENERATION:
            raise ValidationFailed("Graph generation dosya sinirini asiyor")
        if len(symbols) > MAX_SYMBOLS_PER_GENERATION:
            raise ValidationFailed("Graph generation symbol sinirini asiyor")
        if len(edges) > MAX_EDGES_PER_GENERATION:
            raise ValidationFailed("Graph generation edge sinirini asiyor")
        if any(
            file.extractor_profile_digest != extractor_profile_digest for file in files
        ):
            raise ValidationFailed("Graph generation profile/file digest drift")
        generation = self._generation_payload(
            project_id=project_id,
            source_revision=source_revision,
            tree_digest=tree_digest,
            source_manifest_digest=source_manifest_digest,
            extractor_profile_digest=extractor_profile_digest,
            files=files,
            symbols=symbols,
            edges=edges,
        )
        parse_digest(generation)
        existing = self._connection.execute(
            "select file_count,symbol_count,edge_count,state from graph_generation"
            " where generation_digest=?",
            (generation,),
        ).fetchone()
        if existing is not None:
            current = self._connection.execute(
                "select generation_digest from current_graph_generation where project_id=?",
                (project_id,),
            ).fetchone()
            counts = self._connection.execute(
                "select (select count(*) from graph_file where generation_digest=?),"
                " (select count(*) from graph_symbol where generation_digest=?),"
                " (select count(*) from graph_edge where generation_digest=?),"
                " (select count(*) from graph_file_fts where generation_digest=?)",
                (generation, generation, generation, generation),
            ).fetchone()
            if (
                str(existing[3]) != "ready"
                or int(existing[0]) != len(files)
                or int(existing[1]) != len(symbols)
                or int(existing[2]) != len(edges)
                or counts is None
                or tuple(int(value) for value in counts)
                != (len(files), len(symbols), len(edges), len(files))
                or current is None
                or str(current[0]) != generation
            ):
                raise PolicyViolation("Graph generation replay state corrupt/recovery-required")
            return self.generation(project_id)

        total_error_count = sum(file.error_count for file in files)
        try:
            self._connection.execute("begin immediate")
            self._connection.execute(
                "insert into graph_generation values (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    generation,
                    project_id,
                    source_revision,
                    tree_digest,
                    source_manifest_digest,
                    extractor_profile_digest,
                    len(files),
                    len(symbols),
                    len(edges),
                    total_error_count,
                    "building",
                    created_at,
                ),
            )
            previous = self._connection.execute(
                "select generation_digest from current_graph_generation where project_id=?",
                (project_id,),
            ).fetchone()
            file_rowids: dict[str, int] = {}
            for file in files:
                cursor = self._connection.execute(
                    "insert into graph_file(generation_digest,relative_path,content_digest,"
                    "parse_state,error_count,symbol_count) values (?,?,?,?,?,?)",
                    (
                        generation,
                        file.relative_path,
                        file.content_digest,
                        file.parse_state,
                        file.error_count,
                        sum(1 for s in symbols if s.file_relative_path == file.relative_path),
                    ),
                )
                if cursor.lastrowid is None:
                    raise PolicyViolation("Graph file rowid uretilmedi")
                file_rowids[file.relative_path] = int(cursor.lastrowid)
            file_symbols: dict[int, list[str]] = {}
            for symbol in symbols:
                file_rowid = file_rowids[symbol.file_relative_path]
                file_symbols.setdefault(file_rowid, []).append(symbol.qualified_name)
                cursor = self._connection.execute(
                    "insert into graph_symbol(generation_digest,symbol_id,file_rowid,"
                    "qualified_name,kind,parent_symbol_id,body_digest,confidence,"
                    "start_line,end_line) values (?,?,?,?,?,?,?,?,?,?)",
                    (
                        generation,
                        symbol.symbol_id,
                        file_rowid,
                        symbol.qualified_name,
                        symbol.kind.value,
                        symbol.parent_symbol_id,
                        symbol.body_digest,
                        symbol.confidence.value,
                        symbol.start_line,
                        symbol.end_line,
                    ),
                )
                if cursor.lastrowid is None:
                    raise PolicyViolation("Graph symbol rowid uretilmedi")
            for edge in edges:
                self._connection.execute(
                    "insert into graph_edge(generation_digest,edge_id,source_symbol_id,"
                    "relation,target_symbol_id,target_qualified_name,confidence,provenance)"
                    " values (?,?,?,?,?,?,?,?)",
                    (
                        generation,
                        edge.edge_id,
                        edge.source_symbol_id,
                        edge.relation.value,
                        edge.target_symbol_id,
                        edge.target_qualified_name,
                        edge.confidence.value,
                        edge.provenance,
                    ),
                )
            for rowid, names in file_symbols.items():
                relative = next(
                    file.relative_path for file in files if file_rowids[file.relative_path] == rowid
                )
                self._connection.execute(
                    "insert into graph_file_fts(file_rowid,generation_digest,project_id,"
                    "relative_path,symbol_names) values (?,?,?,?,?)",
                    (rowid, generation, project_id, relative, " ".join(names)),
                )
            counts = self._connection.execute(
                "select (select count(*) from graph_file where generation_digest=?),"
                " (select count(*) from graph_symbol where generation_digest=?),"
                " (select count(*) from graph_edge where generation_digest=?),"
                " (select count(*) from graph_file_fts where generation_digest=?)",
                (generation, generation, generation, generation),
            ).fetchone()
            if counts is None or tuple(int(value) for value in counts) != (
                len(files),
                len(symbols),
                len(edges),
                len(files),
            ):
                raise PolicyViolation("Graph generation partial build")
            if previous is not None:
                self._connection.execute(
                    "update graph_generation set state='superseded' where generation_digest=?",
                    (str(previous[0]),),
                )
            self._connection.execute(
                "update graph_generation set state='ready' where generation_digest=?",
                (generation,),
            )
            self._connection.execute(
                "insert into current_graph_generation(project_id,generation_digest) values (?,?)"
                " on conflict(project_id) do update set"
                " generation_digest=excluded.generation_digest",
                (project_id, generation),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        return self.generation(project_id)

    @staticmethod
    def _generation_from_row(row: sqlite3.Row) -> GraphGeneration:
        return GraphGeneration(
            generation_digest=str(row["generation_digest"]),
            project_id=str(row["project_id"]),
            source_revision=str(row["source_revision"]),
            tree_digest=str(row["tree_digest"]),
            source_manifest_digest=str(row["source_manifest_digest"]),
            extractor_profile_digest=str(row["extractor_profile_digest"]),
            file_count=int(row["file_count"]),
            symbol_count=int(row["symbol_count"]),
            edge_count=int(row["edge_count"]),
            error_count=int(row["error_count"]),
            state=str(row["state"]),
            created_at=str(row["created_at"]),
        )

    @_stable_read
    def generation(self, project_id: str) -> GraphGeneration:
        row = self._connection.execute(
            "select g.* from current_graph_generation c join graph_generation g"
            " on g.generation_digest=c.generation_digest where c.project_id=?"
            " and g.state='ready'",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ValidationFailed("Project current graph generation bulunamadi")
        return self._generation_from_row(row)

    @_stable_read
    def current_generation(self, project_id: str) -> GraphGeneration:
        """Read-only alias matching the ranking ``GraphReadPort`` contract."""
        return self.generation(project_id)

    @_stable_read
    def _current_generation_digest(self) -> str | None:
        """Resolve the sole ready generation of this per-project store, if any."""
        row = self._connection.execute(
            "select c.generation_digest from current_graph_generation c"
            " join graph_generation g on g.generation_digest=c.generation_digest"
            " where g.state='ready' order by c.project_id"
        ).fetchone()
        return str(row[0]) if row is not None else None

    @staticmethod
    def _edge_from_row(row: sqlite3.Row) -> GraphEdge:
        return GraphEdge(
            edge_id=str(row["edge_id"]),
            source_symbol_id=str(row["source_symbol_id"]),
            relation=row["relation"],
            target_symbol_id=(
                str(row["target_symbol_id"]) if row["target_symbol_id"] is not None else None
            ),
            target_qualified_name=str(row["target_qualified_name"]),
            confidence=row["confidence"],
            provenance=str(row["provenance"]),
        )

    @_stable_read
    def neighbors(self, symbol_id: str) -> tuple[GraphEdge, ...]:
        """Read-only dependency neighbours of a symbol (outgoing and incoming).

        ``contains`` is excluded via the domain ``DEPENDENCY_RELATIONS``
        allow-list; structural hierarchy edges never participate in ranking.
        """
        parse_digest(symbol_id)
        generation_digest = self._current_generation_digest()
        if generation_digest is None:
            return ()
        relation_values = [relation.value for relation in sorted(DEPENDENCY_RELATIONS, key=str)]
        placeholders = ",".join("?" for _ in relation_values)
        rows = self._connection.execute(
            "select * from graph_edge where generation_digest=?"
            " and (source_symbol_id=? or target_symbol_id=?)"
            f" and relation in ({placeholders})",
            (generation_digest, symbol_id, symbol_id, *relation_values),
        ).fetchall()
        return tuple(self._edge_from_row(row) for row in rows)

    @_stable_read
    def file_for_symbol(self, symbol_id: str) -> str:
        """Read-only relative-path resolution for a symbol in the current generation."""
        generation_digest = self._current_generation_digest()
        if generation_digest is None:
            raise ValidationFailed("Project current graph generation bulunamadi")
        row = self._connection.execute(
            "select f.relative_path from graph_symbol s join graph_file f"
            " on f.rowid=s.file_rowid where s.generation_digest=? and s.symbol_id=?",
            (generation_digest, symbol_id),
        ).fetchone()
        if row is None:
            raise ValidationFailed("Graph symbol bulunamadi")
        return str(row[0])

    @_stable_read
    def symbols_for_file(self, relative_path: str) -> tuple[str, ...]:
        """Read-only symbol-id resolution for a file in the current generation."""
        generation_digest = self._current_generation_digest()
        if generation_digest is None:
            return ()
        rows = self._connection.execute(
            "select s.symbol_id from graph_symbol s join graph_file f on f.rowid=s.file_rowid"
            " where s.generation_digest=? and f.relative_path=? order by s.symbol_id",
            (generation_digest, relative_path),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    @_stable_read
    def symbols(self) -> tuple[GraphSymbol, ...]:
        """Read-only snapshot of every symbol in the current generation.

        Query services (find/outline/impact/map) need symbol-level metadata
        (qualified name, kind, hierarchy, locator) beyond the ranking port's
        minimal surface.  This is still a bounded, immutable read on the sole
        ready generation.
        """
        generation_digest = self._current_generation_digest()
        if generation_digest is None:
            return ()
        rows = self._connection.execute(
            "select s.symbol_id,s.qualified_name,s.kind,s.parent_symbol_id,"
            "s.body_digest,s.confidence,s.start_line,s.end_line,f.relative_path"
            " from graph_symbol s join graph_file f on f.rowid=s.file_rowid"
            " where s.generation_digest=? order by s.symbol_id",
            (generation_digest,),
        ).fetchall()
        return tuple(
            GraphSymbol(
                symbol_id=str(row["symbol_id"]),
                qualified_name=str(row["qualified_name"]),
                kind=row["kind"],
                file_relative_path=str(row["relative_path"]),
                parent_symbol_id=(
                    str(row["parent_symbol_id"]) if row["parent_symbol_id"] is not None else None
                ),
                body_digest=str(row["body_digest"]),
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                confidence=row["confidence"],
            )
            for row in rows
        )

    @_stable_read
    def files(self) -> tuple[GraphFile, ...]:
        """Read-only snapshot of every file in the current generation."""
        generation_digest = self._current_generation_digest()
        if generation_digest is None:
            return ()
        profile = self._connection.execute(
            "select extractor_profile_digest from graph_generation where generation_digest=?",
            (generation_digest,),
        ).fetchone()
        profile_digest = str(profile[0]) if profile is not None else ""
        rows = self._connection.execute(
            "select relative_path,content_digest,parse_state,error_count from graph_file"
            " where generation_digest=? order by relative_path",
            (generation_digest,),
        ).fetchall()
        return tuple(
            GraphFile(
                relative_path=str(row["relative_path"]),
                content_digest=str(row["content_digest"]),
                parse_state=str(row["parse_state"]),
                error_count=int(row["error_count"]),
                extractor_profile_digest=profile_digest,
            )
            for row in rows
        )

    @_stable_read
    def status(self, project_id: str) -> dict[str, object]:
        """Bounded status evidence without a deep content audit."""
        row = self._connection.execute(
            "select g.generation_digest,g.state,g.error_count,g.file_count,g.symbol_count,"
            "g.edge_count from current_graph_generation c join graph_generation g"
            " on g.generation_digest=c.generation_digest where c.project_id=?"
            " and g.state='ready'",
            (project_id,),
        ).fetchone()
        if row is None:
            return {
                "project_id": project_id,
                "state": "unavailable",
                "current_generation_present": False,
            }
        return {
            "project_id": project_id,
            "state": "ready",
            "current_generation_present": True,
            "generation_digest": str(row["generation_digest"]),
            "error_count": int(row["error_count"]),
            "file_count": int(row["file_count"]),
            "symbol_count": int(row["symbol_count"]),
            "edge_count": int(row["edge_count"]),
        }

    @_stable_read
    def integrity(self) -> dict[str, object]:
        quick = self._connection.execute("pragma quick_check").fetchone()
        fk = self._connection.execute("pragma foreign_key_check").fetchall()
        projects = self._connection.execute(
            "select c.project_id,g.file_count,g.symbol_count,g.edge_count,"
            " (select count(*) from graph_file x where x.generation_digest=g.generation_digest),"
            " (select count(*) from graph_symbol s where s.generation_digest=g.generation_digest),"
            " (select count(*) from graph_edge e where e.generation_digest=g.generation_digest)"
            " from current_graph_generation c join graph_generation g"
            " on g.generation_digest=c.generation_digest order by c.project_id"
        ).fetchall()
        consistent = all(
            int(row[1]) == int(row[4])
            and int(row[2]) == int(row[5])
            and int(row[3]) == int(row[6])
            for row in projects
        )
        return {
            "quick_check": str(quick[0]) if quick else "missing",
            "foreign_key_check": "passed" if not fk else "failed",
            "project_count": len(projects),
            "generation_counts_consistent": consistent,
            "status": (
                "passed"
                if quick
                and quick[0] == "ok"
                and not fk
                and consistent
                else "failed"
            ),
        }
