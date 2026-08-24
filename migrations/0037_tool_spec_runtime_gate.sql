-- Versioned tool specification/runtime registry and fail-closed dispatch gate.

create schema if not exists tools;
grant usage on schema tools to zekam_app;

create table tools.spec_revision (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  tool_id text not null,
  revision integer not null,
  name text not null,
  description text not null,
  input_schema_digest text not null,
  output_schema_digest text not null,
  created_at timestamptz not null,
  spec_digest text not null,
  unique(realm_id,id), unique(realm_id,spec_digest), unique(realm_id,tool_id,revision),
  check(btrim(tool_id)<>'' and btrim(name)<>'' and btrim(description)<>''),
  check(revision>0),
  check(input_schema_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(output_schema_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(spec_digest ~ '^sha256:[0-9a-f]{64}$')
);

create function tools.enforce_spec_revision() returns trigger
language plpgsql security invoker set search_path=pg_catalog,tools,runtime,models as $$
declare expected text;
begin
  expected := models.capability_runtime_jsonb_digest(jsonb_build_object(
    'schema','zekam-tool-spec-revision/v1','id',new.id::text,
    'realm_id',new.realm_id::text,'tool_id',new.tool_id,'revision',new.revision,
    'name',new.name,'description',new.description,
    'input_schema_digest',new.input_schema_digest,
    'output_schema_digest',new.output_schema_digest,
    'created_at',runtime.environment_canonical_timestamp(new.created_at)));
  if new.spec_digest is distinct from expected then
    raise exception 'tool spec canonical digest mismatch' using errcode='23514';
  end if;
  return new;
end $$;
create trigger spec_revision_guard before insert on tools.spec_revision
for each row execute function tools.enforce_spec_revision();

create table tools.runtime_revision (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  tool_id text not null,
  revision integer not null,
  adapter_ref text not null,
  executable_revision text not null,
  executable_digest text not null,
  permission_capabilities jsonb not null,
  parallel_supported boolean not null,
  captured_at timestamptz not null,
  expires_at timestamptz not null,
  runtime_digest text not null,
  unique(realm_id,id), unique(realm_id,runtime_digest),
  unique(realm_id,tool_id,revision,runtime_digest),
  check(btrim(tool_id)<>'' and btrim(adapter_ref)<>'' and btrim(executable_revision)<>''),
  check(revision>0 and expires_at>captured_at),
  check(executable_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(runtime_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(permission_capabilities)='array')
);

create function tools.enforce_runtime_revision() returns trigger
language plpgsql security invoker set search_path=pg_catalog,tools,runtime,models as $$
declare capabilities text[]; expected text;
begin
  perform pg_advisory_xact_lock(hashtextextended(new.realm_id::text||':'||new.tool_id,0));
  select array_agg(value order by value) into capabilities
  from jsonb_array_elements_text(new.permission_capabilities);
  capabilities := coalesce(capabilities,'{}'::text[]);
  if to_jsonb(capabilities)<>new.permission_capabilities
     or cardinality(capabilities)<>cardinality(array(select distinct unnest(capabilities)))
     or exists(select 1 from unnest(capabilities) value where btrim(value)='') then
    raise exception 'tool runtime capabilities canonical set olmali' using errcode='23514';
  end if;
  expected := models.capability_runtime_jsonb_digest(jsonb_build_object(
    'schema','zekam-tool-runtime-revision/v1','id',new.id::text,
    'realm_id',new.realm_id::text,'tool_id',new.tool_id,'revision',new.revision,
    'adapter_ref',new.adapter_ref,'executable_revision',new.executable_revision,
    'executable_digest',new.executable_digest,
    'permission_capabilities',new.permission_capabilities,
    'parallel_supported',new.parallel_supported,
    'captured_at',runtime.environment_canonical_timestamp(new.captured_at),
    'expires_at',runtime.environment_canonical_timestamp(new.expires_at)));
  if new.runtime_digest is distinct from expected then
    raise exception 'tool runtime canonical digest mismatch' using errcode='23514';
  end if;
  return new;
end $$;
create trigger runtime_revision_guard before insert on tools.runtime_revision
for each row execute function tools.enforce_runtime_revision();

create table tools.compiled_set (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  role text not null,
  permission_profile_digest text not null,
  entries jsonb not null,
  created_at timestamptz not null,
  tool_set_digest text not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,tool_set_digest),
  check(btrim(role)<>'' and permission_profile_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(entries)='array'),
  check(tool_set_digest ~ '^sha256:[0-9a-f]{64}$' and grants_authority=false)
);

create function tools.enforce_compiled_set() returns trigger
language plpgsql security invoker set search_path=pg_catalog,tools,runtime,models as $$
declare entry jsonb; keys text[]; prior_tool text; expected text;
begin
  for entry in select value from jsonb_array_elements(new.entries) loop
    select array_agg(key order by key) into keys from jsonb_object_keys(entry) key;
    if keys<>array['exposure','revision','runtime_digest','spec_digest','tool_id']
       or btrim(entry->>'tool_id')=''
       or (entry->>'revision')::integer<1
       or entry->>'exposure' not in ('direct','deferred-search','code-mode-only','hidden-dispatch')
       or entry->>'spec_digest' !~ '^sha256:[0-9a-f]{64}$'
       or entry->>'runtime_digest' !~ '^sha256:[0-9a-f]{64}$' then
      raise exception 'compiled tool set entry shape mismatch' using errcode='23514';
    end if;
    if prior_tool is not null and prior_tool>=entry->>'tool_id' then
      raise exception 'compiled tool set entries unique ve tool_id sirali olmali' using errcode='23514';
    end if;
    prior_tool := entry->>'tool_id';
    if not exists(select 1 from tools.spec_revision s
      where s.realm_id=new.realm_id and s.tool_id=entry->>'tool_id'
      and s.revision=(entry->>'revision')::integer and s.spec_digest=entry->>'spec_digest')
      or not exists(select 1 from tools.runtime_revision r
      where r.realm_id=new.realm_id and r.tool_id=entry->>'tool_id'
      and r.revision=(entry->>'revision')::integer
      and r.runtime_digest=entry->>'runtime_digest') then
      raise exception 'compiled tool set exact spec/runtime revision ister' using errcode='23514';
    end if;
  end loop;
  expected := models.capability_runtime_jsonb_digest(jsonb_build_object(
    'schema','zekam-compiled-tool-set/v1','id',new.id::text,'realm_id',new.realm_id::text,
    'role',new.role,'permission_profile_digest',new.permission_profile_digest,
    'entries',new.entries,'created_at',runtime.environment_canonical_timestamp(new.created_at),
    'grants_authority',false));
  if new.tool_set_digest is distinct from expected then
    raise exception 'compiled tool set canonical digest mismatch' using errcode='23514';
  end if;
  return new;
end $$;
create trigger compiled_set_guard before insert on tools.compiled_set
for each row execute function tools.enforce_compiled_set();

alter table runtime.turn_execution_snapshot
  add constraint turn_execution_snapshot_compiled_tool_set_fk
  foreign key(realm_id,exposed_tool_set_digest)
  references tools.compiled_set(realm_id,tool_set_digest) not valid;

create function tools.enforce_turn_compiled_set() returns trigger
language plpgsql security invoker set search_path=pg_catalog,tools,runtime,agents as $$
begin
  if not exists(select 1 from tools.compiled_set tool_set
    join agents.assignment a on a.realm_id=tool_set.realm_id and a.id=new.assignment_id
    join runtime.execution_environment_snapshot env on env.realm_id=tool_set.realm_id
      and env.snapshot_digest=new.execution_environment_snapshot_digest
    where tool_set.realm_id=new.realm_id
      and tool_set.tool_set_digest=new.exposed_tool_set_digest
      and tool_set.role=a.role
      and tool_set.permission_profile_digest=env.permission_profile_digest) then
    raise exception 'turn compiled tool set role/permission mismatch' using errcode='23514';
  end if;
  return new;
end $$;
create trigger turn_execution_snapshot_tool_set_guard
before insert on runtime.turn_execution_snapshot
for each row execute function tools.enforce_turn_compiled_set();
alter table models.request_manifest
  add column tool_visible_payload_digest text;
alter table models.request_manifest
  add column tool_visible_payload_mode text;
alter table models.request_manifest
  add constraint request_manifest_tool_visible_payload_digest_format
  check(tool_visible_payload_digest is null
    or tool_visible_payload_digest ~ '^sha256:[0-9a-f]{64}$');
alter table models.request_manifest
  add constraint request_manifest_tool_visible_payload_mode_check
  check(tool_visible_payload_mode is null or tool_visible_payload_mode in ('direct','code-mode'));
alter table models.request_manifest
  add constraint request_manifest_compiled_tool_set_fk
  foreign key(realm_id,tool_set_digest)
  references tools.compiled_set(realm_id,tool_set_digest) not valid;

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
    ('tool_visible_payload_digest',new.execution_envelope_id is not null
      and new.tool_visible_payload_digest is null),
    ('tool_visible_payload_mode',new.execution_envelope_id is not null
      and new.tool_visible_payload_mode is null),
    ('config_effective_digest',new.execution_envelope_id is not null
      and new.config_effective_digest is null),
    ('hook_set_digest',new.execution_envelope_id is not null and new.hook_set_digest is null)
  ) as fields(name,missing) where missing;
  if new.missing_bindings<>expected then
    raise exception 'missing bindings manifest alanlariyla exact eslesmeli' using errcode='23514';
  end if;
  return new;
end $$;

create or replace function models.enforce_manifest_environment_binding() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, runtime, tools as $$
declare e record; t record; env record;
begin
    if new.execution_envelope_id is null then return new; end if;
    select turn_execution_snapshot_digest into e from runtime.execution_envelope
      where realm_id = new.realm_id and id = new.execution_envelope_id;
    select turn.execution_environment_snapshot_digest,compiled.tool_set_digest,
      turn.config_effective_digest,turn.hook_set_digest into t
      from runtime.turn_execution_snapshot turn
      left join tools.compiled_set compiled on compiled.realm_id=turn.realm_id
        and compiled.tool_set_digest=turn.exposed_tool_set_digest
      where turn.realm_id = new.realm_id
        and turn.turn_snapshot_digest = e.turn_execution_snapshot_digest;
    select permission_profile_digest into env
      from runtime.execution_environment_snapshot
      where realm_id = new.realm_id and snapshot_digest = t.execution_environment_snapshot_digest;
    if row(new.turn_execution_snapshot_digest,new.environment_digest,new.permission_profile_digest,
           new.tool_set_digest,new.config_effective_digest,new.hook_set_digest)
       is distinct from
       row(e.turn_execution_snapshot_digest,t.execution_environment_snapshot_digest,
           env.permission_profile_digest,t.tool_set_digest,
           t.config_effective_digest,t.hook_set_digest) then
      raise exception 'model manifest environment/permission/tool/config binding drift'
        using errcode = '23514';
    end if;
    return new;
end
$$;

create function tools.enforce_model_visible_tool_payload() returns trigger
language plpgsql security invoker set search_path=pg_catalog,tools,models as $$
declare visible jsonb; expected text;
begin
  if new.tool_set_digest is null then
    if new.tool_visible_payload_digest is not null or new.tool_visible_payload_mode is not null then
      raise exception 'tool payload compiled set olmadan yazilamaz' using errcode='23514';
    end if;
    return new;
  end if;
  if new.tool_visible_payload_digest is null or new.tool_visible_payload_mode is null then
    return new;
  end if;
  select coalesce(jsonb_agg(jsonb_build_object(
    'tool_id',entry->>'tool_id','revision',(entry->>'revision')::integer,
    'spec_digest',entry->>'spec_digest') order by entry->>'tool_id'),'[]'::jsonb)
    into visible
    from tools.compiled_set tool_set,
      lateral jsonb_array_elements(tool_set.entries) entry
    where tool_set.realm_id=new.realm_id and tool_set.tool_set_digest=new.tool_set_digest
      and (entry->>'exposure'='direct'
        or (new.tool_visible_payload_mode='code-mode' and entry->>'exposure'='code-mode-only'));
  expected := models.capability_runtime_jsonb_digest(visible);
  if new.tool_visible_payload_digest is distinct from expected then
    raise exception 'model-visible tool payload compiled exposure mismatch' using errcode='23514';
  end if;
  return new;
end $$;
create trigger request_manifest_tool_payload_guard
before insert on models.request_manifest
for each row execute function tools.enforce_model_visible_tool_payload();

create table tools.dispatch_gate_evidence (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  effect_claim_id uuid not null references runtime.effect_claim(id) on delete restrict,
  turn_execution_snapshot_digest text not null,
  tool_set_digest text not null,
  tool_id text not null,
  revision integer not null,
  spec_digest text not null,
  runtime_digest text not null,
  input_digest text not null,
  disposition text not null,
  checked_at timestamptz not null,
  evidence_digest text not null,
  unique(realm_id,id), unique(realm_id,evidence_digest),
  foreign key(realm_id,tool_set_digest) references tools.compiled_set(realm_id,tool_set_digest),
  foreign key(realm_id,turn_execution_snapshot_digest)
    references runtime.turn_execution_snapshot(realm_id,turn_snapshot_digest),
  check(revision>0 and disposition='passed'),
  check(spec_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(runtime_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(input_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(evidence_digest ~ '^sha256:[0-9a-f]{64}$')
);

create function tools.enforce_dispatch_gate_evidence() returns trigger
language plpgsql security invoker set search_path=pg_catalog,tools,runtime,models as $$
declare entry jsonb; current_runtime tools.runtime_revision%rowtype; expected text;
begin
  if not exists(select 1 from runtime.effect_claim c
    join runtime.job j on j.realm_id=c.realm_id and j.id=c.job_id
    join runtime.turn_execution_snapshot t on t.realm_id=c.realm_id
      and t.attempt_id=c.attempt_id and t.run_id=j.run_id and t.assignment_id=j.assignment_id
    join agents.assignment a on a.realm_id=t.realm_id and a.id=t.assignment_id
    join runtime.execution_environment_snapshot env on env.realm_id=t.realm_id
      and env.snapshot_digest=t.execution_environment_snapshot_digest
    join tools.compiled_set tool_set on tool_set.realm_id=t.realm_id
      and tool_set.tool_set_digest=t.exposed_tool_set_digest
    where c.realm_id=new.realm_id and c.id=new.effect_claim_id
      and t.turn_snapshot_digest=new.turn_execution_snapshot_digest
      and t.exposed_tool_set_digest=new.tool_set_digest
      and tool_set.role=a.role
      and tool_set.permission_profile_digest=env.permission_profile_digest
      and not exists(select 1 from runtime.effect_receipt receipt
        where receipt.realm_id=c.realm_id and receipt.claim_id=c.id)) then
    raise exception 'tool dispatch exact unreceipted claim/turn binding ister' using errcode='23514';
  end if;
  select value into entry from tools.compiled_set s,
    lateral jsonb_array_elements(s.entries) value
    where s.realm_id=new.realm_id and s.tool_set_digest=new.tool_set_digest
      and value->>'tool_id'=new.tool_id;
  if entry is null or (entry->>'revision')::integer<>new.revision
     or entry->>'spec_digest'<>new.spec_digest
     or entry->>'runtime_digest'<>new.runtime_digest then
    raise exception 'tool dispatch compiled binding mismatch' using errcode='23514';
  end if;
  select * into current_runtime from tools.runtime_revision r
    where r.realm_id=new.realm_id and r.tool_id=new.tool_id
      and r.captured_at<=new.checked_at and r.expires_at>new.checked_at
    order by r.revision desc,r.captured_at desc,r.id desc limit 1;
  if current_runtime.id is null
     or current_runtime.revision<>new.revision
     or current_runtime.runtime_digest<>new.runtime_digest then
    raise exception 'tool dispatch executable runtime revision mismatch' using errcode='23514';
  end if;
  expected := models.capability_runtime_jsonb_digest(jsonb_build_object(
    'realm_id',new.realm_id::text,'effect_claim_id',new.effect_claim_id::text,
    'turn_execution_snapshot_digest',new.turn_execution_snapshot_digest,
    'tool_set_digest',new.tool_set_digest,
    'tool_id',new.tool_id,'revision',new.revision,'spec_digest',new.spec_digest,
    'runtime_digest',new.runtime_digest,'input_digest',new.input_digest,
    'disposition','passed','checked_at',runtime.environment_canonical_timestamp(new.checked_at)));
  if new.evidence_digest is distinct from expected then
    raise exception 'tool dispatch gate evidence canonical digest mismatch' using errcode='23514';
  end if;
  return new;
end $$;
create trigger dispatch_gate_evidence_guard before insert on tools.dispatch_gate_evidence
for each row execute function tools.enforce_dispatch_gate_evidence();

do $$ declare target text; begin foreach target in array array[
  'tools.spec_revision','tools.runtime_revision','tools.compiled_set','tools.dispatch_gate_evidence'
] loop
 execute format('alter table %s enable row level security',target);
 execute format('alter table %s force row level security',target);
 execute format('create policy scope_select on %s for select using(realm_id=core.current_realm_id())',target);
 execute format('create policy scope_insert on %s for insert with check(realm_id=core.current_realm_id())',target);
 execute format('grant select,insert on %s to zekam_app',target);
 execute format('create trigger no_mutation before update or delete on %s for each statement execute function core.deny_mutation()',target);
end loop; end $$;
