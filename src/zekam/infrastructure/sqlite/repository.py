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

SCHEMA_VERSION = 2

_SCHEMA_V1 = """
create table if not exists schema_migration (
    version integer primary key check (version > 0),
    name text not null unique,
    checksum text not null,
    applied_at text not null
) strict;
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

_SCHEMA_V2 = """
alter table project add column status text not null default 'active'
    check (status in ('active', 'archived'));
alter table project add column revision integer not null default 1 check (revision > 0);
alter table work_item rename to work_item_v1;
drop index if exists work_item_project_idx;
create table work_item (
    id text primary key,
    project_id text not null references project(id),
    kind text not null,
    title text not null,
    state text not null check (
        state in ('proposed', 'ready', 'active', 'blocked', 'verification',
                  'completed', 'cancelled', 'archived')
    ),
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
insert into work_item(
    id, project_id, kind, title, state, revision, evidence_digest, created_at
)
select id, project_id, kind, title, state, revision, evidence_digest, created_at
from work_item_v1;
drop table work_item_v1;
create index work_item_project_idx on work_item(project_id, state);
create table if not exists system_meta (
    key text primary key,
    value text not null,
    value_digest text not null check (
        length(value_digest) = 71 and substr(value_digest, 1, 7) = 'sha256:'
        and substr(value_digest, 8) not glob '*[^0-9a-f]*'
    ),
    updated_at text not null
) strict;
create table if not exists schema_revision (
    version integer primary key check (version > 0),
    name text not null unique,
    checksum text not null check (
        length(checksum) = 71 and substr(checksum, 1, 7) = 'sha256:'
        and substr(checksum, 8) not glob '*[^0-9a-f]*'
    ),
    applied_at text not null
) strict;
create table if not exists config_revision (
    id text primary key,
    config_digest text not null unique check (
        length(config_digest) = 71 and substr(config_digest, 1, 7) = 'sha256:'
        and substr(config_digest, 8) not glob '*[^0-9a-f]*'
    ),
    task_digest text not null check (
        length(task_digest) = 71 and substr(task_digest, 1, 7) = 'sha256:'
        and substr(task_digest, 8) not glob '*[^0-9a-f]*'
    ),
    sanitized_json text not null,
    active integer not null check (active in (0, 1)),
    activated_at text not null
) strict;
create unique index if not exists config_revision_one_active_idx
    on config_revision(active) where active = 1;
create table if not exists artifact_ref (
    digest text primary key check (
        length(digest) = 71 and substr(digest, 1, 7) = 'sha256:'
        and substr(digest, 8) not glob '*[^0-9a-f]*'
    ),
    media_type text not null,
    size_bytes integer not null check (size_bytes >= 0),
    classification text not null check (
        classification in
        ('public', 'internal', 'confidential-corporate', 'restricted', 'secret', 'local-private')
    ),
    created_at text not null
) strict;
create table if not exists bootstrap_receipt (
    receipt_digest text primary key check (
        length(receipt_digest) = 71 and substr(receipt_digest, 1, 7) = 'sha256:'
        and substr(receipt_digest, 8) not glob '*[^0-9a-f]*'
    ),
    plan_digest text not null check (
        length(plan_digest) = 71 and substr(plan_digest, 1, 7) = 'sha256:'
        and substr(plan_digest, 8) not glob '*[^0-9a-f]*'
    ),
    task_digest text not null check (
        length(task_digest) = 71 and substr(task_digest, 1, 7) = 'sha256:'
        and substr(task_digest, 8) not glob '*[^0-9a-f]*'
    ),
    status text not null check (status in ('completed', 'failed')),
    created_at text not null
) strict;
create table if not exists project_alias (
    alias text primary key,
    project_id text not null references project(id),
    created_at text not null
) strict;
create table if not exists source_binding (
    id text primary key,
    project_id text not null references project(id),
    portable_ref text not null,
    source_kind text not null check (source_kind in ('git', 'directory', 'artifact')),
    active integer not null check (active in (0, 1)),
    created_at text not null,
    unique(project_id, portable_ref)
) strict;
create table if not exists source_snapshot (
    id text primary key,
    source_binding_id text not null references source_binding(id),
    revision_ref text not null,
    tree_digest text not null check (
        length(tree_digest) = 71 and substr(tree_digest, 1, 7) = 'sha256:'
        and substr(tree_digest, 8) not glob '*[^0-9a-f]*'
    ),
    content_digest text not null check (
        length(content_digest) = 71 and substr(content_digest, 1, 7) = 'sha256:'
        and substr(content_digest, 8) not glob '*[^0-9a-f]*'
    ),
    config_digest text not null check (
        length(config_digest) = 71 and substr(config_digest, 1, 7) = 'sha256:'
        and substr(config_digest, 8) not glob '*[^0-9a-f]*'
    ),
    captured_at text not null,
    unique(source_binding_id, revision_ref, tree_digest, content_digest, config_digest)
) strict;
create table if not exists project_capability_profile (
    id text primary key,
    project_id text not null references project(id),
    source_snapshot_id text not null references source_snapshot(id),
    profile_digest text not null check (
        length(profile_digest) = 71 and substr(profile_digest, 1, 7) = 'sha256:'
        and substr(profile_digest, 8) not glob '*[^0-9a-f]*'
    ),
    profile_json text not null,
    captured_at text not null,
    unique(project_id, source_snapshot_id, profile_digest)
) strict;
create table if not exists work_revision (
    id text primary key,
    work_item_id text not null references work_item(id),
    revision integer not null check (revision > 0),
    state text not null check (
        state in ('proposed', 'ready', 'active', 'blocked', 'verification',
                  'completed', 'cancelled', 'archived')
    ),
    payload_digest text not null check (
        length(payload_digest) = 71 and substr(payload_digest, 1, 7) = 'sha256:'
        and substr(payload_digest, 8) not glob '*[^0-9a-f]*'
    ),
    evidence_digest text check (
        evidence_digest is null or (
            length(evidence_digest) = 71 and substr(evidence_digest, 1, 7) = 'sha256:'
            and substr(evidence_digest, 8) not glob '*[^0-9a-f]*'
        )
    ),
    created_at text not null,
    unique(work_item_id, revision),
    check (state <> 'completed' or evidence_digest is not null)
) strict;
create table if not exists work_event (
    id text primary key,
    work_item_id text not null references work_item(id),
    revision integer not null,
    event_kind text not null,
    from_state text,
    to_state text not null,
    event_digest text not null unique check (
        length(event_digest) = 71 and substr(event_digest, 1, 7) = 'sha256:'
        and substr(event_digest, 8) not glob '*[^0-9a-f]*'
    ),
    created_at text not null,
    foreign key(work_item_id, revision) references work_revision(work_item_id, revision)
) strict;
create table if not exists run (
    id text primary key,
    work_item_id text not null references work_item(id),
    source_snapshot_id text references source_snapshot(id),
    config_revision_id text not null references config_revision(id),
    status text not null check (
        status in ('planned', 'running', 'succeeded', 'failed', 'cancelled', 'unknown')
    ),
    budget_json text not null,
    plan_digest text not null check (
        length(plan_digest) = 71 and substr(plan_digest, 1, 7) = 'sha256:'
        and substr(plan_digest, 8) not glob '*[^0-9a-f]*'
    ),
    terminal_receipt_digest text check (
        terminal_receipt_digest is null or (
            length(terminal_receipt_digest) = 71
            and substr(terminal_receipt_digest, 1, 7) = 'sha256:'
            and substr(terminal_receipt_digest, 8) not glob '*[^0-9a-f]*'
        )
    ),
    created_at text not null,
    updated_at text not null,
    check (
        status not in ('succeeded', 'failed', 'cancelled')
        or terminal_receipt_digest is not null
    )
) strict;
create table if not exists run_step (
    id text primary key,
    run_id text not null references run(id),
    step_key text not null,
    status text not null check (
        status in ('pending', 'ready', 'running', 'succeeded', 'failed', 'cancelled', 'unknown')
    ),
    input_digest text not null check (
        length(input_digest) = 71 and substr(input_digest, 1, 7) = 'sha256:'
        and substr(input_digest, 8) not glob '*[^0-9a-f]*'
    ),
    evidence_digest text check (
        evidence_digest is null or (
            length(evidence_digest) = 71 and substr(evidence_digest, 1, 7) = 'sha256:'
            and substr(evidence_digest, 8) not glob '*[^0-9a-f]*'
        )
    ),
    created_at text not null,
    updated_at text not null,
    unique(run_id, step_key),
    check (status <> 'succeeded' or evidence_digest is not null)
) strict;
create table if not exists run_step_dependency (
    run_step_id text not null references run_step(id),
    depends_on_step_id text not null references run_step(id),
    primary key(run_step_id, depends_on_step_id),
    check (run_step_id <> depends_on_step_id)
) strict, without rowid;
create table if not exists checkpoint (
    id text primary key,
    run_id text not null references run(id),
    sequence integer not null check (sequence > 0),
    source_snapshot_id text references source_snapshot(id),
    checkpoint_digest text not null unique check (
        length(checkpoint_digest) = 71 and substr(checkpoint_digest, 1, 7) = 'sha256:'
        and substr(checkpoint_digest, 8) not glob '*[^0-9a-f]*'
    ),
    payload_json text not null,
    created_at text not null,
    unique(run_id, sequence)
) strict;
create table if not exists session (
    id text primary key,
    client_id text not null,
    device_id text not null,
    project_id text references project(id),
    work_item_id text references work_item(id),
    status text not null check (status in ('open', 'closing', 'closed', 'abandoned')),
    opened_at text not null,
    closed_at text,
    close_receipt_digest text check (
        close_receipt_digest is null or (
            length(close_receipt_digest) = 71
            and substr(close_receipt_digest, 1, 7) = 'sha256:'
            and substr(close_receipt_digest, 8) not glob '*[^0-9a-f]*'
        )
    ),
    check (status <> 'closed' or close_receipt_digest is not null)
) strict;
create table if not exists session_event (
    id text primary key,
    session_id text not null references session(id),
    event_kind text not null,
    event_digest text not null unique check (
        length(event_digest) = 71 and substr(event_digest, 1, 7) = 'sha256:'
        and substr(event_digest, 8) not glob '*[^0-9a-f]*'
    ),
    created_at text not null
) strict;
create table if not exists model_identity (
    id text primary key,
    canonical_id text not null unique,
    access_name text not null,
    modality text not null check (
        modality in ('chat', 'code', 'embedding', 'reranker', 'audio', 'vision', 'guardrail')
    ),
    created_at text not null
) strict;
create table if not exists model_revision (
    id text primary key,
    model_identity_id text not null references model_identity(id),
    provider_fingerprint_digest text not null check (
        length(provider_fingerprint_digest) = 71
        and substr(provider_fingerprint_digest, 1, 7) = 'sha256:'
        and substr(provider_fingerprint_digest, 8) not glob '*[^0-9a-f]*'
    ),
    observed_revision text not null,
    observed_at text not null,
    unique(model_identity_id, provider_fingerprint_digest, observed_revision)
) strict;
create table if not exists model_availability (
    id text primary key,
    model_revision_id text not null references model_revision(id),
    device_scope text not null,
    client_scope text not null,
    provider_scope text not null,
    available integer not null check (available in (0, 1)),
    observed_at text not null
) strict;
create table if not exists model_health_observation (
    id text primary key,
    model_revision_id text not null references model_revision(id),
    status text not null check (status in ('passed', 'failed', 'timeout', 'unavailable')),
    evidence_digest text not null check (
        length(evidence_digest) = 71 and substr(evidence_digest, 1, 7) = 'sha256:'
        and substr(evidence_digest, 8) not glob '*[^0-9a-f]*'
    ),
    latency_ms integer check (latency_ms is null or latency_ms >= 0),
    observed_at text not null
) strict;
create index if not exists project_alias_project_idx on project_alias(project_id);
create index if not exists source_binding_project_idx on source_binding(project_id, active);
create index if not exists source_snapshot_binding_idx
    on source_snapshot(source_binding_id, captured_at);
create index if not exists work_revision_item_idx on work_revision(work_item_id, revision);
create index if not exists work_event_item_idx on work_event(work_item_id, revision);
create index if not exists run_work_idx on run(work_item_id, created_at);
create index if not exists run_step_run_idx on run_step(run_id, step_key);
create index if not exists checkpoint_run_idx on checkpoint(run_id, sequence);
create index if not exists session_work_idx on session(work_item_id, opened_at);
create index if not exists model_revision_identity_idx
    on model_revision(model_identity_id, observed_at);
create trigger if not exists work_revision_no_update
before update on work_revision begin
    select raise(abort, 'work_revision append-only');
end;
create trigger if not exists work_revision_no_delete
before delete on work_revision begin
    select raise(abort, 'work_revision append-only');
end;
create trigger if not exists work_event_no_update
before update on work_event begin
    select raise(abort, 'work_event append-only');
end;
create trigger if not exists work_event_no_delete
before delete on work_event begin
    select raise(abort, 'work_event append-only');
end;
create trigger if not exists source_snapshot_no_update
before update on source_snapshot begin
    select raise(abort, 'source_snapshot append-only');
end;
create trigger if not exists source_snapshot_no_delete
before delete on source_snapshot begin
    select raise(abort, 'source_snapshot append-only');
end;
create trigger if not exists checkpoint_no_update
before update on checkpoint begin
    select raise(abort, 'checkpoint append-only');
end;
create trigger if not exists checkpoint_no_delete
before delete on checkpoint begin
    select raise(abort, 'checkpoint append-only');
end;
create trigger if not exists bootstrap_receipt_no_update
before update on bootstrap_receipt begin
    select raise(abort, 'bootstrap_receipt immutable');
end;
create trigger if not exists bootstrap_receipt_no_delete
before delete on bootstrap_receipt begin
    select raise(abort, 'bootstrap_receipt immutable');
end;
create trigger if not exists session_event_no_update
before update on session_event begin
    select raise(abort, 'session_event append-only');
end;
create trigger if not exists session_event_no_delete
before delete on session_event begin
    select raise(abort, 'session_event append-only');
end;
"""

MIGRATION_V1_NAME = "operational-schema-v1"
MIGRATION_V1_DIGEST = "sha256:" + hashlib.sha256(_SCHEMA_V1.encode("utf-8")).hexdigest()
MIGRATION_V2_NAME = "operational-authority-v2"
MIGRATION_V2_DIGEST = "sha256:" + hashlib.sha256(_SCHEMA_V2.encode("utf-8")).hexdigest()
_MIGRATIONS = (
    (1, MIGRATION_V1_NAME, MIGRATION_V1_DIGEST, _SCHEMA_V1),
    (2, MIGRATION_V2_NAME, MIGRATION_V2_DIGEST, _SCHEMA_V2),
)
MIGRATION_LEDGER = tuple((version, name, checksum) for version, name, checksum, _ in _MIGRATIONS)


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


def _expected_schema_fingerprint(*, through_version: int = SCHEMA_VERSION) -> str:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        for version, _, _, script in _MIGRATIONS:
            if version > through_version:
                break
            _execute_script(connection, script)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute complete SQLite statements without ``executescript`` auto-commit."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if not sqlite3.complete_statement(pending):
            continue
        statement = pending.strip()
        pending = ""
        if statement:
            connection.execute(statement)
    if pending.strip():
        raise ConfigurationError("SQLite migration statement tamamlanmamis")


SCHEMA_V1_DIGEST = _expected_schema_fingerprint(through_version=1)
SCHEMA_DIGEST = _expected_schema_fingerprint()


def bootstrap(path: Path) -> SQLiteStatus:
    """Minimum semayi atomik ve idempotent bicimde kurar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as connection:
        try:
            connection.execute("begin immediate")
            for version, name, checksum, script in _MIGRATIONS:
                migration = None
                try:
                    migration = connection.execute(
                        "select name, checksum from schema_migration where version = ?",
                        (version,),
                    ).fetchone()
                except sqlite3.OperationalError:
                    if version != 1:
                        raise
                if migration is not None and (
                    migration["name"] != name or migration["checksum"] != checksum
                ):
                    raise ConfigurationError("SQLite migration ledger drift tespit edildi")
                if migration is None:
                    if version > 1:
                        observed_previous = _schema_fingerprint(connection)
                        expected_previous = _expected_schema_fingerprint(
                            through_version=version - 1
                        )
                        if observed_previous != expected_previous:
                            raise ConfigurationError(
                                "SQLite migration oncesi schema manifest drift tespit edildi"
                            )
                    # Scripts are idempotent. A crash before ledger insertion is
                    # recovered by replaying the same CREATE IF NOT EXISTS set.
                    _execute_script(connection, script)
                    connection.execute(
                        "insert into schema_migration(version, name, checksum, applied_at)"
                        " values (?, ?, ?, ?) on conflict(version) do nothing",
                        (version, name, checksum, _now()),
                    )
                    migration = connection.execute(
                        "select name, checksum from schema_migration where version = ?",
                        (version,),
                    ).fetchone()
                    if migration is None or (
                        migration["name"] != name or migration["checksum"] != checksum
                    ):
                        raise ConfigurationError("SQLite concurrent migration ledger drift")
            connection.execute(
                "insert into zekam_meta(key, value) values ('schema_version', ?) "
                "on conflict(key) do update set value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            for version, name, checksum, _ in _MIGRATIONS:
                connection.execute(
                    "insert into schema_revision(version, name, checksum, applied_at)"
                    " values (?, ?, ?, ?) on conflict(version) do nothing",
                    (version, name, checksum, _now()),
                )
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
            migrations = connection.execute(
                "select version, name, checksum from schema_migration order by version"
            ).fetchall()
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
            digest_row
            and digest_row[0] == SCHEMA_DIGEST
            and observed_digest == SCHEMA_DIGEST
            and [tuple(row) for row in migrations] == list(MIGRATION_LEDGER)
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
