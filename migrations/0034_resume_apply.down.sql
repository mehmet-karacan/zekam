drop table if exists runtime.resume_apply_event;
drop function if exists runtime.enforce_resume_apply_event_chain();
drop trigger if exists resume_apply_scope on runtime.resume_apply;
drop function if exists runtime.enforce_resume_apply_scope();
drop table if exists runtime.resume_apply;

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
  if ar.status<>'active'
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
  elsif a.attempt_number<>1 or exists(select 1 from work.checkpoint where realm_id=new.realm_id
      and job_id=new.job_id) then
    raise exception 'genesis checkpoint disposition yalniz ilk checkpointsiz attempt icin'
      using errcode='23514';
  end if;
  return new;
end $$;

alter table runtime.execution_envelope
  drop constraint execution_envelope_checkpoint_shape_v2;
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
  check(checkpoint_disposition in ('bound','not-applicable-genesis'));
alter table runtime.execution_envelope add constraint execution_envelope_checkpoint_shape
  check((checkpoint_disposition='bound' and checkpoint_id is not null
      and checkpoint_digest is not null)
    or (checkpoint_disposition='not-applicable-genesis' and checkpoint_id is null
      and checkpoint_digest is null));
alter table runtime.execution_envelope
  drop constraint execution_envelope_checkpoint_v2_same_realm,
  drop constraint execution_envelope_checkpoint_v2_digest_format,
  drop column checkpoint_v2_id,
  drop column checkpoint_v2_digest;
alter table runtime.execution_envelope
  add constraint execution_envelope_realm_id_lease_id_fkey
  foreign key(realm_id,lease_id) references runtime.lease(realm_id,id);
alter table work.checkpoint_v2
  add constraint checkpoint_v2_realm_id_observed_lease_id_fkey
  foreign key(realm_id,observed_lease_id) references runtime.lease(realm_id,id);
