-- OpenCode configured-model benchmark campaign ledger.
-- Only secret-free identities, exact budgets and digests are durable. Raw endpoint,
-- credential, fixture, prompt and response material is intentionally absent.

create function models.valid_campaign_digest_array(values_ text[])
returns boolean
language sql
immutable
strict
as $$
    select cardinality(values_) > 0
       and count(*) = cardinality(values_)
       and bool_and(value ~ '^sha256:[0-9a-f]{64}$')
       and count(distinct value) = cardinality(values_)
    from unnest(values_) as item(value)
$$;

create table models.opencode_benchmark_campaign (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    work_item_id uuid not null,
    task_plan_id uuid not null references work.task_plan (id) on delete restrict,
    campaign_key text not null,
    revision integer not null,
    source_revision text not null,
    provider_ref text not null,
    catalog_digest text not null,
    endpoint_identity_digest text not null,
    inventory_digest text not null,
    policy_digest text not null,
    fixture_registry_digest text not null,
    verifier_identity text not null,
    verifier_provenance_digest text not null,
    source_digest text not null,
    repetitions integer not null,
    verifier_provider_calls_per_trial integer not null,
    configured_model_count integer not null,
    member_count integer not null,
    eligible_model_count integer not null,
    audio_excluded_count integer not null,
    health_call_budget integer not null,
    tested_call_budget integer not null,
    provider_call_budget integer not null,
    campaign_digest text not null,
    created_at timestamptz not null default now(),
    constraint opencode_campaign_realm_id_unique unique (realm_id, id),
    constraint opencode_campaign_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete restrict,
    constraint opencode_campaign_revision_unique unique (realm_id, campaign_key, revision),
    constraint opencode_campaign_digest_unique unique (realm_id, campaign_digest),
    constraint opencode_campaign_revision_positive check (revision >= 1),
    constraint opencode_campaign_repetitions_minimum check (repetitions >= 5),
    constraint opencode_campaign_verifier_call_factor
        check (verifier_provider_calls_per_trial in (0, 1)),
    constraint opencode_campaign_counts check (
        configured_model_count >= 1
        and member_count >= configured_model_count
        and eligible_model_count >= 1
        and audio_excluded_count >= 0
        and member_count = eligible_model_count + audio_excluded_count
        and health_call_budget = eligible_model_count
        and tested_call_budget >= eligible_model_count * repetitions
        and provider_call_budget = health_call_budget
            + tested_call_budget * (1 + verifier_provider_calls_per_trial)
    ),
    constraint opencode_campaign_metadata_safe check (
        length(btrim(campaign_key)) between 1 and 128
        and length(btrim(source_revision)) between 1 and 128
        and length(btrim(provider_ref)) between 1 and 128
        and length(btrim(verifier_identity)) between 1 and 256
        and position('://' in campaign_key || source_revision || provider_ref || verifier_identity) = 0
    ),
    constraint opencode_campaign_digest_formats check (
        catalog_digest ~ '^sha256:[0-9a-f]{64}$'
        and endpoint_identity_digest ~ '^sha256:[0-9a-f]{64}$'
        and inventory_digest ~ '^sha256:[0-9a-f]{64}$'
        and policy_digest ~ '^sha256:[0-9a-f]{64}$'
        and fixture_registry_digest ~ '^sha256:[0-9a-f]{64}$'
        and verifier_provenance_digest ~ '^sha256:[0-9a-f]{64}$'
        and source_digest ~ '^sha256:[0-9a-f]{64}$'
        and campaign_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create table models.opencode_benchmark_campaign_member (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    campaign_id uuid not null,
    configured_model_id text not null,
    canonical_model_id text,
    modality text not null,
    disposition text not null,
    fixture_digests text[] not null default '{}',
    exclusion_reason text,
    suite_digest text,
    tested_call_budget integer not null,
    provider_call_budget integer not null,
    created_at timestamptz not null default now(),
    constraint opencode_campaign_member_realm_id_unique unique (realm_id, campaign_id, id),
    constraint opencode_campaign_member_campaign_same_realm
        foreign key (realm_id, campaign_id)
        references models.opencode_benchmark_campaign (realm_id, id) on delete restrict,
    constraint opencode_campaign_member_target_unique
        unique nulls not distinct (realm_id, campaign_id, configured_model_id, canonical_model_id),
    constraint opencode_campaign_member_disposition
        check (disposition in ('health-pending', 'excluded-audio')),
    constraint opencode_campaign_member_binding check (
        (
            disposition = 'health-pending'
            and modality <> 'audio_transcription'
            and canonical_model_id is not null
            and exclusion_reason is null
            and suite_digest ~ '^sha256:[0-9a-f]{64}$'
            and models.valid_campaign_digest_array(fixture_digests)
            and tested_call_budget > 0
            and provider_call_budget >= tested_call_budget
        ) or (
            disposition = 'excluded-audio'
            and modality = 'audio_transcription'
            and exclusion_reason = 'audio-user-scope-excluded'
            and suite_digest is null
            and cardinality(fixture_digests) = 0
            and tested_call_budget = 0
            and provider_call_budget = 0
        )
    ),
    constraint opencode_campaign_member_metadata_safe check (
        length(btrim(configured_model_id)) between 1 and 256
        and (canonical_model_id is null or length(btrim(canonical_model_id)) between 1 and 256)
        and length(btrim(modality)) between 1 and 64
        and position('://' in configured_model_id || coalesce(canonical_model_id, '') || modality) = 0
    )
);

create table models.opencode_benchmark_campaign_member_plan (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    campaign_id uuid not null,
    member_id uuid not null,
    benchmark_plan_id uuid not null,
    benchmark_plan_digest text not null,
    health_evidence_digest text not null,
    authorization_manifest_digest text not null,
    tested_call_budget integer not null,
    provider_call_budget integer not null,
    member_plan_digest text not null,
    created_at timestamptz not null default now(),
    constraint opencode_member_plan_realm_id_unique unique (realm_id, campaign_id, id),
    constraint opencode_member_plan_campaign_same_realm
        foreign key (realm_id, campaign_id)
        references models.opencode_benchmark_campaign (realm_id, id) on delete restrict,
    constraint opencode_member_plan_member_same_campaign
        foreign key (realm_id, campaign_id, member_id)
        references models.opencode_benchmark_campaign_member (realm_id, campaign_id, id)
        on delete restrict,
    constraint opencode_member_plan_benchmark_same_realm
        foreign key (realm_id, benchmark_plan_id)
        references models.benchmark_plan (realm_id, id) on delete restrict,
    constraint opencode_member_plan_one_per_member unique (realm_id, campaign_id, member_id),
    constraint opencode_member_plan_benchmark_unique unique (realm_id, benchmark_plan_id),
    constraint opencode_member_plan_digest_unique unique (realm_id, member_plan_digest),
    constraint opencode_member_plan_budgets
        check (tested_call_budget > 0 and provider_call_budget >= tested_call_budget),
    constraint opencode_member_plan_digest_formats check (
        benchmark_plan_digest ~ '^sha256:[0-9a-f]{64}$'
        and health_evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and authorization_manifest_digest ~ '^sha256:[0-9a-f]{64}$'
        and member_plan_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create table models.opencode_benchmark_campaign_member_result (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    campaign_id uuid not null,
    member_id uuid not null,
    member_plan_id uuid,
    stage text not null,
    status text not null,
    aggregate_id uuid references models.benchmark_aggregate (id) on delete restrict,
    evidence_digest text not null,
    result_digest text not null,
    failure_category text,
    actual_tested_call_count integer not null,
    actual_provider_call_count integer not null,
    completed_at timestamptz not null default now(),
    constraint opencode_member_result_realm_id_unique unique (realm_id, campaign_id, id),
    constraint opencode_member_result_campaign_same_realm
        foreign key (realm_id, campaign_id)
        references models.opencode_benchmark_campaign (realm_id, id) on delete restrict,
    constraint opencode_member_result_member_same_campaign
        foreign key (realm_id, campaign_id, member_id)
        references models.opencode_benchmark_campaign_member (realm_id, campaign_id, id)
        on delete restrict,
    constraint opencode_member_result_plan_same_campaign
        foreign key (realm_id, campaign_id, member_plan_id)
        references models.opencode_benchmark_campaign_member_plan (realm_id, campaign_id, id)
        on delete restrict,
    constraint opencode_member_result_one_per_stage
        unique (realm_id, campaign_id, member_id, stage),
    constraint opencode_member_result_digest_unique unique (realm_id, result_digest),
    constraint opencode_member_result_status
        check (status in ('passed', 'failed', 'recovery-required')),
    constraint opencode_member_result_stage check (stage in ('health', 'benchmark')),
    constraint opencode_member_result_binding check (
        (
            stage = 'health' and member_plan_id is null and aggregate_id is null
            and actual_tested_call_count = 0 and actual_provider_call_count between 0 and 1
        ) or (
            stage = 'benchmark' and member_plan_id is not null
            and (status <> 'passed' or aggregate_id is not null)
        )
    ),
    constraint opencode_member_result_terminal_detail check (
        (status = 'passed' and failure_category is null)
        or (status = 'failed' and failure_category is not null)
        or (status = 'recovery-required' and aggregate_id is null and failure_category is not null)
    ),
    constraint opencode_member_result_calls check (
        actual_tested_call_count >= 0
        and actual_provider_call_count >= actual_tested_call_count
    ),
    constraint opencode_member_result_digest_formats check (
        evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and result_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create table models.opencode_benchmark_campaign_outcome (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    campaign_id uuid not null,
    status text not null,
    passed_count integer not null,
    failed_count integer not null,
    recovery_required_count integer not null,
    audio_excluded_count integer not null,
    actual_tested_call_count integer not null,
    actual_provider_call_count integer not null,
    evidence_digest text not null,
    outcome_digest text not null,
    completed_at timestamptz not null default now(),
    constraint opencode_campaign_outcome_realm_id_unique unique (realm_id, campaign_id, id),
    constraint opencode_campaign_outcome_campaign_same_realm
        foreign key (realm_id, campaign_id)
        references models.opencode_benchmark_campaign (realm_id, id) on delete restrict,
    constraint opencode_campaign_outcome_one_per_campaign unique (realm_id, campaign_id),
    constraint opencode_campaign_outcome_digest_unique unique (realm_id, outcome_digest),
    constraint opencode_campaign_outcome_status
        check (status in ('passed', 'failed', 'recovery-required')),
    constraint opencode_campaign_outcome_counts check (
        passed_count >= 0 and failed_count >= 0 and recovery_required_count >= 0
        and audio_excluded_count >= 0 and actual_tested_call_count >= 0
        and actual_provider_call_count >= actual_tested_call_count
        and (
            (status = 'passed' and passed_count > 0 and failed_count = 0
             and recovery_required_count = 0)
            or (status = 'failed' and failed_count > 0 and recovery_required_count = 0)
            or (status = 'recovery-required' and recovery_required_count > 0)
        )
    ),
    constraint opencode_campaign_outcome_digest_formats check (
        evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and outcome_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create table models.opencode_model_qualification_event (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    campaign_id uuid not null,
    member_id uuid not null,
    outcome_id uuid not null,
    model_id text not null,
    action text not null,
    aggregate_id uuid references models.benchmark_aggregate (id) on delete restrict,
    evidence_digest text not null,
    reason_code text,
    event_digest text not null,
    occurred_at timestamptz not null default now(),
    constraint opencode_qualification_campaign_same_realm
        foreign key (realm_id, campaign_id)
        references models.opencode_benchmark_campaign (realm_id, id) on delete restrict,
    constraint opencode_qualification_member_same_campaign
        foreign key (realm_id, campaign_id, member_id)
        references models.opencode_benchmark_campaign_member (realm_id, campaign_id, id)
        on delete restrict,
    constraint opencode_qualification_outcome_same_campaign
        foreign key (realm_id, campaign_id, outcome_id)
        references models.opencode_benchmark_campaign_outcome (realm_id, campaign_id, id)
        on delete restrict,
    constraint opencode_qualification_event_unique unique (realm_id, event_digest),
    constraint opencode_qualification_member_unique
        unique (realm_id, campaign_id, member_id),
    constraint opencode_qualification_action check (action in ('qualified', 'disqualified')),
    constraint opencode_qualification_binding check (
        (action = 'qualified' and aggregate_id is not null and reason_code is null)
        or (action = 'disqualified' and aggregate_id is null and reason_code is not null)
    ),
    constraint opencode_qualification_metadata_safe check (
        length(btrim(model_id)) between 1 and 256
        and position('://' in model_id) = 0
        and (reason_code is null or length(btrim(reason_code)) between 1 and 128)
    ),
    constraint opencode_qualification_digest_formats check (
        evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and event_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create function models.enforce_opencode_campaign_binding()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, models, work
as $$
begin
    if not exists (
        select 1 from work.task_plan p
        where p.id = new.task_plan_id
          and p.realm_id = new.realm_id
          and p.work_item_id = new.work_item_id
          and p.source_revision = new.source_revision
          and p.policy_digest = new.policy_digest
          and not p.grants_authority
    ) then
        raise exception 'OpenCode campaign Work/plan/source/policy binding mismatch'
            using errcode = '42501';
    end if;
    return new;
end
$$;

create function models.enforce_opencode_campaign_member_budget()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, models
as $$
declare campaign models.opencode_benchmark_campaign%rowtype;
begin
    select * into campaign from models.opencode_benchmark_campaign
    where id = new.campaign_id and realm_id = new.realm_id;
    if not found then
        raise exception 'OpenCode campaign member campaign mismatch' using errcode = '23503';
    end if;
    if new.disposition = 'health-pending' and (
        new.tested_call_budget <> cardinality(new.fixture_digests) * campaign.repetitions
        or new.provider_call_budget <>
            new.tested_call_budget * (1 + campaign.verifier_provider_calls_per_trial)
    ) then
        raise exception 'OpenCode campaign member exact budget mismatch' using errcode = '23514';
    end if;
    return new;
end
$$;

create function models.enforce_opencode_member_plan_binding()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, models
as $$
begin
    if not exists (
        select 1
        from models.opencode_benchmark_campaign c
        join models.opencode_benchmark_campaign_member m
          on m.realm_id = c.realm_id and m.campaign_id = c.id
        join models.benchmark_plan p
          on p.realm_id = c.realm_id and p.id = new.benchmark_plan_id
        join models.benchmark_suite s
          on s.realm_id = p.realm_id and s.id = p.suite_id
        where c.realm_id = new.realm_id and c.id = new.campaign_id
          and m.id = new.member_id and m.disposition = 'health-pending'
          and p.model_id = m.canonical_model_id
          and p.repetitions = c.repetitions
          and p.inventory_digest = c.inventory_digest
          and p.policy_digest = c.policy_digest
          and p.fixture_registry_digest = c.fixture_registry_digest
          and p.plan_digest = new.benchmark_plan_digest
          and p.remote_execution
          and cardinality(s.fixture_digests) = cardinality(m.fixture_digests)
          and s.fixture_digests @> m.fixture_digests
          and m.fixture_digests @> s.fixture_digests
          and new.tested_call_budget = m.tested_call_budget
          and new.provider_call_budget = m.provider_call_budget
          and exists (
              select 1 from models.opencode_benchmark_campaign_member_result h
              where h.realm_id = new.realm_id and h.campaign_id = new.campaign_id
                and h.member_id = new.member_id and h.stage = 'health'
                and h.status = 'passed'
                and h.evidence_digest = new.health_evidence_digest
          )
    ) then
        raise exception 'OpenCode campaign benchmark/member plan exact binding mismatch'
            using errcode = '42501';
    end if;
    return new;
end
$$;

create function models.enforce_opencode_member_result_binding()
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
            join models.benchmark_plan p
              on p.realm_id = a.realm_id and p.id = a.plan_id
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

create function models.enforce_opencode_campaign_outcome()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, models
as $$
declare campaign models.opencode_benchmark_campaign%rowtype;
declare observed record;
declare expected_status text;
begin
    select * into campaign from models.opencode_benchmark_campaign
    where realm_id = new.realm_id and id = new.campaign_id;
    select count(*) filter (
               where h.status = 'passed' and b.status = 'passed'
           ) as passed_count,
           count(*) filter (
               where h.status = 'failed' or b.status = 'failed'
           ) as failed_count,
           count(*) filter (
               where h.status = 'recovery-required' or b.status = 'recovery-required'
           ) as recovery_count,
           count(*) filter (
               where h.status is not null
                 and (h.status <> 'passed' or b.status is not null)
           ) as terminal_count,
           (select coalesce(sum(r.actual_tested_call_count), 0)
              from models.opencode_benchmark_campaign_member_result r
             where r.realm_id = new.realm_id and r.campaign_id = new.campaign_id
           ) as tested_calls,
           (select coalesce(sum(r.actual_provider_call_count), 0)
              from models.opencode_benchmark_campaign_member_result r
             where r.realm_id = new.realm_id and r.campaign_id = new.campaign_id
           ) as provider_calls
      into observed
      from models.opencode_benchmark_campaign_member m
      left join models.opencode_benchmark_campaign_member_result h
        on h.realm_id = m.realm_id and h.campaign_id = m.campaign_id
       and h.member_id = m.id and h.stage = 'health'
      left join models.opencode_benchmark_campaign_member_result b
        on b.realm_id = m.realm_id and b.campaign_id = m.campaign_id
       and b.member_id = m.id and b.stage = 'benchmark'
      where m.realm_id = new.realm_id and m.campaign_id = new.campaign_id
        and m.disposition = 'health-pending';
    if not found then
        raise exception 'OpenCode campaign member result set bulunamadi'
            using errcode = '42501';
    end if;
    expected_status := case
        when observed.recovery_count > 0 then 'recovery-required'
        when observed.failed_count > 0 then 'failed'
        else 'passed'
    end;
    if new.status = 'recovery-required' then
        if new.passed_count <> observed.passed_count
           or new.failed_count <> observed.failed_count
           or new.recovery_required_count < greatest(
               1,
               campaign.eligible_model_count - observed.passed_count - observed.failed_count
           )
           or new.actual_tested_call_count < observed.tested_calls
           or new.actual_provider_call_count < observed.provider_calls then
            raise exception 'OpenCode campaign recovery outcome mismatch'
                using errcode = '42501';
        end if;
    elsif observed.terminal_count <> campaign.eligible_model_count
       or new.status <> expected_status
       or new.passed_count <> observed.passed_count
       or new.failed_count <> observed.failed_count
       or new.recovery_required_count <> observed.recovery_count
       or new.actual_tested_call_count <> observed.tested_calls
       or new.actual_provider_call_count <> observed.provider_calls then
        raise exception 'OpenCode campaign terminal outcome mismatch' using errcode = '42501';
    end if;
    if new.audio_excluded_count <> campaign.audio_excluded_count
       or new.actual_tested_call_count > campaign.tested_call_budget
       or new.actual_provider_call_count > campaign.provider_call_budget then
        raise exception 'OpenCode campaign outcome budget mismatch' using errcode = '42501';
    end if;
    return new;
end
$$;

create function models.enforce_opencode_qualification()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, models
as $$
begin
    if not exists (
        select 1
        from models.opencode_benchmark_campaign_member m
        join models.opencode_benchmark_campaign_outcome o
          on o.realm_id = m.realm_id and o.campaign_id = m.campaign_id
        where m.realm_id = new.realm_id and m.campaign_id = new.campaign_id
          and m.id = new.member_id and o.id = new.outcome_id
          and m.canonical_model_id = new.model_id
    ) then
        raise exception 'OpenCode qualification campaign/member/outcome mismatch'
            using errcode = '42501';
    end if;
    if new.action = 'qualified' and not exists (
        select 1
        from models.opencode_benchmark_campaign_outcome o
        join models.opencode_benchmark_campaign_member_result r
          on r.realm_id = o.realm_id and r.campaign_id = o.campaign_id
        where o.realm_id = new.realm_id and o.id = new.outcome_id
          and o.status in ('passed', 'failed') and r.member_id = new.member_id
          and r.status = 'passed' and r.aggregate_id = new.aggregate_id
    ) then
        raise exception 'OpenCode qualification terminal outcome/passed aggregate ister'
            using errcode = '42501';
    end if;
    if new.action = 'disqualified' and not exists (
        select 1
        from models.opencode_benchmark_campaign_outcome o
        join models.opencode_benchmark_campaign_member_result r
          on r.realm_id = o.realm_id and r.campaign_id = o.campaign_id
        where o.realm_id = new.realm_id and o.id = new.outcome_id
          and o.status = 'failed' and r.member_id = new.member_id
          and r.status = 'failed' and r.aggregate_id is null
          and r.failure_category = new.reason_code
    ) then
        raise exception 'OpenCode disqualification failed member result ister'
            using errcode = '42501';
    end if;
    if new.aggregate_id is not null and not exists (
        select 1 from models.opencode_benchmark_campaign_member_result r
        where r.realm_id = new.realm_id and r.campaign_id = new.campaign_id
          and r.member_id = new.member_id and r.stage = 'benchmark'
          and r.aggregate_id = new.aggregate_id
    ) then
        raise exception 'OpenCode qualification aggregate/member mismatch'
            using errcode = '42501';
    end if;
    return new;
end
$$;

create trigger opencode_campaign_binding
    before insert on models.opencode_benchmark_campaign
    for each row execute function models.enforce_opencode_campaign_binding();
create trigger opencode_campaign_member_budget
    before insert on models.opencode_benchmark_campaign_member
    for each row execute function models.enforce_opencode_campaign_member_budget();
create trigger opencode_campaign_member_plan_binding
    before insert on models.opencode_benchmark_campaign_member_plan
    for each row execute function models.enforce_opencode_member_plan_binding();
create trigger opencode_campaign_member_result_binding
    before insert on models.opencode_benchmark_campaign_member_result
    for each row execute function models.enforce_opencode_member_result_binding();
create trigger opencode_campaign_outcome_binding
    before insert on models.opencode_benchmark_campaign_outcome
    for each row execute function models.enforce_opencode_campaign_outcome();
create trigger opencode_qualification_binding
    before insert on models.opencode_model_qualification_event
    for each row execute function models.enforce_opencode_qualification();

create index opencode_campaign_latest_idx
    on models.opencode_benchmark_campaign (realm_id, campaign_key, revision desc);
create index opencode_campaign_member_idx
    on models.opencode_benchmark_campaign_member (realm_id, campaign_id, disposition);
create index opencode_qualification_latest_idx
    on models.opencode_model_qualification_event (realm_id, model_id, occurred_at desc, id desc);

do $$
declare target text;
begin
    foreach target in array array[
        'models.opencode_benchmark_campaign',
        'models.opencode_benchmark_campaign_member',
        'models.opencode_benchmark_campaign_member_plan',
        'models.opencode_benchmark_campaign_member_result',
        'models.opencode_benchmark_campaign_outcome',
        'models.opencode_model_qualification_event'
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
            'for each statement execute function core.deny_mutation()',
            target
        );
        execute format(
            'create trigger deny_delete before delete on %s '
            'for each statement execute function core.deny_mutation()',
            target
        );
    end loop;
end
$$;

grant select, insert on
    models.opencode_benchmark_campaign,
    models.opencode_benchmark_campaign_member,
    models.opencode_benchmark_campaign_member_plan,
    models.opencode_benchmark_campaign_member_result,
    models.opencode_benchmark_campaign_outcome,
    models.opencode_model_qualification_event
to zekam_app;

grant execute on function models.valid_campaign_digest_array(text[]) to zekam_app;
grant execute on function models.enforce_opencode_campaign_binding() to zekam_app;
grant execute on function models.enforce_opencode_campaign_member_budget() to zekam_app;
grant execute on function models.enforce_opencode_member_plan_binding() to zekam_app;
grant execute on function models.enforce_opencode_member_result_binding() to zekam_app;
grant execute on function models.enforce_opencode_campaign_outcome() to zekam_app;
grant execute on function models.enforce_opencode_qualification() to zekam_app;

comment on table models.opencode_benchmark_campaign is
    'OpenCode catalog/source/policy revision-bound exact benchmark budget manifest.';
comment on table models.opencode_benchmark_campaign_member_result is
    'Sanitized terminal member evidence; no prompt, response, endpoint or secret payload.';
comment on table models.opencode_model_qualification_event is
    'Append-only result-based model qualification history; it grants no authority.';
