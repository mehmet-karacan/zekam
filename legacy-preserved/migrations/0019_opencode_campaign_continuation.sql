-- Recovery-required OpenCode campaign continuation and zero-call result adoption.
-- Migration 18 remains immutable; this migration only extends its append-only ledger.

alter table models.opencode_benchmark_campaign
    add column benchmark_suite_version integer not null default 1,
    add column parent_campaign_id uuid,
    add column parent_source_revision text,
    add column compatibility_evidence_digest text,
    add column continuation_provenance_digest text,
    add column continuation_tested_call_budget integer,
    add column continuation_provider_call_budget integer,
    add constraint opencode_campaign_parent_same_realm
        foreign key (realm_id, parent_campaign_id)
        references models.opencode_benchmark_campaign (realm_id, id) on delete restrict,
    add constraint opencode_campaign_suite_version_positive
        check (benchmark_suite_version >= 1),
    add constraint opencode_campaign_continuation_fields check (
        (
            parent_campaign_id is null
            and parent_source_revision is null
            and compatibility_evidence_digest is null
            and continuation_provenance_digest is null
            and continuation_tested_call_budget is null
            and continuation_provider_call_budget is null
        ) or (
            parent_campaign_id is not null
            and length(btrim(parent_source_revision)) between 1 and 128
            and compatibility_evidence_digest ~ '^sha256:[0-9a-f]{64}$'
            and continuation_provenance_digest ~ '^sha256:[0-9a-f]{64}$'
            and continuation_tested_call_budget is not null
            and continuation_provider_call_budget is not null
            and continuation_tested_call_budget between 0 and tested_call_budget
            and continuation_provider_call_budget between
                continuation_tested_call_budget and provider_call_budget
        )
    );

create unique index opencode_campaign_one_child_idx
    on models.opencode_benchmark_campaign (realm_id, parent_campaign_id)
    where parent_campaign_id is not null;

alter table models.opencode_benchmark_campaign_member_plan
    drop constraint opencode_member_plan_benchmark_unique,
    add constraint opencode_member_plan_benchmark_campaign_unique
        unique (realm_id, campaign_id, benchmark_plan_id);

alter table models.opencode_benchmark_campaign_member_result
    add column adopted_from_campaign_id uuid,
    add column adopted_from_result_id uuid,
    add column adoption_provenance_digest text,
    add column recovered_from_claim_id uuid,
    add column recovered_from_receipt_id uuid,
    add column recovery_provenance_digest text,
    add constraint opencode_result_adopted_source_same_realm
        foreign key (realm_id, adopted_from_campaign_id, adopted_from_result_id)
        references models.opencode_benchmark_campaign_member_result
            (realm_id, campaign_id, id) on delete restrict,
    add constraint opencode_result_recovered_claim_exists
        foreign key (recovered_from_claim_id)
        references runtime.effect_claim (id) on delete restrict,
    add constraint opencode_result_recovered_receipt_exists
        foreign key (recovered_from_receipt_id)
        references runtime.effect_receipt (id) on delete restrict;

alter table models.opencode_benchmark_campaign_member_result
    drop constraint opencode_member_result_binding,
    add constraint opencode_member_result_binding check (
        (
            stage = 'health' and member_plan_id is null and aggregate_id is null
            and actual_tested_call_count = 0 and actual_provider_call_count between 0 and 1
        ) or (
            stage = 'benchmark'
            and (member_plan_id is not null or adopted_from_result_id is not null)
            and (status <> 'passed' or aggregate_id is not null)
        )
    ),
    add constraint opencode_member_result_provenance_shape check (
        (
            adopted_from_campaign_id is null and adopted_from_result_id is null
            and adoption_provenance_digest is null
            and recovered_from_claim_id is null and recovered_from_receipt_id is null
            and recovery_provenance_digest is null
        ) or (
            adopted_from_campaign_id is not null and adopted_from_result_id is not null
            and adoption_provenance_digest ~ '^sha256:[0-9a-f]{64}$'
            and recovered_from_claim_id is null and recovered_from_receipt_id is null
            and recovery_provenance_digest is null
            and status in ('passed', 'failed')
            and actual_tested_call_count = 0 and actual_provider_call_count = 0
        ) or (
            adopted_from_campaign_id is null and adopted_from_result_id is null
            and adoption_provenance_digest is null
            and recovered_from_claim_id is not null and recovered_from_receipt_id is not null
            and recovery_provenance_digest ~ '^sha256:[0-9a-f]{64}$'
            and stage = 'health' and status = 'failed'
            and failure_category = 'health-contract-failed'
            and aggregate_id is null
            and actual_tested_call_count = 0 and actual_provider_call_count = 0
        )
    );

create function models.enforce_opencode_continuation_campaign()
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
       or not exists (
           select 1 from models.opencode_benchmark_campaign_outcome o
           where o.realm_id = new.realm_id and o.campaign_id = parent.id
             and o.status = 'recovery-required'
       )
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
    -- Source revision/digest may differ only because compatibility evidence is
    -- explicit and digest-valid; the check constraint rejects silent drift.
    return new;
end
$$;

create function models.enforce_opencode_continuation_outcome_budget()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, models
as $$
begin
    if exists (
        select 1 from models.opencode_benchmark_campaign c
        where c.realm_id = new.realm_id and c.id = new.campaign_id
          and c.parent_campaign_id is not null
          and (
              new.actual_tested_call_count > c.continuation_tested_call_budget
              or new.actual_provider_call_count > c.continuation_provider_call_budget
          )
    ) then
        raise exception 'OpenCode continuation current call budget exceeded'
            using errcode = '42501';
    end if;
    return new;
end
$$;

create function models.enforce_opencode_continuation_member()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, models
as $$
declare parent_id uuid;
begin
    select parent_campaign_id into parent_id
      from models.opencode_benchmark_campaign
     where realm_id = new.realm_id and id = new.campaign_id;
    if parent_id is null then
        return new;
    end if;
    if not exists (
        select 1 from models.opencode_benchmark_campaign_member p
        where p.realm_id = new.realm_id and p.campaign_id = parent_id
          and p.configured_model_id = new.configured_model_id
          and p.canonical_model_id is not distinct from new.canonical_model_id
          and p.modality = new.modality
          and p.disposition = new.disposition
          and p.fixture_digests = new.fixture_digests
          and p.exclusion_reason is not distinct from new.exclusion_reason
          and p.suite_digest is not distinct from new.suite_digest
          and p.tested_call_budget = new.tested_call_budget
          and p.provider_call_budget = new.provider_call_budget
    ) then
        raise exception 'OpenCode continuation member parent target mismatch'
            using errcode = '42501';
    end if;
    return new;
end
$$;

create function models.validate_opencode_campaign_member_set()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, models
as $$
declare current_count integer;
begin
    select count(*) into current_count
      from models.opencode_benchmark_campaign_member m
     where m.realm_id = new.realm_id and m.campaign_id = new.id;
    if current_count <> new.member_count then
        raise exception 'OpenCode campaign full member set incomplete'
            using errcode = '42501';
    end if;
    if new.parent_campaign_id is not null and exists (
        (
            select configured_model_id, canonical_model_id, modality, disposition,
                   fixture_digests, exclusion_reason, suite_digest,
                   tested_call_budget, provider_call_budget
              from models.opencode_benchmark_campaign_member
             where realm_id = new.realm_id and campaign_id = new.parent_campaign_id
            except
            select configured_model_id, canonical_model_id, modality, disposition,
                   fixture_digests, exclusion_reason, suite_digest,
                   tested_call_budget, provider_call_budget
              from models.opencode_benchmark_campaign_member
             where realm_id = new.realm_id and campaign_id = new.id
        )
        union all
        (
            select configured_model_id, canonical_model_id, modality, disposition,
                   fixture_digests, exclusion_reason, suite_digest,
                   tested_call_budget, provider_call_budget
              from models.opencode_benchmark_campaign_member
             where realm_id = new.realm_id and campaign_id = new.id
            except
            select configured_model_id, canonical_model_id, modality, disposition,
                   fixture_digests, exclusion_reason, suite_digest,
                   tested_call_budget, provider_call_budget
              from models.opencode_benchmark_campaign_member
             where realm_id = new.realm_id and campaign_id = new.parent_campaign_id
        )
    ) then
        raise exception 'OpenCode continuation full member set drift'
            using errcode = '42501';
    end if;
    return null;
end
$$;

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
    if new.adopted_from_result_id is not null then
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

create function models.enforce_opencode_result_continuation_provenance()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, models, runtime, security
as $$
declare parent_id uuid;
declare call_id text;
declare call_resource text;
begin
    if new.adopted_from_result_id is null and new.recovered_from_claim_id is null then
        return new;
    end if;
    select parent_campaign_id into parent_id
      from models.opencode_benchmark_campaign
     where realm_id = new.realm_id and id = new.campaign_id;
    if parent_id is null then
        raise exception 'OpenCode result adoption continuation campaign ister'
            using errcode = '42501';
    end if;

    if new.adopted_from_result_id is not null then
        if new.adopted_from_campaign_id <> parent_id or not exists (
            select 1
            from models.opencode_benchmark_campaign_member_result source_result
            join models.opencode_benchmark_campaign_member source_member
              on source_member.realm_id = source_result.realm_id
             and source_member.campaign_id = source_result.campaign_id
             and source_member.id = source_result.member_id
            join models.opencode_benchmark_campaign_member current_member
              on current_member.realm_id = new.realm_id
             and current_member.campaign_id = new.campaign_id
             and current_member.id = new.member_id
            left join models.opencode_benchmark_campaign_member_plan source_member_plan
              on source_member_plan.realm_id = source_result.realm_id
             and source_member_plan.campaign_id = source_result.campaign_id
             and source_member_plan.id = source_result.member_plan_id
            left join models.benchmark_plan source_plan
              on source_plan.realm_id = source_member_plan.realm_id
             and source_plan.id = source_member_plan.benchmark_plan_id
            left join models.benchmark_suite source_suite
              on source_suite.realm_id = source_plan.realm_id
             and source_suite.id = source_plan.suite_id
            left join models.benchmark_aggregate source_aggregate
              on source_aggregate.realm_id = source_result.realm_id
             and source_aggregate.id = source_result.aggregate_id
            where source_result.realm_id = new.realm_id
              and source_result.campaign_id = parent_id
              and source_result.id = new.adopted_from_result_id
              and source_result.status in ('passed', 'failed')
              and source_result.adopted_from_result_id is null
              and source_result.recovered_from_claim_id is null
              and source_result.stage = new.stage
              and source_result.status = new.status
              and source_result.evidence_digest = new.evidence_digest
              and source_result.aggregate_id is not distinct from new.aggregate_id
              and source_result.failure_category is not distinct from new.failure_category
              and source_member.configured_model_id = current_member.configured_model_id
              and source_member.canonical_model_id is not distinct from
                  current_member.canonical_model_id
              and source_member.modality = current_member.modality
              and source_member.suite_digest is not distinct from current_member.suite_digest
              and (
                  new.stage = 'health'
                  or (
                      source_plan.model_id = current_member.canonical_model_id
                      and cardinality(source_suite.fixture_digests) =
                          cardinality(current_member.fixture_digests)
                      and source_suite.fixture_digests @> current_member.fixture_digests
                      and current_member.fixture_digests @> source_suite.fixture_digests
                      and (
                          source_result.aggregate_id is null
                          or source_member_plan.benchmark_plan_id = source_aggregate.plan_id
                      )
                  )
              )
        ) then
            raise exception 'OpenCode adopted result parent/model/plan/suite mismatch'
                using errcode = '42501';
        end if;
        return new;
    end if;

    select c.operation, c.resources -> 0 ->> 'resource'
      into call_id, call_resource
      from runtime.effect_claim c
     where c.realm_id = new.realm_id and c.id = new.recovered_from_claim_id;
    call_id := substr(call_id, length('provider-contract:') + 1);
    if not exists (
        select 1
        from models.opencode_benchmark_campaign parent
        join models.opencode_benchmark_campaign_member parent_member
          on parent_member.realm_id = parent.realm_id
         and parent_member.campaign_id = parent.id
        join models.opencode_benchmark_campaign_member current_member
          on current_member.realm_id = new.realm_id
         and current_member.campaign_id = new.campaign_id
         and current_member.id = new.member_id
        join runtime.effect_claim claim
          on claim.realm_id = parent.realm_id and claim.id = new.recovered_from_claim_id
        join runtime.effect_receipt receipt
          on receipt.realm_id = claim.realm_id and receipt.claim_id = claim.id
         and receipt.id = new.recovered_from_receipt_id
        join runtime.job job
          on job.realm_id = claim.realm_id and job.id = claim.job_id
        join runtime.job_attempt attempt
          on attempt.realm_id = claim.realm_id and attempt.id = claim.attempt_id
         and attempt.job_id = job.id
        join security.authorization authz
          on authz.realm_id = claim.realm_id and authz.id = claim.authorization_id
        join work.task_plan bound_plan
          on bound_plan.realm_id = authz.realm_id and bound_plan.id = authz.plan_id
        where parent.realm_id = new.realm_id and parent.id = parent_id
          and parent_member.configured_model_id = current_member.configured_model_id
          and parent_member.canonical_model_id = current_member.canonical_model_id
          and parent_member.modality = current_member.modality
          and parent_member.suite_digest = current_member.suite_digest
          and claim.operation = 'provider-contract:' || call_id
          and left(call_id, length('health-' || current_member.canonical_model_id || '-')) =
              'health-' || current_member.canonical_model_id || '-'
          and right(call_id, 2) = '-0'
          and jsonb_array_length(claim.resources) = 1
          and claim.resources -> 0 ->> 'mode' = 'write'
          and call_resource = any(authz.allowed_resources)
          and left(call_resource, length('provider:' || current_member.canonical_model_id || ':')) =
              'provider:' || current_member.canonical_model_id || ':'
          and right(call_resource, length(':' || call_id)) = ':' || call_id
          and receipt.status = 'completed' and receipt.result_digest is not null
          and authz.work_item_id = parent.work_item_id
          and authz.plan_id = parent.task_plan_id
          and bound_plan.work_item_id = parent.work_item_id
          and (
              authz.plan_digest = bound_plan.plan_digest
              or exists (
                  select 1
                  from jsonb_array_elements(bound_plan.steps) as step(value)
                  where step.value ->> 'step_id' = call_id
                    and step.value ->> 'effect' = 'provider-call'
                    and step.value -> 'logical_resources' ? call_resource
              )
          )
          and authz.state = 'consumed'
          and authz.effect_digest = claim.effect_digest
          and authz.authorization_digest = claim.authorization_digest
          and job.work_item_id = parent.work_item_id and job.plan_id = parent.task_plan_id
          and not exists (
              select 1 from models.opencode_benchmark_campaign_member_result existing
              where existing.realm_id = parent.realm_id
                and existing.campaign_id = parent.id
                and existing.member_id = parent_member.id
                and existing.stage = 'health'
                and not (
                    existing.status = 'recovery-required'
                    and existing.member_plan_id is null
                    and existing.aggregate_id is null
                    and existing.failure_category = 'campaign-recovery-not-run'
                    and existing.actual_tested_call_count = 0
                    and existing.actual_provider_call_count = 0
                    and existing.adopted_from_result_id is null
                    and existing.recovered_from_claim_id is null
                )
          )
    ) then
        raise exception 'OpenCode recovered health claim/receipt/job/auth mismatch'
            using errcode = '42501';
    end if;
    return new;
end
$$;

create trigger opencode_continuation_campaign_binding
    before insert on models.opencode_benchmark_campaign
    for each row execute function models.enforce_opencode_continuation_campaign();
create trigger opencode_continuation_member_binding
    before insert on models.opencode_benchmark_campaign_member
    for each row execute function models.enforce_opencode_continuation_member();
create trigger opencode_continuation_outcome_budget
    before insert on models.opencode_benchmark_campaign_outcome
    for each row execute function models.enforce_opencode_continuation_outcome_budget();
create constraint trigger opencode_campaign_full_member_set
    after insert on models.opencode_benchmark_campaign
    deferrable initially deferred
    for each row execute function models.validate_opencode_campaign_member_set();
create trigger opencode_result_continuation_provenance
    before insert on models.opencode_benchmark_campaign_member_result
    for each row execute function models.enforce_opencode_result_continuation_provenance();

grant execute on function models.enforce_opencode_continuation_campaign() to zekam_app;
grant execute on function models.enforce_opencode_continuation_member() to zekam_app;
grant execute on function models.enforce_opencode_continuation_outcome_budget() to zekam_app;
grant execute on function models.validate_opencode_campaign_member_set() to zekam_app;
grant execute on function models.enforce_opencode_result_continuation_provenance() to zekam_app;

comment on column models.opencode_benchmark_campaign.benchmark_suite_version is
    'Reviewed benchmark suite version; campaign recovery revisionindan bagimsizdir.';
comment on column models.opencode_benchmark_campaign_member_result.adopted_from_result_id is
    'Exact terminal parent result reused with zero new provider/tested calls.';
comment on column models.opencode_benchmark_campaign_member_result.recovered_from_claim_id is
    'Completed parent provider effect recovered after result projection failed.';
