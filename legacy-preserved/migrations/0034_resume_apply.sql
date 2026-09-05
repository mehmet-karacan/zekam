-- Durable, authority-free ResumeCoordinator apply saga evidence.
alter table runtime.execution_envelope
  add column checkpoint_v2_id uuid,
  add column checkpoint_v2_digest text,
  add constraint execution_envelope_checkpoint_v2_same_realm
    foreign key(realm_id,checkpoint_v2_id) references work.checkpoint_v2(realm_id,id),
  add constraint execution_envelope_checkpoint_v2_digest_format
    check(checkpoint_v2_digest is null or checkpoint_v2_digest ~ '^sha256:[0-9a-f]{64}$');
-- Lease rows are ephemeral and are deleted at terminal completion.  Envelope
-- UUID/fence fields remain durable evidence and the insert trigger validates
-- the live lease, but a durable FK would make normal cleanup impossible.
alter table runtime.execution_envelope
  drop constraint execution_envelope_realm_id_lease_id_fkey;
alter table work.checkpoint_v2
  drop constraint checkpoint_v2_realm_id_observed_lease_id_fkey;

do $$ declare constraint_name text; begin
  select c.conname into constraint_name from pg_constraint c
    where c.conrelid='runtime.execution_envelope'::regclass and c.contype='c'
      and pg_get_constraintdef(c.oid) like '%checkpoint_disposition%bound%'
      and pg_get_constraintdef(c.oid) like '%checkpoint_id%';
  if constraint_name is null then
    raise exception 'execution envelope checkpoint shape constraint bulunamadi';
  end if;
  execute format('alter table runtime.execution_envelope drop constraint %I',constraint_name);
end $$;
alter table runtime.execution_envelope add constraint execution_envelope_checkpoint_shape_v2
  check((checkpoint_disposition='bound' and checkpoint_id is not null
      and checkpoint_digest is not null and checkpoint_v2_id is null
      and checkpoint_v2_digest is null)
    or (checkpoint_disposition='bound-v2' and checkpoint_id is null
      and checkpoint_digest is null and checkpoint_v2_id is not null
      and checkpoint_v2_digest is not null)
    or (checkpoint_disposition='not-applicable-genesis' and checkpoint_id is null
      and checkpoint_digest is null and checkpoint_v2_id is null
      and checkpoint_v2_digest is null));
do $$ declare constraint_name text; begin
  select c.conname into constraint_name from pg_constraint c
    where c.conrelid='runtime.execution_envelope'::regclass and c.contype='c'
      and pg_get_constraintdef(c.oid) like '%checkpoint_disposition%'
      and pg_get_constraintdef(c.oid) not like '%checkpoint_id%';
  if constraint_name is null then
    raise exception 'execution envelope checkpoint disposition constraint bulunamadi';
  end if;
  execute format('alter table runtime.execution_envelope drop constraint %I',constraint_name);
end $$;
alter table runtime.execution_envelope
  add constraint execution_envelope_checkpoint_disposition_check
  check(checkpoint_disposition in ('bound','bound-v2','not-applicable-genesis'));

create or replace function runtime.enforce_execution_envelope() returns trigger
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
  if j.state<>'running' or row(j.project_id,j.work_item_id,j.plan_id,j.assignment_id,j.run_id)
      is distinct from row(r.project_id,r.work_item_id,r.plan_id,new.assignment_id,new.run_id)
    or row(a.job_id,a.fencing_token,a.outcome)
      is distinct from row(new.job_id,new.fencing_token,null)
    or row(l.job_id,l.attempt_id,l.fencing_token)
      is distinct from row(new.job_id,new.attempt_id,new.fencing_token)
    or l.expires_at<=statement_timestamp() then
    raise exception 'execution envelope job/attempt/lease/fence drift' using errcode='23514';
  end if;
  select project_id,work_item_id,plan_id,step_id,role,context_manifest_digest,status into ar
    from agents.assignment where realm_id=new.realm_id and id=new.assignment_id;
  if ar.status not in ('ready','active')
    or row(ar.project_id,ar.work_item_id,ar.plan_id,ar.step_id,ar.role,ar.context_manifest_digest)
      is distinct from row(r.project_id,r.work_item_id,r.plan_id,j.step_id,new.role,
        new.context_manifest_digest) then
    raise exception 'execution envelope assignment drift' using errcode='23514';
  end if;
  select d.role,d.status,d.primary_model_id,d.evidence_digest,d.policy_digest,d.decided_at,
      t.captured_at,t.expires_at into rd
    from models.model_route_decision d join models.execution_target_snapshot t
      on t.realm_id=d.realm_id and t.id=d.execution_target_id
    where d.realm_id=new.realm_id and d.id=new.route_decision_id;
  select model_id,provider_ref,binding_digest,captured_at,expires_at into pb
    from models.provider_binding_snapshot
    where realm_id=new.realm_id and id=new.provider_binding_id;
  route_role := case new.role when 'builder' then 'implementer' when 'reviewer' then 'reviewer'
    when 'researcher' then 'researcher' when 'critic' then 'researcher'
    when 'synthesizer' then 'researcher' when 'verifier' then 'verifier' else null end;
  if route_role is null or rd.status<>'selected'
    or row(rd.role,rd.primary_model_id,rd.evidence_digest,rd.policy_digest)
      is distinct from row(route_role,new.model_id,new.route_decision_digest,new.policy_digest)
    or new.route_expires_at is distinct from rd.expires_at
    or row(pb.model_id,pb.provider_ref,pb.binding_digest)
      is distinct from row(new.model_id,new.provider_ref,new.provider_binding_digest)
    or rd.decided_at>new.created_at or rd.captured_at>new.created_at
    or pb.captured_at>new.created_at or pb.expires_at<=statement_timestamp()
    or new.route_expires_at<=statement_timestamp() then
    raise exception 'execution envelope route/model/policy drift' using errcode='23514';
  end if;
  select project_id,work_item_id,manifest_digest into cm from work.context_manifest
    where realm_id=new.realm_id and id=new.context_manifest_id;
  select manifest_id,manifest_digest,packet_digest into cp from work.context_packet
    where realm_id=new.realm_id and id=new.context_packet_id;
  if row(cm.project_id,cm.work_item_id,cm.manifest_digest)
      is distinct from row(r.project_id,r.work_item_id,new.context_manifest_digest)
    or row(cp.manifest_id,cp.manifest_digest,cp.packet_digest)
      is distinct from row(new.context_manifest_id,new.context_manifest_digest,
        new.context_packet_digest) then
    raise exception 'execution envelope context drift' using errcode='23514';
  end if;
  if new.checkpoint_disposition='bound' then
    select project_id,work_item_id,task_plan_id,checkpoint_digest into ck from work.checkpoint
      where realm_id=new.realm_id and id=new.checkpoint_id;
    if row(ck.project_id,ck.work_item_id,ck.task_plan_id,ck.checkpoint_digest)
        is distinct from row(r.project_id,r.work_item_id,r.plan_id,new.checkpoint_digest) then
      raise exception 'execution envelope checkpoint drift' using errcode='23514';
    end if;
  elsif new.checkpoint_disposition='bound-v2' then
    select project_id,work_item_id,task_plan_id,checkpoint_digest into ck
      from work.checkpoint_v2 where realm_id=new.realm_id and id=new.checkpoint_v2_id;
    if row(ck.project_id,ck.work_item_id,ck.task_plan_id,ck.checkpoint_digest)
        is distinct from row(r.project_id,r.work_item_id,r.plan_id,new.checkpoint_v2_digest) then
      raise exception 'execution envelope checkpoint v2 drift' using errcode='23514';
    end if;
  elsif a.attempt_number<>1 or exists(select 1 from work.checkpoint where realm_id=new.realm_id
      and job_id=new.job_id) or exists(select 1 from work.checkpoint_v2
      where realm_id=new.realm_id and job_id=new.job_id) then
    raise exception 'genesis checkpoint disposition yalniz ilk checkpointsiz attempt icin'
      using errcode='23514';
  end if;
  return new;
end $$;

create table runtime.resume_apply (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  work_item_id uuid not null,
  checkpoint_id uuid not null,
  run_id uuid not null,
  job_id uuid not null,
  actor_id uuid not null,
  authorization_id uuid not null,
  target_client_id text not null,
  resume_plan_digest text not null,
  effect_digest text not null,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,resume_plan_digest),
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id),
  foreign key(realm_id,checkpoint_id) references work.checkpoint_v2(realm_id,id),
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id),
  foreign key(realm_id,job_id) references runtime.job(realm_id,id),
  foreign key(realm_id,actor_id) references core.actor(realm_id,id),
  foreign key(realm_id,authorization_id) references security.authorization(realm_id,id),
  check(btrim(target_client_id)<>''),
  check(resume_plan_digest ~ '^sha256:[0-9a-f]{64}$'
    and effect_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(not grants_authority)
);

create table runtime.resume_apply_event (
  id uuid primary key,
  realm_id uuid not null,
  resume_apply_id uuid not null,
  sequence integer not null,
  phase text not null,
  state text not null,
  reason_code text not null,
  attempt_id uuid,
  lease_id uuid,
  fencing_token bigint,
  claim_id uuid,
  receipt_id uuid,
  result_digest text,
  previous_digest text,
  event_digest text not null,
  event_body jsonb not null,
  occurred_at timestamptz not null,
  unique(realm_id,id), unique(realm_id,resume_apply_id,sequence),
  unique(realm_id,resume_apply_id,event_digest),
  foreign key(realm_id,resume_apply_id) references runtime.resume_apply(realm_id,id),
  foreign key(realm_id,attempt_id) references runtime.job_attempt(realm_id,id),
  -- Lease rows are deliberately ephemeral.  The UUID/fence remain durable evidence,
  -- but a live-lease FK would prevent normal terminal cleanup.
  foreign key(realm_id,claim_id) references runtime.effect_claim(realm_id,id),
  foreign key(realm_id,receipt_id) references runtime.effect_receipt(realm_id,id),
  check(sequence>0 and btrim(reason_code)<>''),
  check(phase in ('claim','dispatch','terminal')),
  check(state in ('claimed','dispatched','completed','recovery-required','failed')),
  check((sequence=1 and previous_digest is null)
    or (sequence>1 and previous_digest is not null)),
  check((attempt_id is null and lease_id is null and fencing_token is null)
    or (attempt_id is not null and lease_id is not null and fencing_token>0)),
  check((claim_id is null and receipt_id is null)
    or claim_id is not null),
  check(result_digest is null or result_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(previous_digest is null or previous_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(event_digest ~ '^sha256:[0-9a-f]{64}$')
);

create function runtime.enforce_resume_apply_scope() returns trigger
language plpgsql security invoker
set search_path=pg_catalog,runtime,work,security,core as $$
declare c record; j record; a record; p record;
begin
  select work_item_id,task_plan_id into c from work.checkpoint_v2
    where realm_id=new.realm_id and id=new.checkpoint_id;
  select work_item_id,run_id,plan_id into j from runtime.job
    where realm_id=new.realm_id and id=new.job_id;
  select actor_id,work_item_id,plan_id,effect_digest,plan_digest into a
    from security.authorization
    where realm_id=new.realm_id and id=new.authorization_id;
  select plan_digest into p from work.task_plan
    where realm_id=new.realm_id and id=j.plan_id;
  if row(c.work_item_id,c.task_plan_id,j.work_item_id,j.run_id,
      a.actor_id,a.work_item_id,a.plan_id,a.effect_digest,a.plan_digest) is distinct from
    row(new.work_item_id,j.plan_id,new.work_item_id,new.run_id,
      new.actor_id,new.work_item_id,j.plan_id,new.effect_digest,p.plan_digest) then
    raise exception 'resume apply exact scope drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger resume_apply_scope before insert on runtime.resume_apply
for each row execute function runtime.enforce_resume_apply_scope();

create function runtime.enforce_resume_apply_event_chain() returns trigger
language plpgsql security invoker set search_path=pg_catalog,runtime as $$
declare head record; apply_ record; attempt_ record; lease_ record; claim_ record;
  receipt_claim_id uuid; receipt_status text; receipt_result_digest text;
begin
  perform pg_advisory_xact_lock(hashtextextended(new.realm_id::text||new.resume_apply_id::text,0));
  select sequence,event_digest,state into head from runtime.resume_apply_event
    where realm_id=new.realm_id and resume_apply_id=new.resume_apply_id
    order by sequence desc limit 1;
  if new.event_digest is distinct from models.capability_runtime_jsonb_digest(new.event_body) then
    raise exception 'resume apply event digest drift' using errcode='23514';
  end if;
  if (not found and row(new.sequence,new.previous_digest,new.phase,new.state)
      is distinct from row(1,null::text,'claim'::text,'claimed'::text))
    or (found and row(new.sequence,new.previous_digest)
      is distinct from row(head.sequence+1,head.event_digest)) then
    raise exception 'resume apply event chain drift' using errcode='40001';
  end if;
  if found and ((head.state='claimed' and new.phase<>'dispatch')
      or (head.state='dispatched' and not (
        new.phase='terminal' or (new.phase='dispatch' and new.state='recovery-required')))
      or head.state in ('completed','failed','recovery-required')) then
    raise exception 'resume apply terminal/phase transition drift' using errcode='23514';
  end if;
  if new.phase='claim' and new.state<>'claimed'
    or new.phase='dispatch' and new.state not in ('dispatched','recovery-required')
    or new.phase='terminal' and new.state not in ('completed','failed') then
    raise exception 'resume apply phase/state drift' using errcode='23514';
  end if;
  select job_id,effect_digest into apply_ from runtime.resume_apply
    where realm_id=new.realm_id and id=new.resume_apply_id;
  if new.attempt_id is not null then
    select job_id,fencing_token into attempt_ from runtime.job_attempt
      where realm_id=new.realm_id and id=new.attempt_id;
    if row(attempt_.job_id,attempt_.fencing_token)
        is distinct from row(apply_.job_id,new.fencing_token) then
      raise exception 'resume apply attempt binding drift' using errcode='23514';
    end if;
  end if;
  if new.lease_id is not null then
    select job_id,attempt_id,fencing_token into lease_ from runtime.lease
      where realm_id=new.realm_id and id=new.lease_id;
    if row(lease_.job_id,lease_.attempt_id,lease_.fencing_token)
        is distinct from row(apply_.job_id,new.attempt_id,new.fencing_token) then
      raise exception 'resume apply lease binding drift' using errcode='23514';
    end if;
  end if;
  if new.claim_id is not null then
    select job_id,attempt_id,fencing_token,effect_digest into claim_ from runtime.effect_claim
      where realm_id=new.realm_id and id=new.claim_id;
    if row(claim_.job_id,claim_.attempt_id,claim_.fencing_token,claim_.effect_digest)
        is distinct from row(apply_.job_id,new.attempt_id,new.fencing_token,apply_.effect_digest) then
      raise exception 'resume apply claim binding drift' using errcode='23514';
    end if;
  end if;
  if new.receipt_id is not null then
    select claim_id,status,result_digest
      into receipt_claim_id,receipt_status,receipt_result_digest from runtime.effect_receipt
      where realm_id=new.realm_id and id=new.receipt_id;
    if receipt_claim_id is distinct from new.claim_id then
      raise exception 'resume apply receipt binding drift' using errcode='23514';
    end if;
  end if;
  if new.state='claimed' and (new.attempt_id is null or new.lease_id is null or new.claim_id is null)
    or new.state='dispatched' and (new.claim_id is null or new.receipt_id is not null)
    or new.state='recovery-required' and (new.claim_id is null or new.receipt_id is not null)
    or new.state='completed' and (new.receipt_id is null or new.result_digest is null
      or receipt_status is distinct from 'completed'
      or receipt_result_digest is distinct from new.result_digest)
    or new.state='failed' and (new.receipt_id is null
      or receipt_status is distinct from 'failed') then
    raise exception 'resume apply state evidence incomplete' using errcode='23514';
  end if;
  return new;
end $$;
create trigger resume_apply_event_chain before insert on runtime.resume_apply_event
for each row execute function runtime.enforce_resume_apply_event_chain();

do $$ declare target text; begin foreach target in array array[
 'runtime.resume_apply','runtime.resume_apply_event'] loop
 execute format('alter table %s enable row level security',target);
 execute format('alter table %s force row level security',target);
 execute format('create policy scope_select on %s for select using(realm_id=core.current_realm_id())',target);
 execute format('create policy scope_insert on %s for insert with check(realm_id=core.current_realm_id())',target);
 execute format('create trigger no_mutation before update or delete on %s for each statement execute function core.deny_mutation()',target);
end loop; end $$;

grant select,insert on runtime.resume_apply,runtime.resume_apply_event to zekam_app;
