-- Layered general/workload/project model routing v2.
-- Durable rows contain identifiers, digests and metrics only; never raw prompts,
-- responses, endpoints, credentials or external source paths.

create function models.valid_routing_text_array(values_ text[])
returns boolean language sql immutable strict as $$
    select count(*) = cardinality(values_)
       and count(distinct value) = cardinality(values_)
       and coalesce(bool_and(length(btrim(value)) between 1 and 256), true)
    from unnest(values_) item(value)
$$;

create function models.valid_routing_digest_array(values_ text[])
returns boolean language sql immutable strict as $$
    select cardinality(values_) > 0
       and count(*) = cardinality(values_)
       and count(distinct value) = cardinality(values_)
       and bool_and(value ~ '^sha256:[0-9a-f]{64}$')
    from unnest(values_) item(value)
$$;

create table projects.routing_context_snapshot (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    project_id uuid not null,
    source_revision_id uuid not null references projects.source_revision (id) on delete restrict,
    source_revision text not null,
    tree_digest text not null,
    capability_profile_digest text not null,
    dependency_digest text not null,
    framework_digest text not null,
    technology_digest text not null,
    architecture_digest text not null,
    rules_digest text not null,
    suite_digest text not null,
    inventory_digest text not null,
    policy_digest text not null,
    context_digest text not null,
    captured_at timestamptz not null,
    expires_at timestamptz not null,
    constraint routing_context_realm_id_unique unique (realm_id, id),
    constraint routing_context_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint routing_context_digest_unique unique (realm_id, context_digest),
    constraint routing_context_source_safe check (
        length(btrim(source_revision)) between 1 and 128
        and position('://' in source_revision) = 0
        and position(E'\\' in source_revision) = 0
    ),
    constraint routing_context_expiry check (expires_at > captured_at),
    constraint routing_context_digests check (
        tree_digest ~ '^sha256:[0-9a-f]{64}$'
        and capability_profile_digest ~ '^sha256:[0-9a-f]{64}$'
        and dependency_digest ~ '^sha256:[0-9a-f]{64}$'
        and framework_digest ~ '^sha256:[0-9a-f]{64}$'
        and technology_digest ~ '^sha256:[0-9a-f]{64}$'
        and architecture_digest ~ '^sha256:[0-9a-f]{64}$'
        and rules_digest ~ '^sha256:[0-9a-f]{64}$'
        and suite_digest ~ '^sha256:[0-9a-f]{64}$'
        and inventory_digest ~ '^sha256:[0-9a-f]{64}$'
        and policy_digest ~ '^sha256:[0-9a-f]{64}$'
        and context_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create function projects.enforce_routing_context_binding() returns trigger
language plpgsql security invoker set search_path = pg_catalog, projects, core as $$
declare
    bound_project uuid;
    recorded_revision text;
    recorded_tree text;
    recorded_profile text;
begin
    select b.project_id, r.revision, r.tree_digest
      into bound_project, recorded_revision, recorded_tree
      from projects.source_revision r
      join projects.source_binding b
        on b.realm_id = r.realm_id and b.id = r.binding_id
     where r.realm_id = new.realm_id and r.id = new.source_revision_id;
    if bound_project is distinct from new.project_id
       or recorded_revision is distinct from new.source_revision
       or recorded_tree is distinct from new.tree_digest then
        raise exception 'routing context source/project binding mismatch' using errcode = '42501';
    end if;
    select p.profile_digest into recorded_profile
      from projects.capability_profile p
     where p.realm_id = new.realm_id and p.project_id = new.project_id
       and p.source_revision_id = new.source_revision_id
     order by p.generated_at desc limit 1;
    if recorded_profile is distinct from new.capability_profile_digest then
        raise exception 'routing context capability binding mismatch' using errcode = '42501';
    end if;
    return new;
end
$$;

create trigger routing_context_binding
    before insert on projects.routing_context_snapshot
    for each row execute function projects.enforce_routing_context_binding();

create table models.routing_role_policy (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    role text not null,
    target_layer text not null,
    required_layers text[] not null,
    top_k integer not null,
    fallback_model_ids text[] not null default '{}',
    max_cost double precision not null,
    max_latency_ms double precision not null,
    independent_from_roles text[] not null default '{}',
    policy_digest text not null,
    effective_from timestamptz not null,
    expires_at timestamptz,
    created_at timestamptz not null default now(),
    constraint routing_role_policy_realm_id_unique unique (realm_id, id),
    constraint routing_role_policy_digest_unique unique (realm_id, policy_digest),
    constraint routing_role_policy_role check (
        role in ('implementer', 'reviewer', 'researcher', 'verifier')
    ),
    constraint routing_role_policy_target check (
        target_layer in ('general', 'workload-technology', 'project')
    ),
    constraint routing_role_policy_layers check (
        (target_layer = 'general' and required_layers = array['general']::text[])
        or (target_layer = 'workload-technology'
            and required_layers = array['general','workload-technology']::text[])
        or (target_layer = 'project'
            and required_layers = array['general','workload-technology','project']::text[])
    ),
    constraint routing_role_policy_top_k check (top_k between 1 and 20),
    constraint routing_role_policy_budget check (max_cost >= 0 and max_latency_ms >= 0),
    constraint routing_role_policy_fallback_unique check (
        models.valid_routing_text_array(fallback_model_ids)
    ),
    constraint routing_role_policy_independence check (
        not (role = any(independent_from_roles))
        and independent_from_roles <@ array['implementer','reviewer','researcher','verifier']::text[]
    ),
    constraint routing_role_policy_digest check (policy_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint routing_role_policy_expiry check (expires_at is null or expires_at > effective_from)
);

create table models.execution_target_snapshot (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    client_id text not null,
    slot text not null,
    execution_mode text not null,
    model_selectable boolean not null,
    structured_result boolean not null,
    cancellation boolean not null,
    max_concurrency integer not null,
    cost_evidence_digest text not null,
    capability_digest text not null,
    snapshot_digest text not null,
    captured_at timestamptz not null,
    expires_at timestamptz not null,
    constraint execution_target_realm_id_unique unique (realm_id, id),
    constraint execution_target_digest_unique unique (realm_id, snapshot_digest),
    constraint execution_target_metadata check (
        length(btrim(client_id)) between 1 and 128
        and length(btrim(slot)) between 1 and 128
        and execution_mode in ('native-parallel','native-sequential','isolated-role-fallback')
        and max_concurrency >= 1
    ),
    constraint execution_target_digests check (
        cost_evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and capability_digest ~ '^sha256:[0-9a-f]{64}$'
        and snapshot_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    constraint execution_target_expiry check (expires_at > captured_at)
);

create table models.routing_suite_binding (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    benchmark_suite_id uuid not null references models.benchmark_suite (id) on delete restrict,
    suite_digest text not null,
    layer text not null,
    role text not null,
    workload text,
    technology text,
    project_context_id uuid,
    binding_digest text not null,
    created_at timestamptz not null default now(),
    constraint routing_suite_binding_realm_id_unique unique (realm_id, id),
    constraint routing_suite_binding_context_same_realm
        foreign key (realm_id, project_context_id)
        references projects.routing_context_snapshot (realm_id, id) on delete restrict,
    constraint routing_suite_binding_digest_unique unique (realm_id, binding_digest),
    constraint routing_suite_binding_scope check (
        (layer = 'general' and workload is null and technology is null
            and project_context_id is null)
        or (layer = 'workload-technology' and btrim(workload) <> ''
            and btrim(technology) <> '' and project_context_id is null)
        or (layer = 'project' and btrim(workload) <> ''
            and btrim(technology) <> '' and project_context_id is not null)
    ),
    constraint routing_suite_binding_role check (
        role in ('implementer', 'reviewer', 'researcher', 'verifier')
    ),
    constraint routing_suite_binding_digests check (
        suite_digest ~ '^sha256:[0-9a-f]{64}$'
        and binding_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create function models.enforce_routing_suite_binding() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, projects, core as $$
declare
    actual_suite text;
    actual_suite_id text;
    actual_kind text;
    actual_project text;
    actual_capability text;
    context_project uuid;
    context_capability text;
    context_suite text;
begin
    select suite_digest, suite_id, suite_kind, project_id, capability_profile_digest
      into actual_suite, actual_suite_id, actual_kind, actual_project, actual_capability
      from models.benchmark_suite
     where realm_id = new.realm_id and id = new.benchmark_suite_id;
    if actual_suite is distinct from new.suite_digest then
        raise exception 'routing suite digest/realm mismatch' using errcode = '42501';
    end if;
    if new.layer = 'project' then
        select project_id, capability_profile_digest, suite_digest
          into context_project, context_capability, context_suite
          from projects.routing_context_snapshot
         where realm_id = new.realm_id and id = new.project_context_id;
        if actual_kind is distinct from 'project'
           or actual_project is distinct from context_project::text
           or actual_capability is distinct from context_capability
           or actual_suite is distinct from context_suite then
            raise exception 'project routing suite/context mismatch' using errcode = '42501';
        end if;
    elsif actual_kind is distinct from 'general' then
        raise exception 'general/workload routing requires general suite'
            using errcode = '42501';
    elsif new.layer = 'workload-technology'
       and actual_suite_id is distinct from
            'workload:' || new.workload || ':' || new.technology then
        raise exception 'workload routing suite label mismatch' using errcode = '42501';
    end if;
    return new;
end
$$;

create trigger routing_suite_binding_guard
    before insert on models.routing_suite_binding
    for each row execute function models.enforce_routing_suite_binding();

create table models.model_routing_qualification (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    model_id text not null,
    suite_binding_id uuid not null,
    aggregate_id uuid not null references models.benchmark_aggregate (id) on delete restrict,
    aggregate_evidence_digest text not null,
    health_result_id uuid not null references models.opencode_benchmark_campaign_member_result (id)
        on delete restrict,
    health_evidence_digest text not null,
    inventory_digest text not null,
    policy_digest text not null,
    verifier_model_id text not null,
    verifier_execution_identity text not null,
    tested_execution_identity text not null,
    score double precision not null,
    mean_latency_ms double precision not null,
    mean_cost double precision not null,
    qualified boolean not null,
    unsafe boolean not null,
    evidence_digest text not null,
    valid_from timestamptz not null,
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    constraint model_routing_qualification_realm_id_unique unique (realm_id, id),
    constraint model_routing_qualification_suite_same_realm
        foreign key (realm_id, suite_binding_id)
        references models.routing_suite_binding (realm_id, id) on delete restrict,
    constraint model_routing_qualification_model_same_realm
        foreign key (realm_id, model_id)
        references models.model_inventory (realm_id, model_id) on delete restrict,
    constraint model_routing_qualification_digest_unique unique (realm_id, evidence_digest),
    constraint model_routing_qualification_score check (
        score between 0 and 1 and mean_latency_ms >= 0 and mean_cost >= 0
    ),
    constraint model_routing_qualification_safety check (not unsafe or not qualified),
    constraint model_routing_qualification_independence check (
        model_id <> verifier_model_id
        and tested_execution_identity <> verifier_execution_identity
    ),
    constraint model_routing_qualification_expiry check (expires_at > valid_from),
    constraint model_routing_qualification_digests check (
        aggregate_evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and health_evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and inventory_digest ~ '^sha256:[0-9a-f]{64}$'
        and policy_digest ~ '^sha256:[0-9a-f]{64}$'
        and evidence_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create function models.enforce_routing_qualification() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, core as $$
declare
    tested text;
    verifier text;
    verifier_identity text;
    approved_ boolean;
    unsafe_ boolean;
    evidence text;
    health_bound boolean;
    bound_suite uuid;
    aggregate_suite uuid;
    aggregate_metrics jsonb;
    tested_adapter_digests text[];
begin
    select tested_model_id, verifier_model_id, verifier_execution_identity,
           approved, unsafe, evidence_digest
      into tested, verifier, verifier_identity, approved_, unsafe_, evidence
      from models.benchmark_aggregate
     where realm_id = new.realm_id and id = new.aggregate_id;
    if tested is distinct from new.model_id
       or verifier is distinct from new.verifier_model_id
       or verifier_identity is distinct from new.verifier_execution_identity
       or evidence is distinct from new.aggregate_evidence_digest
       or approved_ is distinct from new.qualified
       or unsafe_ is distinct from new.unsafe then
        raise exception 'routing qualification aggregate drift' using errcode = '42501';
    end if;
    select benchmark_suite_id into bound_suite
      from models.routing_suite_binding
     where realm_id=new.realm_id and id=new.suite_binding_id;
    select p.suite_id, a.metrics into aggregate_suite, aggregate_metrics
      from models.benchmark_aggregate a
      join models.benchmark_plan p on p.realm_id=a.realm_id and p.id=a.plan_id
     where a.realm_id=new.realm_id and a.id=new.aggregate_id;
    if bound_suite is distinct from aggregate_suite
       or new.score is distinct from (
            ((aggregate_metrics->'quality'->>'mean')::double precision
             + (aggregate_metrics->'reliability'->>'mean')::double precision) / 2.0
       )
       or new.mean_latency_ms is distinct from
            (aggregate_metrics->'latency_ms'->>'mean')::double precision
       or new.mean_cost is distinct from
            (aggregate_metrics->'cost'->>'mean')::double precision then
        raise exception 'routing qualification suite/metric drift' using errcode = '42501';
    end if;
    select array_agg(distinct ec.adapter_digest order by ec.adapter_digest)
      into tested_adapter_digests
      from models.benchmark_trial t
      join runtime.effect_claim ec on ec.realm_id=t.realm_id and ec.id=t.tested_claim_id
     where t.realm_id=new.realm_id
       and t.plan_id=(select plan_id from models.benchmark_aggregate
                       where realm_id=new.realm_id and id=new.aggregate_id);
    if cardinality(tested_adapter_digests) is distinct from 1
       or new.tested_execution_identity is distinct from
            'provider-adapter:' || tested_adapter_digests[1] then
        raise exception 'routing qualification tested execution drift' using errcode = '42501';
    end if;
    select exists (
        select 1 from models.opencode_benchmark_campaign_member_result h
          join models.opencode_benchmark_campaign_member m
            on m.realm_id=h.realm_id and m.campaign_id=h.campaign_id and m.id=h.member_id
         where h.realm_id=new.realm_id and h.id=new.health_result_id
           and h.stage='health' and h.status='passed'
           and h.evidence_digest=new.health_evidence_digest
           and m.canonical_model_id=new.model_id
    ) into health_bound;
    if not health_bound then
        raise exception 'routing qualification health evidence mismatch' using errcode = '42501';
    end if;
    return new;
end
$$;

create trigger routing_qualification_guard
    before insert on models.model_routing_qualification
    for each row execute function models.enforce_routing_qualification();

create table models.model_route_decision (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    role_policy_id uuid not null,
    execution_target_id uuid,
    project_id uuid,
    project_context_id uuid,
    role text not null,
    target_layer text not null,
    workload text,
    technology text,
    inventory_digest text not null,
    routing_policy_digest text not null,
    policy_digest text not null,
    execution_target_digest text not null,
    excluded_model_ids text[] not null default '{}',
    excluded_execution_identities text[] not null default '{}',
    status text not null,
    primary_model_id text,
    fallback_model_id text,
    evidence_digest text not null,
    authority_granted boolean not null default false,
    decided_at timestamptz not null,
    constraint model_route_decision_realm_id_unique unique (realm_id, id),
    constraint model_route_decision_policy_same_realm
        foreign key (realm_id, role_policy_id)
        references models.routing_role_policy (realm_id, id) on delete restrict,
    constraint model_route_decision_execution_same_realm
        foreign key (realm_id, execution_target_id)
        references models.execution_target_snapshot (realm_id, id) on delete restrict,
    constraint model_route_decision_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint model_route_decision_context_same_realm
        foreign key (realm_id, project_context_id)
        references projects.routing_context_snapshot (realm_id, id) on delete restrict,
    constraint model_route_decision_digest_unique unique (realm_id, evidence_digest),
    constraint model_route_decision_status check (status in ('selected','pending')),
    constraint model_route_decision_selection check (
        (status = 'pending' and primary_model_id is null and fallback_model_id is null)
        or (status = 'selected' and primary_model_id is not null
            and fallback_model_id is distinct from primary_model_id
            and execution_target_id is not null)
    ),
    constraint model_route_decision_scope check (
        (target_layer = 'general' and workload is null and technology is null
            and project_id is null and project_context_id is null)
        or (target_layer = 'workload-technology' and btrim(workload) <> ''
            and btrim(technology) <> '' and project_id is null and project_context_id is null)
        or (target_layer = 'project' and btrim(workload) <> '' and btrim(technology) <> ''
            and project_id is not null and project_context_id is not null)
    ),
    constraint model_route_decision_role check (
        role in ('implementer', 'reviewer', 'researcher', 'verifier')
    ),
    constraint model_route_decision_digests check (
        inventory_digest ~ '^sha256:[0-9a-f]{64}$'
        and routing_policy_digest ~ '^sha256:[0-9a-f]{64}$'
        and policy_digest ~ '^sha256:[0-9a-f]{64}$'
        and execution_target_digest ~ '^sha256:[0-9a-f]{64}$'
        and evidence_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    constraint model_route_decision_exclusions check (
        models.valid_routing_text_array(excluded_model_ids)
        and models.valid_routing_text_array(excluded_execution_identities)
    ),
    constraint model_route_decision_no_authority check (authority_granted = false)
);

create table models.model_route_candidate (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    decision_id uuid not null,
    model_id text not null,
    disposition text not null,
    score double precision not null,
    layer_scores jsonb not null,
    evidence_digests text[] not null,
    rejection_reasons text[] not null,
    rank integer,
    created_at timestamptz not null default now(),
    constraint model_route_candidate_decision_same_realm
        foreign key (realm_id, decision_id)
        references models.model_route_decision (realm_id, id) on delete restrict,
    constraint model_route_candidate_model_same_realm
        foreign key (realm_id, model_id)
        references models.model_inventory (realm_id, model_id) on delete restrict,
    constraint model_route_candidate_unique unique (realm_id, decision_id, model_id),
    constraint model_route_candidate_disposition check (
        disposition in ('primary','fallback','eligible','rejected')
    ),
    constraint model_route_candidate_score check (score between 0 and 1),
    constraint model_route_candidate_shape check (
        jsonb_typeof(layer_scores) = 'object'
        and models.valid_routing_digest_array(evidence_digests)
        and ((disposition = 'rejected' and cardinality(rejection_reasons) > 0 and rank is null)
          or (disposition <> 'rejected' and cardinality(rejection_reasons) = 0
              and rank is not null))
    ),
    constraint model_route_candidate_rank check (rank is null or rank >= 1)
);

create function models.enforce_route_decision() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, projects, core as $$
declare
    policy_role text;
    policy_layer text;
    policy_digest_ text;
    context_project uuid;
    target_digest text;
begin
    select role, target_layer, policy_digest
      into policy_role, policy_layer, policy_digest_
      from models.routing_role_policy
     where realm_id = new.realm_id and id = new.role_policy_id;
    if policy_role is distinct from new.role or policy_layer is distinct from new.target_layer
       or policy_digest_ is distinct from new.routing_policy_digest then
        raise exception 'route decision role policy drift' using errcode = '42501';
    end if;
    if new.project_context_id is not null then
        select project_id into context_project from projects.routing_context_snapshot
         where realm_id = new.realm_id and id = new.project_context_id;
        if context_project is distinct from new.project_id then
            raise exception 'route decision project context mismatch' using errcode = '42501';
        end if;
    end if;
    select snapshot_digest into target_digest from models.execution_target_snapshot
     where realm_id=new.realm_id and id=new.execution_target_id;
    if target_digest is distinct from new.execution_target_digest then
        raise exception 'route decision execution target drift' using errcode = '42501';
    end if;
    return new;
end
$$;

create trigger route_decision_guard
    before insert on models.model_route_decision
    for each row execute function models.enforce_route_decision();

create unique index model_route_one_primary
    on models.model_route_candidate (realm_id, decision_id) where disposition = 'primary';
create unique index model_route_one_fallback
    on models.model_route_candidate (realm_id, decision_id) where disposition = 'fallback';

create function models.enforce_route_candidate() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, core as $$
declare primary_ text; fallback_ text;
begin
    if exists (select 1 from unnest(new.evidence_digests) value
               where value !~ '^sha256:[0-9a-f]{64}$') then
        raise exception 'route candidate invalid evidence digest' using errcode = '23514';
    end if;
    select primary_model_id, fallback_model_id into primary_, fallback_
      from models.model_route_decision
     where realm_id = new.realm_id and id = new.decision_id;
    if (new.disposition = 'primary' and new.model_id is distinct from primary_)
       or (new.disposition = 'fallback' and new.model_id is distinct from fallback_)
       or (new.model_id = primary_ and new.disposition <> 'primary')
       or (new.model_id = fallback_ and new.disposition <> 'fallback') then
        raise exception 'route candidate decision selection mismatch' using errcode = '42501';
    end if;
    return new;
end
$$;

create trigger route_candidate_guard
    before insert on models.model_route_candidate
    for each row execute function models.enforce_route_candidate();

create view projects.routing_context_current_status
with (security_barrier = true, security_invoker = true) as
select c.*,
       array_remove(array[
           case when now() > c.expires_at then 'expired' end,
           case when latest.revision is distinct from c.source_revision
                then 'source-revision' end,
           case when latest.tree_digest is distinct from c.tree_digest then 'tree' end,
           case when profile.profile_digest is distinct from c.capability_profile_digest
                then 'capability-profile' end
       ], null) as stale_reasons
from projects.routing_context_snapshot c
left join lateral (
    select r.revision, r.tree_digest, r.id
      from projects.source_revision r
      join projects.source_binding b on b.realm_id = r.realm_id and b.id = r.binding_id
     where r.realm_id = c.realm_id and b.project_id = c.project_id
     order by r.observed_at desc, r.id desc limit 1
) latest on true
left join lateral (
    select p.profile_digest from projects.capability_profile p
     where p.realm_id = c.realm_id and p.project_id = c.project_id
       and p.source_revision_id = latest.id
     order by p.generated_at desc, p.id desc limit 1
) profile on true
where c.realm_id = core.current_realm_id();

do $$
declare target text;
begin
    foreach target in array array[
        'projects.routing_context_snapshot',
        'models.routing_role_policy',
        'models.execution_target_snapshot',
        'models.routing_suite_binding',
        'models.model_routing_qualification',
        'models.model_route_decision',
        'models.model_route_candidate'
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

create index routing_context_project_latest_idx
    on projects.routing_context_snapshot (realm_id, project_id, captured_at desc, id desc);
create index routing_policy_latest_idx
    on models.routing_role_policy (realm_id, role, target_layer, effective_from desc, id desc);
create index routing_qualification_lookup_idx
    on models.model_routing_qualification (realm_id, model_id, valid_from desc, id desc);
create index model_route_decision_latest_idx
    on models.model_route_decision (realm_id, role, target_layer, decided_at desc, id desc);

grant select, insert on projects.routing_context_snapshot to zekam_app;
grant select on projects.routing_context_current_status to zekam_app;
grant select, insert on models.routing_role_policy, models.execution_target_snapshot,
    models.routing_suite_binding, models.model_routing_qualification,
    models.model_route_decision, models.model_route_candidate to zekam_app;
grant execute on function projects.enforce_routing_context_binding() to zekam_app;
grant execute on function models.enforce_routing_suite_binding() to zekam_app;
grant execute on function models.enforce_routing_qualification() to zekam_app;
grant execute on function models.enforce_route_decision() to zekam_app;
grant execute on function models.enforce_route_candidate() to zekam_app;
