-- Offline document ingestion: parser profile, source-scoped idempotency and
-- activation only after normalized content and its exact indexing profile exist.

alter table knowledge.normalized_document
    add column parser_profile jsonb not null default '{}'::jsonb;

alter table knowledge.normalized_document
    add constraint normalized_document_parser_profile_object
    check (jsonb_typeof(parser_profile) = 'object');

alter table knowledge.ingestion_job drop constraint job_idempotent;
alter table knowledge.ingestion_job
    add constraint job_idempotent_per_source
    unique (realm_id, source_id, idempotency_key);

create table knowledge.document_index_profile (
    id uuid primary key,
    realm_id uuid not null,
    document_id uuid not null,
    chunk_profile_id uuid not null,
    embedding_profile_id uuid not null,
    lexical_state text not null default 'ready',
    embedding_state text not null default 'pending',
    created_at timestamptz not null,
    constraint document_index_profile_unique unique (realm_id, document_id),
    constraint document_index_document_same_realm
        foreign key (realm_id, document_id)
        references knowledge.normalized_document (realm_id, id) on delete cascade,
    constraint document_index_chunk_profile_same_realm
        foreign key (realm_id, chunk_profile_id)
        references knowledge.chunk_profile (realm_id, id) on delete restrict,
    constraint document_index_embedding_profile_same_realm
        foreign key (realm_id, embedding_profile_id)
        references knowledge.embedding_profile (realm_id, id) on delete restrict,
    constraint document_index_lexical_state check (lexical_state in ('ready', 'failed')),
    constraint document_index_embedding_state
        check (embedding_state in ('pending', 'ready', 'failed'))
);

alter table knowledge.document_index_profile enable row level security;
alter table knowledge.document_index_profile force row level security;
create policy scope_select on knowledge.document_index_profile for select
    using (realm_id = core.current_realm_id());
create policy scope_insert on knowledge.document_index_profile for insert
    with check (realm_id = core.current_realm_id());
create trigger deny_update before update on knowledge.document_index_profile
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on knowledge.document_index_profile
    for each statement execute function core.deny_mutation();
grant select, insert on knowledge.document_index_profile to zekam_app;

create or replace function knowledge.require_completed_ingestion() returns trigger
language plpgsql security invoker set search_path = pg_catalog, knowledge, core as $$
begin
    if new.state = 'active' and (
        not exists (
            select 1 from knowledge.ingestion_job j
            where j.realm_id = new.realm_id
              and j.source_id = new.source_id
              and j.artifact_id = new.artifact_id
              and j.failure is null
              and 'activated' = any (j.completed_stages)
        )
        or not exists (
            select 1 from knowledge.normalized_document d
            where d.realm_id = new.realm_id and d.version_id = new.id
              and d.content_digest = new.content_digest
        )
        or not exists (
            select 1 from knowledge.normalized_document d
            join knowledge.document_index_profile p
              on p.realm_id = d.realm_id and p.document_id = d.id
            join knowledge.chunk_profile cp
              on cp.realm_id = p.realm_id and cp.id = p.chunk_profile_id
            where d.realm_id = new.realm_id and d.version_id = new.id
              and p.lexical_state = 'ready'
              and exists (
                  select 1 from knowledge.chunk c
                  where c.realm_id = d.realm_id
                    and c.document_id = d.id
                    and c.profile_digest = cp.profile_digest
              )
        )
    ) then
        raise exception 'aktif surum tamamlanmis ingestion ve indeks profil zinciri ister'
            using errcode = '23514';
    end if;
    return new;
end
$$;

-- declared_mode transport seklidir; capability kategorisi declared_category'den gelir.
update models.model_inventory
set modality = 'vision_language'
where declared_category = 'multimodal_generation'
  and modality is distinct from 'vision_language';
