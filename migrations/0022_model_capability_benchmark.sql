-- Parallel, duration-bounded model capability benchmark ledger.
-- Raw prompts/responses and endpoint/secret values are deliberately absent.

create table models.capability_benchmark_suite (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    registry_digest text not null,
    execution_profile_digest text not null,
    evaluator_provenance_digest text not null,
    task_digests text[] not null,
    task_roles jsonb not null,
    task_budgets jsonb not null,
    task_count integer not null,
    max_duration_seconds integer not null,
    max_model_turns integer not null,
    max_input_tokens integer not null,
    max_output_tokens integer not null,
    max_tool_calls integer not null,
    max_parallelism integer not null,
    suite_digest text not null,
    created_at timestamptz not null default now(),
    constraint capability_suite_realm_id_unique unique (realm_id, id),
    constraint capability_suite_digest_unique unique (realm_id, suite_digest),
    constraint capability_suite_shape check (
        registry_digest ~ '^sha256:[0-9a-f]{64}$'
        and execution_profile_digest ~ '^sha256:[0-9a-f]{64}$'
        and evaluator_provenance_digest ~ '^sha256:[0-9a-f]{64}$'
        and suite_digest ~ '^sha256:[0-9a-f]{64}$'
        and models.valid_routing_digest_array(task_digests)
        and task_count = cardinality(task_digests)
        and task_count >= 3
        and jsonb_typeof(task_roles) = 'object'
        and jsonb_typeof(task_budgets) = 'object'
        and max_duration_seconds between 30 and 300
        and max_model_turns between 1 and 16
        and max_input_tokens >= 256 and max_output_tokens >= 256
        and max_tool_calls between 0 and 64
        and max_parallelism > 0
    )
);

create table models.capability_benchmark_cohort (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    suite_id uuid not null,
    source_campaign_id uuid not null,
    source_revision text not null,
    inventory_digest text not null,
    policy_digest text not null,
    verifier_provenance_digest text not null,
    model_ids text[] not null,
    provider_call_budget integer not null,
    start_skew_budget_ms integer not null,
    plan_digest text not null,
    created_at timestamptz not null default now(),
    constraint capability_cohort_realm_id_unique unique (realm_id, id),
    constraint capability_cohort_suite_same_realm foreign key (realm_id, suite_id)
        references models.capability_benchmark_suite (realm_id, id) on delete restrict,
    constraint capability_cohort_campaign_same_realm foreign key (realm_id, source_campaign_id)
        references models.opencode_benchmark_campaign (realm_id, id) on delete restrict,
    constraint capability_cohort_plan_unique unique (realm_id, plan_digest),
    constraint capability_cohort_shape check (
        length(btrim(source_revision)) between 1 and 128
        and position('://' in source_revision) = 0
        and models.valid_routing_text_array(model_ids)
        and provider_call_budget > 0
        and start_skew_budget_ms between 0 and 2000
        and inventory_digest ~ '^sha256:[0-9a-f]{64}$'
        and policy_digest ~ '^sha256:[0-9a-f]{64}$'
        and verifier_provenance_digest ~ '^sha256:[0-9a-f]{64}$'
        and plan_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create function models.enforce_capability_cohort() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models as $$
declare campaign_revision text; campaign_inventory text; campaign_policy text;
        campaign_verifier text; qualified_models text[]; tasks integer; turns integer;
        parallelism integer;
begin
    select c.source_revision,c.inventory_digest,c.policy_digest,c.verifier_provenance_digest
      into campaign_revision,campaign_inventory,campaign_policy,campaign_verifier
      from models.opencode_benchmark_campaign c
      join models.opencode_benchmark_campaign_outcome o
        on o.realm_id=c.realm_id and o.campaign_id=c.id
     where c.realm_id=new.realm_id and c.id=new.source_campaign_id
       and o.status in ('passed','failed');
    select array_agg(q.model_id order by q.model_id) into qualified_models
      from models.opencode_model_qualification_event q
     where q.realm_id=new.realm_id and q.campaign_id=new.source_campaign_id
       and q.action='qualified';
    select task_count,max_model_turns,max_parallelism into tasks,turns,parallelism
      from models.capability_benchmark_suite
     where realm_id=new.realm_id and id=new.suite_id;
    if campaign_revision is distinct from new.source_revision
       or campaign_inventory is distinct from new.inventory_digest
       or campaign_policy is distinct from new.policy_digest
       or campaign_verifier is distinct from new.verifier_provenance_digest
       or qualified_models is distinct from new.model_ids
       or new.provider_call_budget is distinct from cardinality(new.model_ids)*tasks*turns
       or parallelism is distinct from cardinality(new.model_ids) then
        raise exception 'capability cohort source/budget binding mismatch' using errcode='42501';
    end if;
    return new;
end
$$;

create trigger capability_cohort_binding before insert on models.capability_benchmark_cohort
    for each row execute function models.enforce_capability_cohort();

create table models.capability_benchmark_episode (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    cohort_id uuid not null,
    model_id text not null,
    task_digest text not null,
    role text not null,
    status text not null,
    started_at timestamptz not null,
    duration_ms integer not null,
    start_skew_ms integer not null,
    model_turn_count integer not null,
    input_token_count integer not null,
    output_token_count integer not null,
    correctness double precision not null,
    completion double precision not null,
    sustained_progress double precision not null,
    context_retention double precision not null,
    self_correction double precision not null,
    tool_efficiency double precision not null,
    safety double precision not null,
    hidden_acceptance_ratio double precision not null,
    sustained_progress_auc double precision not null,
    longest_stagnation_ms integer not null,
    regression_count integer not null,
    noop_ratio double precision not null,
    checkpoint_count integer not null,
    self_correction_count integer not null,
    tool_call_count integer not null,
    checkpoint_receipt_digests text[] not null,
    tool_receipt_digests text[] not null,
    response_digest text not null,
    verifier_model_id text not null,
    verifier_execution_identity text not null,
    verifier_provenance_digest text not null,
    evidence_digest text not null,
    acceptance_evidence_digest text not null,
    created_at timestamptz not null default now(),
    constraint capability_episode_cohort_same_realm foreign key (realm_id, cohort_id)
        references models.capability_benchmark_cohort (realm_id, id) on delete restrict,
    constraint capability_episode_model_same_realm foreign key (realm_id, model_id)
        references models.model_inventory (realm_id, model_id) on delete restrict,
    constraint capability_episode_unique unique (realm_id, cohort_id, model_id, task_digest),
    constraint capability_episode_evidence_unique unique (realm_id, evidence_digest),
    constraint capability_episode_shape check (
        role in ('implementer', 'reviewer', 'researcher', 'verifier')
        and status in ('passed', 'failed', 'timeout', 'unsafe', 'infrastructure-invalid', 'not-comparable')
        and duration_ms >= 0 and start_skew_ms >= 0 and model_turn_count > 0
        and input_token_count >= 0 and output_token_count >= 0
        and correctness between 0 and 1 and completion between 0 and 1
        and sustained_progress between 0 and 1 and context_retention between 0 and 1
        and self_correction between 0 and 1 and tool_efficiency between 0 and 1
        and safety between 0 and 1 and hidden_acceptance_ratio between 0 and 1
        and sustained_progress_auc between 0 and 1 and noop_ratio between 0 and 1
        and least(longest_stagnation_ms, regression_count, checkpoint_count,
                  self_correction_count, tool_call_count) >= 0
        and models.valid_routing_digest_array(checkpoint_receipt_digests)
        and (
            (tool_call_count = 0 and cardinality(tool_receipt_digests) = 0)
            or (
                tool_call_count > 0
                and models.valid_routing_digest_array(tool_receipt_digests)
            )
        )
        and cardinality(checkpoint_receipt_digests)=checkpoint_count
        and cardinality(tool_receipt_digests)=tool_call_count
        and task_digest ~ '^sha256:[0-9a-f]{64}$'
        and response_digest ~ '^sha256:[0-9a-f]{64}$'
        and verifier_provenance_digest ~ '^sha256:[0-9a-f]{64}$'
        and evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and acceptance_evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and model_id <> verifier_model_id
    )
);

create table models.capability_benchmark_scorecard (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    cohort_id uuid not null,
    model_id text not null,
    episode_evidence_digests text[] not null,
    general_score double precision not null,
    role_scores jsonb not null,
    completion_rate double precision not null,
    mean_duration_ms double precision not null,
    evidence_digest text not null,
    created_at timestamptz not null default now(),
    constraint capability_scorecard_cohort_same_realm foreign key (realm_id, cohort_id)
        references models.capability_benchmark_cohort (realm_id, id) on delete restrict,
    constraint capability_scorecard_unique unique (realm_id, cohort_id, model_id),
    constraint capability_scorecard_evidence_unique unique (realm_id, evidence_digest),
    constraint capability_scorecard_shape check (
        models.valid_routing_digest_array(episode_evidence_digests)
        and general_score between 0 and 1 and completion_rate between 0 and 1
        and mean_duration_ms >= 0 and jsonb_typeof(role_scores) = 'object'
        and evidence_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create function models.enforce_capability_episode() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models as $$
declare bound_tasks text[]; bound_models text[]; campaign uuid; roles jsonb; budgets jsonb;
        max_duration integer; max_tools integer; skew_budget integer; verifier_digest text;
        max_turns integer; max_input integer; max_output integer; task_budget jsonb;
begin
    select s.task_digests,s.task_roles,s.task_budgets,c.model_ids,c.source_campaign_id,
           s.max_duration_seconds,s.max_tool_calls,c.start_skew_budget_ms,
           s.evaluator_provenance_digest,s.max_model_turns,s.max_input_tokens,
           s.max_output_tokens
      into bound_tasks,roles,budgets,bound_models,campaign,max_duration,max_tools,skew_budget,
           verifier_digest,max_turns,max_input,max_output
      from models.capability_benchmark_cohort c
      join models.capability_benchmark_suite s
        on s.realm_id = c.realm_id and s.id = c.suite_id
     where c.realm_id = new.realm_id and c.id = new.cohort_id;
    if not new.task_digest = any(bound_tasks) or not new.model_id = any(bound_models) then
        raise exception 'capability episode cohort scope mismatch' using errcode = '42501';
    end if;
    task_budget := budgets->new.task_digest;
    if roles->>new.task_digest is distinct from new.role or task_budget is null
       or new.verifier_provenance_digest is distinct from verifier_digest
       or new.start_skew_ms > skew_budget or new.model_turn_count > max_turns
       or new.input_token_count > max_input
       or new.output_token_count > least(max_output,(task_budget->>'output_tokens')::integer)
       or new.tool_call_count > least(max_tools,(task_budget->>'tool_calls')::integer)
       or (new.status='passed' and (
            new.duration_ms > (task_budget->>'duration_seconds')::integer*1000
            or new.correctness <> 1
            or new.completion <> 1 or new.hidden_acceptance_ratio <> 1
            or new.safety <> 1 or new.regression_count <> 0
       )) or (new.duration_ms > (task_budget->>'duration_seconds')::integer*1000
              and new.status <> 'timeout') then
        raise exception 'capability episode metric/budget binding mismatch' using errcode='42501';
    end if;
    if not exists (
        select 1 from models.opencode_model_qualification_event q
        where q.realm_id = new.realm_id and q.campaign_id = campaign
          and q.model_id = new.model_id and q.action = 'qualified'
    ) then
        raise exception 'capability episode requires qualified source model' using errcode = '42501';
    end if;
    return new;
end
$$;

create trigger capability_episode_binding before insert on models.capability_benchmark_episode
    for each row execute function models.enforce_capability_episode();

create function models.enforce_capability_scorecard() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models as $$
declare expected_count integer; actual_digests text[]; computed_score double precision;
        computed_completion double precision; computed_duration double precision;
        computed_roles jsonb;
begin
    select s.task_count into expected_count
      from models.capability_benchmark_cohort c
      join models.capability_benchmark_suite s
        on s.realm_id = c.realm_id and s.id = c.suite_id
     where c.realm_id = new.realm_id and c.id = new.cohort_id
       and new.model_id = any(c.model_ids);
    select array_agg(e.evidence_digest order by e.evidence_digest)
      into actual_digests from models.capability_benchmark_episode e
     where e.realm_id = new.realm_id and e.cohort_id = new.cohort_id
       and e.model_id = new.model_id;
    if expected_count is null or cardinality(actual_digests) <> expected_count
       or actual_digests <> (
           select array_agg(value order by value) from unnest(new.episode_evidence_digests) value
       ) then
        raise exception 'capability scorecard exact episode coverage mismatch' using errcode = '42501';
    end if;
    select avg(e.correctness*0.30 + e.completion*0.20 + e.sustained_progress*0.15
                   + e.context_retention*0.15 + e.self_correction*0.10
                   + e.tool_efficiency*0.05 + e.safety*0.05),
           avg(case when e.status='passed' then 1.0 else 0.0 end), avg(e.duration_ms)
      into computed_score,computed_completion,computed_duration
      from models.capability_benchmark_episode e
     where e.realm_id=new.realm_id and e.cohort_id=new.cohort_id
       and e.model_id=new.model_id;
    select jsonb_object_agg(role,score) into computed_roles from (
        select e.role,
               avg(e.correctness*0.30 + e.completion*0.20 + e.sustained_progress*0.15
                   + e.context_retention*0.15 + e.self_correction*0.10
                   + e.tool_efficiency*0.05 + e.safety*0.05) score
          from models.capability_benchmark_episode e
         where e.realm_id=new.realm_id and e.cohort_id=new.cohort_id
           and e.model_id=new.model_id group by e.role
    ) role_scores;
    if abs(new.general_score-computed_score) > 1e-12
       or abs(new.completion_rate-computed_completion) > 1e-12
       or abs(new.mean_duration_ms-computed_duration) > 1e-9
       or new.role_scores is distinct from computed_roles then
        raise exception 'capability scorecard metric forgery' using errcode='42501';
    end if;
    return new;
end
$$;

create trigger capability_scorecard_binding before insert on models.capability_benchmark_scorecard
    for each row execute function models.enforce_capability_scorecard();

do $$ declare target text; begin
    foreach target in array array[
        'models.capability_benchmark_suite',
        'models.capability_benchmark_cohort',
        'models.capability_benchmark_episode',
        'models.capability_benchmark_scorecard'
    ] loop
        execute format('alter table %s enable row level security', target);
        execute format('alter table %s force row level security', target);
        execute format('create policy scope_select on %s for select using (realm_id = core.current_realm_id())', target);
        execute format('create policy scope_insert on %s for insert with check (realm_id = core.current_realm_id())', target);
        execute format('create trigger deny_update before update on %s for each statement execute function core.deny_mutation()', target);
        execute format('create trigger deny_delete before delete on %s for each statement execute function core.deny_mutation()', target);
    end loop;
end $$;

create index capability_cohort_campaign_idx
    on models.capability_benchmark_cohort (realm_id, source_campaign_id, created_at desc);
create index capability_scorecard_model_idx
    on models.capability_benchmark_scorecard (realm_id, model_id, created_at desc);

grant select, insert on models.capability_benchmark_suite,
    models.capability_benchmark_cohort, models.capability_benchmark_episode,
    models.capability_benchmark_scorecard to zekam_app;
grant execute on function models.enforce_capability_episode() to zekam_app;
grant execute on function models.enforce_capability_scorecard() to zekam_app;
grant execute on function models.enforce_capability_cohort() to zekam_app;
