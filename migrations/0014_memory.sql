-- Native bellek: aday, kayit, revision, iliski ve vektor.

create table memory.candidate (
    id uuid primary key,
    realm_id uuid not null,
    scope text not null,
    project_id uuid,
    work_item_id uuid,
    memory_class text not null,
    content text not null,
    author_ref text not null,
    evidence jsonb not null,
    occurrence_key text,
    observation_count integer not null default 1,
    reviewed boolean not null default false,
    reviewer_ref text,
    review_reason text,
    created_at timestamptz not null,
    constraint candidate_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint candidate_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint candidate_realm_scoped_key unique (realm_id, id),
    constraint candidate_scope check (
        scope in ('global-user', 'project', 'work-item', 'run', 'agent')
    ),
    constraint candidate_class check (
        memory_class in ('working', 'episodic', 'semantic', 'procedural', 'preference', 'failure')
    ),
    constraint candidate_evidence_array check (jsonb_typeof(evidence) = 'array'),
    constraint candidate_observations check (observation_count > 0),
    -- Failure adayi tekrar sayimi icin occurrence key ister.
    constraint candidate_failure_key check (memory_class <> 'failure' or occurrence_key is not null),
    constraint candidate_review_pairing check (reviewed = false or reviewer_ref is not null)
);

create table memory.record (
    id uuid primary key,
    realm_id uuid not null,
    scope text not null,
    project_id uuid,
    work_item_id uuid,
    memory_class text not null,
    content text not null,
    state text not null,
    revision integer not null,
    evidence jsonb not null,
    entities text[] not null default '{}',
    valid_from timestamptz,
    valid_until timestamptz,
    author_ref text,
    reviewed_by text,
    superseded_by uuid,
    last_used_at timestamptz,
    record_digest text not null,
    grants_authority boolean not null default false,
    created_at timestamptz not null,
    search_vector tsvector generated always as (to_tsvector('simple', content)) stored,
    constraint record_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint record_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint record_unique unique (realm_id, record_digest),
    constraint record_realm_scoped_key unique (realm_id, id),
    constraint record_scope check (scope in ('global-user', 'project', 'work-item', 'run', 'agent')),
    constraint record_class check (
        memory_class in ('working', 'episodic', 'semantic', 'procedural', 'preference', 'failure')
    ),
    constraint record_state check (
        state in ('candidate', 'active', 'superseded', 'revoked', 'archived')
    ),
    constraint record_revision check (revision > 0),
    constraint record_evidence_array check (jsonb_typeof(evidence) = 'array'),
    constraint record_validity check (valid_until is null or valid_from is null
        or valid_until > valid_from),
    constraint record_supersede_pairing check ((state = 'superseded') = (superseded_by is not null)),
    -- Bellek hicbir kosulda authority tasimaz.
    constraint record_no_authority check (grants_authority = false),
    -- Kanitsiz kayit aktif olamaz.
    constraint record_active_needs_evidence check (
        state <> 'active' or jsonb_array_length(evidence) > 0
    ),
    -- Semantic, procedural ve failure siniflari bagimsiz review ister.
    constraint record_active_needs_review check (
        state <> 'active'
        or memory_class not in ('semantic', 'procedural', 'failure')
        or (reviewed_by is not null and reviewed_by is distinct from author_ref)
    ),
    -- Agent ve run kapsami kalici bellek uretemez.
    constraint record_scope_persistent check (state <> 'active' or scope not in ('run', 'agent'))
);

-- Ayni kapsamda ayni icerik iki kez aktif olamaz.
create unique index record_active_content_idx
    on memory.record (realm_id, scope, coalesce(project_id, '00000000-0000-0000-0000-000000000000'::uuid), md5(content))
    where state = 'active';

create table memory.relation (
    id uuid primary key,
    realm_id uuid not null,
    from_id uuid not null,
    to_id uuid not null,
    kind text not null,
    created_at timestamptz not null,
    constraint relation_from_same_realm
        foreign key (realm_id, from_id) references memory.record (realm_id, id) on delete cascade,
    constraint relation_to_same_realm
        foreign key (realm_id, to_id) references memory.record (realm_id, id) on delete cascade,
    constraint relation_unique unique (realm_id, from_id, to_id, kind),
    constraint relation_kind check (kind in ('supersedes', 'contradicts', 'supports', 'derived-from')),
    constraint relation_not_self check (from_id <> to_id)
);

create table memory.embedding (
    id uuid primary key,
    realm_id uuid not null,
    record_id uuid not null,
    profile_digest text not null,
    embedding vector(1024) not null,
    created_at timestamptz not null,
    constraint memory_embedding_same_realm
        foreign key (realm_id, record_id) references memory.record (realm_id, id) on delete cascade,
    constraint memory_embedding_unique unique (realm_id, record_id, profile_digest)
);

do $$
declare target text;
begin
    foreach target in array array[
        'memory.candidate', 'memory.record', 'memory.relation', 'memory.embedding'
    ] loop
        execute format('alter table %s enable row level security', target);
        execute format('alter table %s force row level security', target);
        execute format(
            'create policy scope_select on %s for select using (realm_id = core.current_realm_id())',
            target
        );
        execute format(
            'create policy scope_insert on %s for insert with check (realm_id = core.current_realm_id())',
            target
        );
    end loop;
end
$$;

-- Kayit durumu ilerler (aktif -> superseded/revoked); icerik degismez.
create policy scope_update on memory.record for update
    using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_update on memory.candidate for update
    using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());

create trigger deny_update before update on memory.relation
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on memory.relation
    for each statement execute function core.deny_mutation();
create trigger deny_update before update on memory.embedding
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on memory.embedding
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on memory.record
    for each statement execute function core.deny_mutation();

-- Icerik ve sinif degistirilemez; yalniz durum alanlari ilerler.
create function memory.enforce_immutable_content() returns trigger
language plpgsql security invoker set search_path = pg_catalog, memory, core as $$
begin
    if new.content is distinct from old.content
       or new.memory_class is distinct from old.memory_class
       or new.scope is distinct from old.scope
       or new.record_digest is distinct from old.record_digest then
        raise exception 'bellek icerigi degistirilemez; yeni revision gerekir'
            using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger record_immutable_content_guard
    before update on memory.record
    for each row execute function memory.enforce_immutable_content();

create index memory_record_search_idx on memory.record using gin (search_vector);
create index memory_record_entities_idx on memory.record using gin (entities);
create index memory_record_scope_idx on memory.record (realm_id, scope, state);
create index memory_record_temporal_idx on memory.record (realm_id, valid_from, valid_until);
create index memory_embedding_hnsw_idx
    on memory.embedding using hnsw (embedding vector_cosine_ops);

grant select, insert on memory.candidate, memory.record, memory.relation,
    memory.embedding to zekam_app;
grant update (state, superseded_by, valid_until, last_used_at) on memory.record to zekam_app;
grant update (reviewed, reviewer_ref, review_reason, observation_count) on memory.candidate
    to zekam_app;
grant execute on function memory.enforce_immutable_content() to zekam_app;
