-- Memory scope ve logical/storage identity ayrimini zorlar.

alter table memory.candidate
    add column logical_candidate_id text,
    add column project_ref text,
    add column work_ref text;

alter table memory.record
    add column logical_memory_id text,
    add column project_ref text,
    add column work_ref text;

update memory.candidate c
set project_ref = p.slug
from projects.project p
where p.realm_id = c.realm_id and p.id = c.project_id and c.project_ref is null;

update memory.record r
set project_ref = p.slug
from projects.project p
where p.realm_id = r.realm_id and p.id = r.project_id and r.project_ref is null;

update memory.candidate c
set work_ref = coalesce(w.external_number, 'work-digest:' || w.record_digest)
from work.work_item w
where w.realm_id = c.realm_id and w.id = c.work_item_id and c.work_ref is null;

update memory.record r
set work_ref = coalesce(w.external_number, 'work-digest:' || w.record_digest)
from work.work_item w
where w.realm_id = r.realm_id and w.id = r.work_item_id and r.work_ref is null;

update memory.candidate
set logical_candidate_id = 'legacy-candidate:' || encode(
    digest(id::text || ':' || created_at::text, 'sha256'), 'hex'
)
where logical_candidate_id is null;

update memory.record
set logical_memory_id = 'legacy-memory:' || substr(record_digest, 8)
where logical_memory_id is null;

do $$
begin
    if exists (
        select 1 from memory.candidate
        where (scope = 'project' and project_id is null)
           or (scope = 'work-item' and (project_id is null or work_item_id is null))
    ) or exists (
        select 1 from memory.record
        where (scope = 'project' and project_id is null)
           or (scope = 'work-item' and (project_id is null or work_item_id is null))
    ) then
        raise exception 'memory scope binding eksik; migration kimlik uydurmaz'
            using errcode = '23514';
    end if;
end
$$;

alter table memory.candidate
    alter column logical_candidate_id set not null,
    add constraint candidate_logical_id_unique unique (realm_id, logical_candidate_id),
    add constraint candidate_work_project_same_realm
        foreign key (realm_id, project_id, work_item_id)
        references work.work_item (realm_id, project_id, id) on delete cascade,
    add constraint candidate_scope_identity check (
        (scope = 'global-user' and project_id is null and work_item_id is null
            and project_ref is null and work_ref is null)
        or (scope = 'project' and project_id is not null and work_item_id is null
            and project_ref is not null and work_ref is null)
        or (scope = 'work-item' and project_id is not null and work_item_id is not null
            and project_ref is not null and work_ref is not null)
        or (scope in ('run', 'agent') and project_id is null and work_item_id is null
            and project_ref is null and work_ref is null)
    );

alter table memory.record
    alter column logical_memory_id set not null,
    add constraint record_logical_id_unique unique (realm_id, logical_memory_id),
    add constraint record_work_project_same_realm
        foreign key (realm_id, project_id, work_item_id)
        references work.work_item (realm_id, project_id, id) on delete cascade,
    add constraint record_scope_identity check (
        (scope = 'global-user' and project_id is null and work_item_id is null
            and project_ref is null and work_ref is null)
        or (scope = 'project' and project_id is not null and work_item_id is null
            and project_ref is not null and work_ref is null)
        or (scope = 'work-item' and project_id is not null and work_item_id is not null
            and project_ref is not null and work_ref is not null)
        or (scope in ('run', 'agent') and project_id is null and work_item_id is null
            and project_ref is null and work_ref is null)
    );

drop index memory.record_active_content_idx;
create unique index record_active_content_idx
    on memory.record (
        realm_id,
        scope,
        coalesce(project_id, '00000000-0000-0000-0000-000000000000'::uuid),
        coalesce(work_item_id, '00000000-0000-0000-0000-000000000000'::uuid),
        md5(content)
    )
    where state = 'active';

create or replace function memory.enforce_immutable_content() returns trigger
language plpgsql security invoker set search_path = pg_catalog, memory, core as $$
begin
    if new.content is distinct from old.content
       or new.memory_class is distinct from old.memory_class
       or new.scope is distinct from old.scope
       or new.project_id is distinct from old.project_id
       or new.work_item_id is distinct from old.work_item_id
       or new.project_ref is distinct from old.project_ref
       or new.work_ref is distinct from old.work_ref
       or new.logical_memory_id is distinct from old.logical_memory_id
       or new.record_digest is distinct from old.record_digest then
        raise exception 'bellek icerigi veya kimligi degistirilemez; yeni revision gerekir'
            using errcode = '23514';
    end if;
    return new;
end
$$;

comment on column memory.record.logical_memory_id is
    'Domain logical kimligi; PostgreSQL storage UUID ve record digest yerine gecmez.';
comment on column memory.record.work_ref is
    'Portable is referansi; work_item_id storage/FK kimliginden ayridir.';
