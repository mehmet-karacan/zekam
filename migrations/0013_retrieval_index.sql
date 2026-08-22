-- Chunk, embedding profili, dense vector ve lexical/identifier indeksleri.

create table knowledge.chunk_profile (
    id uuid primary key,
    realm_id uuid not null,
    name text not null,
    max_tokens integer not null,
    overlap_tokens integer not null,
    keep_tables_whole boolean not null,
    keep_code_whole boolean not null,
    profile_digest text not null,
    created_at timestamptz not null,
    constraint chunk_profile_unique unique (realm_id, profile_digest),
    constraint chunk_profile_realm_scoped_key unique (realm_id, id),
    constraint chunk_profile_tokens check (max_tokens between 1 and 2048),
    constraint chunk_profile_overlap check (overlap_tokens >= 0 and overlap_tokens < max_tokens)
);

create table knowledge.embedding_profile (
    id uuid primary key,
    realm_id uuid not null,
    model_ref text not null,
    dimension integer not null,
    distance text not null,
    query_prefix text not null default '',
    passage_prefix text not null default '',
    profile_digest text not null,
    created_at timestamptz not null,
    constraint embedding_profile_unique unique (realm_id, profile_digest),
    constraint embedding_profile_realm_scoped_key unique (realm_id, id),
    constraint embedding_profile_distance check (distance in ('cosine', 'l2', 'ip')),
    -- Ilk kanonik profil BGE-M3 dense 1024'tur; baska boyut ayri profil olur.
    constraint embedding_profile_dimension check (dimension between 1 and 4096)
);

create table knowledge.chunk (
    id uuid primary key,
    realm_id uuid not null,
    document_id uuid not null,
    chunk_ref text not null,
    parent_id uuid,
    kind text not null,
    chunk_order integer not null,
    body text not null,
    locator jsonb not null,
    token_count integer not null,
    content_digest text not null,
    chunk_digest text not null,
    profile_digest text not null,
    -- Lexical arama icin turetilen sutun; Turkce/Ingilizce karisik icerik icin
    -- 'simple' sozlugu kullanilir, boylece kok bulma teknik kimligi bozmaz.
    search_vector tsvector generated always as (to_tsvector('simple', body)) stored,
    constraint chunk_document_same_realm
        foreign key (realm_id, document_id) references knowledge.normalized_document (realm_id, id)
        on delete cascade,
    constraint chunk_unique unique (realm_id, document_id, chunk_order),
    constraint chunk_ref_unique unique (realm_id, chunk_ref),
    constraint chunk_realm_scoped_key unique (realm_id, id),
    constraint chunk_parent_not_self check (parent_id is null or parent_id <> id),
    constraint chunk_tokens_positive check (token_count > 0),
    constraint chunk_locator_object check (jsonb_typeof(locator) = 'object'),
    constraint chunk_locator_not_empty check (
        locator->>'page' is not null
        or locator->>'block_index' is not null
        or locator->>'line_start' is not null
        or locator->>'symbol' is not null
        or locator->>'object_name' is not null
        or jsonb_array_length(coalesce(locator->'heading_path', '[]'::jsonb)) > 0
    )
);

create table knowledge.chunk_embedding (
    id uuid primary key,
    realm_id uuid not null,
    chunk_id uuid not null,
    profile_id uuid not null,
    profile_digest text not null,
    embedding vector(1024) not null,
    created_at timestamptz not null,
    constraint embedding_chunk_same_realm
        foreign key (realm_id, chunk_id) references knowledge.chunk (realm_id, id)
        on delete cascade,
    constraint embedding_profile_same_realm
        foreign key (realm_id, profile_id) references knowledge.embedding_profile (realm_id, id)
        on delete restrict,
    -- Bir chunk ayni profilde yalniz bir vektor tasir; profil degisirse yeni satir.
    constraint embedding_unique unique (realm_id, chunk_id, profile_id)
);

-- Profil digest'i vektor satiriyla tutarli olmalidir: sessiz profil karismasi
-- retrieval'i bozar ve fark edilmesi zordur.
create function knowledge.enforce_embedding_profile() returns trigger
-- `public` gerekli: vector tipi ve vector_dims() eklenti semasinda yasar.
language plpgsql security invoker set search_path = pg_catalog, knowledge, core, public as $$
declare
    expected text;
    expected_dimension integer;
begin
    select profile_digest, dimension into expected, expected_dimension
    from knowledge.embedding_profile
    where realm_id = new.realm_id and id = new.profile_id;
    if expected is distinct from new.profile_digest then
        raise exception 'embedding profil digest uyusmuyor' using errcode = '23514';
    end if;
    if expected_dimension is distinct from vector_dims(new.embedding) then
        raise exception 'embedding boyutu profille uyusmuyor' using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger embedding_profile_guard
    before insert on knowledge.chunk_embedding
    for each row execute function knowledge.enforce_embedding_profile();

do $$
declare target text;
begin
    foreach target in array array[
        'knowledge.chunk_profile', 'knowledge.embedding_profile',
        'knowledge.chunk', 'knowledge.chunk_embedding'
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
        execute format(
            'create trigger deny_update before update on %s '
            'for each statement execute function core.deny_mutation()', target
        );
        execute format(
            'create trigger deny_delete before delete on %s '
            'for each statement execute function core.deny_mutation()', target
        );
    end loop;
end
$$;

-- Lexical: full text arama.
create index chunk_search_idx on knowledge.chunk using gin (search_vector);
-- Identifier: teknik kimlik ve kismi eslesme icin trigram.
create index chunk_trigram_idx on knowledge.chunk using gin (body gin_trgm_ops);
-- Filtreli dense arama icin realm/profil oncelikli erisim.
create index chunk_document_order_idx on knowledge.chunk (realm_id, document_id, chunk_order);
create index chunk_parent_idx on knowledge.chunk (realm_id, parent_id);
create index embedding_profile_lookup_idx
    on knowledge.chunk_embedding (realm_id, profile_id);

-- HNSW cosine; filtreli recall olcumu evaluation ile yapilir.
create index chunk_embedding_hnsw_idx
    on knowledge.chunk_embedding using hnsw (embedding vector_cosine_ops);

grant select, insert on knowledge.chunk_profile, knowledge.embedding_profile,
    knowledge.chunk, knowledge.chunk_embedding to zekam_app;
grant execute on function knowledge.enforce_embedding_profile() to zekam_app;
