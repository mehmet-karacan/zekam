-- Persisted runtime agent graph, shared root budget and typed communication.

create table agents.graph_root (
  id uuid primary key,
  realm_id uuid not null,
  run_id uuid not null,
  coordinator_assignment_id uuid not null,
  max_concurrency integer not null,
  max_input_tokens integer not null,
  max_output_tokens integer not null,
  max_cost_micros bigint not null,
  active_count integer not null default 0,
  reserved_input_tokens integer not null default 0,
  reserved_output_tokens integer not null default 0,
  reserved_cost_micros bigint not null default 0,
  used_input_tokens integer not null default 0,
  used_output_tokens integer not null default 0,
  used_cost_micros bigint not null default 0,
  root_digest text not null,
  root_body jsonb not null,
  grants_authority boolean not null default false,
  created_at timestamptz not null,
  unique(realm_id,id),unique(realm_id,run_id),unique(realm_id,root_digest),
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id),
  foreign key(realm_id,coordinator_assignment_id) references agents.assignment(realm_id,id),
  check(max_concurrency>0 and max_input_tokens>0 and max_output_tokens>0 and max_cost_micros>0),
  check(active_count>=0 and active_count<=max_concurrency),
  check(reserved_input_tokens>=0 and reserved_output_tokens>=0 and reserved_cost_micros>=0),
  check(used_input_tokens>=0 and used_output_tokens>=0 and used_cost_micros>=0),
  check(reserved_input_tokens+used_input_tokens<=max_input_tokens),
  check(reserved_output_tokens+used_output_tokens<=max_output_tokens),
  check(reserved_cost_micros+used_cost_micros<=max_cost_micros),
  check(root_digest ~ '^sha256:[0-9a-f]{64}$' and not grants_authority)
);

create function agents.enforce_graph_root_binding() returns trigger language plpgsql
security invoker set search_path=pg_catalog,agents,runtime as $$
declare r record; declare a record;
begin
  select project_id,work_item_id,plan_id,max_input_tokens,max_output_tokens,max_cost_micros,
    input_tokens_used,output_tokens_used,cost_micros_used,state
    into r from runtime.execution_run where realm_id=new.realm_id and id=new.run_id;
  if not found or r.state<>'active' then
    raise exception 'agent graph root active execution run ister' using errcode='23514';
  end if;
  select project_id,work_item_id,plan_id,role,parent_assignment_id
    into a from agents.assignment
    where realm_id=new.realm_id and id=new.coordinator_assignment_id;
  if not found or a.role<>'coordinator' or a.parent_assignment_id is not null
    or row(a.project_id,a.work_item_id,a.plan_id) is distinct from
       row(r.project_id,r.work_item_id,r.plan_id)
    or new.max_input_tokens>r.max_input_tokens-r.input_tokens_used
    or new.max_output_tokens>r.max_output_tokens-r.output_tokens_used
    or new.max_cost_micros>r.max_cost_micros-r.cost_micros_used then
    raise exception 'agent graph root run/coordinator/budget binding gecersiz' using errcode='23514';
  end if;
  if models.capability_runtime_jsonb_digest(new.root_body)<>new.root_digest
    or (select count(*) from jsonb_object_keys(new.root_body))<>11
    or new.root_body->>'schema'<>'zekam-agent-graph-root/v1'
    or new.root_body->>'id'<>new.id::text
    or new.root_body->>'realm_id'<>new.realm_id::text
    or new.root_body->>'run_id'<>new.run_id::text
    or new.root_body->>'coordinator_assignment_id'<>new.coordinator_assignment_id::text
    or (new.root_body->>'max_concurrency')::integer<>new.max_concurrency
    or (new.root_body->>'max_input_tokens')::integer<>new.max_input_tokens
    or (new.root_body->>'max_output_tokens')::integer<>new.max_output_tokens
    or (new.root_body->>'max_cost_micros')::bigint<>new.max_cost_micros
    or (new.root_body->>'created_at')::timestamptz<>new.created_at
    or (new.root_body->>'grants_authority')::boolean then
    raise exception 'agent graph root body/digest drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger graph_root_binding before insert on agents.graph_root
for each row execute function agents.enforce_graph_root_binding();

create function agents.enforce_graph_root_update() returns trigger language plpgsql as $$
begin
  if row(old.id,old.realm_id,old.run_id,old.coordinator_assignment_id,old.max_concurrency,
      old.max_input_tokens,old.max_output_tokens,old.max_cost_micros,old.root_digest,
      old.root_body,old.grants_authority,old.created_at) is distinct from
    row(new.id,new.realm_id,new.run_id,new.coordinator_assignment_id,new.max_concurrency,
      new.max_input_tokens,new.max_output_tokens,new.max_cost_micros,new.root_digest,
      new.root_body,new.grants_authority,new.created_at) then
    raise exception 'agent graph root identity degistirilemez' using errcode='42501';
  end if;
  if new.used_input_tokens<old.used_input_tokens
    or new.used_output_tokens<old.used_output_tokens
    or new.used_cost_micros<old.used_cost_micros then
    raise exception 'agent graph kullanimi azaltilamaz' using errcode='23514';
  end if;
  return new;
end $$;
create trigger graph_root_update before update on agents.graph_root
for each row execute function agents.enforce_graph_root_update();

create table agents.spawn_edge (
  id uuid primary key,
  realm_id uuid not null,
  root_id uuid not null,
  parent_assignment_id uuid not null,
  child_assignment_id uuid not null,
  status text not null default 'reserved',
  reserved_input_tokens integer not null,
  reserved_output_tokens integer not null,
  reserved_cost_micros bigint not null,
  input_tokens_used integer,
  output_tokens_used integer,
  cost_micros_used bigint,
  edge_digest text not null,
  edge_body jsonb not null,
  grants_authority boolean not null default false,
  created_at timestamptz not null,
  terminal_at timestamptz,
  unique(realm_id,id),unique(realm_id,child_assignment_id),unique(realm_id,edge_digest),
  foreign key(realm_id,root_id) references agents.graph_root(realm_id,id),
  foreign key(realm_id,parent_assignment_id) references agents.assignment(realm_id,id),
  foreign key(realm_id,child_assignment_id) references agents.assignment(realm_id,id),
  check(parent_assignment_id<>child_assignment_id),
  check(status in ('reserved','active','blocked','completed','failed','cancelled')),
  check(reserved_input_tokens>0 and reserved_output_tokens>0 and reserved_cost_micros>0),
  check(edge_digest ~ '^sha256:[0-9a-f]{64}$' and not grants_authority),
  check((status in ('completed','failed','cancelled'))=(terminal_at is not null)),
  check((terminal_at is null and input_tokens_used is null and output_tokens_used is null
      and cost_micros_used is null) or
    (terminal_at is not null and input_tokens_used between 0 and reserved_input_tokens
      and output_tokens_used between 0 and reserved_output_tokens
      and cost_micros_used between 0 and reserved_cost_micros))
);

create function agents.enforce_spawn_edge_binding() returns trigger language plpgsql
security invoker set search_path=pg_catalog,agents,runtime as $$
declare g record; declare child record; declare run record;
begin
  select coordinator_assignment_id,run_id into g from agents.graph_root
    where realm_id=new.realm_id and id=new.root_id;
  if not found then raise exception 'spawn edge root bulunamadi' using errcode='23514'; end if;
  select parent_assignment_id,project_id,work_item_id,plan_id into child from agents.assignment
    where realm_id=new.realm_id and id=new.child_assignment_id;
  if not found then raise exception 'spawn edge child assignment bulunamadi' using errcode='23514'; end if;
  select project_id,work_item_id,plan_id,state into run from runtime.execution_run
    where realm_id=new.realm_id and id=g.run_id;
  if not found or run.state<>'active' or new.parent_assignment_id<>g.coordinator_assignment_id
    or child.parent_assignment_id is distinct from new.parent_assignment_id
    or row(child.project_id,child.work_item_id,child.plan_id) is distinct from
       row(run.project_id,run.work_item_id,run.plan_id) then
    raise exception 'spawn edge canonical assignment/root/run binding gecersiz' using errcode='23514';
  end if;
  if models.capability_runtime_jsonb_digest(new.edge_body)<>new.edge_digest
    or (select count(*) from jsonb_object_keys(new.edge_body))<>12
    or new.edge_body->>'schema'<>'zekam-agent-spawn-edge/v1'
    or new.edge_body->>'id'<>new.id::text
    or new.edge_body->>'realm_id'<>new.realm_id::text
    or new.edge_body->>'root_id'<>new.root_id::text
    or new.edge_body->>'parent_assignment_id'<>new.parent_assignment_id::text
    or new.edge_body->>'child_assignment_id'<>new.child_assignment_id::text
    or (new.edge_body->>'reserved_input_tokens')::integer<>new.reserved_input_tokens
    or (new.edge_body->>'reserved_output_tokens')::integer<>new.reserved_output_tokens
    or (new.edge_body->>'reserved_cost_micros')::bigint<>new.reserved_cost_micros
    or new.edge_body->>'status'<>'reserved'
    or (new.edge_body->>'created_at')::timestamptz<>new.created_at
    or (new.edge_body->>'grants_authority')::boolean then
    raise exception 'spawn edge body/digest drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger spawn_edge_binding before insert on agents.spawn_edge
for each row execute function agents.enforce_spawn_edge_binding();

create function agents.enforce_spawn_edge_update() returns trigger language plpgsql as $$
begin
  if row(old.id,old.realm_id,old.root_id,old.parent_assignment_id,old.child_assignment_id,
      old.reserved_input_tokens,old.reserved_output_tokens,old.reserved_cost_micros,
      old.edge_digest,old.edge_body,old.grants_authority,old.created_at) is distinct from
    row(new.id,new.realm_id,new.root_id,new.parent_assignment_id,new.child_assignment_id,
      new.reserved_input_tokens,new.reserved_output_tokens,new.reserved_cost_micros,
      new.edge_digest,new.edge_body,new.grants_authority,new.created_at) then
    raise exception 'spawn edge identity degistirilemez' using errcode='42501';
  end if;
  if old.status='reserved' and new.status in ('active','blocked','completed','failed','cancelled')
    or old.status='active' and new.status in ('active','blocked','completed','failed','cancelled')
    or old.status='blocked' and new.status in ('active','blocked','completed','failed','cancelled') then
    return new;
  end if;
  raise exception 'spawn edge status gecisi gecersiz' using errcode='23514';
end $$;
create trigger spawn_edge_update before update on agents.spawn_edge
for each row execute function agents.enforce_spawn_edge_update();

create table agents.child_status_event (
  id uuid primary key,
  realm_id uuid not null,
  root_id uuid not null,
  edge_id uuid not null,
  sequence integer not null,
  previous_digest text,
  status text not null,
  event_digest text not null,
  event_body jsonb not null,
  occurred_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),unique(realm_id,edge_id,sequence),unique(realm_id,event_digest),
  foreign key(realm_id,root_id) references agents.graph_root(realm_id,id),
  foreign key(realm_id,edge_id) references agents.spawn_edge(realm_id,id),
  check(sequence>0 and status in ('reserved','active','blocked','completed','failed','cancelled')),
  check((sequence=1)=(previous_digest is null)),
  check(previous_digest is null or previous_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(event_digest ~ '^sha256:[0-9a-f]{64}$' and not grants_authority)
);

create function agents.enforce_child_status_event() returns trigger language plpgsql
security invoker set search_path=pg_catalog,agents,models as $$
declare edge record; declare previous record;
begin
  select root_id,status into edge from agents.spawn_edge
    where realm_id=new.realm_id and id=new.edge_id;
  if not found or edge.root_id<>new.root_id or edge.status<>new.status then
    raise exception 'child status event edge/root/current status drift' using errcode='23514';
  end if;
  select sequence,event_digest,occurred_at into previous from agents.child_status_event
    where realm_id=new.realm_id and edge_id=new.edge_id order by sequence desc limit 1;
  if (not found and (new.sequence<>1 or new.previous_digest is not null))
    or (found and (new.sequence<>previous.sequence+1
      or new.previous_digest is distinct from previous.event_digest
      or new.occurred_at<previous.occurred_at)) then
    raise exception 'child status event chain drift' using errcode='23514';
  end if;
  if models.capability_runtime_jsonb_digest(new.event_body)<>new.event_digest
    or (select count(*) from jsonb_object_keys(new.event_body))<>10
    or new.event_body->>'schema'<>'zekam-agent-child-status/v1'
    or new.event_body->>'id'<>new.id::text
    or new.event_body->>'realm_id'<>new.realm_id::text
    or new.event_body->>'root_id'<>new.root_id::text
    or new.event_body->>'edge_id'<>new.edge_id::text
    or (new.event_body->>'sequence')::integer<>new.sequence
    or new.event_body->>'previous_digest' is distinct from new.previous_digest
    or new.event_body->>'status'<>new.status
    or (new.event_body->>'occurred_at')::timestamptz<>new.occurred_at
    or (new.event_body->>'grants_authority')::boolean then
    raise exception 'child status event body/digest drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger child_status_event_guard before insert on agents.child_status_event
for each row execute function agents.enforce_child_status_event();

create table agents.message (
  id uuid primary key,
  realm_id uuid not null,
  root_id uuid not null,
  sender_assignment_id uuid not null,
  recipient_assignment_id uuid not null,
  context_type text not null,
  context_ref text not null,
  context_digest text not null,
  payload_schema text not null,
  payload jsonb not null,
  message_body jsonb not null,
  message_digest text not null,
  grants_authority boolean not null default false,
  created_at timestamptz not null,
  unique(realm_id,id),unique(realm_id,message_digest),
  foreign key(realm_id,root_id) references agents.graph_root(realm_id,id),
  foreign key(realm_id,sender_assignment_id) references agents.assignment(realm_id,id),
  foreign key(realm_id,recipient_assignment_id) references agents.assignment(realm_id,id),
  check(sender_assignment_id<>recipient_assignment_id),
  check(btrim(context_type)<>'' and btrim(context_ref)<>'' and btrim(payload_schema)<>''),
  check(context_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(payload)='object' and payload<>'{}'::jsonb and octet_length(payload::text)<=65536),
  check(message_digest ~ '^sha256:[0-9a-f]{64}$' and not grants_authority)
);

create function agents.enforce_message_membership() returns trigger language plpgsql
security invoker set search_path=pg_catalog,agents as $$
declare coordinator uuid;
declare recipient record;
begin
  select coordinator_assignment_id into coordinator from agents.graph_root
    where realm_id=new.realm_id and id=new.root_id;
  if not found or not (
    (new.sender_assignment_id=coordinator or exists(select 1 from agents.spawn_edge
      where realm_id=new.realm_id and root_id=new.root_id
        and child_assignment_id=new.sender_assignment_id))
    and
    (new.recipient_assignment_id=coordinator or exists(select 1 from agents.spawn_edge
      where realm_id=new.realm_id and root_id=new.root_id
        and child_assignment_id=new.recipient_assignment_id))
  ) then raise exception 'agent message root membership ister' using errcode='23514'; end if;
  select context_manifest_digest into recipient from agents.assignment
    where realm_id=new.realm_id and id=new.recipient_assignment_id;
  if new.context_type<>'assignment-context-manifest'
    or new.context_ref<>new.recipient_assignment_id::text
    or new.context_digest<>recipient.context_manifest_digest then
    raise exception 'agent message canonical assignment context binding ister' using errcode='23514';
  end if;
  if models.capability_runtime_jsonb_digest(new.message_body)<>new.message_digest
    or (select count(*) from jsonb_object_keys(new.message_body))<>11
    or (select count(*) from jsonb_object_keys(new.message_body->'context'))<>3
    or new.message_body->>'schema'<>'zekam-agent-message/v1'
    or new.message_body->>'id'<>new.id::text
    or new.message_body->>'realm_id'<>new.realm_id::text
    or new.message_body->>'root_id'<>new.root_id::text
    or new.message_body->>'sender_assignment_id'<>new.sender_assignment_id::text
    or new.message_body->>'recipient_assignment_id'<>new.recipient_assignment_id::text
    or new.message_body#>>'{context,type}'<>new.context_type
    or new.message_body#>>'{context,ref}'<>new.context_ref
    or new.message_body#>>'{context,digest}'<>new.context_digest
    or new.message_body->>'payload_schema'<>new.payload_schema
    or new.message_body->'payload' is distinct from new.payload
    or (new.message_body->>'created_at')::timestamptz<>new.created_at
    or (new.message_body->>'grants_authority')::boolean then
    raise exception 'agent message body/digest drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger message_membership before insert on agents.message
for each row execute function agents.enforce_message_membership();

create function agents.reserve_spawn_edge(
  p_id uuid,p_realm_id uuid,p_root_id uuid,p_parent_assignment_id uuid,
  p_child_assignment_id uuid,p_reserved_input_tokens integer,
  p_reserved_output_tokens integer,p_reserved_cost_micros bigint,p_edge_digest text,
  p_edge_body jsonb,p_created_at timestamptz
) returns void language plpgsql security definer
set search_path=pg_catalog,agents,core,models as $$
declare root record; declare event_id uuid:=gen_random_uuid(); declare event_body jsonb;
begin
  if p_realm_id is distinct from core.current_realm_id() then
    raise exception 'cross-realm spawn reservation denied' using errcode='42501';
  end if;
  select * into root from agents.graph_root
    where realm_id=p_realm_id and id=p_root_id for update;
  if not found then raise exception 'agent graph root bulunamadi' using errcode='P0002'; end if;
  if root.active_count>=root.max_concurrency then
    raise exception 'agent graph concurrency butcesi tukendi' using errcode='23514';
  end if;
  if root.reserved_input_tokens+root.used_input_tokens+p_reserved_input_tokens
       >root.max_input_tokens
    or root.reserved_output_tokens+root.used_output_tokens+p_reserved_output_tokens
       >root.max_output_tokens
    or root.reserved_cost_micros+root.used_cost_micros+p_reserved_cost_micros
       >root.max_cost_micros then
    raise exception 'agent graph token/maliyet butcesi tukendi' using errcode='23514';
  end if;
  insert into agents.spawn_edge
    (id,realm_id,root_id,parent_assignment_id,child_assignment_id,status,
     reserved_input_tokens,reserved_output_tokens,reserved_cost_micros,edge_digest,edge_body,
     created_at)
    values(p_id,p_realm_id,p_root_id,p_parent_assignment_id,p_child_assignment_id,'reserved',
      p_reserved_input_tokens,p_reserved_output_tokens,p_reserved_cost_micros,p_edge_digest,
      p_edge_body,p_created_at);
  update agents.graph_root set active_count=active_count+1,
    reserved_input_tokens=reserved_input_tokens+p_reserved_input_tokens,
    reserved_output_tokens=reserved_output_tokens+p_reserved_output_tokens,
    reserved_cost_micros=reserved_cost_micros+p_reserved_cost_micros
    where realm_id=p_realm_id and id=p_root_id;
  event_body:=jsonb_build_object('schema','zekam-agent-child-status/v1',
    'id',event_id::text,'realm_id',p_realm_id::text,'root_id',p_root_id::text,
    'edge_id',p_id::text,'sequence',1,'previous_digest',null,'status','reserved',
    'occurred_at',to_jsonb(p_created_at)#>>'{}','grants_authority',false);
  insert into agents.child_status_event
    (id,realm_id,root_id,edge_id,sequence,previous_digest,status,event_digest,event_body,
     occurred_at)
    values(event_id,p_realm_id,p_root_id,p_id,1,null,'reserved',
      models.capability_runtime_jsonb_digest(event_body),event_body,p_created_at);
end $$;

create function agents.transition_graph_child(
  p_realm_id uuid,p_edge_id uuid,p_status text,p_occurred_at timestamptz,
  p_input_tokens_used integer,p_output_tokens_used integer,p_cost_micros_used bigint
) returns void language plpgsql security definer
set search_path=pg_catalog,agents,core,models as $$
declare edge record; declare previous record; declare terminal boolean;
declare event_id uuid:=gen_random_uuid(); declare event_body jsonb;
begin
  if p_realm_id is distinct from core.current_realm_id() then
    raise exception 'cross-realm child transition denied' using errcode='42501';
  end if;
  select * into edge from agents.spawn_edge
    where realm_id=p_realm_id and id=p_edge_id for update;
  if not found then raise exception 'spawn edge bulunamadi' using errcode='P0002'; end if;
  if edge.status in ('completed','failed','cancelled') then
    raise exception 'terminal child status degistirilemez' using errcode='23514';
  end if;
  if p_status=edge.status then
    raise exception 'duplicate child status transition reddedildi' using errcode='23514';
  end if;
  if not ((edge.status='reserved' and p_status in
      ('active','blocked','completed','failed','cancelled'))
    or (edge.status='active' and p_status in
      ('active','blocked','completed','failed','cancelled'))
    or (edge.status='blocked' and p_status in
      ('active','blocked','completed','failed','cancelled'))) then
    raise exception 'spawn edge status gecisi gecersiz' using errcode='23514';
  end if;
  terminal := p_status in ('completed','failed','cancelled');
  if least(p_input_tokens_used,p_output_tokens_used,p_cost_micros_used)<0
    or p_input_tokens_used>edge.reserved_input_tokens
    or p_output_tokens_used>edge.reserved_output_tokens
    or p_cost_micros_used>edge.reserved_cost_micros
    or (not terminal and greatest(p_input_tokens_used,p_output_tokens_used,p_cost_micros_used)>0)
    then raise exception 'child usage reservation/status gecersiz' using errcode='23514';
  end if;
  perform 1 from agents.graph_root where realm_id=p_realm_id and id=edge.root_id for update;
  if terminal then
    update agents.spawn_edge set status=p_status,terminal_at=p_occurred_at,
      input_tokens_used=p_input_tokens_used,output_tokens_used=p_output_tokens_used,
      cost_micros_used=p_cost_micros_used where realm_id=p_realm_id and id=p_edge_id;
    update agents.graph_root set active_count=active_count-1,
      reserved_input_tokens=reserved_input_tokens-edge.reserved_input_tokens,
      reserved_output_tokens=reserved_output_tokens-edge.reserved_output_tokens,
      reserved_cost_micros=reserved_cost_micros-edge.reserved_cost_micros,
      used_input_tokens=used_input_tokens+p_input_tokens_used,
      used_output_tokens=used_output_tokens+p_output_tokens_used,
      used_cost_micros=used_cost_micros+p_cost_micros_used
      where realm_id=p_realm_id and id=edge.root_id;
  else
    update agents.spawn_edge set status=p_status where realm_id=p_realm_id and id=p_edge_id;
  end if;
  select sequence,event_digest,occurred_at into strict previous from agents.child_status_event
    where realm_id=p_realm_id and edge_id=p_edge_id order by sequence desc limit 1;
  if p_occurred_at<previous.occurred_at then
    raise exception 'child status zamani geriye gidemez' using errcode='23514';
  end if;
  event_body:=jsonb_build_object('schema','zekam-agent-child-status/v1',
    'id',event_id::text,'realm_id',p_realm_id::text,'root_id',edge.root_id::text,
    'edge_id',p_edge_id::text,'sequence',previous.sequence+1,
    'previous_digest',previous.event_digest,'status',p_status,
    'occurred_at',to_jsonb(p_occurred_at)#>>'{}','grants_authority',false);
  insert into agents.child_status_event
    (id,realm_id,root_id,edge_id,sequence,previous_digest,status,event_digest,event_body,
     occurred_at)
    values(event_id,p_realm_id,edge.root_id,p_edge_id,previous.sequence+1,
      previous.event_digest,p_status,models.capability_runtime_jsonb_digest(event_body),
      event_body,p_occurred_at);
end $$;

create trigger graph_root_no_delete before delete on agents.graph_root
for each statement execute function core.deny_mutation();
create trigger spawn_edge_no_delete before delete on agents.spawn_edge
for each statement execute function core.deny_mutation();
create trigger child_status_event_no_mutation before update or delete on agents.child_status_event
for each statement execute function core.deny_mutation();
create trigger agent_message_no_mutation before update or delete on agents.message
for each statement execute function core.deny_mutation();

do $$ declare target text; begin foreach target in array array[
  'agents.graph_root','agents.spawn_edge','agents.child_status_event','agents.message'
] loop
  execute format('alter table %s enable row level security',target);
  execute format('alter table %s force row level security',target);
  execute format('create policy scope_select on %s for select using (realm_id=core.current_realm_id())',target);
  execute format('create policy scope_insert on %s for insert with check (realm_id=core.current_realm_id())',target);
end loop; end $$;
create policy scope_update on agents.graph_root for update using(realm_id=core.current_realm_id())
  with check(realm_id=core.current_realm_id());
create policy scope_update on agents.spawn_edge for update using(realm_id=core.current_realm_id())
  with check(realm_id=core.current_realm_id());
grant select,insert on agents.graph_root to zekam_app;
grant select on agents.spawn_edge,agents.child_status_event to zekam_app;
grant select,insert on agents.message to zekam_app;
revoke all on function agents.reserve_spawn_edge(uuid,uuid,uuid,uuid,uuid,integer,integer,
  bigint,text,jsonb,timestamptz) from public;
revoke all on function agents.transition_graph_child(uuid,uuid,text,timestamptz,integer,integer,
  bigint) from public;
grant execute on function agents.reserve_spawn_edge(uuid,uuid,uuid,uuid,uuid,integer,integer,
  bigint,text,jsonb,timestamptz) to zekam_app;
grant execute on function agents.transition_graph_child(uuid,uuid,text,timestamptz,integer,integer,
  bigint) to zekam_app;
