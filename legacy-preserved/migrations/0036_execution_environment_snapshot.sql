-- Sticky execution environment snapshots, live-probe evidence and exact turn binding.

create function runtime.environment_canonical_timestamp(value_ timestamptz) returns text
language sql immutable strict set search_path=pg_catalog,runtime as $$
  select case when extract(microseconds from value_)::bigint % 1000000 = 0
    then to_char(value_ at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"')
    else to_char(value_ at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') end
$$;

create table runtime.execution_environment_snapshot (
    id uuid primary key,
    realm_id uuid not null references core.realm(id) on delete restrict,
    environment_id text not null,
    execution_identity text not null,
    provider text not null,
    platform text not null,
    executor_protocol_version text not null,
    cwd_locator text not null,
    workspace_roots jsonb not null,
    shell jsonb not null,
    permission_profile_id text not null,
    permission_profile_digest text not null,
    filesystem_policy_digest text not null,
    network_policy_digest text not null,
    tool_runtime_digest text not null,
    capability_digest text not null,
    config_effective_digest text not null,
    source_revision text not null,
    captured_at timestamptz not null,
    expires_at timestamptz not null,
    grants_authority boolean not null default false,
    snapshot_digest text not null,
    unique (realm_id, id),
    unique (realm_id, snapshot_digest),
    check (btrim(environment_id) <> '' and btrim(execution_identity) <> ''),
    check (btrim(provider) <> '' and btrim(platform) <> ''),
    check (btrim(executor_protocol_version) <> '' and btrim(permission_profile_id) <> ''),
    check (cwd_locator ~ '^workspace:[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$'),
    check (jsonb_typeof(workspace_roots) = 'array' and jsonb_array_length(workspace_roots) > 0),
    check (jsonb_typeof(shell) = 'object'),
    check (permission_profile_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (filesystem_policy_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (network_policy_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (tool_runtime_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (capability_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (config_effective_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (expires_at > captured_at and grants_authority = false)
);

create function runtime.enforce_environment_snapshot_shape() returns trigger
language plpgsql security invoker set search_path = pg_catalog, runtime as $$
declare roots text[]; shell_keys text[]; expected_digest text;
begin
    select array_agg(value order by value) into roots
      from jsonb_array_elements_text(new.workspace_roots);
    if roots is null or cardinality(roots) <> cardinality(array(select distinct unnest(roots)))
       or to_jsonb(roots) <> new.workspace_roots
       or not (new.cwd_locator = any(roots))
       or exists(select 1 from unnest(roots) r
         where r !~ '^workspace:[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$') then
        raise exception 'environment workspace roots canonical/native locator mismatch'
          using errcode = '23514';
    end if;
    select array_agg(key order by key) into shell_keys from jsonb_object_keys(new.shell) key;
    if shell_keys <> array['binary_digest','kind','startup_profile_digest']
       or btrim(new.shell->>'kind') = ''
       or new.shell->>'binary_digest' !~ '^sha256:[0-9a-f]{64}$'
       or new.shell->>'startup_profile_digest' !~ '^sha256:[0-9a-f]{64}$' then
        raise exception 'environment shell snapshot shape mismatch' using errcode = '23514';
    end if;
    expected_digest := models.capability_runtime_jsonb_digest(jsonb_build_object(
      'schema','zekam-execution-environment-snapshot/v1','id',new.id::text,
      'realm_id',new.realm_id::text,'environment_id',new.environment_id,
      'execution_identity',new.execution_identity,'provider',new.provider,
      'platform',new.platform,'executor_protocol_version',new.executor_protocol_version,
      'cwd_locator',new.cwd_locator,'workspace_roots',new.workspace_roots,'shell',new.shell,
      'permission_profile_id',new.permission_profile_id,
      'permission_profile_digest',new.permission_profile_digest,
      'filesystem_policy_digest',new.filesystem_policy_digest,
      'network_policy_digest',new.network_policy_digest,
      'tool_runtime_digest',new.tool_runtime_digest,'capability_digest',new.capability_digest,
      'config_effective_digest',new.config_effective_digest,
      'source_revision',new.source_revision,
      'captured_at',runtime.environment_canonical_timestamp(new.captured_at),
      'expires_at',runtime.environment_canonical_timestamp(new.expires_at),
      'grants_authority',false));
    if new.snapshot_digest is distinct from expected_digest then
      raise exception 'execution environment canonical digest mismatch' using errcode='23514';
    end if;
    return new;
end
$$;
create trigger execution_environment_shape_guard
before insert on runtime.execution_environment_snapshot
for each row execute function runtime.enforce_environment_snapshot_shape();

create table runtime.environment_probe_evidence (
    id uuid primary key,
    realm_id uuid not null references core.realm(id) on delete restrict,
    execution_identity text not null,
    sticky_snapshot_digest text not null,
    current_snapshot_digest text not null,
    drift_dimensions text[] not null,
    checked_at timestamptz not null,
    evidence_digest text not null,
    unique (realm_id, id),
    unique (realm_id, evidence_digest),
    foreign key (realm_id, sticky_snapshot_digest)
      references runtime.execution_environment_snapshot(realm_id, snapshot_digest),
    foreign key (realm_id, current_snapshot_digest)
      references runtime.execution_environment_snapshot(realm_id, snapshot_digest),
    check (btrim(execution_identity) <> ''),
    check (sticky_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (current_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (evidence_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (drift_dimensions <@ array[
      'environment.workspace-roots-drift','environment.shell-drift',
      'environment.permission-profile-drift','environment.filesystem-policy-drift',
      'environment.network-policy-drift','environment.tool-runtime-drift',
      'environment.capability-drift','environment.config-drift',
      'environment.source-revision-drift']::text[])
);

create function runtime.enforce_environment_probe_evidence() returns trigger
language plpgsql security invoker set search_path = pg_catalog, runtime as $$
declare s record; c record; expected text[] := array[]::text[]; expected_digest text;
begin
  perform pg_advisory_xact_lock(hashtextextended(
    new.realm_id::text || ':' || new.sticky_snapshot_digest,0));
  if exists(select 1 from runtime.environment_probe_evidence p
      where p.realm_id=new.realm_id
        and p.sticky_snapshot_digest=new.sticky_snapshot_digest
        and p.checked_at>=new.checked_at) then
    raise exception 'environment probe checked_at strictly monotonic olmali' using errcode='23514';
  end if;
  select * into s from runtime.execution_environment_snapshot
    where realm_id=new.realm_id and snapshot_digest=new.sticky_snapshot_digest;
  select * into c from runtime.execution_environment_snapshot
    where realm_id=new.realm_id and snapshot_digest=new.current_snapshot_digest;
  if s.execution_identity is distinct from new.execution_identity
     or c.execution_identity is distinct from new.execution_identity then
    raise exception 'environment probe execution identity mismatch' using errcode='23514';
  end if;
  if c.captured_at > new.checked_at or new.checked_at > statement_timestamp()
     or c.expires_at <= new.checked_at
     or c.captured_at < new.checked_at - interval '5 minutes'
     or c.id = s.id then
    raise exception 'environment probe temporal/force provenance mismatch' using errcode='23514';
  end if;
  if s.workspace_roots is distinct from c.workspace_roots then
    expected := array_append(expected,'environment.workspace-roots-drift'); end if;
  if s.shell is distinct from c.shell then
    expected := array_append(expected,'environment.shell-drift'); end if;
  if s.permission_profile_digest is distinct from c.permission_profile_digest then
    expected := array_append(expected,'environment.permission-profile-drift'); end if;
  if s.filesystem_policy_digest is distinct from c.filesystem_policy_digest then
    expected := array_append(expected,'environment.filesystem-policy-drift'); end if;
  if s.network_policy_digest is distinct from c.network_policy_digest then
    expected := array_append(expected,'environment.network-policy-drift'); end if;
  if s.tool_runtime_digest is distinct from c.tool_runtime_digest then
    expected := array_append(expected,'environment.tool-runtime-drift'); end if;
  if s.capability_digest is distinct from c.capability_digest then
    expected := array_append(expected,'environment.capability-drift'); end if;
  if row(s.environment_id,s.provider,s.platform,s.executor_protocol_version,s.cwd_locator)
     is distinct from
     row(c.environment_id,c.provider,c.platform,c.executor_protocol_version,c.cwd_locator)
     and not ('environment.capability-drift'=any(expected)) then
    expected := array_append(expected,'environment.capability-drift'); end if;
  if s.config_effective_digest is distinct from c.config_effective_digest then
    expected := array_append(expected,'environment.config-drift'); end if;
  if s.source_revision is distinct from c.source_revision then
    expected := array_append(expected,'environment.source-revision-drift'); end if;
  select coalesce(array_agg(x order by x),array[]::text[]) into expected from unnest(expected) x;
  if new.drift_dimensions is distinct from expected then
    raise exception 'environment probe drift dimensions forged or incomplete' using errcode='23514';
  end if;
  expected_digest := models.capability_runtime_jsonb_digest(jsonb_build_object(
    'schema','zekam-environment-probe-evidence/v1',
    'sticky_snapshot_digest',new.sticky_snapshot_digest,
    'current_snapshot_digest',new.current_snapshot_digest,
    'drift_dimensions',to_jsonb(new.drift_dimensions),
    'checked_at',runtime.environment_canonical_timestamp(new.checked_at)));
  if new.evidence_digest is distinct from expected_digest then
    raise exception 'environment probe canonical digest mismatch' using errcode='23514';
  end if;
  return new;
end
$$;
create trigger environment_probe_evidence_guard
before insert on runtime.environment_probe_evidence
for each row execute function runtime.enforce_environment_probe_evidence();

create table agents.assignment_environment_binding (
    id uuid primary key,
    realm_id uuid not null references core.realm(id) on delete restrict,
    assignment_id uuid not null,
    execution_environment_snapshot_digest text not null,
    bound_at timestamptz not null,
    grants_authority boolean not null default false,
    binding_digest text not null,
    unique (realm_id, id),
    unique (realm_id, assignment_id),
    unique (realm_id, binding_digest),
    foreign key (realm_id, assignment_id) references agents.assignment(realm_id, id),
    foreign key (realm_id, execution_environment_snapshot_digest)
      references runtime.execution_environment_snapshot(realm_id, snapshot_digest),
    check (execution_environment_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (binding_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (grants_authority = false)
);

create function agents.enforce_assignment_environment_binding() returns trigger
language plpgsql security invoker set search_path=pg_catalog,agents,runtime,models as $$
declare expected_digest text;
begin
  expected_digest := models.capability_runtime_jsonb_digest(jsonb_build_object(
    'schema','zekam-assignment-environment-binding/v1','id',new.id::text,
    'realm_id',new.realm_id::text,'assignment_id',new.assignment_id::text,
    'execution_environment_snapshot_digest',new.execution_environment_snapshot_digest,
    'bound_at',runtime.environment_canonical_timestamp(new.bound_at),'grants_authority',false));
  if new.binding_digest is distinct from expected_digest then
    raise exception 'assignment environment canonical digest mismatch' using errcode='23514';
  end if;
  return new;
end
$$;
create trigger assignment_environment_binding_guard
before insert on agents.assignment_environment_binding
for each row execute function agents.enforce_assignment_environment_binding();

create table runtime.turn_execution_snapshot (
    id uuid primary key,
    realm_id uuid not null references core.realm(id) on delete restrict,
    assignment_id uuid not null,
    run_id uuid not null,
    attempt_id uuid not null,
    client_session_id text not null,
    turn_id text not null,
    model_id text not null,
    provider_id text not null,
    route_decision_digest text not null,
    reasoning_profile_digest text not null,
    execution_environment_snapshot_digest text not null,
    context_manifest_digest text not null,
    exposed_tool_set_digest text not null,
    hook_set_digest text not null,
    config_effective_digest text not null,
    trace_id text,
    grants_authority boolean not null default false,
    created_at timestamptz not null,
    turn_snapshot_digest text not null,
    unique (realm_id, id),
    unique (realm_id, turn_snapshot_digest),
    unique (realm_id, assignment_id, run_id, attempt_id, turn_id),
    foreign key (realm_id, assignment_id) references agents.assignment(realm_id, id),
    foreign key (realm_id, run_id) references runtime.execution_run(realm_id, id),
    foreign key (realm_id, attempt_id) references runtime.job_attempt(realm_id, id),
    foreign key (realm_id, model_id) references models.model_inventory(realm_id, model_id),
    foreign key (realm_id, execution_environment_snapshot_digest)
      references runtime.execution_environment_snapshot(realm_id, snapshot_digest),
    check (btrim(client_session_id) <> '' and btrim(turn_id) <> ''),
    check (btrim(provider_id) <> ''),
    check (route_decision_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (reasoning_profile_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (execution_environment_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (context_manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (exposed_tool_set_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (hook_set_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (config_effective_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (turn_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (trace_id is null or btrim(trace_id) <> ''),
    check (grants_authority = false)
);

create function runtime.enforce_turn_execution_snapshot() returns trigger
language plpgsql security invoker set search_path = pg_catalog, runtime, agents, models as $$
declare a record; r record; ja record; env record; j record; expected_digest text;
begin
    select project_id, work_item_id, context_manifest_digest, status into a
      from agents.assignment where realm_id = new.realm_id and id = new.assignment_id;
    select project_id, work_item_id, state into r
      from runtime.execution_run where realm_id = new.realm_id and id = new.run_id;
    select job_id, outcome into ja
      from runtime.job_attempt where realm_id = new.realm_id and id = new.attempt_id;
    select project_id,work_item_id,run_id,assignment_id into j from runtime.job
      where realm_id=new.realm_id and id=ja.job_id;
    select config_effective_digest, expires_at into env
      from runtime.execution_environment_snapshot
      where realm_id = new.realm_id
        and snapshot_digest = new.execution_environment_snapshot_digest;
    if a.status <> 'active' or r.state <> 'active'
       or row(a.project_id, a.work_item_id) is distinct from row(r.project_id, r.work_item_id)
       or a.context_manifest_digest <> new.context_manifest_digest
       or ja.outcome is not null
       or row(j.project_id,j.work_item_id,j.run_id,j.assignment_id)
          is distinct from row(a.project_id,a.work_item_id,new.run_id,new.assignment_id)
       or env.config_effective_digest <> new.config_effective_digest
       or env.expires_at <= new.created_at
       or not exists (
         select 1 from agents.assignment_environment_binding b
         where b.realm_id = new.realm_id and b.assignment_id = new.assignment_id
           and b.execution_environment_snapshot_digest =
               new.execution_environment_snapshot_digest
       )
       or not exists (
         select 1 from runtime.environment_probe_evidence p
         join runtime.execution_environment_snapshot current_env
           on current_env.realm_id=p.realm_id
          and current_env.snapshot_digest=p.current_snapshot_digest
         where p.realm_id = new.realm_id
           and p.sticky_snapshot_digest = new.execution_environment_snapshot_digest
           and cardinality(p.drift_dimensions) = 0
           and p.checked_at <= new.created_at
           and p.checked_at >= new.created_at - interval '5 minutes'
           and current_env.expires_at > new.created_at
           and p.id = (
             select latest.id from runtime.environment_probe_evidence latest
             where latest.realm_id=p.realm_id
               and latest.sticky_snapshot_digest=p.sticky_snapshot_digest
             order by latest.checked_at desc,latest.id desc limit 1
           )
       ) then
        raise exception 'turn execution snapshot assignment/run/environment drift'
          using errcode = '23514';
    end if;
    expected_digest := models.capability_runtime_jsonb_digest(jsonb_build_object(
      'schema','zekam-turn-execution-snapshot/v1','id',new.id::text,
      'realm_id',new.realm_id::text,'assignment_id',new.assignment_id::text,
      'run_id',new.run_id::text,'attempt_id',new.attempt_id::text,
      'client_session_id',new.client_session_id,'turn_id',new.turn_id,
      'model_id',new.model_id,'provider_id',new.provider_id,
      'route_decision_digest',new.route_decision_digest,
      'reasoning_profile_digest',new.reasoning_profile_digest,
      'execution_environment_snapshot_digest',new.execution_environment_snapshot_digest,
      'context_manifest_digest',new.context_manifest_digest,
      'exposed_tool_set_digest',new.exposed_tool_set_digest,
      'hook_set_digest',new.hook_set_digest,'config_effective_digest',new.config_effective_digest,
      'trace_id',new.trace_id,'created_at',runtime.environment_canonical_timestamp(new.created_at),
      'grants_authority',false));
    if new.turn_snapshot_digest is distinct from expected_digest then
      raise exception 'turn execution snapshot canonical digest mismatch' using errcode='23514';
    end if;
    return new;
end
$$;
create trigger turn_execution_snapshot_guard before insert on runtime.turn_execution_snapshot
for each row execute function runtime.enforce_turn_execution_snapshot();

alter table runtime.execution_envelope
  add column turn_execution_snapshot_id uuid,
  add column turn_execution_snapshot_digest text,
  add constraint execution_envelope_turn_snapshot_fk foreign key
    (realm_id, turn_execution_snapshot_id)
    references runtime.turn_execution_snapshot(realm_id, id),
  add constraint execution_envelope_turn_snapshot_required check
    (turn_execution_snapshot_id is not null
     and turn_execution_snapshot_digest ~ '^sha256:[0-9a-f]{64}$') not valid;

alter table models.request_manifest
  add column turn_execution_snapshot_digest text,
  add column config_effective_digest text,
  add column hook_set_digest text;

create function runtime.enforce_envelope_turn_snapshot() returns trigger
language plpgsql security invoker set search_path = pg_catalog, runtime as $$
declare t record;
begin
    select assignment_id, run_id, attempt_id, model_id, route_decision_digest,
      context_manifest_digest, exposed_tool_set_digest, config_effective_digest,
      turn_snapshot_digest, execution_environment_snapshot_digest
      into t from runtime.turn_execution_snapshot
      where realm_id = new.realm_id and id = new.turn_execution_snapshot_id;
    if row(t.assignment_id,t.run_id,t.attempt_id,t.model_id,t.route_decision_digest,
           t.context_manifest_digest,t.turn_snapshot_digest)
       is distinct from
       row(new.assignment_id,new.run_id,new.attempt_id,new.model_id,new.route_decision_digest,
           new.context_manifest_digest,new.turn_execution_snapshot_digest) then
        raise exception 'execution envelope turn snapshot exact binding drift'
          using errcode = '23514';
    end if;
    return new;
end
$$;
create trigger execution_envelope_turn_snapshot_guard
before insert on runtime.execution_envelope
for each row execute function runtime.enforce_envelope_turn_snapshot();

create function models.enforce_manifest_environment_binding() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, runtime as $$
declare e record; t record; env record;
begin
    if new.execution_envelope_id is null then return new; end if;
    select turn_execution_snapshot_digest into e from runtime.execution_envelope
      where realm_id = new.realm_id and id = new.execution_envelope_id;
    select execution_environment_snapshot_digest, exposed_tool_set_digest,
      config_effective_digest, hook_set_digest into t
      from runtime.turn_execution_snapshot
      where realm_id = new.realm_id and turn_snapshot_digest = e.turn_execution_snapshot_digest;
    select permission_profile_digest into env
      from runtime.execution_environment_snapshot
      where realm_id = new.realm_id and snapshot_digest = t.execution_environment_snapshot_digest;
    if row(new.turn_execution_snapshot_digest,new.environment_digest,new.permission_profile_digest,
           new.tool_set_digest,new.config_effective_digest,new.hook_set_digest)
       is distinct from
       row(e.turn_execution_snapshot_digest,t.execution_environment_snapshot_digest,
           env.permission_profile_digest,t.exposed_tool_set_digest,
           t.config_effective_digest,t.hook_set_digest) then
      raise exception 'model manifest environment/permission/tool/config binding drift'
        using errcode = '23514';
    end if;
    return new;
end
$$;
create trigger request_manifest_environment_guard
before insert on models.request_manifest
for each row execute function models.enforce_manifest_environment_binding();

create or replace function models.enforce_manifest_missing_bindings() returns trigger
language plpgsql as $$
declare expected text[];
begin
  select coalesce(array_agg(name order by name),'{}'::text[]) into expected from (values
    ('assignment_id',new.assignment_id is null),
    ('authorization_scope_digest',new.authorization_scope_digest is null),
    ('checkpoint_digest',new.checkpoint_digest is null),
    ('context_fragment_set_digest',new.context_fragment_set_digest is null),
    ('context_manifest_digest',new.context_manifest_digest is null),
    ('context_packet_digest',new.context_packet_digest is null),
    ('execution_envelope_digest',new.execution_envelope_digest is null),
    ('execution_envelope_id',new.execution_envelope_id is null),
    ('max_cost_micros',new.max_cost_micros is null),
    ('max_input_tokens',new.max_input_tokens is null),
    ('max_output_tokens',new.max_output_tokens is null),
    ('model_visible_payload_digest',new.model_visible_payload_digest is null),
    ('output_schema_digest',new.output_schema_digest is null),
    ('policy_digest',new.policy_digest is null),
    ('role',new.role is null),
    ('route_decision_digest',new.route_decision_digest is null),
    ('route_expires_at',new.route_expires_at is null),
    ('run_id',new.run_id is null),
    ('source_revision',new.source_revision is null),
    ('turn_execution_snapshot_digest',new.execution_envelope_id is not null
      and new.turn_execution_snapshot_digest is null),
    ('environment_digest',new.execution_envelope_id is not null and new.environment_digest is null),
    ('permission_profile_digest',new.execution_envelope_id is not null
      and new.permission_profile_digest is null),
    ('tool_set_digest',new.execution_envelope_id is not null and new.tool_set_digest is null),
    ('config_effective_digest',new.execution_envelope_id is not null
      and new.config_effective_digest is null),
    ('hook_set_digest',new.execution_envelope_id is not null and new.hook_set_digest is null)
  ) as fields(name,missing) where missing;
  if new.missing_bindings<>expected then
    raise exception 'missing bindings manifest alanlariyla exact eslesmeli' using errcode='23514';
  end if;
  return new;
end $$;

do $$ declare target text; begin foreach target in array array[
  'runtime.execution_environment_snapshot','runtime.environment_probe_evidence',
  'runtime.turn_execution_snapshot','agents.assignment_environment_binding'
] loop
 execute format('alter table %s enable row level security',target);
 execute format('alter table %s force row level security',target);
 execute format('create policy scope_select on %s for select using(realm_id=core.current_realm_id())',target);
 execute format('create policy scope_insert on %s for insert with check(realm_id=core.current_realm_id())',target);
 execute format('grant select,insert on %s to zekam_app',target);
end loop; end $$;

create trigger execution_environment_no_mutation
before update or delete on runtime.execution_environment_snapshot
for each statement execute function core.deny_mutation();
create trigger environment_probe_no_mutation
before update or delete on runtime.environment_probe_evidence
for each statement execute function core.deny_mutation();
create trigger turn_execution_snapshot_no_mutation
before update or delete on runtime.turn_execution_snapshot
for each statement execute function core.deny_mutation();
create trigger assignment_environment_binding_no_mutation
before update or delete on agents.assignment_environment_binding
for each statement execute function core.deny_mutation();
