-- Rollback on kosulu: ayni proje/icerikte farkli work-item aktif kayitlari once
-- archive/supersede edilmelidir. Migration veri silmez; eski dar index aksi halde
-- unique violation ile fail-closed durur.
drop index if exists memory.record_active_content_idx;

create unique index record_active_content_idx
    on memory.record (
        realm_id,
        scope,
        coalesce(project_id, '00000000-0000-0000-0000-000000000000'::uuid),
        md5(content)
    )
    where state = 'active';

alter table memory.record
    drop constraint if exists record_scope_identity,
    drop constraint if exists record_work_project_same_realm,
    drop constraint if exists record_logical_id_unique,
    drop column if exists work_ref,
    drop column if exists project_ref,
    drop column if exists logical_memory_id;

alter table memory.candidate
    drop constraint if exists candidate_scope_identity,
    drop constraint if exists candidate_work_project_same_realm,
    drop constraint if exists candidate_logical_id_unique,
    drop column if exists work_ref,
    drop column if exists project_ref,
    drop column if exists logical_candidate_id;

create or replace function memory.enforce_immutable_content() returns trigger
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
