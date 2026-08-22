-- Restore the migration-0019 continuation binding. Durable campaign rows remain intact.

create or replace function models.enforce_opencode_continuation_campaign()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, models
as $$
declare parent models.opencode_benchmark_campaign%rowtype;
begin
    if new.parent_campaign_id is null then
        return new;
    end if;
    select * into parent
      from models.opencode_benchmark_campaign
     where realm_id = new.realm_id and id = new.parent_campaign_id;
    if not found
       or new.id = parent.id
       or new.campaign_key <> parent.campaign_key
       or new.revision <> parent.revision + 1
       or new.work_item_id <> parent.work_item_id
       or new.parent_source_revision <> parent.source_revision
       or new.provider_ref <> parent.provider_ref
       or new.catalog_digest <> parent.catalog_digest
       or new.endpoint_identity_digest <> parent.endpoint_identity_digest
       or new.inventory_digest <> parent.inventory_digest
       or new.policy_digest <> parent.policy_digest
       or new.fixture_registry_digest <> parent.fixture_registry_digest
       or new.verifier_identity <> parent.verifier_identity
       or new.verifier_provenance_digest <> parent.verifier_provenance_digest
       or new.repetitions <> parent.repetitions
       or new.verifier_provider_calls_per_trial <> parent.verifier_provider_calls_per_trial
       or new.configured_model_count <> parent.configured_model_count
       or new.member_count <> parent.member_count
       or new.eligible_model_count <> parent.eligible_model_count
       or new.audio_excluded_count <> parent.audio_excluded_count
       or new.health_call_budget <> parent.health_call_budget
       or new.tested_call_budget <> parent.tested_call_budget
       or new.provider_call_budget <> parent.provider_call_budget
       or new.benchmark_suite_version <> parent.benchmark_suite_version then
        raise exception 'OpenCode continuation parent/revision/binding mismatch'
            using errcode = '42501';
    end if;
    return new;
end
$$;

delete from core.schema_migrations where version = 20;
