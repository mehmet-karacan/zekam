"""Immutable operational v1/v2 and explicitly admitted transactional v3 migration.

Historical minimum-profile, unknown and drifted databases are never migrated.
Status is read-only; every existing older version requires an explicit upgrade.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zekam.application.operational_store import OperationalSchemaStatus
from zekam.domain.errors import ConfigurationError
from zekam.infrastructure.sqlite.continuity_control_schema import SCHEMA_V3_SQL
from zekam.infrastructure.sqlite.continuity_native_schema import SCHEMA_V4_SQL
from zekam.infrastructure.sqlite.continuity_schema import SCHEMA_V2_SQL
from zekam.infrastructure.sqlite.repository import (
    _SCHEMA_V1 as _LEGACY_TABLE_SCAFFOLD,
)
from zekam.infrastructure.sqlite.repository import (
    _SCHEMA_V2 as _OPERATIONAL_TABLE_EXTENSION,
)

SCHEMA_VERSION = 3
MIGRATION_NAME = "operational-authority-v1"

# The historical scaffold is used only as a schema-construction detail for a
# brand-new empty file.  Knowledge tables from that compatibility profile are
# removed before publication; no legacy database or row is ever migrated.
_SCHEMA = (
    _LEGACY_TABLE_SCAFFOLD
    + _OPERATIONAL_TABLE_EXTENSION
    + """
alter table work_item add column external_number text;
create unique index work_item_project_external_number_idx
    on work_item(project_id, external_number) where external_number is not null;
alter table work_revision add column payload_json text not null default '{}';
drop table knowledge_embedding;
drop table knowledge_chunk;
create table local_job (
    id text primary key,
    idempotency_key text not null unique,
    payload_json text not null,
    state text not null check(state in
        ('ready','running','recovery-required','completed','failed','cancelled','quarantined')),
    attempt_count integer not null default 0 check(attempt_count >= 0),
    max_attempts integer not null check(max_attempts between 1 and 100),
    available_at text not null,
    timeout_at text,
    fencing_counter integer not null default 0 check(fencing_counter >= 0),
    terminal_evidence_digest text,
    created_at text not null,
    updated_at text not null,
    check(
        (state in ('ready','running') and terminal_evidence_digest is null)
        or
        (state in ('recovery-required','completed','failed','cancelled','quarantined')
         and terminal_evidence_digest is not null)
    )
);
create index local_job_claim_idx on local_job(state, available_at, created_at, id);
create table local_runtime_config (
    singleton integer primary key check(singleton=1),
    max_pending_outbox integer not null check(max_pending_outbox between 1 and 100000)
);
create table local_lease (
    id text primary key,
    job_id text not null unique references local_job(id) on delete cascade,
    owner_id text not null,
    owner_pid integer not null check(owner_pid > 0),
    owner_token text not null,
    fencing_token integer not null check(fencing_token > 0),
    heartbeat_at text not null,
    expires_at text not null
);
create table local_resource_lock (
    resource text primary key,
    job_id text not null references local_job(id) on delete cascade,
    lease_id text not null references local_lease(id) on delete cascade,
    fencing_token integer not null,
    acquired_at text not null
);
create table local_effect_claim (
    id text primary key,
    job_id text not null references local_job(id),
    lease_id text not null,
    fencing_token integer not null,
    operation text not null,
    effect_digest text not null,
    idempotency_key text not null unique,
    claimed_at text not null
);
create table local_effect_receipt (
    id text primary key,
    claim_id text not null unique references local_effect_claim(id),
    status text not null check(status in ('completed','failed','unknown')),
    evidence_digest text not null,
    created_at text not null
);
create table local_outbox (
    id text primary key,
    job_id text not null references local_job(id),
    idempotency_key text not null unique,
    event_kind text not null,
    payload_json text not null,
    payload_digest text not null,
    created_at text not null
);
create table local_outbox_delivery (
    outbox_id text primary key references local_outbox(id),
    state text not null check(state in
        ('pending','claimed','delivered','failed','recovery-required')),
    fencing_counter integer not null default 0 check(fencing_counter >= 0),
    claim_id text unique,
    owner_id text,
    owner_pid integer,
    owner_token text,
    expires_at text,
    updated_at text not null,
    check(
        (state='pending' and claim_id is null and owner_id is null and owner_pid is null
         and owner_token is null and expires_at is null)
        or
        (state<>'pending' and claim_id is not null and owner_id is not null and owner_pid>0
         and owner_token is not null)
    )
);
create index local_outbox_delivery_state_idx on local_outbox_delivery(state,updated_at,outbox_id);
create table local_outbox_receipt (
    id text primary key,
    outbox_id text not null unique references local_outbox(id),
    claim_id text not null unique,
    fencing_token integer not null check(fencing_token > 0),
    status text not null check(status in ('delivered','failed','unknown')),
    evidence_digest text not null,
    created_at text not null
);
create table local_recovery_case (
    id text primary key,
    job_id text references local_job(id),
    effect_claim_id text references local_effect_claim(id),
    outbox_id text references local_outbox(id),
    case_kind text not null check(case_kind in ('effect-unknown','outbox-delivery-unknown')),
    evidence_digest text not null,
    state text not null check(state in ('open','resolved')),
    created_at text not null,
    resolved_at text,
    check(
        (state='open' and resolved_at is null)
        or (state='resolved' and resolved_at is not null)
    ),
    check((effect_claim_id is not null) <> (outbox_id is not null)),
    unique(effect_claim_id),
    unique(outbox_id)
);
create table local_recovery_resolution (
    id text primary key,
    recovery_case_id text not null unique references local_recovery_case(id),
    outcome text not null check(outcome in ('completed','failed','delivered')),
    evidence_digest text not null,
    created_at text not null
);
create table local_scheduler_slot (
    slot_key text primary key,
    schedule_digest text not null,
    job_id text not null unique references local_job(id) on delete cascade,
    created_at text not null
);
create table project_knowledge_realm (
    project_id text primary key references project(id),
    realm_id text not null,
    created_at text not null,
    check(length(realm_id)=36 and substr(realm_id,9,1)='-'
          and substr(realm_id,14,1)='-' and substr(realm_id,19,1)='-'
          and substr(realm_id,24,1)='-'
          and replace(realm_id,'-','') not glob '*[^0-9a-f]*')
);
create table knowledge_note (
    id text primary key,
    realm_id text not null,
    project_id text references project(id),
    project_slug text,
    owner_scope text not null,
    portable_ref text not null unique,
    note_kind text not null check(note_kind in
        ('report','research','idea','decision','reference','note','daylog','concept',
         'connection','failure','lesson','skill','handoff')),
    authorship text not null check(authorship in ('user','generated')),
    classification text not null check(classification in
        ('public','internal','confidential-corporate','restricted','secret','local-private')),
    content_digest text not null,
    materialized integer not null check(materialized in (0,1)),
    materialization_evidence_digest text,
    state text not null check(state in ('inbox','active','archived')),
    archived_ref text,
    created_at text not null,
    updated_at text not null,
    unique(realm_id,owner_scope,content_digest),
    check(
        (state in ('inbox','active') and archived_ref is null)
        or (state='archived' and archived_ref is not null)
    ),
    check(
        (materialized=0 and materialization_evidence_digest is null)
        or
        (materialized=1 and length(materialization_evidence_digest)=71
         and substr(materialization_evidence_digest,1,7)='sha256:'
         and lower(substr(materialization_evidence_digest,8)) not glob '*[^0-9a-f]*')
    ),
    check(length(portable_ref)>3 and lower(substr(portable_ref,-3))='.md'
          and instr(portable_ref,'\\')=0 and instr(portable_ref,char(0))=0
          and substr(portable_ref,1,1)<>'/'
          and instr('/'||portable_ref||'/','/../')=0
          and instr('/'||portable_ref||'/','/./')=0),
    check(
        archived_ref is null
        or
        (archived_ref like 'archive/'||replace(owner_scope,':','/')||'/%'
         and instr(archived_ref,'\\')=0
         and instr(archived_ref,char(0))=0
         and instr('/'||archived_ref||'/','/../')=0
         and instr('/'||archived_ref||'/','/./')=0)
    ),
    check(
        owner_scope='global-user'
        or
        (
            substr(owner_scope,1,instr(owner_scope,':')-1)
                in ('project','work','run','session')
            and length(substr(owner_scope,instr(owner_scope,':')+1))=36
            and substr(owner_scope,instr(owner_scope,':')+9,1)='-'
            and substr(owner_scope,instr(owner_scope,':')+14,1)='-'
            and substr(owner_scope,instr(owner_scope,':')+19,1)='-'
            and substr(owner_scope,instr(owner_scope,':')+24,1)='-'
            and replace(substr(owner_scope,instr(owner_scope,':')+1),'-','')
                not glob '*[^0-9a-f]*'
        )
    ),
    check(length(realm_id)=36 and substr(realm_id,9,1)='-'
          and substr(realm_id,14,1)='-' and substr(realm_id,19,1)='-'
          and substr(realm_id,24,1)='-'
          and replace(realm_id,'-','') not glob '*[^0-9a-f]*'),
    check(
        (owner_scope='global-user' and project_id is null and project_slug is null)
        or
        (owner_scope<>'global-user' and project_id is not null and project_slug is not null)
    ),
    check(
        substr(owner_scope,1,8)<>'project:'
        or substr(owner_scope,9)=project_id
    ),
    check(length(content_digest)=71 and substr(content_digest,1,7)='sha256:'
          and lower(substr(content_digest,8)) not glob '*[^0-9a-f]*'),
    check(instr('/'||portable_ref||'/','/'||authorship||'/')>0
          and (
              (authorship='user' and instr('/'||portable_ref||'/','/generated/')=0)
              or
              (authorship='generated' and instr('/'||portable_ref||'/','/user/')=0)
          )),
    check(
        (owner_scope='global-user'
         and (portable_ref like 'global/%'
              or portable_ref like 'inbox/'||authorship||'/global/%'))
        or
        (owner_scope<>'global-user'
         and (portable_ref like 'projeler/'||project_slug||'/%'
              or portable_ref like 'inbox/'||authorship||'/'||project_slug||'/%'))
    )
);
create index knowledge_note_owner_idx on knowledge_note(owner_scope,state,note_kind);
create table knowledge_relation (
    id text primary key,
    from_note_id text not null references knowledge_note(id),
    to_note_id text not null references knowledge_note(id),
    relation_kind text not null,
    source_digest text not null,
    verified integer not null check(verified=1),
    created_at text not null,
    check(from_note_id <> to_note_id),
    check(length(relation_kind)>0 and instr(relation_kind,'\\')=0
          and instr(relation_kind,char(0))=0
          and substr(relation_kind,1,1)<>'/'
          and instr('/'||relation_kind||'/','/../')=0
          and instr('/'||relation_kind||'/','/./')=0),
    check(length(source_digest)=71 and substr(source_digest,1,7)='sha256:'
          and lower(substr(source_digest,8)) not glob '*[^0-9a-f]*'),
    unique(from_note_id,to_note_id,relation_kind)
);
create index knowledge_relation_from_idx on knowledge_relation(from_note_id,verified);
create index knowledge_relation_to_idx on knowledge_relation(to_note_id,verified);
create trigger knowledge_relation_active_notes before insert on knowledge_relation
when not exists(select 1 from knowledge_note where id=new.from_note_id and state<>'archived')
  or not exists(select 1 from knowledge_note where id=new.to_note_id and state<>'archived')
  or not exists(select 1 from knowledge_note where id=new.from_note_id and materialized=1)
  or not exists(select 1 from knowledge_note where id=new.to_note_id and materialized=1)
  or (select realm_id from knowledge_note where id=new.from_note_id)
     <> (select realm_id from knowledge_note where id=new.to_note_id)
begin
    select raise(abort,'knowledge_relation active same-realm notes required');
end;
create trigger artifact_ref_no_update before update on artifact_ref begin
    select raise(abort,'artifact_ref append-only');
end;
create trigger artifact_ref_no_secret before insert on artifact_ref
when new.classification='secret'
begin
    select raise(abort,'secret artifact requires secret backend');
end;
create trigger artifact_ref_guard_insert before insert on artifact_ref
when length(trim(new.media_type))=0 or instr(new.media_type,'/')=0
  or new.media_type<>lower(new.media_type)
  or typeof(new.size_bytes)<>'integer'
  or new.size_bytes<0 or new.size_bytes>67108864
begin
    select raise(abort,'artifact_ref media/size contract');
end;
create trigger artifact_ref_no_delete before delete on artifact_ref begin
    select raise(abort,'artifact_ref append-only');
end;
create trigger project_knowledge_realm_no_update before update on project_knowledge_realm begin
    select raise(abort,'project_knowledge_realm immutable');
end;
create trigger project_knowledge_realm_no_delete before delete on project_knowledge_realm begin
    select raise(abort,'project_knowledge_realm immutable');
end;
create trigger knowledge_note_project_realm_guard before insert on knowledge_note
when new.owner_scope<>'global-user' and not exists(
    select 1 from project p join project_knowledge_realm r on r.project_id=p.id
    where p.id=new.project_id and p.slug=new.project_slug and r.realm_id=new.realm_id
)
begin
    select raise(abort,'knowledge_note exact project/realm binding required');
end;
create trigger knowledge_note_owner_project_guard before insert on knowledge_note
when (substr(new.owner_scope,1,8)='project:' and substr(new.owner_scope,9)<>new.project_id)
  or (substr(new.owner_scope,1,5)='work:' and not exists(
      select 1 from work_item w
      where w.id=substr(new.owner_scope,6) and w.project_id=new.project_id
  ))
  or (substr(new.owner_scope,1,4)='run:' and not exists(
      select 1 from run r join work_item w on w.id=r.work_item_id
      where r.id=substr(new.owner_scope,5) and w.project_id=new.project_id
  ))
  or (substr(new.owner_scope,1,8)='session:' and not exists(
      select 1 from session s left join work_item w on w.id=s.work_item_id
      where s.id=substr(new.owner_scope,9)
        and (s.project_id is not null or w.project_id is not null)
        and (s.project_id is null or s.project_id=new.project_id)
        and (w.project_id is null or w.project_id=new.project_id)
  ))
begin
    select raise(abort,'knowledge_note exact owner/project binding required');
end;
create trigger knowledge_note_exact_authorship_guard before insert on knowledge_note
when instr('/'||new.portable_ref||'/','/'||new.authorship||'/')=0
  or instr(
      substr(
          '/'||new.portable_ref||'/',
          instr('/'||new.portable_ref||'/','/'||new.authorship||'/')
              + length('/'||new.authorship||'/')
      ),
      '/'||new.authorship||'/'
  )>0
begin
    select raise(abort,'knowledge_note exact single authorship segment required');
end;
create trigger knowledge_note_guard_update before update on knowledge_note
when new.id is not old.id
  or new.realm_id is not old.realm_id
  or new.project_id is not old.project_id
  or new.project_slug is not old.project_slug
  or new.owner_scope is not old.owner_scope
  or new.portable_ref is not old.portable_ref
  or new.note_kind is not old.note_kind
  or new.authorship is not old.authorship
  or new.classification is not old.classification
  or new.content_digest is not old.content_digest
  or new.created_at is not old.created_at
  or not (
      (old.materialized=0 and new.materialized=1
       and old.materialization_evidence_digest is null
       and new.materialization_evidence_digest is not null
       and new.state=old.state and new.archived_ref is old.archived_ref)
      or
      (old.materialized=1 and new.materialized=1
       and new.materialization_evidence_digest=old.materialization_evidence_digest
       and old.state in ('inbox','active') and new.state='archived'
       and old.archived_ref is null and new.archived_ref is not null)
  )
begin
    select raise(abort,'knowledge_note immutable except materialize/archive transition');
end;
create trigger knowledge_note_no_secret before insert on knowledge_note
when new.classification='secret'
begin
    select raise(abort,'secret note requires secret backend');
end;
create trigger knowledge_note_no_delete before delete on knowledge_note begin
    select raise(abort,'knowledge_note audit-retained');
end;
create trigger knowledge_relation_no_update before update on knowledge_relation begin
    select raise(abort,'knowledge_relation append-only');
end;
create trigger knowledge_relation_no_delete before delete on knowledge_relation begin
    select raise(abort,'knowledge_relation append-only');
end;
create trigger local_effect_claim_no_update before update on local_effect_claim begin
    select raise(abort,'local_effect_claim append-only');
end;
create trigger local_effect_claim_no_delete before delete on local_effect_claim begin
    select raise(abort,'local_effect_claim append-only');
end;
create trigger local_effect_receipt_no_update before update on local_effect_receipt begin
    select raise(abort,'local_effect_receipt append-only');
end;
create trigger local_effect_receipt_no_delete before delete on local_effect_receipt begin
    select raise(abort,'local_effect_receipt append-only');
end;
create trigger local_outbox_no_update before update on local_outbox begin
    select raise(abort,'local_outbox append-only');
end;
create trigger local_outbox_no_delete before delete on local_outbox begin
    select raise(abort,'local_outbox append-only');
end;
create trigger local_outbox_receipt_no_update before update on local_outbox_receipt begin
    select raise(abort,'local_outbox_receipt append-only');
end;
create trigger local_outbox_receipt_no_delete before delete on local_outbox_receipt begin
    select raise(abort,'local_outbox_receipt append-only');
end;
create trigger local_recovery_case_no_delete before delete on local_recovery_case begin
    select raise(abort,'local_recovery_case audit-retained');
end;
create trigger local_recovery_case_insert_open before insert on local_recovery_case
when new.state <> 'open' or new.resolved_at is not null
begin
    select raise(abort,'local_recovery_case must start open');
end;
create trigger local_recovery_case_guard_update before update on local_recovery_case
when new.id is not old.id
  or new.job_id is not old.job_id
  or new.effect_claim_id is not old.effect_claim_id
  or new.outbox_id is not old.outbox_id
  or new.case_kind is not old.case_kind
  or new.evidence_digest is not old.evidence_digest
  or new.created_at is not old.created_at
  or old.state <> 'open'
  or new.state <> 'resolved'
  or new.resolved_at is null
  or not exists(
      select 1 from local_recovery_resolution r where r.recovery_case_id=old.id
  )
begin
    select raise(abort,'local_recovery_case immutable evidence/state transition');
end;
create trigger local_recovery_resolution_no_update before update on local_recovery_resolution begin
    select raise(abort,'local_recovery_resolution append-only');
end;
create trigger local_recovery_resolution_kind_guard before insert on local_recovery_resolution
when not exists(
    select 1 from local_recovery_case c
    where c.id=new.recovery_case_id and c.state='open'
      and (
          (c.case_kind='effect-unknown' and new.outcome in ('completed','failed'))
          or
          (c.case_kind='outbox-delivery-unknown' and new.outcome in ('delivered','failed'))
      )
)
begin
    select raise(abort,'local_recovery_resolution case/outcome mismatch');
end;
create trigger local_recovery_resolution_no_delete before delete on local_recovery_resolution begin
    select raise(abort,'local_recovery_resolution append-only');
end;
create trigger local_runtime_config_no_update before update on local_runtime_config begin
    select raise(abort,'local_runtime_config immutable');
end;
create trigger local_runtime_config_no_delete before delete on local_runtime_config begin
    select raise(abort,'local_runtime_config immutable');
end;
"""
)
V1_SCHEMA_SQL = _SCHEMA
V1_MIGRATION_NAME = MIGRATION_NAME
V1_MIGRATION_DIGEST = "sha256:d91114ad970241a779d183f9646616b6d5b04d0af8d2e01451473a0c5d6d769e"
V1_SCHEMA_DIGEST = "sha256:67ea597d286df31d5fe14a66003879a733e50a1cad25c7e8a7bcdcadc2839f20"
MIGRATION_DIGEST = "sha256:" + hashlib.sha256(V1_SCHEMA_SQL.encode("utf-8")).hexdigest()
if MIGRATION_DIGEST != V1_MIGRATION_DIGEST:
    raise ConfigurationError("Immutable operational v1 migration SQL drift")
V2_MIGRATION_NAME = "operational-continuity-v2"
V2_MIGRATION_DIGEST = "sha256:a4efb21d80a634c6fe8b42030c19d7ec25de2cc8b6bafeb230cec09744aaafaf"
V2_SCHEMA_DIGEST = "sha256:812d64b984d774154a710b6bead73f065004bccb4a5d633b9aa4c64a42d5914d"
if "sha256:" + hashlib.sha256(SCHEMA_V2_SQL.encode("utf-8")).hexdigest() != V2_MIGRATION_DIGEST:
    raise ConfigurationError("Immutable operational v2 migration SQL drift")
V3_MIGRATION_NAME = "operational-continuity-control-v3"
V3_MIGRATION_DIGEST = "sha256:888535556d91c344720573fb1efb23f4f058b4a509debf557fb052e7a8f439fc"
if "sha256:" + hashlib.sha256(SCHEMA_V3_SQL.encode("utf-8")).hexdigest() != V3_MIGRATION_DIGEST:
    raise ConfigurationError("Immutable operational v3 migration SQL drift")
V4_MIGRATION_NAME = "operational-native-lifecycle-v4"
REJECTED_DRAFT_V4_MIGRATION_DIGEST = (
    "sha256:81b55df4aacadb0f4e097117dbaaa86da160dbcfc69d155c9f07d8a195645397"
)
REJECTED_DRAFT_V4_SCHEMA_DIGEST = (
    "sha256:23ac4658da46add8b2fd860b9eb00ed740043ebc47bf8b33194f0f6fe32180bd"
)
REJECTED_UNREACHABLE_RECOVERY_V4_MIGRATION_DIGEST = (
    "sha256:63030981c2e739de0ef21493c92d662484353ca5a9a6ac8a47542974eaf7d792"
)
REJECTED_UNREACHABLE_RECOVERY_V4_SCHEMA_DIGEST = (
    "sha256:1be35c8ed9bbf79c05077d1aa813a3a137e65eea73136f22ce30391f01979384"
)
REJECTED_PARTIAL_RESTORED_FROZEN_V4_MIGRATION_DIGEST = (
    "sha256:b973cc4fe3ef583e6f56a38bf3c11da6bba5a5afecc0d7627a502a459c1f064e"
)
REJECTED_PARTIAL_RESTORED_FROZEN_V4_SCHEMA_DIGEST = (
    "sha256:3fd79b81da9f0908d4efb02cd043288941990e2acf78611428173feb0756670e"
)
REJECTED_V4_MIGRATION_DIGESTS = (
    REJECTED_DRAFT_V4_MIGRATION_DIGEST,
    REJECTED_UNREACHABLE_RECOVERY_V4_MIGRATION_DIGEST,
    REJECTED_PARTIAL_RESTORED_FROZEN_V4_MIGRATION_DIGEST,
)
REJECTED_V4_SCHEMA_DIGESTS = (
    REJECTED_DRAFT_V4_SCHEMA_DIGEST,
    REJECTED_UNREACHABLE_RECOVERY_V4_SCHEMA_DIGEST,
    REJECTED_PARTIAL_RESTORED_FROZEN_V4_SCHEMA_DIGEST,
)
V4_MIGRATION_DIGEST = "sha256:0d7ee1b6dae8bb0ad043a4f3fabebff3237ab5de18b718df42eecbd0e5b91361"
if "sha256:" + hashlib.sha256(SCHEMA_V4_SQL.encode("utf-8")).hexdigest() != V4_MIGRATION_DIGEST:
    raise ConfigurationError("Immutable operational v4 migration SQL drift")
V1_MIGRATION_LEDGER = ((1, V1_MIGRATION_NAME, V1_MIGRATION_DIGEST),)
V2_MIGRATION_LEDGER = (*V1_MIGRATION_LEDGER, (2, V2_MIGRATION_NAME, V2_MIGRATION_DIGEST))
MIGRATION_LEDGER = (*V2_MIGRATION_LEDGER, (3, V3_MIGRATION_NAME, V3_MIGRATION_DIGEST))
V3_MIGRATION_LEDGER = MIGRATION_LEDGER
V4_MIGRATION_LEDGER = (*V3_MIGRATION_LEDGER, (4, V4_MIGRATION_NAME, V4_MIGRATION_DIGEST))
_MIGRATION_SQL = {
    1: V1_SCHEMA_SQL,
    2: SCHEMA_V2_SQL,
    3: SCHEMA_V3_SQL,
    4: SCHEMA_V4_SQL,
}


class SQLiteOperationalSchema:
    """Application-facing schema adapter."""

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION

    @property
    def schema_digest(self) -> str:
        return SCHEMA_DIGEST

    def bootstrap(self, path: Path) -> OperationalSchemaStatus:
        return bootstrap(path)

    def status(self, path: Path) -> OperationalSchemaStatus:
        return status(path)

    def upgrade(
        self, path: Path, *, target_version: int = SCHEMA_VERSION
    ) -> OperationalSchemaStatus:
        return upgrade(path, target_version=target_version)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute statements without ``executescript``'s implicit commit."""
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
        raise ConfigurationError("Operational SQLite migration statement tamamlanmamis")


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "select type, name, tbl_name, sql from sqlite_master "
        "where name not like 'sqlite_%' order by type, name"
    ).fetchall()
    payload = json.dumps(
        [tuple(row) for row in rows],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _expected_schema_fingerprint(version: int) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        for migration_version in range(1, version + 1):
            _execute_script(connection, _MIGRATION_SQL[migration_version])
        return _schema_fingerprint(connection)
    finally:
        connection.close()


if _expected_schema_fingerprint(1) != V1_SCHEMA_DIGEST:
    raise ConfigurationError("Immutable operational v1 schema fingerprint drift")
if _expected_schema_fingerprint(2) != V2_SCHEMA_DIGEST:
    raise ConfigurationError("Immutable operational v2 schema fingerprint drift")
SCHEMA_DIGEST = "sha256:3a9c5586b334148166e1e9670f07930d775e81f347e87274a2d0846e12be8533"
if _expected_schema_fingerprint(3) != SCHEMA_DIGEST:
    raise ConfigurationError("Immutable operational v3 schema fingerprint drift")
V4_SCHEMA_DIGEST = "sha256:e24c03cfb576f11774ffa1a1b2e7251800e2b68d41217b259d4ae947f19a163f"
if _expected_schema_fingerprint(4) != V4_SCHEMA_DIGEST:
    raise ConfigurationError("Immutable operational v4 schema fingerprint drift")
SCHEMA_DIGESTS = {
    1: V1_SCHEMA_DIGEST,
    2: V2_SCHEMA_DIGEST,
    3: SCHEMA_DIGEST,
    4: V4_SCHEMA_DIGEST,
}


def _schema_table_columns(version: int) -> tuple[tuple[str, tuple[str, ...]], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        for migration_version in range(1, version + 1):
            _execute_script(connection, _MIGRATION_SQL[migration_version])
        return tuple(
            (
                str(row[0]),
                tuple(
                    str(column[1])
                    for column in connection.execute(f'pragma table_info("{row[0]}")')
                ),
            )
            for row in connection.execute(
                "select name from sqlite_master where type='table'"
                " and name not like 'sqlite_%' order by name"
            )
        )
    finally:
        connection.close()


_TABLE_COLUMNS = {version: _schema_table_columns(version) for version in SCHEMA_DIGESTS}
V1_TABLE_NAMES = tuple(table for table, _columns in _TABLE_COLUMNS[1])
V2_TABLE_NAMES = tuple(table for table, _columns in _TABLE_COLUMNS[2])


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigurationError("Operational SQLite path regular file olmali")
    if read_only:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma foreign_keys = on")
    if connection.execute("pragma foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise ConfigurationError("Operational SQLite foreign key enforcement acilamadi")
    connection.execute("pragma busy_timeout = 5000")
    return connection


def _integrity_ok(connection: sqlite3.Connection) -> bool:
    return [str(row[0]) for row in connection.execute("pragma integrity_check")] == [
        "ok"
    ] and connection.execute("pragma foreign_key_check").fetchone() is None


def _validate_connection(connection: sqlite3.Connection) -> int:
    """Validate metadata, both complete ledgers, fingerprint and row integrity."""
    version_row = connection.execute(
        "select value from zekam_meta where key='schema_version'"
    ).fetchone()
    raw_version = None if version_row is None else version_row[0]
    if raw_version not in {str(version) for version in SCHEMA_DIGESTS}:
        raise ConfigurationError("Operational SQLite unsupported schema version")
    version = int(raw_version)
    digest_row = connection.execute(
        "select value from zekam_meta where key='schema_digest'"
    ).fetchone()
    expected_ledger = list(V4_MIGRATION_LEDGER[:version])
    for table, label in (
        ("schema_migration", "migration ledger"),
        ("schema_revision", "schema revision"),
    ):
        rows = connection.execute(
            f"select version,name,checksum from {table} order by version"
        ).fetchall()
        if version == 4 and (
            any(row[0] == 4 and row[2] in REJECTED_V4_MIGRATION_DIGESTS for row in rows)
            or (digest_row is not None and digest_row[0] in REJECTED_V4_SCHEMA_DIGESTS)
        ):
            raise ConfigurationError("Operational SQLite rejected unsafe dormant-v4 schema")
        if [tuple(row) for row in rows] != expected_ledger:
            raise ConfigurationError(f"Operational SQLite {label} drift")
    if _schema_fingerprint(connection) != SCHEMA_DIGESTS[version]:
        raise ConfigurationError("Operational SQLite schema manifest drift")
    if digest_row is None or digest_row[0] != SCHEMA_DIGESTS[version]:
        raise ConfigurationError("Operational SQLite schema digest metadata drift")
    if not _integrity_ok(connection):
        raise ConfigurationError("Operational SQLite integrity/foreign key gate gecmedi")
    return version


def _json_row_value(value: Any) -> Any:
    return {"bytes_hex": value.hex()} if isinstance(value, bytes) else value


def _original_rows_digest(connection: sqlite3.Connection, version: int) -> str:
    """Preserve original columns/rows and historical ledgers, excluding current meta."""
    if type(version) is not int or version not in SCHEMA_DIGESTS:
        raise ConfigurationError("Operational SQLite unsupported source version")
    payload = []
    for table, original_columns in _TABLE_COLUMNS[version]:
        columns = list(original_columns)
        ordering = ",".join(f'"{column}"' for column in columns)
        where = ""
        if table in {"schema_migration", "schema_revision"}:
            where = f" where version<={version}"
        elif table == "zekam_meta":
            where = " where key not in ('schema_version','schema_digest')"
        rows = connection.execute(f'select {ordering} from "{table}"{where} order by {ordering}')
        payload.append((table, columns, [[_json_row_value(item) for item in row] for row in rows]))
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _original_v1_rows_digest(connection: sqlite3.Connection) -> str:
    """Compatibility helper for the immutable v1 row-preservation contract."""
    return _original_rows_digest(connection, 1)


def _assert_quiescent(
    connection: sqlite3.Connection,
    *,
    restoring: bool = False,
    require_terminal_close: bool = False,
) -> None:
    """Admission only observes authority; it never expires owners or invents receipts."""
    checks = (
        ("local_lease", "select 1 from local_lease limit 1"),
        ("local_resource_lock", "select 1 from local_resource_lock limit 1"),
        (
            "active job",
            "select 1 from local_job where state in ('running','recovery-required') limit 1",
        ),
        ("active run", "select 1 from run where status in ('running','unknown') limit 1"),
        ("active run step", "select 1 from run_step where status in ('running','unknown') limit 1"),
        (
            "claimed delivery",
            "select 1 from local_outbox_delivery"
            " where state in ('claimed','recovery-required') limit 1",
        ),
        (
            "unresolved recovery",
            "select 1 from local_recovery_case c left join local_recovery_resolution r"
            " on r.recovery_case_id=c.id where c.state<>'resolved' or r.id is null limit 1",
        ),
        (
            "unresolved effect",
            "select 1 from local_effect_claim c left join local_effect_receipt r on r.claim_id=c.id"
            " where (r.id is null or r.status='unknown') and not exists("
            "select 1 from local_recovery_case rc join local_recovery_resolution rr"
            " on rr.recovery_case_id=rc.id where rc.effect_claim_id=c.id"
            " and rc.state='resolved' and rr.outcome in ('completed','failed')) limit 1",
        ),
        (
            "unresolved delivery",
            "select 1 from local_outbox o left join local_outbox_delivery d on d.outbox_id=o.id"
            " left join local_outbox_receipt r on r.outbox_id=o.id where d.outbox_id is null"
            " or (d.state in ('delivered','failed') and (r.id is null"
            " or (r.status='unknown' and not exists(select 1 from local_recovery_case rc"
            " join local_recovery_resolution rr on rr.recovery_case_id=rc.id"
            " where rc.outbox_id=o.id and rc.state='resolved'"
            " and rr.outcome=d.state)) or (r.status<>'unknown' and r.status<>d.state))) limit 1",
        ),
    )
    for label, statement in checks:
        if connection.execute(statement).fetchone() is not None:
            raise ConfigurationError(f"Operational SQLite recovery-required: quiescent {label}")
    if restoring and (
        connection.execute(
            "select 1 from local_outbox o left join local_outbox_delivery d on d.outbox_id=o.id"
            " left join local_outbox_receipt r on r.outbox_id=o.id"
            " where d.outbox_id is null or d.state not in ('delivered','failed')"
            " or (d.state in ('delivered','failed') and (r.id is null"
            " or (r.status='unknown' and not exists(select 1 from local_recovery_case rc"
            " join local_recovery_resolution rr on rr.recovery_case_id=rc.id"
            " where rc.outbox_id=o.id and rc.state='resolved'"
            " and rr.outcome=d.state)) or (r.status<>'unknown' and r.status<>d.state))) limit 1"
        ).fetchone()
        is not None
    ):
        raise ConfigurationError(
            "Operational SQLite recovery-required: unresolved snapshot delivery"
        )
    if (
        (restoring or require_terminal_close)
        and connection.execute(
            "select 1 from sqlite_master where type='table' and name='continuity_close_request'"
        ).fetchone()
        is not None
        and connection.execute(
            "select 1 from continuity_close_request c left join close_receipt r"
            " on r.request_digest=c.request_digest where r.receipt_digest is null limit 1"
        ).fetchone()
        is not None
    ):
        raise ConfigurationError("Operational SQLite recovery-required: pending snapshot close")


def _apply_migration(connection: sqlite3.Connection, version: int) -> None:
    _execute_script(connection, _MIGRATION_SQL[version])
    _, name, checksum = V4_MIGRATION_LEDGER[version - 1]
    applied_at = _now()
    for table in ("schema_migration", "schema_revision"):
        connection.execute(
            f"insert into {table}(version,name,checksum,applied_at) values(?,?,?,?)",
            (version, name, checksum, applied_at),
        )
    for key, value in (
        ("schema_version", str(version)),
        ("schema_digest", SCHEMA_DIGESTS[version]),
    ):
        connection.execute(
            "insert into zekam_meta(key,value) values(?,?)"
            " on conflict(key) do update set value=excluded.value",
            (key, value),
        )


def _apply_forward_path(
    connection: sqlite3.Connection, source_version: int, target_version: int
) -> None:
    original_digests = {}
    for next_version in range(source_version + 1, target_version + 1):
        prior_version = next_version - 1
        if prior_version:
            original_digests[prior_version] = _original_rows_digest(connection, prior_version)
        _apply_migration(connection, next_version)
        _validate_connection(connection)
        for original_version, original_digest in original_digests.items():
            if _original_rows_digest(connection, original_version) != original_digest:
                raise ConfigurationError(
                    f"Operational SQLite original v{original_version} row parity drift"
                )


def bootstrap(path: Path, *, target_version: int = SCHEMA_VERSION) -> OperationalSchemaStatus:
    """Create ordered migrations atomically; existing versions are never auto-upgraded."""
    _validate_target_version(target_version)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(path)
    try:
        connection.execute("begin immediate")
        objects = connection.execute(
            "select name from sqlite_master where name not like 'sqlite_%'"
        ).fetchall()
        if objects:
            version = _validate_connection(connection)
            if version > target_version:
                raise ConfigurationError("Operational SQLite downgrade forbidden")
            if version != target_version:
                raise ConfigurationError(
                    "Operational SQLite migration-required: explicit upgrade gerekli"
                )
        else:
            _apply_forward_path(connection, 0, target_version)
        connection.commit()
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        raise ConfigurationError("Operational SQLite unknown/corrupt schema rejected") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return status(path)


def bootstrap_v4(path: Path) -> OperationalSchemaStatus:
    """Create a fresh dormant v4 database without changing the v3 default API."""
    if path.exists() or path.is_symlink():
        raise ConfigurationError("Operational SQLite fresh v4 empty destination required")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(path)
    try:
        connection.execute("begin immediate")
        _apply_forward_path(connection, 0, 4)
        connection.commit()
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        raise ConfigurationError("Operational SQLite v4 bootstrap rejected") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return status(path)


def _validate_target_version(version: int) -> None:
    if type(version) is not int or version not in {1, 2, 3}:
        raise ConfigurationError("Operational SQLite unsupported target version")


def upgrade(path: Path, *, target_version: int = SCHEMA_VERSION) -> OperationalSchemaStatus:
    """Serialize admission and the complete ordered forward path in one transaction."""
    _validate_target_version(target_version)
    if target_version == 4:
        raise ConfigurationError("Operational SQLite v4 external migration orchestrator required")
    if not path.is_file():
        raise ConfigurationError("Operational SQLite upgrade mevcut exact supported schema ister")
    connection = _connect(path)
    try:
        connection.execute("begin immediate")
        version = _validate_connection(connection)
        if version > target_version:
            raise ConfigurationError("Operational SQLite downgrade forbidden")
        if version < target_version:
            _assert_quiescent(connection)
            _apply_forward_path(connection, version, target_version)
        connection.commit()
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        raise ConfigurationError("Operational SQLite upgrade schema/transaction rejected") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return status(path)


def status(path: Path) -> OperationalSchemaStatus:
    """Read integrity and exact schema state without mutating the file."""
    if not path.is_file() or path.is_symlink():
        return OperationalSchemaStatus(False, None, False, False)
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(path, read_only=True)
        connection.execute("begin")
        integrity_ok = _integrity_ok(connection)
        version_row = connection.execute(
            "select value from zekam_meta where key = 'schema_version'"
        ).fetchone()
        try:
            version = int(version_row[0]) if version_row is not None else None
        except (TypeError, ValueError):
            version = None
        try:
            schema_ok = _validate_connection(connection) == version
        except (ConfigurationError, sqlite3.DatabaseError):
            schema_ok = False
        return OperationalSchemaStatus(
            True,
            version,
            integrity_ok,
            schema_ok,
        )
    except (OSError, ConfigurationError, sqlite3.DatabaseError):
        return OperationalSchemaStatus(True, None, False, False)
    finally:
        if connection is not None:
            connection.close()
