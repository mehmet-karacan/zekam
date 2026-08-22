create or replace function knowledge.require_completed_ingestion() returns trigger
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

update models.model_inventory
set modality = declared_mode
where declared_category = 'multimodal_generation'
  and declared_mode is not null;

drop table knowledge.document_index_profile;

alter table knowledge.ingestion_job drop constraint job_idempotent_per_source;
alter table knowledge.ingestion_job
    add constraint job_idempotent unique (realm_id, idempotency_key);

alter table knowledge.normalized_document
    drop constraint normalized_document_parser_profile_object;
alter table knowledge.normalized_document drop column parser_profile;

delete from core.schema_migrations where version = 17;
