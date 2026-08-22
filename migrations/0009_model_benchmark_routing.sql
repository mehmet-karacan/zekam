-- Model benchmark, route karari, quota observation ve bounded deliberation.
-- Ham fixture, prompt, yanit, endpoint veya credential bu tablolarda tutulmaz.

create table models.benchmark_fixture_registry (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    schema_version integer not null,
    fixtures jsonb not null,
    registry_digest text not null,
    created_at timestamptz not null default now(),
    constraint benchmark_fixture_registry_unique unique (realm_id, registry_digest),
    constraint benchmark_fixture_registry_version check (schema_version >= 1),
    constraint benchmark_fixture_registry_fixtures check (jsonb_typeof(fixtures) = 'array')
);

create table models.benchmark_suite (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    suite_id text not null,
    suite_version integer not null,
    suite_kind text not null,
    project_id text,
    capability_profile_digest text,
    fixture_digests text[] not null,
    fixture_registry_digest text not null,
    suite_digest text not null,
    created_at timestamptz not null default now(),
    constraint benchmark_suite_realm_id_unique unique (realm_id, id),
    constraint benchmark_suite_registry_same_realm
        foreign key (realm_id, fixture_registry_digest)
        references models.benchmark_fixture_registry (realm_id, registry_digest)
        on delete restrict,
    constraint benchmark_suite_unique unique (realm_id, suite_digest),
    constraint benchmark_suite_version_positive check (suite_version >= 1),
    constraint benchmark_suite_kind check (suite_kind in ('general', 'project')),
    constraint benchmark_suite_project_binding check (
        (suite_kind = 'project' and project_id is not null and capability_profile_digest is not null)
        or (suite_kind = 'general' and project_id is null and capability_profile_digest is null)
    ),
    constraint benchmark_suite_digest_format check (suite_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint benchmark_suite_capability_digest_format check (
        capability_profile_digest is null or capability_profile_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create table models.benchmark_plan (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    suite_id uuid not null,
    model_id text not null,
    repetitions integer not null,
    inventory_digest text not null,
    policy_digest text not null,
    fixture_registry_digest text not null,
    plan_digest text not null,
    remote_execution boolean not null default false,
    state text not null default 'prepared',
    created_at timestamptz not null default now(),
    constraint benchmark_plan_realm_id_unique unique (realm_id, id),
    constraint benchmark_plan_suite_same_realm
        foreign key (realm_id, suite_id) references models.benchmark_suite (realm_id, id)
        on delete restrict,
    constraint benchmark_plan_registry_same_realm
        foreign key (realm_id, fixture_registry_digest)
        references models.benchmark_fixture_registry (realm_id, registry_digest)
        on delete restrict,
    constraint benchmark_plan_unique unique (realm_id, plan_digest),
    constraint benchmark_repetitions_minimum check (repetitions >= 5),
    constraint benchmark_plan_state check (state in ('prepared', 'running', 'passed', 'failed', 'stale')),
    constraint benchmark_plan_digest_format check (plan_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table models.benchmark_verifier_result (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    claim_id uuid not null references runtime.effect_claim (id) on delete restrict,
    tested_model_id text not null,
    verifier_model_id text not null,
    execution_identity text not null,
    tested_response_digest text not null,
    approved boolean not null,
    evidence_digest text not null,
    created_at timestamptz not null default now(),
    constraint benchmark_verifier_result_unique unique (realm_id, claim_id),
    constraint benchmark_verifier_result_independent check (tested_model_id <> verifier_model_id),
    constraint benchmark_verifier_result_identity check (length(btrim(execution_identity)) > 0)
);

create table models.benchmark_trial (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    plan_id uuid not null,
    tested_claim_id uuid not null references runtime.effect_claim (id) on delete restrict,
    verifier_claim_id uuid not null references runtime.effect_claim (id) on delete restrict,
    tested_model_id text not null,
    verifier_model_id text not null,
    verifier_execution_identity text not null,
    verifier_provenance_digest text not null,
    verifier_evidence_digest text not null,
    fixture_digest text not null,
    repetition integer not null,
    status text not null,
    parse_ok boolean not null,
    format_ok boolean not null,
    evidence_ok boolean not null,
    verifier_approved boolean not null,
    quality double precision not null,
    reliability double precision not null,
    latency_ms integer not null,
    input_tokens integer not null,
    output_tokens integer not null,
    retry_count integer not null,
    human_corrections integer not null,
    estimated_cost double precision not null,
    actual_cost double precision,
    response_digest text not null,
    evidence_digest text not null,
    failure_category text,
    observed_at timestamptz not null default now(),
    constraint benchmark_trial_plan_same_realm
        foreign key (realm_id, plan_id) references models.benchmark_plan (realm_id, id)
        on delete restrict,
    constraint benchmark_trial_repetition_unique
        unique (realm_id, plan_id, fixture_digest, repetition),
    constraint benchmark_trial_tested_claim_unique unique (realm_id, tested_claim_id),
    constraint benchmark_trial_verifier_claim_unique unique (realm_id, verifier_claim_id),
    constraint benchmark_trial_verifier_result_fk
        foreign key (realm_id, verifier_claim_id)
        references models.benchmark_verifier_result (realm_id, claim_id) on delete restrict,
    constraint benchmark_trial_distinct_claims check (tested_claim_id <> verifier_claim_id),
    constraint benchmark_trial_independent_verifier check (tested_model_id <> verifier_model_id),
    constraint benchmark_trial_verifier_identity check (
        length(btrim(verifier_execution_identity)) > 0
        and verifier_provenance_digest ~ '^sha256:[0-9a-f]{64}$'
        and verifier_evidence_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    constraint benchmark_trial_status check (status in ('passed', 'failed', 'unsafe', 'timeout')),
    constraint benchmark_trial_score_range check (quality between 0 and 1 and reliability between 0 and 1),
    constraint benchmark_trial_non_negative check (
        latency_ms >= 0 and input_tokens >= 0 and output_tokens >= 0 and retry_count >= 0
        and human_corrections >= 0 and estimated_cost >= 0
        and (actual_cost is null or actual_cost >= 0)
    ),
    constraint benchmark_trial_failure_category check (
        (status = 'passed' and failure_category is null)
        or (status <> 'passed' and failure_category is not null)
    )
);

create table models.benchmark_aggregate (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    plan_id uuid not null,
    tested_model_id text not null,
    verifier_model_id text not null,
    verifier_execution_identity text not null,
    verifier_provenance_digest text not null,
    approved boolean not null,
    unsafe boolean not null,
    metrics jsonb not null,
    evidence_digest text not null,
    created_at timestamptz not null default now(),
    constraint benchmark_aggregate_plan_same_realm
        foreign key (realm_id, plan_id) references models.benchmark_plan (realm_id, id)
        on delete restrict,
    constraint benchmark_aggregate_unique unique (realm_id, plan_id),
    constraint benchmark_verifier_independent check (tested_model_id <> verifier_model_id),
    constraint benchmark_unsafe_not_approved check (not unsafe or not approved)
);

create table models.quota_observation (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    quota_pool text not null,
    trust text not null,
    remaining_ratio double precision,
    source_digest text,
    observed_at timestamptz not null,
    constraint quota_pool_allowed check (quota_pool in ('codex', 'claude', 'local')),
    constraint quota_trust_allowed check (trust in ('trusted', 'unknown')),
    constraint quota_unknown_no_guess check (
        (trust = 'unknown' and remaining_ratio is null and source_digest is null)
        or (trust = 'trusted' and remaining_ratio between 0 and 1 and source_digest is not null)
    )
);

create table models.model_quota_pool_binding (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    model_id text not null,
    quota_pool text not null,
    evidence_digest text not null,
    created_at timestamptz not null default now(),
    constraint quota_pool_binding_model_exists
        foreign key (realm_id, model_id) references models.model_inventory (realm_id, model_id)
        on delete cascade,
    constraint quota_pool_binding_unique unique (realm_id, model_id),
    constraint quota_pool_binding_allowed check (quota_pool in ('codex', 'claude', 'local'))
);

create table models.model_decision (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    selected_model_id text,
    selected_score double precision,
    candidates jsonb not null,
    rejected jsonb not null,
    evidence_digest text not null,
    authority_granted boolean not null default false,
    decided_at timestamptz not null default now(),
    constraint model_decision_no_authority check (authority_granted = false)
);

create table models.runtime_observation (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    model_id text not null,
    workload text not null,
    outcome text not null,
    latency_ms integer not null,
    input_tokens integer not null,
    output_tokens integer not null,
    cost double precision not null,
    human_corrections integer not null,
    evidence_digest text not null,
    authority_granted boolean not null default false,
    observed_at timestamptz not null,
    constraint runtime_observation_outcome check (outcome in ('succeeded', 'failed', 'corrected')),
    constraint runtime_observation_non_negative check (
        latency_ms >= 0 and input_tokens >= 0 and output_tokens >= 0
        and cost >= 0 and human_corrections >= 0
    ),
    constraint runtime_observation_no_authority check (authority_granted = false)
);

create table models.deliberation_result (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    question_digest text not null,
    evidence_packet_digest text not null,
    max_rounds integer not null,
    max_seconds integer not null,
    max_tokens integer not null,
    max_cost double precision not null,
    max_evidence_items integer not null,
    consensus_digests text[] not null,
    contradiction_digests text[] not null,
    synthesizer_identity text not null,
    review_required boolean not null,
    authority_granted boolean not null default false,
    created_at timestamptz not null default now(),
    constraint deliberation_round_limit check (max_rounds between 1 and 2),
    constraint deliberation_time_limit check (max_seconds between 1 and 600),
    constraint deliberation_positive_budget check (max_tokens >= 1 and max_cost >= 0 and max_evidence_items >= 1),
    constraint deliberation_no_authority check (authority_granted = false)
);

create function models.enforce_benchmark_claim_realm() returns trigger
language plpgsql security invoker set search_path = pg_catalog, runtime, core as $$
declare
    expected_plan_digest text;
    expected_effect_digest text;
    expected_verifier_effect_digest text;
    plan_model_id text;
begin
    select plan_digest, model_id into expected_plan_digest, plan_model_id
    from models.benchmark_plan where id = new.plan_id and realm_id = new.realm_id;
    expected_effect_digest := encode(
        public.digest(
            convert_to(
                expected_plan_digest || ':' || new.fixture_digest || ':' || new.repetition::text,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    );
    expected_verifier_effect_digest := encode(
        public.digest(
            convert_to(
                expected_plan_digest || ':' || new.fixture_digest || ':' || new.repetition::text
                || ':verifier:' || new.verifier_model_id || ':' || new.response_digest,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    );
    if new.tested_model_id <> plan_model_id then
        raise exception 'benchmark tested model plan mismatch' using errcode = '42501';
    end if;
    if not exists (
        select 1
        from runtime.effect_claim c
        join runtime.effect_receipt r on r.claim_id = c.id and r.realm_id = c.realm_id
        where c.id = new.tested_claim_id
          and c.realm_id = new.realm_id
          and c.operation = 'model-benchmark-tested'
          and c.effect_digest = 'sha256:' || expected_effect_digest
          and r.status = 'completed'
          and r.result_digest = new.response_digest
    ) then
        raise exception 'benchmark tested claim/receipt exact evidence mismatch' using errcode = '42501';
    end if;
    if not exists (
        select 1 from models.benchmark_verifier_result vr
        where vr.realm_id = new.realm_id
          and vr.claim_id = new.verifier_claim_id
          and vr.tested_model_id = new.tested_model_id
          and vr.verifier_model_id = new.verifier_model_id
          and vr.execution_identity = new.verifier_execution_identity
          and vr.tested_response_digest = new.response_digest
          and vr.approved = new.verifier_approved
          and vr.evidence_digest = new.verifier_evidence_digest
    ) then
        raise exception 'benchmark verifier canonical result mismatch' using errcode = '42501';
    end if;
    if not exists (
        select 1
        from runtime.effect_claim c
        join runtime.effect_receipt r on r.claim_id = c.id and r.realm_id = c.realm_id
        where c.id = new.verifier_claim_id
          and c.realm_id = new.realm_id
          and c.operation = 'model-benchmark-verifier'
          and c.effect_digest = 'sha256:' || expected_verifier_effect_digest
          and c.adapter_digest = new.verifier_provenance_digest
          and r.status = 'completed'
          and r.result_digest = new.verifier_evidence_digest
    ) then
        raise exception 'benchmark verifier claim/receipt exact evidence mismatch'
            using errcode = '42501';
    end if;
    return new;
end
$$;

create trigger benchmark_trial_claim_realm
    before insert on models.benchmark_trial
    for each row execute function models.enforce_benchmark_claim_realm();

do $$
declare target text;
begin
    foreach target in array array[
        'models.benchmark_fixture_registry', 'models.benchmark_suite',
        'models.benchmark_plan', 'models.benchmark_verifier_result', 'models.benchmark_trial',
        'models.benchmark_aggregate', 'models.quota_observation',
        'models.model_quota_pool_binding', 'models.model_decision',
        'models.runtime_observation', 'models.deliberation_result'
    ] loop
        execute format('alter table %s enable row level security', target);
        execute format('alter table %s force row level security', target);
        execute format('create policy scope_select on %s for select using (realm_id = core.current_realm_id())', target);
        execute format('create policy scope_insert on %s for insert with check (realm_id = core.current_realm_id())', target);
        execute format('create trigger deny_update before update on %s for each statement execute function core.deny_mutation()', target);
        execute format('create trigger deny_delete before delete on %s for each statement execute function core.deny_mutation()', target);
    end loop;
end
$$;

grant select, insert on models.benchmark_fixture_registry, models.benchmark_suite,
    models.benchmark_plan, models.benchmark_verifier_result, models.benchmark_trial,
    models.benchmark_aggregate, models.quota_observation, models.model_quota_pool_binding,
    models.model_decision,
    models.runtime_observation, models.deliberation_result to zekam_app;
grant execute on function models.enforce_benchmark_claim_realm() to zekam_app;

comment on table models.benchmark_trial is
    'Provider receipt claim''ine bagli metric/provenance; ham prompt veya yanit tasimaz.';
comment on table models.model_decision is
    'Aciklanabilir model assignment kaniti; authority veya mutation approval degildir.';
