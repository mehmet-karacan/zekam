-- Encrypted, short-lived and authority-free Diagnostic Trace Plane.

create schema if not exists diagnostics;
grant usage on schema diagnostics to zekam_app;

create table diagnostics.trace_bundle (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  trace_ref text not null,
  project_id uuid,
  work_item_id uuid,
  run_id uuid,
  root_assignment_id uuid,
  root_client_session_id text not null,
  policy_digest text not null,
  policy_body jsonb not null,
  manifest_digest text not null,
  manifest_body jsonb not null,
  state text not null default 'open',
  created_at timestamptz not null,
  expires_at timestamptz not null,
  closed_at timestamptz,
  purged_at timestamptz,
  purge_receipt_digest text,
  grants_authority boolean not null default false,
  unique(realm_id,id),unique(realm_id,trace_ref),unique(realm_id,manifest_digest),
  foreign key(realm_id,project_id) references projects.project(realm_id,id),
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id),
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id),
  foreign key(realm_id,root_assignment_id) references agents.assignment(realm_id,id),
  check(btrim(trace_ref)<>'' and btrim(root_client_session_id)<>''),
  check(policy_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(policy_body)='object' and jsonb_typeof(manifest_body)='object'),
  check(state in ('open','closed','expired','purged')),
  check(expires_at>created_at and expires_at<=created_at+interval '30 days'),
  check((state='open' and closed_at is null) or (state<>'open' and closed_at is not null)),
  check((state='purged')=(purged_at is not null and purge_receipt_digest is not null)),
  check(purge_receipt_digest is null or purge_receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(not grants_authority),
  check((work_item_id is null or project_id is not null)
    and (run_id is null or (project_id is not null and work_item_id is not null)))
);

create function diagnostics.enforce_trace_bundle() returns trigger
language plpgsql security invoker
set search_path=pg_catalog,diagnostics,models,runtime,work,agents as $$
declare expected_manifest jsonb; declare scoped record;
begin
  if (select count(*) from jsonb_object_keys(new.policy_body))<>10
    or new.policy_body->>'schema'<>'zekam-diagnostic-trace-policy/v1'
    or jsonb_typeof(new.policy_body->'enabled')<>'boolean'
    or jsonb_typeof(new.policy_body->'retention_days')<>'number'
    or jsonb_typeof(new.policy_body->'max_payload_bytes')<>'number'
    or jsonb_typeof(new.policy_body->'max_events')<>'number'
    or jsonb_typeof(new.policy_body->'max_total_bytes')<>'number'
    or jsonb_typeof(new.policy_body->'encryption_key_ref')<>'string'
    or jsonb_typeof(new.policy_body->'export_allowed')<>'boolean'
    or jsonb_typeof(new.policy_body->'redaction_profile')<>'string'
    or jsonb_typeof(new.policy_body->'grants_authority')<>'boolean'
    or coalesce((new.policy_body->>'enabled')::boolean,false) is not true
    or coalesce((new.policy_body->>'grants_authority')::boolean,true) is not false
    or (new.policy_body->>'retention_days')::integer not between 1 and 30
    or (new.policy_body->>'max_payload_bytes')::bigint not between 1 and 8388608
    or (new.policy_body->>'max_events')::bigint not between 1 and 100000
    or (new.policy_body->>'max_total_bytes')::bigint
      not between (new.policy_body->>'max_payload_bytes')::bigint and 536870912
    or btrim(coalesce(new.policy_body->>'encryption_key_ref',''))=''
    or btrim(coalesce(new.policy_body->>'redaction_profile',''))=''
    or new.expires_at<>new.created_at+((new.policy_body->>'retention_days')::integer*interval '1 day')
    or new.policy_digest<>models.capability_runtime_jsonb_digest(new.policy_body) then
    raise exception 'diagnostic trace policy invalid veya digest drift' using errcode='23514';
  end if;
  if new.work_item_id is not null then
    select project_id into scoped from work.work_item
      where realm_id=new.realm_id and id=new.work_item_id;
    if not found or scoped.project_id<>new.project_id then
      raise exception 'diagnostic trace work/project causal binding drift' using errcode='23514';
    end if;
  end if;
  if new.run_id is not null then
    select project_id,work_item_id into scoped from runtime.execution_run
      where realm_id=new.realm_id and id=new.run_id;
    if not found or row(scoped.project_id,scoped.work_item_id)
      is distinct from row(new.project_id,new.work_item_id) then
      raise exception 'diagnostic trace run/work/project causal binding drift' using errcode='23514';
    end if;
  end if;
  if new.root_assignment_id is not null then
    select project_id,work_item_id into scoped from agents.assignment
      where realm_id=new.realm_id and id=new.root_assignment_id;
    if not found or row(scoped.project_id,scoped.work_item_id)
      is distinct from row(new.project_id,new.work_item_id) then
      raise exception 'diagnostic trace assignment/work/project causal binding drift'
        using errcode='23514';
    end if;
  end if;
  expected_manifest:=jsonb_build_object(
    'schema','zekam-diagnostic-trace-manifest/v1','id',new.id::text,
    'realm_id',new.realm_id::text,'trace_ref',new.trace_ref,
    'project_id',case when new.project_id is null then null else to_jsonb(new.project_id::text) end,
    'work_item_id',case when new.work_item_id is null then null else to_jsonb(new.work_item_id::text) end,
    'run_id',case when new.run_id is null then null else to_jsonb(new.run_id::text) end,
    'root_assignment_id',case when new.root_assignment_id is null then null
      else to_jsonb(new.root_assignment_id::text) end,
    'root_client_session_id',new.root_client_session_id,'policy_digest',new.policy_digest,
    'created_at',runtime.environment_canonical_timestamp(new.created_at),
    'expires_at',runtime.environment_canonical_timestamp(new.expires_at),
    'grants_authority',false);
  if new.manifest_body<>expected_manifest
    or new.manifest_digest<>models.capability_runtime_jsonb_digest(expected_manifest) then
    raise exception 'diagnostic trace manifest digest drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger diagnostic_trace_bundle_guard before insert on diagnostics.trace_bundle
for each row execute function diagnostics.enforce_trace_bundle();

create table diagnostics.payload_ref (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  trace_id uuid not null,
  object_digest text not null,
  cipher_digest text not null,
  plain_digest text not null,
  plain_size_bytes bigint not null,
  cipher_size_bytes bigint not null,
  encryption_key_ref text not null,
  redaction_digest text not null,
  durability_receipt_body jsonb not null,
  durability_receipt_digest text not null,
  created_at timestamptz not null,
  retention_until timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),unique(realm_id,trace_id,object_digest),
  foreign key(realm_id,trace_id) references diagnostics.trace_bundle(realm_id,id),
  check(object_digest ~ '^sha256:[0-9a-f]{64}$' and cipher_digest=object_digest),
  check(plain_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(redaction_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(durability_receipt_body)='object'),
  check(durability_receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(plain_size_bytes>0 and cipher_size_bytes>plain_size_bytes
    and btrim(encryption_key_ref)<>'' and retention_until>created_at),
  check(not grants_authority)
);

create table diagnostics.trace_event (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  trace_id uuid not null,
  sequence integer not null,
  event_type text not null,
  visibility text not null,
  occurred_at timestamptz not null,
  correlation jsonb not null,
  payload_ref_id uuid not null,
  previous_event_digest text,
  event_digest text not null,
  event_body jsonb not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),unique(realm_id,trace_id,sequence),unique(realm_id,trace_id,event_digest),
  foreign key(realm_id,trace_id) references diagnostics.trace_bundle(realm_id,id),
  foreign key(realm_id,payload_ref_id) references diagnostics.payload_ref(realm_id,id),
  check(sequence>0 and jsonb_typeof(correlation)='object'),
  check(event_type in ('session-started','session-stopped','turn-started','turn-completed',
    'user-input','context-fragment-selected','context-serialized','model-request-prepared',
    'model-request','model-response','tool-request','tool-result','terminal-output',
    'agent-spawn','agent-task','agent-result','agent-close','compaction-requested',
    'compaction-installed','checkpoint-created','environment-probed','environment-drifted',
    'runtime-state','error')),
  check(visibility in ('model-visible','runtime-only','diagnostic-only')),
  check(visibility<>'model-visible' or event_type in ('model-request','model-response')),
  check(previous_event_digest is null or previous_event_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(event_digest ~ '^sha256:[0-9a-f]{64}$'),check(not grants_authority)
);

create function diagnostics.enforce_trace_event() returns trigger
language plpgsql security invoker set search_path=pg_catalog,diagnostics,models,runtime as $$
declare bundle record; declare payload record; declare prior record; declare expected jsonb;
declare used_count integer; declare used_bytes bigint;
begin
  perform pg_advisory_xact_lock(hashtextextended(new.realm_id::text||':'||new.trace_id::text,0));
  select * into bundle from diagnostics.trace_bundle
    where realm_id=new.realm_id and id=new.trace_id;
  select * into payload from diagnostics.payload_ref
    where realm_id=new.realm_id and id=new.payload_ref_id and trace_id=new.trace_id;
  select sequence,event_digest into prior from diagnostics.trace_event
    where realm_id=new.realm_id and trace_id=new.trace_id order by sequence desc limit 1;
  select count(*),coalesce(sum(p.plain_size_bytes),0) into used_count,used_bytes
    from diagnostics.trace_event e join diagnostics.payload_ref p
      on p.realm_id=e.realm_id and p.id=e.payload_ref_id
    where e.realm_id=new.realm_id and e.trace_id=new.trace_id;
  if bundle.id is null or payload.id is null or bundle.state<>'open'
    or statement_timestamp()>bundle.expires_at
    or new.sequence<>coalesce(prior.sequence,0)+1
    or new.previous_event_digest is distinct from prior.event_digest
    or payload.plain_size_bytes>(bundle.policy_body->>'max_payload_bytes')::bigint
    or used_count>=(bundle.policy_body->>'max_events')::integer
    or used_bytes+payload.plain_size_bytes>(bundle.policy_body->>'max_total_bytes')::bigint then
    raise exception 'diagnostic trace sequence/bundle/payload/quota binding mismatch'
      using errcode='23514';
  end if;
  if (select count(*) from jsonb_object_keys(new.correlation)) not between 1 and 16 then
    raise exception 'diagnostic trace correlation alan siniri gecersiz' using errcode='23514';
  end if;
  if new.correlation ? 'parent_event_id' and not exists(
    select 1 from diagnostics.trace_event parent
    where parent.realm_id=new.realm_id and parent.trace_id=new.trace_id
      and parent.id::text=new.correlation->>'parent_event_id'
      and parent.sequence<new.sequence
  ) then
    raise exception 'diagnostic trace parent event missing/reordered' using errcode='23514';
  end if;
  expected:=jsonb_build_object(
    'schema','zekam-diagnostic-trace-event/v1','id',new.id::text,
    'realm_id',new.realm_id::text,'bundle_id',new.trace_id::text,'sequence',new.sequence,
    'event_type',new.event_type,'visibility',new.visibility,
    'occurred_at',runtime.environment_canonical_timestamp(new.occurred_at),
    'correlation',new.correlation,'payload_ref',payload.object_digest,
    'payload_cipher_digest',payload.cipher_digest,'payload_size_bytes',payload.plain_size_bytes,
    'payload_plain_digest',payload.plain_digest,
    'encryption_key_ref',payload.encryption_key_ref,'redaction_digest',payload.redaction_digest,
    'previous_event_digest',new.previous_event_digest,'grants_authority',false);
  if new.event_body<>expected
    or new.event_digest<>models.capability_runtime_jsonb_digest(expected) then
    raise exception 'diagnostic trace event body/digest drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger diagnostic_trace_event_guard before insert on diagnostics.trace_event
for each row execute function diagnostics.enforce_trace_event();

create function diagnostics.append_trace_event(
  p_trace_id uuid,p_event_id uuid,p_payload_id uuid,p_event_type text,p_visibility text,
  p_occurred_at timestamptz,p_correlation jsonb,p_object_digest text,p_cipher_digest text,
  p_plain_digest text,p_plain_size_bytes bigint,p_cipher_size_bytes bigint,
  p_encryption_key_ref text,p_redaction_digest text,
  p_durability_receipt_body jsonb,p_durability_receipt_digest text
) returns table(out_sequence integer,out_event_digest text)
language plpgsql security definer
set search_path=pg_catalog,diagnostics,models,runtime,core as $$
declare active_realm uuid; declare bundle record; declare prior record;
declare receipt_body jsonb; declare receipt_digest text; declare body jsonb;
begin
  active_realm:=core.current_realm_id();
  perform pg_advisory_xact_lock(hashtextextended(active_realm::text||':'||p_trace_id::text,0));
  select * into bundle from diagnostics.trace_bundle
    where realm_id=active_realm and id=p_trace_id;
  if not found then
    raise exception 'diagnostic trace bundle bulunamadi' using errcode='23503';
  end if;
  select sequence,event_digest into prior from diagnostics.trace_event
    where realm_id=active_realm and trace_id=p_trace_id order by sequence desc limit 1;
  out_sequence:=coalesce(prior.sequence,0)+1;
  receipt_body:=jsonb_build_object(
    'schema','zekam-trace-payload-durability-receipt/v2',
    'trace_id',p_trace_id::text,'event_id',p_event_id::text,
    'object',jsonb_build_object(
      'digest',p_object_digest,'size_bytes',p_cipher_size_bytes,
      'stored_at',p_durability_receipt_body->'object'->'stored_at',
      'media_type','application/vnd.zekam.trace+ciphertext',
      'metadata',jsonb_build_object('cipher','aes-256-gcm','purpose','diagnostic-trace')),
    'durable_before_event',true);
  receipt_digest:=models.capability_runtime_jsonb_digest(receipt_body);
  if p_durability_receipt_body<>receipt_body
    or p_durability_receipt_digest<>receipt_digest then
    raise exception 'CAS-issued durability receipt binding mismatch' using errcode='23514';
  end if;
  insert into diagnostics.payload_ref(
    id,realm_id,trace_id,object_digest,cipher_digest,plain_digest,plain_size_bytes,
    cipher_size_bytes,
    encryption_key_ref,redaction_digest,durability_receipt_body,durability_receipt_digest,created_at,
    retention_until,grants_authority)
  values(p_payload_id,active_realm,p_trace_id,p_object_digest,p_cipher_digest,p_plain_digest,
    p_plain_size_bytes,p_cipher_size_bytes,p_encryption_key_ref,p_redaction_digest,
    receipt_body,receipt_digest,p_occurred_at,
    bundle.expires_at,false);
  body:=jsonb_build_object(
    'schema','zekam-diagnostic-trace-event/v1','id',p_event_id::text,
    'realm_id',active_realm::text,'bundle_id',p_trace_id::text,'sequence',out_sequence,
    'event_type',p_event_type,'visibility',p_visibility,
    'occurred_at',runtime.environment_canonical_timestamp(p_occurred_at),
    'correlation',p_correlation,'payload_ref',p_object_digest,
    'payload_cipher_digest',p_cipher_digest,'payload_plain_digest',p_plain_digest,
    'payload_size_bytes',p_plain_size_bytes,'encryption_key_ref',p_encryption_key_ref,
    'redaction_digest',p_redaction_digest,
    'previous_event_digest',prior.event_digest,'grants_authority',false);
  out_event_digest:=models.capability_runtime_jsonb_digest(body);
  insert into diagnostics.trace_event(
    id,realm_id,trace_id,sequence,event_type,visibility,occurred_at,correlation,
    payload_ref_id,previous_event_digest,event_digest,event_body,grants_authority)
  values(p_event_id,active_realm,p_trace_id,out_sequence,p_event_type,p_visibility,
    p_occurred_at,p_correlation,p_payload_id,prior.event_digest,out_event_digest,body,false);
  return next;
end $$;
revoke all on function diagnostics.append_trace_event(
  uuid,uuid,uuid,text,text,timestamptz,jsonb,text,text,text,bigint,bigint,text,text,jsonb,text) from public;
grant execute on function diagnostics.append_trace_event(
  uuid,uuid,uuid,text,text,timestamptz,jsonb,text,text,text,bigint,bigint,text,text,jsonb,text) to zekam_app;

create table diagnostics.reduction (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  trace_id uuid not null,
  reducer_version text not null,
  source_event_count integer not null,
  source_head_digest text not null,
  output_digest text not null,
  reduced_body jsonb not null,
  state text not null,
  failure_category text,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),unique(realm_id,trace_id,reducer_version,source_head_digest),
  foreign key(realm_id,trace_id) references diagnostics.trace_bundle(realm_id,id),
  check(btrim(reducer_version)<>'' and source_event_count>0),
  check(source_head_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(output_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(reduced_body)='object'),
  check(state in ('completed','failed')),
  check((state='completed')=(failure_category is null)),check(not grants_authority)
);

create function diagnostics.expected_reduction_body(p_trace_id uuid) returns jsonb
language sql stable security definer
set search_path=pg_catalog,diagnostics,core as $$
  with scoped as (
    select e.id,e.sequence,e.event_type,e.visibility,e.correlation,e.event_digest,
      p.plain_digest
    from diagnostics.trace_event e
    join diagnostics.payload_ref p on p.realm_id=e.realm_id and p.id=e.payload_ref_id
    where e.realm_id=core.current_realm_id() and e.trace_id=p_trace_id
  ), nodes as (
    select coalesce(jsonb_agg(jsonb_build_object(
      'node_id','event:'||id::text,
      'kind',case
        when visibility='model-visible' then 'ConversationItem'
        when event_type in ('model-request-prepared','model-request','model-response')
          then 'InferenceCall'
        when event_type in ('tool-request','tool-result') then 'ToolCall'
        when event_type='terminal-output' then 'TerminalOperation'
        when event_type in ('agent-spawn','agent-task','agent-result','agent-close')
          then 'AgentThread'
        when event_type in ('compaction-requested','compaction-installed') then 'Compaction'
        when event_type in ('environment-probed','environment-drifted')
          then 'EnvironmentSnapshot'
        else 'RawPayloadRef' end,
      'visibility',visibility,'sequence',sequence,'payload_digest',plain_digest
    ) order by sequence),'[]'::jsonb) value from scoped
  ), edges as (
    select coalesce(jsonb_agg(edge order by sequence,edge_order),'[]'::jsonb) value
    from (
      select current.sequence,1 edge_order,jsonb_build_object(
        'source_node_id','event:'||previous.id::text,
        'target_node_id','event:'||current.id::text,'kind','next') edge
      from scoped current join scoped previous on previous.sequence=current.sequence-1
      union all
      select current.sequence,2,jsonb_build_object(
        'source_node_id','event:'||(current.correlation->>'parent_event_id'),
        'target_node_id','event:'||current.id::text,'kind','caused')
      from scoped current where current.correlation ? 'parent_event_id'
    ) ordered_edges
  ), summary as (
    select count(*)::integer event_count,
      (array_agg(event_digest order by sequence))[1] first_digest,
      (array_agg(event_digest order by sequence desc))[1] last_digest from scoped
  )
  select jsonb_build_object(
    'schema','zekam-diagnostic-trace-graph/v1','bundle_id',p_trace_id::text,
    'event_count',summary.event_count,'nodes',nodes.value,'edges',edges.value,
    'first_event_digest',summary.first_digest,'last_event_digest',summary.last_digest,
    'grants_authority',false)
  from summary,nodes,edges
$$;
revoke all on function diagnostics.expected_reduction_body(uuid) from public;
grant execute on function diagnostics.expected_reduction_body(uuid) to zekam_app;

create function diagnostics.enforce_reduction() returns trigger
language plpgsql security invoker set search_path=pg_catalog,diagnostics,models as $$
declare actual_count integer; declare actual_head text; declare expected jsonb;
begin
  select count(*),(array_agg(event_digest order by sequence desc))[1]
    into actual_count,actual_head from diagnostics.trace_event
    where realm_id=new.realm_id and trace_id=new.trace_id;
  expected:=diagnostics.expected_reduction_body(new.trace_id);
  if (new.source_event_count,new.source_head_digest) is distinct from (actual_count,actual_head)
    or new.reduced_body<>expected
    or new.output_digest<>models.capability_runtime_jsonb_digest(expected) then
    raise exception 'diagnostic reduction source/output digest mismatch' using errcode='23514';
  end if;
  return new;
end $$;
create trigger diagnostic_reduction_guard before insert on diagnostics.reduction
for each row execute function diagnostics.enforce_reduction();

create function diagnostics.store_reduction(
  p_id uuid,p_trace_id uuid,p_source_event_count integer,p_source_head_digest text,
  p_output_digest text,p_reduced_body jsonb,p_created_at timestamptz
) returns table(out_id uuid,out_created boolean)
language plpgsql security definer set search_path=pg_catalog,diagnostics,core as $$
declare active_realm uuid; declare found_id uuid;
begin
  active_realm:=core.current_realm_id();
  insert into diagnostics.reduction(
    id,realm_id,trace_id,reducer_version,source_event_count,source_head_digest,
    output_digest,reduced_body,state,failure_category,created_at,grants_authority)
  values(p_id,active_realm,p_trace_id,'v1',p_source_event_count,p_source_head_digest,
    p_output_digest,p_reduced_body,'completed',null,p_created_at,false)
  on conflict(realm_id,trace_id,reducer_version,source_head_digest) do nothing returning id
  into found_id;
  if found_id is not null then
    out_id:=found_id; out_created:=true; return next; return;
  end if;
  select id into out_id from diagnostics.reduction
    where realm_id=active_realm and trace_id=p_trace_id and reducer_version='v1'
      and source_head_digest=p_source_head_digest;
  out_created:=false; return next;
end $$;
revoke all on function diagnostics.store_reduction(
  uuid,uuid,integer,text,text,jsonb,timestamptz) from public;
grant execute on function diagnostics.store_reduction(
  uuid,uuid,integer,text,text,jsonb,timestamptz) to zekam_app;

create table diagnostics.access_event (
  id uuid primary key,realm_id uuid not null references core.realm(id) on delete restrict,
  trace_id uuid not null,operation text not null,actor_ref text not null,
  authorization_ref text,occurred_at timestamptz not null,event_digest text not null,
  unique(realm_id,id),foreign key(realm_id,trace_id) references diagnostics.trace_bundle(realm_id,id),
  check(operation in ('read','reduce','export','purge')),
  check(btrim(actor_ref)<>''),check(event_digest ~ '^sha256:[0-9a-f]{64}$')
);

create function diagnostics.enforce_access_event() returns trigger
language plpgsql security invoker set search_path=pg_catalog,diagnostics,models,runtime as $$
declare expected jsonb;
begin
  if new.operation in ('reduce','export','purge')
    and btrim(coalesce(new.authorization_ref,''))='' then
    raise exception 'diagnostic trace reduce/export/purge authorization ister'
      using errcode='23514';
  end if;
  expected:=jsonb_build_object(
    'schema','zekam-diagnostic-access-event/v1','id',new.id::text,
    'realm_id',new.realm_id::text,'trace_id',new.trace_id::text,
    'operation',new.operation,'actor_ref',new.actor_ref,
    'authorization_ref',new.authorization_ref,
    'occurred_at',runtime.environment_canonical_timestamp(new.occurred_at));
  if new.event_digest<>models.capability_runtime_jsonb_digest(expected) then
    raise exception 'diagnostic access event digest mismatch' using errcode='23514';
  end if;
  return new;
end $$;
create trigger diagnostic_access_event_guard before insert on diagnostics.access_event
for each row execute function diagnostics.enforce_access_event();

create function diagnostics.reject_direct_memory_promotion() returns trigger
language plpgsql security invoker set search_path=pg_catalog,diagnostics,memory as $$
begin
  if exists(select 1 from jsonb_array_elements(new.evidence) item
    where item->>'kind'='diagnostic-trace'
      or coalesce(item->>'reference','') like 'db:diagnostics.%') then
    raise exception 'diagnostic trace direct memory candidate/promosyon kaynagi olamaz'
      using errcode='23514';
  end if;
  return new;
end $$;
create trigger diagnostic_trace_memory_candidate_guard
before insert or update on memory.candidate for each row
execute function diagnostics.reject_direct_memory_promotion();

create function diagnostics.close_trace(p_trace_id uuid) returns boolean
language plpgsql security definer set search_path=pg_catalog,diagnostics,core as $$
declare changed integer; declare active_realm uuid;
begin
  active_realm:=core.current_realm_id();
  update diagnostics.trace_bundle set state='closed',closed_at=clock_timestamp()
    where realm_id=active_realm and id=p_trace_id and state='open';
  get diagnostics changed=row_count;
  return changed=1;
end $$;
revoke all on function diagnostics.close_trace(uuid) from public;
grant execute on function diagnostics.close_trace(uuid) to zekam_app;

create function diagnostics.purge_trace(
  p_trace_id uuid,p_purged_at timestamptz,p_receipt_digest text,p_authorization_ref text
) returns boolean
language plpgsql security definer set search_path=pg_catalog,diagnostics,models,runtime,core as $$
declare changed integer; declare active_realm uuid; declare access_id uuid; declare access_body jsonb;
begin
  active_realm:=core.current_realm_id();
  if p_purged_at>clock_timestamp()+interval '1 second'
    or p_receipt_digest !~ '^sha256:[0-9a-f]{64}$'
    or btrim(coalesce(p_authorization_ref,''))='' then
    raise exception 'diagnostic trace purge receipt/authorization invalid' using errcode='23514';
  end if;
  update diagnostics.trace_bundle
    set state='purged',closed_at=coalesce(closed_at,p_purged_at),purged_at=p_purged_at,
      purge_receipt_digest=p_receipt_digest
    where realm_id=active_realm and id=p_trace_id and state in ('open','closed','expired')
      and expires_at<=p_purged_at;
  get diagnostics changed=row_count;
  if changed<>1 then return false; end if;
  access_id:=gen_random_uuid();
  access_body:=jsonb_build_object(
    'schema','zekam-diagnostic-access-event/v1','id',access_id::text,
    'realm_id',active_realm::text,'trace_id',p_trace_id::text,'operation','purge',
    'actor_ref','zekam-diagnostic-retention','authorization_ref',p_authorization_ref,
    'occurred_at',runtime.environment_canonical_timestamp(p_purged_at));
  insert into diagnostics.access_event(
    id,realm_id,trace_id,operation,actor_ref,authorization_ref,occurred_at,event_digest)
  values(access_id,active_realm,p_trace_id,'purge','zekam-diagnostic-retention',
    p_authorization_ref,p_purged_at,models.capability_runtime_jsonb_digest(access_body));
  return true;
end $$;
revoke all on function diagnostics.purge_trace(uuid,timestamptz,text,text) from public;
grant execute on function diagnostics.purge_trace(uuid,timestamptz,text,text) to zekam_app;

do $$ declare target text; begin foreach target in array array[
  'diagnostics.trace_bundle','diagnostics.payload_ref','diagnostics.trace_event',
  'diagnostics.reduction','diagnostics.access_event'
] loop
  execute format('alter table %s enable row level security',target);
  execute format('alter table %s force row level security',target);
  execute format('create policy scope_select on %s for select using(realm_id=core.current_realm_id())',target);
  execute format('create policy scope_insert on %s for insert with check(realm_id=core.current_realm_id())',target);
  execute format('grant select,insert on %s to zekam_app',target);
end loop; end $$;

revoke update,delete on diagnostics.trace_bundle from zekam_app;
revoke insert,update,delete on diagnostics.payload_ref,diagnostics.trace_event from zekam_app;
revoke insert,update,delete on diagnostics.reduction from zekam_app;
do $$ declare target text; begin foreach target in array array[
  'diagnostics.payload_ref','diagnostics.trace_event','diagnostics.reduction',
  'diagnostics.access_event'
] loop
  execute format('create trigger no_mutation before update or delete on %s '
    'for each statement execute function core.deny_mutation()',target);
end loop; end $$;

comment on schema diagnostics is
  'Kisa retentionli encrypted debugging duzlemi; Work/Run/audit/recovery authority degildir.';
