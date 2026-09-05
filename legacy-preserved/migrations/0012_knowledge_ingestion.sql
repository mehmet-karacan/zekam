-- Knowledge Plane: immutable artifact, surumlu ingestion ve normalize icerik.

create table knowledge.artifact (
    id uuid primary key,
    realm_id uuid not null,
    project_id uuid,
    content_digest text not null,
    artifact_digest text not null,
    byte_size bigint not null,
    media_type text not null,
    original_name text not null,
    stored_at timestamptz not null,
    constraint artifact_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint artifact_unique unique (realm_id, artifact_digest),
    constraint artifact_realm_scoped_key unique (realm_id, id),
    constraint artifact_size check (byte_size > 0),
    -- Orijinal ad portable kalir: absolute path ve traversal saklanmaz.
    constraint artifact_name_relative check (
        original_name !~ '^([a-zA-Z]:|/|\\)' and original_name !~ '(^|/)\.\.(/|$)'
    )
);

create table knowledge.source (
    id uuid primary key,
    realm_id uuid not null,
    project_id uuid,
    slug text not null,
    source_format text not null,
    created_at timestamptz not null,
    constraint source_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint source_unique unique (realm_id, slug),
    constraint source_realm_scoped_key unique (realm_id, id),
    constraint source_format_known check (
        source_format in (
            'docx', 'pdf', 'txt', 'markdown', 'png', 'jpeg', 'tiff',
            'archive', 'repository', 'directory', 'oracle-metadata', 'postgres-metadata'
        )
    )
);

create table knowledge.ingestion_job (
    id uuid primary key,
    realm_id uuid not null,
    source_id uuid not null,
    artifact_id uuid not null,
    idempotency_key text not null,
    completed_stages text[] not null default '{}',
    failure text,
    updated_at timestamptz not null,
    constraint job_source_same_realm
        foreign key (realm_id, source_id) references knowledge.source (realm_id, id)
        on delete cascade,
    constraint job_artifact_same_realm
        foreign key (realm_id, artifact_id) references knowledge.artifact (realm_id, id)
        on delete restrict,
    -- Ayni idempotency anahtari ikinci kez is baslatmaz.
    constraint job_idempotent unique (realm_id, idempotency_key),
    constraint job_realm_scoped_key unique (realm_id, id),
    constraint job_stages_known check (
        completed_stages <@ array[
            'validated', 'stored', 'parsed', 'normalized', 'indexed', 'activated'
        ]::text[]
    )
);

-- Asamalar sirali ilerler ve geri alinmaz.
create function knowledge.enforce_stage_order() returns trigger
language plpgsql security invoker set search_path = pg_catalog, knowledge, core as $$
declare
    canonical text[] := array[
        'validated', 'stored', 'parsed', 'normalized', 'indexed', 'activated'
    ];
    expected text[];
begin
    expected := canonical[1:array_length(new.completed_stages, 1)];
    if new.completed_stages is distinct from coalesce(expected, '{}'::text[]) then
        raise exception 'ingestion asamalari sirali olmali' using errcode = '23514';
    end if;
    if tg_op = 'UPDATE'
       and array_length(old.completed_stages, 1) is not null
       and coalesce(array_length(new.completed_stages, 1), 0)
           < array_length(old.completed_stages, 1) then
        raise exception 'ingestion asamasi geri alinamaz' using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger ingestion_stage_order_guard
    before insert or update on knowledge.ingestion_job
    for each row execute function knowledge.enforce_stage_order();

create table knowledge.source_version (
    id uuid primary key,
    realm_id uuid not null,
    source_id uuid not null,
    revision integer not null,
    artifact_id uuid not null,
    artifact_digest text not null,
    content_digest text not null,
    state text not null,
    superseded_by uuid,
    created_at timestamptz not null,
    constraint version_source_same_realm
        foreign key (realm_id, source_id) references knowledge.source (realm_id, id)
        on delete cascade,
    constraint version_artifact_same_realm
        foreign key (realm_id, artifact_id) references knowledge.artifact (realm_id, id)
        on delete restrict,
    constraint version_unique unique (realm_id, source_id, revision),
    constraint version_realm_scoped_key unique (realm_id, id),
    constraint version_revision_positive check (revision > 0),
    constraint version_state check (state in ('pending', 'active', 'superseded', 'failed')),
    constraint version_supersede_pairing check (
        (state = 'superseded') = (superseded_by is not null)
    )
);

-- Bir kaynagin ayni anda yalniz bir aktif surumu olabilir.
create unique index source_single_active_version
    on knowledge.source_version (realm_id, source_id)
    where state = 'active';

-- Aktivasyon yalniz tamamlanmis ingestion ile olur.
create function knowledge.require_completed_ingestion() returns trigger
language plpgsql security invoker set search_path = pg_catalog, knowledge, core as $$
begin
    if new.state = 'active' and not exists (
        select 1 from knowledge.ingestion_job j
        where j.realm_id = new.realm_id
          and j.artifact_id = new.artifact_id
          and j.failure is null
          and 'activated' = any (j.completed_stages)
    ) then
        raise exception 'tamamlanmamis ingestion aktif surum uretemez'
            using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger version_requires_completed_ingestion
    before insert or update of state on knowledge.source_version
    for each row execute function knowledge.require_completed_ingestion();

create table knowledge.normalized_document (
    id uuid primary key,
    realm_id uuid not null,
    version_id uuid not null,
    source_format text not null,
    parser_ref text not null,
    parser_version text not null,
    unit_count integer not null,
    content_digest text not null,
    created_at timestamptz not null,
    constraint document_version_same_realm
        foreign key (realm_id, version_id) references knowledge.source_version (realm_id, id)
        on delete cascade,
    constraint document_unique unique (realm_id, content_digest),
    constraint document_realm_scoped_key unique (realm_id, id),
    constraint document_units_positive check (unit_count > 0)
);

create table knowledge.content_unit (
    id uuid primary key,
    realm_id uuid not null,
    document_id uuid not null,
    unit_ref text not null,
    kind text not null,
    unit_order integer not null,
    body text not null,
    locator jsonb not null,
    confidence double precision,
    unit_digest text not null,
    constraint unit_document_same_realm
        foreign key (realm_id, document_id) references knowledge.normalized_document (realm_id, id)
        on delete cascade,
    constraint unit_unique unique (realm_id, document_id, unit_order),
    constraint unit_order_nonnegative check (unit_order >= 0),
    constraint unit_locator_object check (jsonb_typeof(locator) = 'object'),
    constraint unit_confidence_range check (
        confidence is null or (confidence >= 0 and confidence <= 1)
    ),
    -- OCR birimi confidence olmadan kaydedilemez.
    constraint unit_ocr_confidence check (kind <> 'ocr-block' or confidence is not null),
    constraint unit_kind_known check (
        kind in (
            'heading', 'paragraph', 'list', 'table', 'code', 'formula', 'image',
            'caption', 'ocr-block', 'file-header', 'symbol', 'configuration', 'db-object'
        )
    ),
    -- Locator'siz birim kabul edilmez: alintilanamayan icerik indekslenmez.
    constraint unit_locator_not_empty check (
        locator - array['page', 'bbox', 'heading_path', 'block_index',
                        'line_start', 'line_end', 'symbol', 'object_name']
            <> locator
        and (
            locator->>'page' is not null
            or locator->>'block_index' is not null
            or locator->>'line_start' is not null
            or locator->>'symbol' is not null
            or locator->>'object_name' is not null
            or jsonb_array_length(coalesce(locator->'heading_path', '[]'::jsonb)) > 0
        )
    )
);

do $$
declare target text;
begin
    foreach target in array array[
        'knowledge.artifact', 'knowledge.source', 'knowledge.ingestion_job',
        'knowledge.source_version', 'knowledge.normalized_document', 'knowledge.content_unit'
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

-- Ingestion isi ve surum durumu ilerler; artifact ve icerik degismez.
create policy scope_update on knowledge.ingestion_job for update
    using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_update on knowledge.source_version for update
    using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());

create trigger deny_update before update on knowledge.artifact
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on knowledge.artifact
    for each statement execute function core.deny_mutation();
create trigger deny_update before update on knowledge.normalized_document
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on knowledge.normalized_document
    for each statement execute function core.deny_mutation();
create trigger deny_update before update on knowledge.content_unit
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on knowledge.content_unit
    for each statement execute function core.deny_mutation();

create index artifact_project_idx on knowledge.artifact (realm_id, project_id);
create index job_source_idx on knowledge.ingestion_job (realm_id, source_id);
create index version_source_idx on knowledge.source_version (realm_id, source_id, revision desc);
create index unit_document_idx on knowledge.content_unit (realm_id, document_id, unit_order);

grant select, insert on knowledge.artifact, knowledge.source, knowledge.source_version,
    knowledge.normalized_document, knowledge.content_unit, knowledge.ingestion_job to zekam_app;
grant update (completed_stages, failure, updated_at) on knowledge.ingestion_job to zekam_app;
grant update (state, superseded_by) on knowledge.source_version to zekam_app;
grant execute on function knowledge.enforce_stage_order() to zekam_app;
grant execute on function knowledge.require_completed_ingestion() to zekam_app;
