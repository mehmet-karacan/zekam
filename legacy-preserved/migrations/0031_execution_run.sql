-- Canonical execution run, exact context packet and fully-bound execution envelope.

alter table work.context_manifest
  add constraint context_manifest_realm_scoped_key unique(realm_id,id);
alter table work.checkpoint
  add constraint checkpoint_realm_scoped_key unique(realm_id,id);

create table runtime.execution_run (
  id uuid primary key, realm_id uuid not null references core.realm(id) on delete restrict,
  project_id uuid not null, work_item_id uuid not null, plan_id uuid not null,
  client_id text not null, session_id text, source_revision text not null,
  policy_digest text not null, max_input_tokens integer not null,
  max_output_tokens integer not null, max_cost_micros bigint not null,
  deadline timestamptz not null, state text not null default 'prepared',
  input_tokens_used integer not null default 0, output_tokens_used integer not null default 0,
  cost_micros_used bigint not null default 0, run_digest text not null,
  grants_authority boolean not null default false, created_at timestamptz not null,
  started_at timestamptz, terminal_at timestamptz,
  unique(realm_id,id), unique(realm_id,run_digest),
  foreign key(realm_id,project_id) references projects.project(realm_id,id),
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id),
  foreign key(realm_id,plan_id) references work.task_plan(realm_id,id),
  check(btrim(client_id)<>'' and btrim(source_revision)<>''),
  check(session_id is null or btrim(session_id)<>''),
  check(policy_digest ~ '^sha256:[0-9a-f]{64}$' and run_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(max_input_tokens>0 and max_output_tokens>0 and max_cost_micros>0),
  check(input_tokens_used>=0 and output_tokens_used>=0 and cost_micros_used>=0),
  check(input_tokens_used<=max_input_tokens and output_tokens_used<=max_output_tokens
        and cost_micros_used<=max_cost_micros),
  check(deadline>created_at),
  check(state in ('prepared','active','completed','failed','cancelled','reconciliation-required')),
  check(not grants_authority),
  check((state='prepared' and started_at is null and terminal_at is null)
    or (state='active' and started_at is not null and terminal_at is null)
    or (state in ('completed','failed','cancelled','reconciliation-required')
      and started_at is not null and terminal_at is not null))
);

create function runtime.enforce_execution_run_plan() returns trigger
language plpgsql security invoker set search_path=pg_catalog,runtime,work as $$
declare p record;
begin
  select project_id,work_item_id,source_revision,policy_digest into p
    from work.task_plan where realm_id=new.realm_id and id=new.plan_id;
  if not found or row(p.project_id,p.work_item_id,p.source_revision,p.policy_digest)
    is distinct from row(new.project_id,new.work_item_id,new.source_revision,new.policy_digest) then
    raise exception 'execution run plan/source/policy binding drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger execution_run_plan_check before insert on runtime.execution_run
for each row execute function runtime.enforce_execution_run_plan();

create function runtime.enforce_execution_run_transition() returns trigger language plpgsql as $$
begin
  if row(old.id,old.realm_id,old.project_id,old.work_item_id,old.plan_id,old.client_id,
      old.session_id,old.source_revision,old.policy_digest,old.max_input_tokens,
      old.max_output_tokens,old.max_cost_micros,old.deadline,old.run_digest,
      old.grants_authority,old.created_at) is distinct from
    row(new.id,new.realm_id,new.project_id,new.work_item_id,new.plan_id,new.client_id,
      new.session_id,new.source_revision,new.policy_digest,new.max_input_tokens,
      new.max_output_tokens,new.max_cost_micros,new.deadline,new.run_digest,
      new.grants_authority,new.created_at) then
    raise exception 'execution run identity/budget degistirilemez' using errcode='42501';
  end if;
  if old.started_at is not null and new.started_at is distinct from old.started_at then
    raise exception 'execution run started_at degistirilemez' using errcode='42501';
  end if;
  if old.terminal_at is not null and new.terminal_at is distinct from old.terminal_at then
    raise exception 'execution run terminal_at degistirilemez' using errcode='42501';
  end if;
  if new.started_at is not null and (new.started_at<new.created_at or new.started_at>=new.deadline
      or new.started_at>statement_timestamp())
    or new.terminal_at is not null and (new.started_at is null or new.terminal_at<new.started_at) then
    raise exception 'execution run temporal sirasi gecersiz' using errcode='23514';
  end if;
  if new.input_tokens_used<old.input_tokens_used or new.output_tokens_used<old.output_tokens_used
     or new.cost_micros_used<old.cost_micros_used then
    raise exception 'execution run usage azaltilamaz' using errcode='23514';
  end if;
  if old.state='prepared' and new.state='active' then return new; end if;
  if old.state='active' and new.state in
     ('active','completed','failed','cancelled','reconciliation-required') then return new; end if;
  raise exception 'execution run state gecisi gecersiz' using errcode='23514';
end $$;
create trigger execution_run_transition before update on runtime.execution_run
for each row execute function runtime.enforce_execution_run_transition();
create trigger execution_run_no_delete before delete on runtime.execution_run
for each statement execute function core.deny_mutation();

create table work.context_packet (
  id uuid primary key, realm_id uuid not null references core.realm(id) on delete restrict,
  project_id uuid not null, work_item_id uuid not null, manifest_id uuid not null,
  manifest_digest text not null, ordered_sections jsonb not null, packet_digest text not null,
  grants_authority boolean not null default false, created_at timestamptz not null,
  unique(realm_id,id), unique(realm_id,packet_digest),
  foreign key(realm_id,project_id) references projects.project(realm_id,id),
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id),
  foreign key(realm_id,manifest_id) references work.context_manifest(realm_id,id),
  check(manifest_digest ~ '^sha256:[0-9a-f]{64}$'
    and packet_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(ordered_sections)='array' and jsonb_array_length(ordered_sections)>0),
  check(not grants_authority)
);

create function work.enforce_context_packet() returns trigger
language plpgsql security invoker set search_path=pg_catalog,work as $$
declare m record; packet_projection jsonb; manifest_projection jsonb;
begin
  select project_id,work_item_id,manifest_digest,selected into m from work.context_manifest
    where realm_id=new.realm_id and id=new.manifest_id;
  if not found or row(m.project_id,m.work_item_id,m.manifest_digest) is distinct from
    row(new.project_id,new.work_item_id,new.manifest_digest) then
    raise exception 'context packet manifest scope/digest drift' using errcode='23514';
  end if;
  select jsonb_agg(jsonb_build_object('candidate_id',section->>'candidate_id',
      'content_digest',section->>'content_digest') order by (section->>'ordinal')::integer)
    into packet_projection from jsonb_array_elements(new.ordered_sections) section;
  select jsonb_agg(jsonb_build_object('candidate_id',section->>'candidate_id',
      'content_digest',section->>'content_digest') order by ordinal)
    into manifest_projection from jsonb_array_elements(m.selected) with ordinality x(section,ordinal);
  if packet_projection is distinct from manifest_projection then
    raise exception 'context packet sections manifest selected ile exact eslesmiyor'
      using errcode='23514';
  end if;
  if exists(select 1 from jsonb_array_elements(new.ordered_sections) with ordinality x(section,n)
      where (section->>'ordinal')::integer<>n or section->>'candidate_id' is null
      or section->>'content_digest' !~ '^sha256:[0-9a-f]{64}$'
      or (select array_agg(key order by key) from jsonb_object_keys(section) key)
         <> array['candidate_id','content_digest','ordinal']) then
    raise exception 'context packet ordinal/section gecersiz' using errcode='23514';
  end if;
  return new;
end $$;
create trigger context_packet_check before insert on work.context_packet
for each row execute function work.enforce_context_packet();
create trigger context_packet_no_mutation before update or delete on work.context_packet
for each statement execute function core.deny_mutation();

create table models.provider_binding_snapshot (
  id uuid primary key, realm_id uuid not null references core.realm(id) on delete restrict,
  model_id text not null, provider_ref text not null, endpoint_ref text not null,
  operation text not null, binding_digest text not null,
  captured_at timestamptz not null, expires_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,binding_digest),
  foreign key(realm_id,model_id) references models.model_inventory(realm_id,model_id),
  check(btrim(provider_ref)<>'' and btrim(endpoint_ref)<>'' and btrim(operation)<>''),
  check(binding_digest ~ '^sha256:[0-9a-f]{64}$' and expires_at>captured_at),
  check(not grants_authority)
);
create trigger provider_binding_snapshot_no_mutation
before update or delete on models.provider_binding_snapshot
for each statement execute function core.deny_mutation();

alter table runtime.job add column run_id uuid;
alter table runtime.job add constraint job_run_same_realm
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id);
create function runtime.enforce_job_run_binding() returns trigger
language plpgsql security invoker set search_path=pg_catalog,runtime as $$
declare r record;
begin
  if tg_op='UPDATE' and old.run_id is not null and new.run_id is distinct from old.run_id then
    raise exception 'job execution run binding degistirilemez' using errcode='42501';
  end if;
  if new.run_id is null then return new; end if;
  select project_id,work_item_id,plan_id into r from runtime.execution_run
    where realm_id=new.realm_id and id=new.run_id;
  if not found or row(r.project_id,r.work_item_id,r.plan_id) is distinct from
    row(new.project_id,new.work_item_id,new.plan_id) then
    raise exception 'job execution run scope drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger job_run_binding before insert or update of run_id on runtime.job
for each row execute function runtime.enforce_job_run_binding();

create table runtime.execution_envelope (
  id uuid primary key, realm_id uuid not null references core.realm(id) on delete restrict,
  run_id uuid not null, job_id uuid not null, attempt_id uuid not null, lease_id uuid not null,
  fencing_token bigint not null, request_ordinal integer not null,
  idempotency_key text not null, assignment_id uuid not null, role text not null,
  route_decision_id uuid not null, route_decision_digest text not null,
  route_expires_at timestamptz not null, model_id text not null,
  provider_binding_id uuid not null, provider_binding_digest text not null,
  provider_ref text not null,
  context_manifest_id uuid not null, context_manifest_digest text not null,
  context_packet_id uuid not null, context_packet_digest text not null,
  checkpoint_id uuid, checkpoint_digest text, checkpoint_disposition text not null,
  source_revision text not null, policy_digest text not null,
  authorization_scope_digest text not null, output_schema_digest text not null,
  payload_digest text not null, max_input_tokens integer not null,
  max_output_tokens integer not null, max_cost_micros bigint not null,
  deadline timestamptz not null, envelope_digest text not null,
  grants_authority boolean not null default false, created_at timestamptz not null,
  unique(realm_id,id), unique(realm_id,envelope_digest),
  unique(realm_id,run_id,job_id,attempt_id,request_ordinal),
  unique(realm_id,run_id,idempotency_key),
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id),
  foreign key(realm_id,job_id) references runtime.job(realm_id,id),
  foreign key(realm_id,attempt_id) references runtime.job_attempt(realm_id,id),
  foreign key(realm_id,lease_id) references runtime.lease(realm_id,id),
  foreign key(realm_id,assignment_id) references agents.assignment(realm_id,id),
  foreign key(realm_id,route_decision_id) references models.model_route_decision(realm_id,id),
  foreign key(realm_id,model_id) references models.model_inventory(realm_id,model_id),
  foreign key(realm_id,provider_binding_id)
    references models.provider_binding_snapshot(realm_id,id),
  foreign key(realm_id,context_manifest_id) references work.context_manifest(realm_id,id),
  foreign key(realm_id,context_packet_id) references work.context_packet(realm_id,id),
  foreign key(realm_id,checkpoint_id) references work.checkpoint(realm_id,id),
  check(fencing_token>0 and request_ordinal>0 and btrim(idempotency_key)<>''
    and max_input_tokens>0 and max_output_tokens>0 and max_cost_micros>0),
  check(checkpoint_disposition in ('bound','not-applicable-genesis')),
  check((checkpoint_disposition='bound' and checkpoint_id is not null and checkpoint_digest is not null)
    or (checkpoint_disposition='not-applicable-genesis' and checkpoint_id is null
      and checkpoint_digest is null)),
  check(route_expires_at>=deadline and deadline>created_at),
  check(btrim(role)<>'' and btrim(provider_ref)<>'' and btrim(source_revision)<>''),
  check(route_decision_digest ~ '^sha256:[0-9a-f]{64}$'
    and context_manifest_digest ~ '^sha256:[0-9a-f]{64}$'
    and context_packet_digest ~ '^sha256:[0-9a-f]{64}$'
    and provider_binding_digest ~ '^sha256:[0-9a-f]{64}$'
    and (checkpoint_digest is null or checkpoint_digest ~ '^sha256:[0-9a-f]{64}$')
    and policy_digest ~ '^sha256:[0-9a-f]{64}$'
    and authorization_scope_digest ~ '^sha256:[0-9a-f]{64}$'
    and output_schema_digest ~ '^sha256:[0-9a-f]{64}$'
    and payload_digest ~ '^sha256:[0-9a-f]{64}$'
    and envelope_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(not grants_authority)
);

create function runtime.enforce_execution_envelope() returns trigger
language plpgsql security invoker set search_path=pg_catalog,runtime,work,agents,models as $$
declare r record; j record; a record; l record; ar record; rd record; pb record;
  cm record; cp record; ck record;
  route_role text;
begin
  select project_id,work_item_id,plan_id,source_revision,policy_digest,max_input_tokens,
    max_output_tokens,max_cost_micros,deadline,state,started_at into r from runtime.execution_run
    where realm_id=new.realm_id and id=new.run_id;
  if not found or r.state<>'active' or row(r.source_revision,r.policy_digest,r.max_input_tokens,
      r.max_output_tokens,r.max_cost_micros,r.deadline) is distinct from
      row(new.source_revision,new.policy_digest,new.max_input_tokens,new.max_output_tokens,
      new.max_cost_micros,new.deadline) or new.created_at<r.started_at
      or new.created_at>statement_timestamp() then
    raise exception 'execution envelope run binding/current state drift' using errcode='23514';
  end if;
  select project_id,work_item_id,plan_id,step_id,assignment_id,run_id,state into j from runtime.job
    where realm_id=new.realm_id and id=new.job_id;
  select job_id,fencing_token,attempt_number,outcome into a from runtime.job_attempt
    where realm_id=new.realm_id and id=new.attempt_id;
  select job_id,attempt_id,fencing_token,expires_at into l from runtime.lease
    where realm_id=new.realm_id and id=new.lease_id;
  if j.state<>'running' or row(j.project_id,j.work_item_id,j.plan_id,j.assignment_id,j.run_id) is distinct from
      row(r.project_id,r.work_item_id,r.plan_id,new.assignment_id,new.run_id)
    or row(a.job_id,a.fencing_token,a.outcome) is distinct from row(new.job_id,new.fencing_token,null)
    or row(l.job_id,l.attempt_id,l.fencing_token) is distinct from
      row(new.job_id,new.attempt_id,new.fencing_token)
    or l.expires_at<=statement_timestamp() then
    raise exception 'execution envelope job/attempt/lease/fence drift' using errcode='23514';
  end if;
  select project_id,work_item_id,plan_id,step_id,role,context_manifest_digest,status into ar
    from agents.assignment where realm_id=new.realm_id and id=new.assignment_id;
  if ar.status<>'active' or row(ar.project_id,ar.work_item_id,ar.plan_id,ar.step_id,ar.role,ar.context_manifest_digest)
    is distinct from row(r.project_id,r.work_item_id,r.plan_id,j.step_id,new.role,
      new.context_manifest_digest) then
    raise exception 'execution envelope assignment drift' using errcode='23514';
  end if;
  select d.role,d.status,d.primary_model_id,d.evidence_digest,d.policy_digest,d.decided_at,
      t.captured_at,t.expires_at into rd
    from models.model_route_decision d
    join models.execution_target_snapshot t
      on t.realm_id=d.realm_id and t.id=d.execution_target_id
    where d.realm_id=new.realm_id and d.id=new.route_decision_id;
  select model_id,provider_ref,binding_digest,captured_at,expires_at into pb
    from models.provider_binding_snapshot
    where realm_id=new.realm_id and id=new.provider_binding_id;
  route_role := case new.role
    when 'builder' then 'implementer'
    when 'reviewer' then 'reviewer'
    when 'researcher' then 'researcher'
    when 'critic' then 'researcher'
    when 'synthesizer' then 'researcher'
    when 'verifier' then 'verifier'
    else null
  end;
  if route_role is null or rd.status<>'selected'
    or row(rd.role,rd.primary_model_id,rd.evidence_digest,rd.policy_digest)
    is distinct from row(route_role,new.model_id,new.route_decision_digest,new.policy_digest)
    or new.route_expires_at is distinct from rd.expires_at
    or row(pb.model_id,pb.provider_ref,pb.binding_digest) is distinct from
      row(new.model_id,new.provider_ref,new.provider_binding_digest)
    or rd.decided_at>new.created_at or rd.captured_at>new.created_at
    or pb.captured_at>new.created_at or pb.expires_at<=statement_timestamp()
    or new.route_expires_at<=statement_timestamp() then
    raise exception 'execution envelope route/model/policy drift' using errcode='23514';
  end if;
  select project_id,work_item_id,manifest_digest into cm from work.context_manifest
    where realm_id=new.realm_id and id=new.context_manifest_id;
  select manifest_id,manifest_digest,packet_digest into cp from work.context_packet
    where realm_id=new.realm_id and id=new.context_packet_id;
  if row(cm.project_id,cm.work_item_id,cm.manifest_digest) is distinct from
      row(r.project_id,r.work_item_id,new.context_manifest_digest)
    or row(cp.manifest_id,cp.manifest_digest,cp.packet_digest) is distinct from
      row(new.context_manifest_id,new.context_manifest_digest,new.context_packet_digest) then
    raise exception 'execution envelope context drift' using errcode='23514';
  end if;
  if new.checkpoint_disposition='bound' then
    select project_id,work_item_id,task_plan_id,checkpoint_digest into ck from work.checkpoint
      where realm_id=new.realm_id and id=new.checkpoint_id;
    if row(ck.project_id,ck.work_item_id,ck.task_plan_id,ck.checkpoint_digest) is distinct from
      row(r.project_id,r.work_item_id,r.plan_id,new.checkpoint_digest) then
      raise exception 'execution envelope checkpoint drift' using errcode='23514';
    end if;
  elsif a.attempt_number<>1 or exists(select 1 from work.checkpoint where realm_id=new.realm_id
      and job_id=new.job_id) then
    raise exception 'genesis checkpoint disposition yalniz ilk checkpointsiz attempt icin'
      using errcode='23514';
  end if;
  return new;
end $$;
create trigger execution_envelope_check before insert on runtime.execution_envelope
for each row execute function runtime.enforce_execution_envelope();
create trigger execution_envelope_no_mutation before update or delete on runtime.execution_envelope
for each statement execute function core.deny_mutation();

do $$ declare target text; begin foreach target in array array[
  'runtime.execution_run','runtime.execution_envelope','work.context_packet',
  'models.provider_binding_snapshot'
] loop
 execute format('alter table %s enable row level security',target);
 execute format('alter table %s force row level security',target);
 execute format('create policy scope_select on %s for select using(realm_id=core.current_realm_id())',target);
 execute format('create policy scope_insert on %s for insert with check(realm_id=core.current_realm_id())',target);
end loop; end $$;
create policy scope_update on runtime.execution_run for update
 using(realm_id=core.current_realm_id()) with check(realm_id=core.current_realm_id());
grant select,insert,update on runtime.execution_run to zekam_app;
grant select,insert on runtime.execution_envelope,work.context_packet to zekam_app;
grant select,insert on models.provider_binding_snapshot to zekam_app;
