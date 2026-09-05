-- Typed HookRuntime v2 registry, generation pinning and authority-free results.

create schema if not exists hooks;
grant usage on schema hooks to zekam_app;

create function hooks.json_schema_supported(schema_body jsonb) returns boolean
language plpgsql immutable strict security invoker set search_path=pg_catalog,hooks as $$
declare key text; declare child jsonb;
begin
  if jsonb_typeof(schema_body)<>'object' then return false; end if;
  for key in select jsonb_object_keys(schema_body) loop
    if key not in ('$schema','type','properties','required','additionalProperties',
      'items','enum','const','description','title') then return false; end if;
  end loop;
  if schema_body ? 'type' and (
    jsonb_typeof(schema_body->'type')<>'string'
    or schema_body->>'type' not in ('object','array','string','number','integer','boolean','null')
  ) then return false; end if;
  if schema_body ? 'properties' then
    if jsonb_typeof(schema_body->'properties')<>'object' then return false; end if;
    for child in select value from jsonb_each(schema_body->'properties') loop
      if not hooks.json_schema_supported(child) then return false; end if;
    end loop;
  end if;
  if schema_body ? 'required' and (
    jsonb_typeof(schema_body->'required')<>'array'
    or exists(select 1 from jsonb_array_elements(schema_body->'required') item
      where jsonb_typeof(item)<>'string')
  ) then return false; end if;
  if schema_body ? 'additionalProperties'
    and jsonb_typeof(schema_body->'additionalProperties')<>'boolean' then return false; end if;
  if schema_body ? 'items' and not hooks.json_schema_supported(schema_body->'items') then
    return false;
  end if;
  if schema_body ? 'enum' and jsonb_typeof(schema_body->'enum')<>'array' then return false; end if;
  return true;
end $$;

create function hooks.json_payload_valid(schema_body jsonb,payload jsonb) returns boolean
language plpgsql immutable strict security invoker set search_path=pg_catalog,hooks as $$
declare expected_type text; declare required_key jsonb; declare property record;
declare array_item jsonb; declare type_matches boolean;
begin
  if not hooks.json_schema_supported(schema_body) then return false; end if;
  if schema_body ? 'enum' and not exists(
    select 1 from jsonb_array_elements(schema_body->'enum') item where item=payload
  ) then return false; end if;
  if schema_body ? 'const' and schema_body->'const'<>payload then return false; end if;
  expected_type:=schema_body->>'type';
  type_matches:=case expected_type
    when 'object' then jsonb_typeof(payload)='object'
    when 'array' then jsonb_typeof(payload)='array'
    when 'string' then jsonb_typeof(payload)='string'
    when 'number' then jsonb_typeof(payload)='number'
    when 'integer' then jsonb_typeof(payload)='number' and payload::text !~ '[.eE]'
    when 'boolean' then jsonb_typeof(payload)='boolean'
    when 'null' then jsonb_typeof(payload)='null'
    else true end;
  if not type_matches then return false; end if;
  if jsonb_typeof(payload)='object' then
    for required_key in select value from jsonb_array_elements(
      coalesce(schema_body->'required','[]'::jsonb)) loop
      if not(payload ? (required_key#>>'{}')) then return false; end if;
    end loop;
    for property in select key,value from jsonb_each(payload) loop
      if schema_body->'properties' ? property.key then
        if not hooks.json_payload_valid(
          schema_body->'properties'->property.key,property.value) then return false; end if;
      elsif coalesce((schema_body->>'additionalProperties')::boolean,true)=false then
        return false;
      end if;
    end loop;
  end if;
  if jsonb_typeof(payload)='array' and schema_body ? 'items' then
    for array_item in select value from jsonb_array_elements(payload) loop
      if not hooks.json_payload_valid(schema_body->'items',array_item) then return false; end if;
    end loop;
  end if;
  return true;
end $$;

create table hooks.spec_revision (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  hook_id text not null,
  revision integer not null,
  event_type text not null,
  required boolean not null,
  source_layer text not null,
  timeout_ms integer not null,
  execution_mode text not null,
  input_schema jsonb not null,
  output_schema jsonb not null,
  input_schema_digest text not null,
  output_schema_digest text not null,
  permission_profile_revision_id uuid not null,
  permission_profile_name text not null,
  permission_profile_digest text not null,
  failure_policy text not null,
  created_at timestamptz not null,
  hook_digest text not null,
  hook_body jsonb not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),unique(realm_id,hook_id,revision),unique(realm_id,hook_digest),
  foreign key(realm_id,permission_profile_revision_id)
    references security.permission_profile_revision(realm_id,id) on delete restrict,
  check(btrim(hook_id)<>'' and btrim(source_layer)<>'' and revision>0),
  check(timeout_ms between 1 and 300000),
  check(event_type in ('session.start','session.end','user.input.submitted','turn.start',
    'turn.stop','pre.tool','post.tool','permission.request','pre.compact','post.compact',
    'checkpoint.created','agent.spawned','agent.completed','recovery.required')),
  check(execution_mode in ('command','python','mcp','internal')),
  check(failure_policy in ('abort','warn','quarantine')),
  check(not(required and failure_policy='warn')),
  check(jsonb_typeof(input_schema)='object' and jsonb_typeof(output_schema)='object'),
  check(input_schema_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(output_schema_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(permission_profile_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(hook_digest ~ '^sha256:[0-9a-f]{64}$' and not grants_authority)
);

create function hooks.enforce_spec_revision() returns trigger
language plpgsql security invoker set search_path=pg_catalog,hooks,security,models,runtime as $$
declare expected jsonb; declare profile record;
begin
  perform pg_advisory_xact_lock(hashtextextended(new.realm_id::text||':'||new.hook_id,0));
  if not exists(select 1 from hooks.spec_revision prior
      where prior.realm_id=new.realm_id and prior.hook_digest=new.hook_digest)
    and new.revision<>(select coalesce(max(prior.revision),0)+1 from hooks.spec_revision prior
      where prior.realm_id=new.realm_id and prior.hook_id=new.hook_id) then
    raise exception 'hook spec revision monotonic olmali' using errcode='23514';
  end if;
  select name,profile_digest into profile from security.permission_profile_revision
    where realm_id=new.realm_id and id=new.permission_profile_revision_id;
  if profile.name is distinct from new.permission_profile_name
    or profile.profile_digest is distinct from new.permission_profile_digest then
    raise exception 'hook permission profile exact binding mismatch' using errcode='23514';
  end if;
  expected:=jsonb_build_object(
    'schema','zekam-hook-spec-revision/v1','id',new.id::text,'realm_id',new.realm_id::text,
    'hook_id',new.hook_id,'revision',new.revision,'event_type',new.event_type,
    'required',new.required,'source_layer',new.source_layer,'timeout_ms',new.timeout_ms,
    'execution_mode',new.execution_mode,'input_schema_digest',new.input_schema_digest,
    'output_schema_digest',new.output_schema_digest,
    'permission_profile_name',new.permission_profile_name,
    'permission_profile_digest',new.permission_profile_digest,
    'failure_policy',new.failure_policy,
    'created_at',runtime.environment_canonical_timestamp(new.created_at),
    'grants_authority',false);
  if not hooks.json_schema_supported(new.input_schema)
    or not hooks.json_schema_supported(new.output_schema)
    or new.input_schema_digest<>models.capability_runtime_jsonb_digest(new.input_schema)
    or new.output_schema_digest<>models.capability_runtime_jsonb_digest(new.output_schema)
    or new.hook_body<>expected
    or new.hook_digest<>models.capability_runtime_jsonb_digest(expected) then
    raise exception 'hook spec schema/body/digest drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger hook_spec_revision_guard before insert on hooks.spec_revision
for each row execute function hooks.enforce_spec_revision();

create table hooks.runtime_revision (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  hook_id text not null,
  hook_revision integer not null,
  adapter_ref text not null,
  adapter_digest text not null,
  permission_capabilities text[] not null,
  load_state text not null,
  captured_at timestamptz not null,
  expires_at timestamptz not null,
  runtime_digest text not null,
  runtime_body jsonb not null,
  unique(realm_id,id),unique(realm_id,hook_id,hook_revision,runtime_digest),
  foreign key(realm_id,hook_id,hook_revision)
    references hooks.spec_revision(realm_id,hook_id,revision) on delete restrict,
  check(btrim(adapter_ref)<>'' and hook_revision>0),
  check(adapter_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(load_state in ('ready','failed','quarantined')),
  check(expires_at>captured_at),
  check(runtime_digest ~ '^sha256:[0-9a-f]{64}$')
);

create function hooks.enforce_runtime_revision() returns trigger
language plpgsql security invoker set search_path=pg_catalog,hooks,models,runtime as $$
declare expected jsonb;
begin
  if new.permission_capabilities<>(select coalesce(array_agg(value order by value),'{}'::text[])
      from (select distinct unnest(new.permission_capabilities) value) valueset)
    or exists(select 1 from unnest(new.permission_capabilities) value where btrim(value)='') then
    raise exception 'hook runtime capabilities canonical olmali' using errcode='23514';
  end if;
  expected:=jsonb_build_object(
    'schema','zekam-hook-runtime-revision/v1','id',new.id::text,
    'realm_id',new.realm_id::text,'hook_id',new.hook_id,
    'hook_revision',new.hook_revision,'adapter_ref',new.adapter_ref,
    'adapter_digest',new.adapter_digest,
    'permission_capabilities',to_jsonb(new.permission_capabilities),
    'load_state',new.load_state,
    'captured_at',runtime.environment_canonical_timestamp(new.captured_at),
    'expires_at',runtime.environment_canonical_timestamp(new.expires_at));
  if new.runtime_body<>expected
    or new.runtime_digest<>models.capability_runtime_jsonb_digest(expected) then
    raise exception 'hook runtime body/digest drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger hook_runtime_revision_guard before insert on hooks.runtime_revision
for each row execute function hooks.enforce_runtime_revision();

create table hooks.compiled_set (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  generation integer not null,
  config_effective_digest text not null,
  required_load_errors text[] not null,
  hook_set_digest text not null,
  set_body jsonb not null,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),unique(realm_id,generation),unique(realm_id,hook_set_digest),
  check(generation>0),
  check(config_effective_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(hook_set_digest ~ '^sha256:[0-9a-f]{64}$' and not grants_authority)
);

create table hooks.compiled_set_entry (
  realm_id uuid not null,
  compiled_set_id uuid not null,
  ordinal integer not null,
  spec_revision_id uuid not null,
  runtime_revision_id uuid,
  disabled_reason text,
  primary key(realm_id,compiled_set_id,ordinal),
  unique(realm_id,compiled_set_id,spec_revision_id),
  foreign key(realm_id,compiled_set_id) references hooks.compiled_set(realm_id,id) on delete restrict,
  foreign key(realm_id,spec_revision_id) references hooks.spec_revision(realm_id,id) on delete restrict,
  foreign key(realm_id,runtime_revision_id) references hooks.runtime_revision(realm_id,id)
    on delete restrict,
  check(ordinal>0),
  check((runtime_revision_id is null)=(disabled_reason is not null))
);

create function hooks.enforce_compiled_set() returns trigger
language plpgsql security invoker set search_path=pg_catalog,hooks,models as $$
declare expected jsonb; declare actual_entries jsonb; declare item record;
begin
  if tg_table_name='compiled_set_entry' then
    select * into item from hooks.compiled_set
      where realm_id=new.realm_id and id=new.compiled_set_id;
  else
    item:=new;
  end if;
  select coalesce(jsonb_agg(jsonb_build_object(
      'ordinal',entry.ordinal,'hook_digest',spec.hook_digest,
      'runtime_digest',runtime.runtime_digest,'disabled_reason',entry.disabled_reason)
      order by entry.ordinal),'[]'::jsonb)
    into actual_entries
    from hooks.compiled_set_entry entry
    join hooks.spec_revision spec on spec.realm_id=entry.realm_id and spec.id=entry.spec_revision_id
    left join hooks.runtime_revision runtime
      on runtime.realm_id=entry.realm_id and runtime.id=entry.runtime_revision_id
    where entry.realm_id=item.realm_id and entry.compiled_set_id=item.id;
  if exists(select 1 from jsonb_array_elements(actual_entries) with ordinality item(value,ordinal)
      where (value->>'ordinal')::integer<>ordinal) then
    raise exception 'compiled hook set ordinal gap' using errcode='23514';
  end if;
  if exists(
    select 1 from hooks.compiled_set_entry entry
    join hooks.spec_revision spec
      on spec.realm_id=entry.realm_id and spec.id=entry.spec_revision_id
    left join hooks.runtime_revision runtime
      on runtime.realm_id=entry.realm_id and runtime.id=entry.runtime_revision_id
    left join security.permission_profile_revision profile
      on profile.realm_id=spec.realm_id
      and profile.id=spec.permission_profile_revision_id
    where entry.realm_id=item.realm_id and entry.compiled_set_id=item.id and (
      (runtime.id is not null and
        (runtime.hook_id<>spec.hook_id or runtime.hook_revision<>spec.revision
          or runtime.load_state<>'ready' or runtime.expires_at<=item.created_at
          or runtime.permission_capabilities&&profile.denied_capabilities
          or not(runtime.permission_capabilities<@profile.allowed_capabilities)))
      or (spec.required and runtime.id is null and not exists(
        select 1 from unnest(item.required_load_errors) error
          where error like spec.hook_id||':%'))
    )
  ) then
    raise exception 'compiled hook set spec/runtime/profile/load binding drift'
      using errcode='23514';
  end if;
  expected:=jsonb_build_object(
    'schema','zekam-compiled-hook-set/v1','realm_id',item.realm_id::text,
    'generation',item.generation,'config_effective_digest',item.config_effective_digest,
    'entries',actual_entries,'required_load_errors',to_jsonb(item.required_load_errors),
    'grants_authority',false);
  if item.required_load_errors<>(select coalesce(array_agg(value order by value),'{}'::text[])
      from (select distinct unnest(item.required_load_errors) value) errors)
    or item.set_body<>expected
    or item.hook_set_digest<>models.capability_runtime_jsonb_digest(expected) then
    raise exception 'compiled hook set body/digest drift' using errcode='23514';
  end if;
  return new;
end $$;
create constraint trigger compiled_hook_set_guard after insert on hooks.compiled_set
deferrable initially deferred for each row execute function hooks.enforce_compiled_set();
create constraint trigger compiled_hook_entry_guard after insert on hooks.compiled_set_entry
deferrable initially deferred for each row execute function hooks.enforce_compiled_set();

create table hooks.current_generation (
  realm_id uuid primary key references core.realm(id) on delete restrict,
  compiled_set_id uuid not null,
  generation integer not null,
  hook_set_digest text not null,
  updated_at timestamptz not null,
  foreign key(realm_id,compiled_set_id) references hooks.compiled_set(realm_id,id) on delete restrict,
  unique(realm_id,hook_set_digest)
);

create function hooks.enforce_current_generation() returns trigger
language plpgsql security invoker set search_path=pg_catalog,hooks as $$
declare target record;
begin
  select * into target from hooks.compiled_set
    where realm_id=new.realm_id and id=new.compiled_set_id;
  if not found or cardinality(target.required_load_errors)>0
    or (new.generation,new.hook_set_digest) is distinct from
       (target.generation,target.hook_set_digest)
    or (tg_op='INSERT' and new.generation<>1)
    or (tg_op='UPDATE' and new.generation<>old.generation+1) then
    raise exception 'current hook generation compiled set/monotonic binding mismatch'
      using errcode='23514';
  end if;
  return new;
end $$;
create trigger hook_current_generation_guard before insert or update
on hooks.current_generation for each row execute function hooks.enforce_current_generation();

create function hooks.activate_compiled_set(p_compiled_set_id uuid)
returns table(generation integer,hook_set_digest text)
language plpgsql security definer set search_path=pg_catalog,hooks,core as $$
declare target record; declare prior integer; declare active_realm uuid;
begin
  active_realm:=core.current_realm_id();
  perform pg_advisory_xact_lock(hashtextextended(active_realm::text||':hook-generation',0));
  select * into target from hooks.compiled_set
    where realm_id=active_realm and id=p_compiled_set_id;
  if not found or cardinality(target.required_load_errors)>0 then
    raise exception 'compiled hook set aktivasyon icin uygun degil' using errcode='23514';
  end if;
  select current.generation into prior from hooks.current_generation current
    where current.realm_id=active_realm for update;
  if target.generation<>coalesce(prior,0)+1 then
    raise exception 'hook generation monotonic olmali' using errcode='23514';
  end if;
  if prior is null then
    insert into hooks.current_generation(
      realm_id,compiled_set_id,generation,hook_set_digest,updated_at)
      values(active_realm,target.id,target.generation,target.hook_set_digest,clock_timestamp());
  else
    update hooks.current_generation set compiled_set_id=target.id,
      generation=target.generation,hook_set_digest=target.hook_set_digest,
      updated_at=clock_timestamp() where realm_id=active_realm;
  end if;
  return query select target.generation,target.hook_set_digest;
end $$;
revoke all on function hooks.activate_compiled_set(uuid) from public;
grant execute on function hooks.activate_compiled_set(uuid) to zekam_app;

create table hooks.session_binding (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  session_ref text not null,
  compiled_set_id uuid not null,
  generation integer not null,
  hook_set_digest text not null,
  config_effective_digest text not null,
  state text not null,
  started_at timestamptz not null,
  ended_at timestamptz,
  unique(realm_id,id),unique(realm_id,session_ref),
  foreign key(realm_id,compiled_set_id) references hooks.compiled_set(realm_id,id) on delete restrict,
  check(btrim(session_ref)<>''),check(state in ('active','closed')),
  check((state='active' and ended_at is null) or (state='closed' and ended_at is not null)),
  check(hook_set_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(config_effective_digest ~ '^sha256:[0-9a-f]{64}$')
);

create function hooks.enforce_session_binding() returns trigger
language plpgsql security invoker set search_path=pg_catalog,hooks as $$
declare item record;
begin
  select generation,hook_set_digest,config_effective_digest,required_load_errors into item
    from hooks.compiled_set where realm_id=new.realm_id and id=new.compiled_set_id;
  if cardinality(item.required_load_errors)>0
    or (new.generation,new.hook_set_digest,new.config_effective_digest) is distinct from
       (item.generation,item.hook_set_digest,item.config_effective_digest) then
    raise exception 'required hook load/session binding mismatch' using errcode='23514';
  end if;
  return new;
end $$;
create trigger hook_session_binding_guard before insert on hooks.session_binding
for each row execute function hooks.enforce_session_binding();

create function hooks.start_session(p_session_id uuid, p_session_ref text)
returns table(session_binding_id uuid,generation integer,hook_set_digest text)
language plpgsql security definer set search_path=pg_catalog,hooks,core as $$
declare active_realm uuid; declare current_set record;
begin
  active_realm:=core.current_realm_id();
  select configured.* into current_set from hooks.current_generation current
    join hooks.compiled_set configured
      on configured.realm_id=current.realm_id and configured.id=current.compiled_set_id
    where current.realm_id=active_realm;
  if not found or cardinality(current_set.required_load_errors)>0 then
    raise exception 'current hook generation session baslangici icin uygun degil'
      using errcode='23514';
  end if;
  insert into hooks.session_binding(
    id,realm_id,session_ref,compiled_set_id,generation,hook_set_digest,
    config_effective_digest,state,started_at)
    values(p_session_id,active_realm,p_session_ref,current_set.id,current_set.generation,
      current_set.hook_set_digest,current_set.config_effective_digest,'active',clock_timestamp());
  return query select p_session_id,current_set.generation,current_set.hook_set_digest;
end $$;
revoke all on function hooks.start_session(uuid,text) from public;
grant execute on function hooks.start_session(uuid,text) to zekam_app;

create function hooks.close_session(p_session_id uuid) returns boolean
language plpgsql security definer set search_path=pg_catalog,hooks,core as $$
declare changed integer; declare active_realm uuid;
begin
  active_realm:=core.current_realm_id();
  update hooks.session_binding set state='closed',ended_at=clock_timestamp()
    where realm_id=active_realm and id=p_session_id and state='active';
  get diagnostics changed=row_count;
  return changed=1;
end $$;
revoke all on function hooks.close_session(uuid) from public;
grant execute on function hooks.close_session(uuid) to zekam_app;

create table hooks.invocation (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  session_binding_id uuid not null,
  generation integer not null,
  event_type text not null,
  spec_revision_id uuid not null,
  runtime_revision_id uuid not null,
  input_body jsonb not null,
  input_digest text not null,
  deadline_at timestamptz not null,
  created_at timestamptz not null,
  unique(realm_id,id),
  foreign key(realm_id,session_binding_id) references hooks.session_binding(realm_id,id),
  foreign key(realm_id,spec_revision_id) references hooks.spec_revision(realm_id,id),
  foreign key(realm_id,runtime_revision_id) references hooks.runtime_revision(realm_id,id),
  check(input_digest ~ '^sha256:[0-9a-f]{64}$' and deadline_at>created_at)
);

create function hooks.enforce_invocation_binding() returns trigger
language plpgsql security invoker set search_path=pg_catalog,hooks as $$
begin
  if new.input_digest<>models.capability_runtime_jsonb_digest(new.input_body)
    or not exists(select 1 from hooks.spec_revision typed
      where typed.realm_id=new.realm_id and typed.id=new.spec_revision_id
        and hooks.json_payload_valid(typed.input_schema,new.input_body))
    or not exists(
    select 1 from hooks.session_binding session
    join hooks.compiled_set_entry entry
      on entry.realm_id=session.realm_id and entry.compiled_set_id=session.compiled_set_id
    join hooks.spec_revision spec
      on spec.realm_id=entry.realm_id and spec.id=entry.spec_revision_id
    where session.realm_id=new.realm_id and session.id=new.session_binding_id
      and session.state='active' and session.generation=new.generation
      and entry.spec_revision_id=new.spec_revision_id
      and entry.runtime_revision_id=new.runtime_revision_id
      and spec.event_type=new.event_type
      and new.deadline_at<=new.created_at+(spec.timeout_ms * interval '1 millisecond')
  ) then
    raise exception 'hook invocation pinned generation/spec/runtime mismatch'
      using errcode='23514';
  end if;
  return new;
end $$;
create trigger hook_invocation_guard before insert on hooks.invocation
for each row execute function hooks.enforce_invocation_binding();

create table hooks.result_receipt (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  invocation_id uuid not null,
  status text not null,
  result_kind text,
  output_body jsonb,
  output_digest text,
  failure_category text,
  failure_digest text,
  latency_ms integer not null,
  completed_at timestamptz not null,
  effect_performed boolean not null default false,
  grants_authority boolean not null default false,
  unique(realm_id,id),unique(realm_id,invocation_id),
  foreign key(realm_id,invocation_id) references hooks.invocation(realm_id,id),
  check(status in ('completed','warning','quarantined','failed')),
  check(result_kind is null or result_kind in ('proposal','deny','observation')),
  check(output_digest is null or output_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(failure_digest is null or failure_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(latency_ms>=0 and not effect_performed and not grants_authority)
);

create function hooks.enforce_result_receipt() returns trigger
language plpgsql security invoker set search_path=pg_catalog,hooks,models as $$
declare invocation record;
begin
  select created_at,deadline_at into invocation from hooks.invocation
    where realm_id=new.realm_id and id=new.invocation_id;
  if (new.status='completed' and
      (new.result_kind is null or new.output_body is null or new.output_digest is null
        or new.failure_category is not null or new.failure_digest is not null
        or new.output_digest<>models.capability_runtime_jsonb_digest(new.output_body)
        or not exists(select 1 from hooks.invocation called
          join hooks.spec_revision typed
            on typed.realm_id=called.realm_id and typed.id=called.spec_revision_id
          where called.realm_id=new.realm_id and called.id=new.invocation_id
            and hooks.json_payload_valid(typed.output_schema,new.output_body))))
    or (new.status<>'completed' and
      (new.result_kind is not null or new.output_body is not null or new.output_digest is not null
        or btrim(coalesce(new.failure_category,''))=''
        or new.failure_digest<>models.capability_runtime_jsonb_digest(
          jsonb_build_object('category',new.failure_category))))
    or new.completed_at<invocation.created_at
    or (new.status='completed' and new.completed_at>invocation.deadline_at) then
    raise exception 'hook result terminal shape mismatch' using errcode='23514';
  end if;
  return new;
end $$;
create trigger hook_result_receipt_guard before insert on hooks.result_receipt
for each row execute function hooks.enforce_result_receipt();

create table hooks.proposal (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  result_receipt_id uuid not null,
  proposal_body jsonb not null,
  proposal_digest text not null,
  status text not null default 'pending-governance',
  grants_authority boolean not null default false,
  unique(realm_id,id),unique(realm_id,result_receipt_id),unique(realm_id,proposal_digest),
  foreign key(realm_id,result_receipt_id) references hooks.result_receipt(realm_id,id),
  check(jsonb_typeof(proposal_body)='object'),
  check(proposal_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(status='pending-governance' and not grants_authority)
);

create function hooks.enforce_proposal() returns trigger
language plpgsql security invoker set search_path=pg_catalog,hooks,models as $$
declare kind text;
begin
  select result_kind into kind from hooks.result_receipt
    where realm_id=new.realm_id and id=new.result_receipt_id;
  if kind is distinct from 'proposal'
    or new.proposal_digest<>models.capability_runtime_jsonb_digest(new.proposal_body) then
    raise exception 'hook proposal kind/body/digest mismatch' using errcode='23514';
  end if;
  return new;
end $$;
create trigger hook_proposal_guard before insert on hooks.proposal
for each row execute function hooks.enforce_proposal();

create function hooks.admit_invocation(
  p_invocation_id uuid,p_session_binding_id uuid,p_event_type text,
  p_spec_revision_id uuid,p_runtime_revision_id uuid,p_input_body jsonb)
returns table(invocation_id uuid,input_digest text,deadline_at timestamptz)
language plpgsql security definer set search_path=pg_catalog,hooks,core,models as $$
declare active_realm uuid; declare started timestamptz; declare timeout_value integer;
declare session_generation integer; declare calculated_digest text;
begin
  active_realm:=core.current_realm_id();
  select generation into session_generation from hooks.session_binding
    where realm_id=active_realm and id=p_session_binding_id and state='active';
  select timeout_ms into timeout_value from hooks.spec_revision
    where realm_id=active_realm and id=p_spec_revision_id;
  if session_generation is null or timeout_value is null then
    raise exception 'hook invocation active session/spec ister' using errcode='23514';
  end if;
  started:=clock_timestamp();
  calculated_digest:=models.capability_runtime_jsonb_digest(p_input_body);
  insert into hooks.invocation(
    id,realm_id,session_binding_id,generation,event_type,spec_revision_id,
    runtime_revision_id,input_body,input_digest,deadline_at,created_at)
    values(p_invocation_id,active_realm,p_session_binding_id,session_generation,p_event_type,
      p_spec_revision_id,p_runtime_revision_id,p_input_body,calculated_digest,
      started+(timeout_value*interval '1 millisecond'),started);
  return query select p_invocation_id,calculated_digest,
    started+(timeout_value*interval '1 millisecond');
end $$;
revoke all on function hooks.admit_invocation(uuid,uuid,text,uuid,uuid,jsonb) from public;
grant execute on function hooks.admit_invocation(uuid,uuid,text,uuid,uuid,jsonb) to zekam_app;

create function hooks.complete_invocation(
  p_receipt_id uuid,p_invocation_id uuid,p_status text,p_result_kind text,
  p_output_body jsonb,p_failure_category text)
returns table(receipt_id uuid,output_digest text,failure_digest text,completed_at timestamptz)
language plpgsql security definer set search_path=pg_catalog,hooks,core,models as $$
declare active_realm uuid; declare finished timestamptz; declare started timestamptz;
declare calculated_output text; declare calculated_failure text;
begin
  active_realm:=core.current_realm_id();
  select created_at into started from hooks.invocation
    where realm_id=active_realm and id=p_invocation_id;
  if started is null then
    raise exception 'hook invocation bulunamadi' using errcode='23514';
  end if;
  finished:=clock_timestamp();
  calculated_output:=case when p_output_body is null then null
    else models.capability_runtime_jsonb_digest(p_output_body) end;
  calculated_failure:=case when p_failure_category is null then null
    else models.capability_runtime_jsonb_digest(
      jsonb_build_object('category',p_failure_category)) end;
  insert into hooks.result_receipt(
    id,realm_id,invocation_id,status,result_kind,output_body,output_digest,
    failure_category,failure_digest,latency_ms,completed_at,effect_performed,grants_authority)
    values(p_receipt_id,active_realm,p_invocation_id,p_status,p_result_kind,p_output_body,
      calculated_output,p_failure_category,calculated_failure,
      greatest(0,(extract(epoch from finished-started)*1000)::integer),finished,false,false);
  if p_result_kind='proposal' then
    insert into hooks.proposal(
      id,realm_id,result_receipt_id,proposal_body,proposal_digest,status,grants_authority)
      values(gen_random_uuid(),active_realm,p_receipt_id,p_output_body,calculated_output,
        'pending-governance',false);
  end if;
  return query select p_receipt_id,calculated_output,calculated_failure,finished;
end $$;
revoke all on function hooks.complete_invocation(uuid,uuid,text,text,jsonb,text) from public;
grant execute on function hooks.complete_invocation(uuid,uuid,text,text,jsonb,text) to zekam_app;

create table hooks.reconfigure_attempt (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  request_digest text not null,
  prior_generation integer,
  proposed_generation integer not null,
  outcome text not null,
  failure_digest text,
  created_at timestamptz not null,
  unique(realm_id,id),unique(realm_id,request_digest),
  check(request_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(proposed_generation>0 and (prior_generation is null or prior_generation>0)),
  check(outcome in ('activated','rejected-required-load')),
  check(failure_digest is null or failure_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table hooks.shutdown_receipt (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  generation integer not null,
  joined_count integer not null,
  cancelled_count integer not null,
  still_running_count integer not null,
  bounded boolean not null,
  completed_at timestamptz not null,
  unique(realm_id,id),unique(realm_id,generation),
  check(generation>0 and joined_count>=0 and cancelled_count>=0 and still_running_count>=0),
  check(bounded)
);

create function hooks.enforce_turn_hook_set_binding() returns trigger
language plpgsql security invoker set search_path=pg_catalog,hooks,runtime as $$
begin
  if exists(select 1 from hooks.compiled_set configured where configured.realm_id=new.realm_id)
    and not exists(
      select 1 from hooks.session_binding session
      join hooks.compiled_set configured
        on configured.realm_id=session.realm_id and configured.id=session.compiled_set_id
      where session.realm_id=new.realm_id and session.session_ref=new.client_session_id
        and session.state='active' and session.generation=configured.generation
        and session.hook_set_digest=new.hook_set_digest
        and session.config_effective_digest=new.config_effective_digest
        and cardinality(configured.required_load_errors)=0
    ) then
    raise exception 'turn hook set exact active session pin ile eslesmiyor'
      using errcode='23514';
  end if;
  return new;
end $$;
create trigger turn_hook_set_binding_guard before insert or update
on runtime.turn_execution_snapshot for each row
execute function hooks.enforce_turn_hook_set_binding();

do $$ declare target text; begin foreach target in array array[
  'hooks.spec_revision','hooks.runtime_revision','hooks.compiled_set',
  'hooks.compiled_set_entry','hooks.current_generation','hooks.session_binding',
  'hooks.invocation','hooks.result_receipt','hooks.proposal','hooks.reconfigure_attempt',
  'hooks.shutdown_receipt'
] loop
  execute format('alter table %s enable row level security',target);
  execute format('alter table %s force row level security',target);
  execute format('create policy scope_select on %s for select using(realm_id=core.current_realm_id())',target);
  execute format('create policy scope_insert on %s for insert with check(realm_id=core.current_realm_id())',target);
  execute format('grant select,insert on %s to zekam_app',target);
end loop; end $$;

revoke insert on hooks.current_generation,hooks.session_binding from zekam_app;
revoke insert on hooks.invocation,hooks.result_receipt,hooks.proposal from zekam_app;

do $$ declare target text; begin foreach target in array array[
  'hooks.spec_revision','hooks.runtime_revision','hooks.compiled_set','hooks.compiled_set_entry',
  'hooks.invocation','hooks.result_receipt','hooks.proposal','hooks.reconfigure_attempt',
  'hooks.shutdown_receipt'
] loop
  execute format('create trigger no_mutation before update or delete on %s '
    'for each statement execute function core.deny_mutation()',target);
end loop; end $$;
