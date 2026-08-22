-- Fail closed rather than discard durable continuation provenance.
do $$
begin
    if exists (
        select 1 from models.opencode_benchmark_campaign where parent_campaign_id is not null
    ) or exists (
        select 1 from models.opencode_benchmark_campaign_member_result
         where adopted_from_result_id is not null or recovered_from_claim_id is not null
    ) then
        raise exception 'Migration 0019 continuation kaniti varken geri alinamaz'
            using errcode = '42501';
    end if;
end
$$;

drop trigger if exists opencode_result_continuation_provenance
    on models.opencode_benchmark_campaign_member_result;
drop trigger if exists opencode_campaign_full_member_set
    on models.opencode_benchmark_campaign;
drop trigger if exists opencode_continuation_outcome_budget
    on models.opencode_benchmark_campaign_outcome;
drop trigger if exists opencode_continuation_member_binding
    on models.opencode_benchmark_campaign_member;
drop trigger if exists opencode_continuation_campaign_binding
    on models.opencode_benchmark_campaign;

drop function if exists models.enforce_opencode_result_continuation_provenance();
drop function if exists models.validate_opencode_campaign_member_set();
drop function if exists models.enforce_opencode_continuation_outcome_budget();
drop function if exists models.enforce_opencode_continuation_member();
drop function if exists models.enforce_opencode_continuation_campaign();

alter table models.opencode_benchmark_campaign_member_result
    drop constraint opencode_result_adopted_source_same_realm,
    drop constraint opencode_result_recovered_claim_exists,
    drop constraint opencode_result_recovered_receipt_exists,
    drop constraint opencode_member_result_provenance_shape,
    drop constraint opencode_member_result_binding,
    add constraint opencode_member_result_binding check (
        (
            stage = 'health' and member_plan_id is null and aggregate_id is null
            and actual_tested_call_count = 0 and actual_provider_call_count between 0 and 1
        ) or (
            stage = 'benchmark' and member_plan_id is not null
            and (status <> 'passed' or aggregate_id is not null)
        )
    ),
    drop column adopted_from_campaign_id,
    drop column adopted_from_result_id,
    drop column adoption_provenance_digest,
    drop column recovered_from_claim_id,
    drop column recovered_from_receipt_id,
    drop column recovery_provenance_digest;

alter table models.opencode_benchmark_campaign_member_plan
    drop constraint opencode_member_plan_benchmark_campaign_unique,
    add constraint opencode_member_plan_benchmark_unique unique (realm_id, benchmark_plan_id);

drop index if exists models.opencode_campaign_one_child_idx;

alter table models.opencode_benchmark_campaign
    drop constraint opencode_campaign_parent_same_realm,
    drop constraint opencode_campaign_suite_version_positive,
    drop constraint opencode_campaign_continuation_fields,
    drop column benchmark_suite_version,
    drop column parent_campaign_id,
    drop column parent_source_revision,
    drop column compatibility_evidence_digest,
    drop column continuation_provenance_digest,
    drop column continuation_tested_call_budget,
    drop column continuation_provider_call_budget;

create or replace function models.enforce_opencode_member_result_binding()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, models
as $$
declare plan_row models.opencode_benchmark_campaign_member_plan%rowtype;
begin
    if new.stage = 'health' then
        if not exists (
            select 1 from models.opencode_benchmark_campaign_member m
            where m.realm_id = new.realm_id and m.campaign_id = new.campaign_id
              and m.id = new.member_id and m.disposition = 'health-pending'
        ) then
            raise exception 'OpenCode health result member mismatch' using errcode = '42501';
        end if;
        return new;
    end if;
    select * into plan_row from models.opencode_benchmark_campaign_member_plan
    where realm_id = new.realm_id and campaign_id = new.campaign_id
      and member_id = new.member_id and id = new.member_plan_id;
    if not found
       or new.actual_tested_call_count > plan_row.tested_call_budget
       or new.actual_provider_call_count > plan_row.provider_call_budget then
        raise exception 'OpenCode campaign member result plan/budget mismatch'
            using errcode = '42501';
    end if;
    if new.aggregate_id is not null and not exists (
        select 1 from models.benchmark_aggregate a
        where a.id = new.aggregate_id and a.realm_id = new.realm_id
          and a.plan_id = plan_row.benchmark_plan_id
    ) then
        raise exception 'OpenCode member aggregate exact benchmark plan mismatch'
            using errcode = '42501';
    end if;
    if new.status = 'passed' and (
        new.actual_tested_call_count <> plan_row.tested_call_budget
        or new.actual_provider_call_count <> plan_row.provider_call_budget
        or not exists (
            select 1 from models.benchmark_aggregate a
            where a.id = new.aggregate_id and a.realm_id = new.realm_id
              and a.plan_id = plan_row.benchmark_plan_id
              and a.approved and not a.unsafe
        )
    ) then
        raise exception 'OpenCode passed member exact approved aggregate ister'
            using errcode = '42501';
    end if;
    return new;
end
$$;
