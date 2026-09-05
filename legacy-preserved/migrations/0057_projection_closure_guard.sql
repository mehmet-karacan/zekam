-- DB-grade projection-aware completion admission and bounded closure locking.

create table work.completion_admission (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  project_id uuid not null,
  work_item_id uuid not null,
  mode text not null,
  expected_work_revision integer not null,
  expected_work_record_digest text not null,
  plan_id uuid not null,
  plan_digest text not null,
  job_id uuid not null,
  attempt_id uuid not null,
  claim_id uuid not null,
  authorization_id uuid not null,
  run_id uuid,
  close_receipt_id uuid,
  projection_receipt_id uuid,
  pre_close_outbox_id uuid,
  checkpoint_id uuid not null,
  effect_receipt_id uuid not null,
  operation text not null,
  source_authorization_id uuid,
  source_authorization_digest text,
  source_claim_id uuid,
  source_claim_digest text,
  source_effect_receipt_id uuid,
  source_operation text,
  source_consumed_by text,
  source_effect_digest text,
  source_adapter_digest text,
  source_adapter_evidence_digest text,
  source_resources text[],
  source_effects text[],
  source_data_classifications text[],
  completion_evidence jsonb,
  request_digest text,
  evidence_digest text,
  transaction_id xid8 not null default pg_current_xact_id(),
  admission_body jsonb not null,
  admission_digest text not null,
  admitted_at timestamptz not null,
  consumed_at timestamptz,
  grants_authority boolean not null default false,
  unique(realm_id,id),
  unique(realm_id,admission_digest),
  unique(realm_id,work_item_id,expected_work_revision),
  foreign key(realm_id,project_id,work_item_id)
    references work.work_item(realm_id,project_id,id) on delete restrict,
  foreign key(realm_id,plan_id) references work.task_plan(realm_id,id) on delete restrict,
  foreign key(realm_id,job_id) references runtime.job(realm_id,id) on delete restrict,
  foreign key(realm_id,attempt_id) references runtime.job_attempt(realm_id,id) on delete restrict,
  foreign key(realm_id,claim_id) references runtime.effect_claim(realm_id,id) on delete restrict,
  foreign key(realm_id,authorization_id)
    references security.authorization(realm_id,id) on delete restrict,
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id) on delete restrict,
  foreign key(realm_id,close_receipt_id)
    references continuity.session_close_receipt(realm_id,id) on delete restrict,
  foreign key(realm_id,projection_receipt_id)
    references continuity.projection_generation_receipt(realm_id,id) on delete restrict,
  foreign key(realm_id,pre_close_outbox_id)
    references continuity.lifecycle_delivery_outbox(realm_id,id) on delete restrict,
  foreign key(realm_id,checkpoint_id) references work.checkpoint(realm_id,id) on delete restrict,
  foreign key(realm_id,effect_receipt_id)
    references runtime.effect_receipt(realm_id,id) on delete restrict,
  foreign key(realm_id,source_authorization_id)
    references security.authorization(realm_id,id) on delete restrict,
  foreign key(realm_id,source_claim_id)
    references runtime.effect_claim(realm_id,id) on delete restrict,
  foreign key(realm_id,source_effect_receipt_id)
    references runtime.effect_receipt(realm_id,id) on delete restrict,
  check(mode in ('projection-aware','control-plane')),
  check(expected_work_revision>1),
  check(expected_work_record_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(plan_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(btrim(operation)<>'' and length(operation)<=96),
  check(jsonb_typeof(admission_body)='object'),
  check(admission_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(source_authorization_digest is null
    or source_authorization_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(source_claim_digest is null or source_claim_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(source_effect_digest is null or source_effect_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(source_adapter_digest is null or source_adapter_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(source_adapter_evidence_digest is null
    or source_adapter_evidence_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(request_digest is null or request_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(evidence_digest is null or evidence_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(consumed_at is null or consumed_at>=admitted_at),
  check((mode='projection-aware' and run_id is not null and close_receipt_id is not null
      and projection_receipt_id is not null and pre_close_outbox_id is not null
      and source_authorization_id is null and source_authorization_digest is null
      and source_claim_id is null and source_claim_digest is null
      and source_effect_receipt_id is null and source_operation is null
      and source_consumed_by is null and source_effect_digest is null
      and source_adapter_digest is null and source_adapter_evidence_digest is null
      and source_resources is null and source_effects is null
      and source_data_classifications is null and completion_evidence is null
      and request_digest is null and evidence_digest is null)
    or (mode='control-plane' and run_id is null and close_receipt_id is null
      and projection_receipt_id is null and pre_close_outbox_id is null
      and source_authorization_id is not null and source_authorization_digest is not null
      and source_claim_id is not null and source_claim_digest is not null
      and source_effect_receipt_id is not null and source_operation is not null
      and btrim(source_operation)<>'' and source_consumed_by is not null
      and btrim(source_consumed_by)<>'' and source_effect_digest is not null
      and source_adapter_digest is not null and source_adapter_evidence_digest is not null
      and source_resources is not null and source_effects is not null
      and source_data_classifications is not null and completion_evidence is not null
      and cardinality(source_resources)>0 and cardinality(source_effects)>0
      and cardinality(source_data_classifications)>0
      and jsonb_typeof(completion_evidence)='array'
      and request_digest is not null and evidence_digest is not null)),
  check(not grants_authority)
);

alter table work.completion_admission enable row level security;
alter table work.completion_admission force row level security;
create policy completion_admission_scope_select on work.completion_admission
  for select using(realm_id=core.current_realm_id());
create policy completion_admission_scope_insert on work.completion_admission
  for insert with check(realm_id=core.current_realm_id());
create policy completion_admission_scope_update on work.completion_admission
  for update using(realm_id=core.current_realm_id())
  with check(realm_id=core.current_realm_id());

create function work.enforce_completion_admission_body() returns trigger
language plpgsql security invoker set search_path=pg_catalog,work,continuity as $$
begin
  if new.transaction_id<>pg_current_xact_id()
    or new.admitted_at<>statement_timestamp()
    or new.admission_body<>jsonb_strip_nulls(jsonb_build_object(
      'schema','zekam-work-completion-admission/v1','admission_id',new.id,
      'realm_id',new.realm_id,'project_id',new.project_id,'work_item_id',new.work_item_id,
      'mode',new.mode,'expected_work_revision',new.expected_work_revision,
      'expected_work_record_digest',new.expected_work_record_digest,
      'plan_id',new.plan_id,'plan_digest',new.plan_digest,'job_id',new.job_id,
      'attempt_id',new.attempt_id,'claim_id',new.claim_id,
      'authorization_id',new.authorization_id,'run_id',new.run_id,
      'close_receipt_id',new.close_receipt_id,
      'projection_receipt_id',new.projection_receipt_id,
      'pre_close_outbox_id',new.pre_close_outbox_id,'checkpoint_id',new.checkpoint_id,
      'effect_receipt_id',new.effect_receipt_id,'operation',new.operation,
      'source_authorization_id',new.source_authorization_id,
      'source_authorization_digest',new.source_authorization_digest,
      'source_claim_id',new.source_claim_id,'source_claim_digest',new.source_claim_digest,
      'source_effect_receipt_id',new.source_effect_receipt_id,
      'source_operation',new.source_operation,'source_consumed_by',new.source_consumed_by,
      'source_effect_digest',new.source_effect_digest,
      'source_adapter_digest',new.source_adapter_digest,
      'source_adapter_evidence_digest',new.source_adapter_evidence_digest,
      'source_resources',new.source_resources,'source_effects',new.source_effects,
      'source_data_classifications',new.source_data_classifications,
      'completion_evidence',new.completion_evidence,'request_digest',new.request_digest,
      'evidence_digest',new.evidence_digest,
      'transaction_id',new.transaction_id::text,'admitted_at',new.admitted_at,
      'grants_authority',false))
    or new.admission_digest<>continuity.jsonb_digest(new.admission_body) then
    raise exception 'completion admission identity/body/digest drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger completion_admission_body_guard before insert on work.completion_admission
  for each row execute function work.enforce_completion_admission_body();

create function work.enforce_completion_admission_update() returns trigger
language plpgsql security invoker set search_path=pg_catalog,work as $$
begin
  if row(new.id,new.realm_id,new.project_id,new.work_item_id,new.mode,
      new.expected_work_revision,new.expected_work_record_digest,new.plan_id,new.plan_digest,
      new.job_id,new.attempt_id,new.claim_id,new.authorization_id,new.run_id,
      new.close_receipt_id,new.projection_receipt_id,new.pre_close_outbox_id,
      new.checkpoint_id,new.effect_receipt_id,new.operation,new.transaction_id,
      new.source_authorization_id,new.source_authorization_digest,
      new.source_claim_id,new.source_claim_digest,new.source_effect_receipt_id,
      new.source_operation,new.source_consumed_by,new.source_effect_digest,
      new.source_adapter_digest,new.source_adapter_evidence_digest,
      new.source_resources,new.source_effects,new.source_data_classifications,
      new.completion_evidence,new.request_digest,new.evidence_digest,
      new.admission_body,new.admission_digest,new.admitted_at,new.grants_authority)
    is distinct from
     row(old.id,old.realm_id,old.project_id,old.work_item_id,old.mode,
      old.expected_work_revision,old.expected_work_record_digest,old.plan_id,old.plan_digest,
      old.job_id,old.attempt_id,old.claim_id,old.authorization_id,old.run_id,
      old.close_receipt_id,old.projection_receipt_id,old.pre_close_outbox_id,
      old.checkpoint_id,old.effect_receipt_id,old.operation,old.transaction_id,
      old.source_authorization_id,old.source_authorization_digest,
      old.source_claim_id,old.source_claim_digest,old.source_effect_receipt_id,
      old.source_operation,old.source_consumed_by,old.source_effect_digest,
      old.source_adapter_digest,old.source_adapter_evidence_digest,
      old.source_resources,old.source_effects,old.source_data_classifications,
      old.completion_evidence,old.request_digest,old.evidence_digest,
      old.admission_body,old.admission_digest,old.admitted_at,old.grants_authority)
    or old.consumed_at is not null or new.consumed_at is null
    or new.consumed_at<>statement_timestamp() then
    raise exception 'completion admission append-only contract' using errcode='42501';
  end if;
  return new;
end $$;
create trigger completion_admission_update_guard before update on work.completion_admission
  for each row execute function work.enforce_completion_admission_update();
create trigger completion_admission_deny_delete before delete on work.completion_admission
  for each statement execute function core.deny_mutation();

create function continuity.enforce_exact_close_receipt() returns trigger
language plpgsql security invoker
set search_path=pg_catalog,continuity,runtime,work as $$
declare job_ record; attempt_ record; envelope_ record; checkpoint_ record;
begin
  -- Generic lifecycle close receipts keep the 0055 identity/digest contract.
  -- The stronger terminal chain applies only to the claimed projection close.
  if not exists(select 1 from runtime.effect_claim claim
      where claim.realm_id=new.realm_id and claim.job_id=new.job_id
        and claim.attempt_id=new.attempt_id and claim.operation='projection-aware-close') then
    return new;
  end if;
  if not (new.receipt_body ?& array[
      'schema','receipt_id','realm_id','project_id','work_item_id','run_id','session_id',
      'client_id','job_id','attempt_id','envelope_digest','fencing_token','verified_outcomes',
      'pending_steps','next_safe_action','checkpoint_ref','source_digest','policy_digest',
      'migration_digest','context_digest','status','closed_at','grants_authority'])
    or new.receipt_body->>'schema'<>'zekam-session-close-receipt/v1'
    or new.close_status<>new.receipt_body->>'status'
    or (new.receipt_body->>'job_id')::uuid<>new.job_id
    or (new.receipt_body->>'attempt_id')::uuid<>new.attempt_id
    or new.receipt_body->>'session_id'<>new.session_id
    or new.receipt_body->>'client_id'<>new.client_id
    or (new.receipt_body->>'fencing_token')::bigint<1
    or (new.receipt_body->>'closed_at')::timestamptz<>new.created_at
    or new.created_at>statement_timestamp()
    or jsonb_typeof(new.receipt_body->'verified_outcomes')<>'array'
    or jsonb_typeof(new.receipt_body->'pending_steps')<>'array'
    or jsonb_typeof(new.receipt_body->'checkpoint_ref')<>'object'
    or new.receipt_body->'checkpoint_ref'->>'digest' !~ '^sha256:[0-9a-f]{64}$'
    or btrim(new.receipt_body->'checkpoint_ref'->>'ref')=''
    or jsonb_array_length(new.receipt_body->'verified_outcomes')=0
    or (new.close_status='closed' and
      (new.receipt_body->'next_safe_action'<>'null'::jsonb
       or jsonb_array_length(new.receipt_body->'pending_steps')<>0)) then
    raise exception 'session close receipt exact body/status/timing drift' using errcode='23514';
  end if;
  select project_id,work_item_id,plan_id,run_id,state,fencing_token into strict job_
    from runtime.job where realm_id=new.realm_id and id=new.job_id;
  select job_id,fencing_token,outcome,started_at,finished_at into strict attempt_
    from runtime.job_attempt where realm_id=new.realm_id and id=new.attempt_id;
  if row(job_.project_id,job_.work_item_id,job_.run_id,job_.state,job_.fencing_token)
      is distinct from row(new.project_id,new.work_item_id,new.run_id,'running'::text,
        (new.receipt_body->>'fencing_token')::bigint)
    or row(attempt_.job_id,attempt_.fencing_token,attempt_.outcome)
      is distinct from row(new.job_id,(new.receipt_body->>'fencing_token')::bigint,null::text)
    or new.created_at<attempt_.started_at then
    raise exception 'session close receipt job/attempt/fence drift' using errcode='23514';
  end if;
  select envelope_digest,checkpoint_id,checkpoint_digest into strict envelope_
    from runtime.execution_envelope
    where realm_id=new.realm_id and run_id=new.run_id and job_id=new.job_id
      and attempt_id=new.attempt_id
      and fencing_token=(new.receipt_body->>'fencing_token')::bigint
    order by request_ordinal desc,id desc limit 1;
  if envelope_.envelope_digest<>new.receipt_body->>'envelope_digest'
    or envelope_.checkpoint_id is null
    or envelope_.checkpoint_digest<>new.receipt_body->'checkpoint_ref'->>'digest' then
    raise exception 'session close receipt envelope/checkpoint digest drift' using errcode='23514';
  end if;
  select id,checkpoint_key,checkpoint_digest,task_plan_id,job_id,created_at into strict checkpoint_
    from work.checkpoint where realm_id=new.realm_id and id=envelope_.checkpoint_id;
  if row(checkpoint_.checkpoint_digest,checkpoint_.task_plan_id,checkpoint_.job_id)
      is distinct from row(new.receipt_body->'checkpoint_ref'->>'digest',job_.plan_id,new.job_id)
    or new.receipt_body->'checkpoint_ref'->>'ref' not in
      (checkpoint_.checkpoint_key,'db:work.checkpoint/'||checkpoint_.id::text)
    or checkpoint_.created_at>new.created_at then
    raise exception 'session close receipt exact checkpoint binding drift' using errcode='23514';
  end if;
  return new;
exception when no_data_found or too_many_rows then
  raise exception 'session close receipt terminal dependency missing' using errcode='23514';
end $$;
create trigger close_exact_guard before insert on continuity.session_close_receipt
  for each row execute function continuity.enforce_exact_close_receipt();

create function runtime.reject_late_projection_close_claim() returns trigger
language plpgsql security invoker set search_path=pg_catalog,runtime,continuity as $$
begin
  if new.operation='projection-aware-close' and exists(
      select 1 from continuity.session_close_receipt receipt
      where receipt.realm_id=new.realm_id and receipt.job_id=new.job_id
        and receipt.attempt_id=new.attempt_id) then
    raise exception 'projection-aware close claim must precede close receipt'
      using errcode='42501';
  end if;
  return new;
end $$;
create trigger projection_close_claim_order_guard before insert on runtime.effect_claim
  for each row execute function runtime.reject_late_projection_close_claim();

create function continuity.enforce_hydration_authorization() returns trigger
language plpgsql security definer
set search_path=pg_catalog,core,continuity,runtime,security as $$
declare run_ record; resource_ text; effect_digest_ text; plan_digest_ text; exact_ integer;
begin
  if new.realm_id is distinct from core.current_realm_id() then
    raise exception 'hydration authorization realm scope drift' using errcode='42501';
  end if;
  select project_id,work_item_id,plan_id,session_id,client_id into strict run_
    from runtime.execution_run
    where realm_id=new.realm_id and id=new.run_id;
  if row(new.project_id,new.work_item_id,new.session_id,new.client_id)
      is distinct from row(run_.project_id,run_.work_item_id,run_.session_id,run_.client_id)
    or new.receipt_digest<>continuity.jsonb_digest(new.receipt_body) then
    raise exception 'hydration authorization receipt/run identity drift' using errcode='23514';
  end if;
  resource_:='continuity:hydration:'||new.id::text;
  effect_digest_:=continuity.jsonb_digest(jsonb_build_object(
    'effect','database-write','resource',resource_,'receipt_digest',new.receipt_digest));
  plan_digest_:=continuity.jsonb_digest(jsonb_build_object(
    'schema','zekam-continuity-receipt-plan/v1','kind','hydration',
    'receipt_id',new.id::text,'receipt_digest',new.receipt_digest,
    'idempotency_key',new.idempotency_key,'resource',resource_,
    'source_digest',new.receipt_body->>'source_digest',
    'policy_digest',new.receipt_body->>'policy_digest',
    'migration_digest',new.receipt_body->>'migration_digest',
    'context_digest',new.receipt_body->>'context_digest',
    'effect_digest',effect_digest_,'grants_authority',false));
  select count(*) into exact_ from security.authorization auth
    where auth.realm_id=new.realm_id and auth.work_item_id=new.work_item_id
      and auth.plan_id=run_.plan_id and auth.state='consumed'
      and auth.consumed_by='memory-continuity/v1'
      and auth.plan_digest=plan_digest_ and auth.effect_digest=effect_digest_
      and auth.allowed_resources=array[resource_]
      and auth.allowed_effects=array['database-write']
      and cardinality(auth.provider_refs)=0 and cardinality(auth.secret_ref_ids)=0
      and auth.scope->'data_classifications'='[]'::jsonb
      and auth.consumed_at<=statement_timestamp() and auth.expires_at>=auth.consumed_at;
  if exact_<>1 then
    raise exception 'hydration insert lacks prior exact consumed authorization'
      using errcode='42501';
  end if;
  return new;
exception when no_data_found or too_many_rows then
  raise exception 'hydration authorization runtime dependency missing' using errcode='23514';
end $$;
create trigger hydration_authorization_guard
  before insert on continuity.session_hydration_receipt
  for each row execute function continuity.enforce_hydration_authorization();

create function work.task_plan_execution_order(steps_ jsonb) returns text[]
language plpgsql immutable security invoker
set search_path=pg_catalog as $$
declare pending_ jsonb:=steps_; ordered_ text[]:='{}'::text[]; ready_ text[];
begin
  if jsonb_typeof(steps_)<>'array'
    or exists(select 1 from jsonb_array_elements(steps_) step
      where jsonb_typeof(step)<>'object' or coalesce(btrim(step->>'step_id'),'')=''
        or jsonb_typeof(step->'depends_on') is distinct from 'array')
    or (select count(*) from jsonb_array_elements(steps_))
      <>(select count(distinct step->>'step_id') from jsonb_array_elements(steps_) step) then
    return null;
  end if;
  while jsonb_array_length(pending_)>0 loop
    select array_agg(step->>'step_id' order by step->>'step_id') into ready_
      from jsonb_array_elements(pending_) step
      where not exists(select 1 from jsonb_array_elements_text(step->'depends_on') dependency
        where not dependency=any(ordered_));
    if cardinality(coalesce(ready_,'{}'::text[]))=0 then return null; end if;
    ordered_:=ordered_||ready_;
    select coalesce(jsonb_agg(step order by step->>'step_id'),'[]'::jsonb) into pending_
      from jsonb_array_elements(pending_) step
      where not (step->>'step_id'=any(ready_));
  end loop;
  return ordered_;
end $$;

create function continuity.lock_projection_closure_scope(
  realm_id_ uuid, project_id_ uuid, work_item_id_ uuid, run_id_ uuid,
  job_id_ uuid, attempt_id_ uuid
) returns timestamptz
language plpgsql security definer
set search_path=pg_catalog,core,projects,work,runtime,security,continuity,memory as $$
declare now_ timestamptz:=statement_timestamp();
begin
  if realm_id_ is distinct from core.current_realm_id() then
    raise exception 'projection closure realm scope drift' using errcode='42501';
  end if;
  if run_id_ is null or job_id_ is null or attempt_id_ is null then
    raise exception 'projection closure runtime identity missing' using errcode='23514';
  end if;
  -- Keep the migration head stable until the projection admission commits. This table is
  -- read-only in the closure transaction, so the targeted SHARE lock has no lock-upgrade edge.
  lock table core.schema_migrations in share mode;
  perform pg_advisory_xact_lock(hashtextextended(
    realm_id_::text||':'||project_id_::text||':'||work_item_id_::text||':'||run_id_::text,0));
  perform 1 from projects.source_binding
    where realm_id=realm_id_ and project_id=project_id_ for update;
  if not found then raise exception 'projection closure source binding missing' using errcode='P0002'; end if;
  perform 1 from work.work_item
    where realm_id=realm_id_ and project_id=project_id_ and id=work_item_id_ for update;
  if not found then raise exception 'projection closure work missing' using errcode='P0002'; end if;
  perform 1 from runtime.execution_run run
    join runtime.job job on job.realm_id=run.realm_id and job.id=job_id_
    join runtime.job_attempt attempt on attempt.realm_id=job.realm_id and attempt.id=attempt_id_
    where run.realm_id=realm_id_ and run.id=run_id_ and run.project_id=project_id_
      and run.work_item_id=work_item_id_ and run.state='active'
      and job.project_id=project_id_ and job.work_item_id=work_item_id_
      and job.run_id=run.id and job.state='running'
      and attempt.job_id=job.id and attempt.outcome is null
      and attempt.fencing_token=job.fencing_token
    for update of run,job,attempt;
  if not found then
    raise exception 'projection closure exact runtime chain missing' using errcode='23514';
  end if;
  return now_;
end $$;

create function work.lock_control_plane_completion_scope(
  realm_id_ uuid, project_id_ uuid, work_item_id_ uuid, plan_id_ uuid,
  job_id_ uuid, attempt_id_ uuid
) returns timestamptz
language plpgsql security definer
set search_path=pg_catalog,core,projects,work,runtime,security,continuity,memory as $$
declare now_ timestamptz:=statement_timestamp();
begin
  if realm_id_ is distinct from core.current_realm_id() then
    raise exception 'control-plane completion realm scope drift' using errcode='42501';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(
    realm_id_::text||':'||project_id_::text||':'||work_item_id_::text||':'||plan_id_::text,0));
  perform 1 from projects.source_binding
    where realm_id=realm_id_ and project_id=project_id_ for update;
  if not found then raise exception 'control-plane source binding missing' using errcode='P0002'; end if;
  perform 1 from work.work_item
    where realm_id=realm_id_ and project_id=project_id_ and id=work_item_id_ for update;
  if not found then raise exception 'control-plane work missing' using errcode='P0002'; end if;
  perform 1 from runtime.job job
    join runtime.job_attempt attempt on attempt.realm_id=job.realm_id and attempt.id=attempt_id_
    where job.realm_id=realm_id_ and job.id=job_id_ and job.project_id=project_id_
      and job.work_item_id=work_item_id_ and job.plan_id=plan_id_
      and job.run_id is null and job.state='completed'
      and attempt.job_id=job.id and attempt.outcome='succeeded'
      and attempt.fencing_token=job.fencing_token
    for update of job,attempt;
  if not found then
    raise exception 'control-plane exact runtime chain missing' using errcode='23514';
  end if;
  return now_;
end $$;

create function work.admit_projection_completion(
  realm_id_ uuid, project_id_ uuid, work_item_id_ uuid,
  expected_revision_ integer, expected_digest_ text,
  plan_id_ uuid, plan_digest_ text, job_id_ uuid, attempt_id_ uuid,
  claim_id_ uuid, authorization_id_ uuid, run_id_ uuid, close_receipt_id_ uuid,
  projection_receipt_id_ uuid, pre_close_outbox_id_ uuid, checkpoint_id_ uuid,
  effect_receipt_id_ uuid, operation_ text
) returns uuid
language plpgsql security definer
set search_path=pg_catalog,core,work,runtime,security,continuity as $$
declare id_ uuid:=gen_random_uuid(); now_ timestamptz:=statement_timestamp(); body_ jsonb;
  exact_ integer; admission_digest_ text; source_head_ text; source_tree_digest_ text;
  migration_head_ integer; database_revision_digest_ text; expected_source_digest_ text;
  expected_projection_digest_ text; current_projection_digest_ text;
  current_work_revision_ integer; current_work_state_ text;
  current_work_record_digest_ text; current_database_revision_digest_ text;
  current_projection_source_digest_ text; current_migration_digest_ text;
begin
  if realm_id_ is distinct from core.current_realm_id() then
    raise exception 'projection completion realm scope drift' using errcode='42501';
  end if;
  perform continuity.lock_projection_closure_scope(
    realm_id_,project_id_,work_item_id_,run_id_,job_id_,attempt_id_);
  if operation_<>'projection-aware-close' then
    raise exception 'projection completion operation drift' using errcode='23514';
  end if;
  select revision,state,record_digest
    into strict current_work_revision_,current_work_state_,current_work_record_digest_
    from work.work_item
    where realm_id=realm_id_ and project_id=project_id_ and id=work_item_id_;
  select revision.revision,revision.tree_digest into strict source_head_,source_tree_digest_
    from projects.source_revision revision
    join projects.source_binding binding on binding.realm_id=revision.realm_id
      and binding.id=revision.binding_id
    where binding.realm_id=realm_id_ and binding.project_id=project_id_
    order by revision.observed_at desc,revision.id desc limit 1;
  select coalesce(max(version),0) into migration_head_ from core.schema_migrations;
  if migration_head_<1 then
    raise exception 'projection completion migration head missing' using errcode='23514';
  end if;
  select continuity.jsonb_digest(to_jsonb(checksum)) into strict current_migration_digest_
    from core.schema_migrations order by version desc limit 1;
  database_revision_digest_=continuity.jsonb_digest(jsonb_build_object(
    'project_id',project_id_,'work_item_id',work_item_id_,
    'work_revision',expected_revision_,'work_state','completed',
    'work_record_digest',expected_digest_));
  current_database_revision_digest_=continuity.jsonb_digest(jsonb_build_object(
    'project_id',project_id_,'work_item_id',work_item_id_,
    'work_revision',current_work_revision_,'work_state',current_work_state_,
    'work_record_digest',current_work_record_digest_));
  current_projection_source_digest_=continuity.jsonb_digest(jsonb_build_object(
    'source_head',source_head_,'source_tree_digest',source_tree_digest_,
    'migration_head',migration_head_,
    'database_revision_digest',current_database_revision_digest_));
  current_projection_digest_=continuity.jsonb_digest(jsonb_build_object(
    'schema','zekam-memory-continuity-public-projection/v1',
    'project_id',project_id_,'work_item_id',work_item_id_,
    'work_revision',current_work_revision_,'work_state',current_work_state_,
    'source_head',source_head_,'source_tree_digest',source_tree_digest_,
    'migration_head',migration_head_,
    'database_revision_digest',current_database_revision_digest_,
    'source_digest',current_projection_source_digest_,'classification','public',
    'public_filtered',true,'content_included',false,'fresh',true,
    'read_only',true,'grants_authority',false));
  expected_source_digest_=continuity.jsonb_digest(jsonb_build_object(
    'source_head',source_head_,'source_tree_digest',source_tree_digest_,
    'migration_head',migration_head_,'database_revision_digest',database_revision_digest_));
  expected_projection_digest_=continuity.jsonb_digest(jsonb_build_object(
    'schema','zekam-memory-continuity-public-projection/v1',
    'project_id',project_id_,'work_item_id',work_item_id_,
    'work_revision',expected_revision_,'work_state','completed',
    'source_head',source_head_,'source_tree_digest',source_tree_digest_,
    'migration_head',migration_head_,'database_revision_digest',database_revision_digest_,
    'source_digest',expected_source_digest_,'classification','public',
    'public_filtered',true,'content_included',false,'fresh',true,
    'read_only',true,'grants_authority',false));
  select count(*) into exact_
  from work.work_item item
  join work.task_plan plan on plan.realm_id=item.realm_id and plan.id=plan_id_
  join runtime.execution_run run on run.realm_id=item.realm_id and run.id=run_id_
  join runtime.job job on job.realm_id=item.realm_id and job.id=job_id_
  join runtime.job_attempt attempt on attempt.realm_id=item.realm_id and attempt.id=attempt_id_
  join runtime.lease lease on lease.realm_id=item.realm_id and lease.job_id=job.id
    and lease.attempt_id=attempt.id
  join runtime.resource_lock held_lock on held_lock.realm_id=item.realm_id
    and held_lock.job_id=job.id and held_lock.lease_id=lease.id
  join runtime.execution_envelope envelope on envelope.realm_id=item.realm_id
    and envelope.run_id=run.id and envelope.job_id=job.id
    and envelope.attempt_id=attempt.id and envelope.lease_id=lease.id
  join runtime.effect_claim claim on claim.realm_id=item.realm_id and claim.id=claim_id_
  join security.authorization auth on auth.realm_id=item.realm_id and auth.id=authorization_id_
  join runtime.effect_receipt effect on effect.realm_id=item.realm_id
    and effect.id=effect_receipt_id_ and effect.claim_id=claim.id
  join work.checkpoint checkpoint on checkpoint.realm_id=item.realm_id and checkpoint.id=checkpoint_id_
  join continuity.session_close_receipt close_receipt on close_receipt.realm_id=item.realm_id
    and close_receipt.id=close_receipt_id_
  join lateral (select hydration.id,hydration.idempotency_key,hydration.receipt_digest,
      hydration.fresh,hydration.complete,hydration.receipt_body,hydration.created_at
    from continuity.session_hydration_receipt hydration
    where hydration.realm_id=item.realm_id and hydration.project_id=project_id_
      and hydration.work_item_id=item.id and hydration.run_id=run.id
      and hydration.session_id=close_receipt.session_id
      and hydration.client_id=close_receipt.client_id
    order by hydration.created_at desc,hydration.id desc limit 1) hydration on true
  join lateral (select
      'continuity:hydration:'||hydration.id::text resource,
      continuity.jsonb_digest(jsonb_build_object(
        'effect','database-write',
        'resource','continuity:hydration:'||hydration.id::text,
        'receipt_digest',hydration.receipt_digest)) effect_digest
    ) hydration_effect on true
  join lateral (select continuity.jsonb_digest(jsonb_build_object(
      'schema','zekam-continuity-receipt-plan/v1','kind','hydration',
      'receipt_id',hydration.id::text,'receipt_digest',hydration.receipt_digest,
      'idempotency_key',hydration.idempotency_key,'resource',hydration_effect.resource,
      'source_digest',hydration.receipt_body->>'source_digest',
      'policy_digest',hydration.receipt_body->>'policy_digest',
      'migration_digest',hydration.receipt_body->>'migration_digest',
      'context_digest',hydration.receipt_body->>'context_digest',
      'effect_digest',hydration_effect.effect_digest,'grants_authority',false)) plan_digest
    ) hydration_plan on true
  join continuity.projection_generation_receipt projection on projection.realm_id=item.realm_id
    and projection.id=projection_receipt_id_
  join lateral (select prior.id,prior.receipt_digest,prior.source_ref,prior.source_digest,
      prior.projection_digest,prior.generator_version
    from continuity.projection_generation_receipt prior
    where prior.realm_id=item.realm_id and prior.project_id=project_id_
      and prior.work_item_id=item.id and prior.projection_ref='projection/active-work'
      and prior.id<>projection_receipt_id_
    order by prior.generated_at desc,prior.id desc limit 1) prior_projection on true
  join continuity.lifecycle_delivery_outbox outbox on outbox.realm_id=item.realm_id
    and outbox.id=pre_close_outbox_id_
  join continuity.session_lifecycle_event event on event.realm_id=outbox.realm_id
    and event.id=outbox.event_id
  where item.realm_id=realm_id_ and item.project_id=project_id_ and item.id=work_item_id_
    and item.state='verification' and item.revision+1=expected_revision_
    and not exists(select 1 from jsonb_array_elements(item.acceptance_criteria) criterion
      where criterion->'verified' is distinct from 'true'::jsonb)
    and plan.project_id=project_id_ and plan.work_item_id=item.id
    and plan.source_revision=source_head_
    and plan.id=(select current_plan.id from work.task_plan current_plan
      where current_plan.realm_id=realm_id_ and current_plan.work_item_id=work_item_id_
      order by current_plan.revision desc,current_plan.id desc limit 1)
    and run.project_id=project_id_ and run.work_item_id=item.id and run.plan_id=plan.id
    and run.source_revision=source_head_ and run.policy_digest=plan.policy_digest
    and run.state='active'
    and job.project_id=project_id_ and job.work_item_id=item.id and job.plan_id=plan.id
    and job.run_id=run.id and job.state='running'
    and not exists(select 1 from runtime.job other_job
      where other_job.realm_id=realm_id_ and other_job.run_id=run.id
        and other_job.id<>job.id
        and other_job.state in ('ready','running','blocked','recovery-required'))
    and not exists(select 1 from runtime.lease other_lease
      join runtime.job other_job on other_job.realm_id=other_lease.realm_id
        and other_job.id=other_lease.job_id
      where other_job.realm_id=realm_id_ and other_job.work_item_id=item.id
        and other_job.id<>job.id)
    and not exists(select 1 from runtime.resource_lock other_lock
      join runtime.job other_job on other_job.realm_id=other_lock.realm_id
        and other_job.id=other_lock.job_id
      where other_job.realm_id=realm_id_ and other_job.work_item_id=item.id
        and other_job.id<>job.id)
    and attempt.job_id=job.id and attempt.outcome is null
    and attempt.fencing_token=job.fencing_token
    and lease.fencing_token=job.fencing_token and lease.expires_at>now_
    and held_lock.resource='work:'||project_id_::text||':'||work_item_id_::text
      ||':projection-close:'||run_id_::text and held_lock.mode='write'
    and (select count(*) from runtime.resource_lock exact_lock
      where exact_lock.realm_id=realm_id_ and exact_lock.job_id=job_id_)=1
    and envelope.fencing_token=job.fencing_token
    and envelope.source_revision=source_head_ and envelope.policy_digest=plan.policy_digest
    and envelope.id=(select latest_envelope.id from runtime.execution_envelope latest_envelope
      where latest_envelope.realm_id=realm_id_ and latest_envelope.run_id=run.id
        and latest_envelope.job_id=job.id and latest_envelope.attempt_id=attempt.id
      order by latest_envelope.request_ordinal desc,latest_envelope.id desc limit 1)
    and envelope.envelope_digest=close_receipt.receipt_body->>'envelope_digest'
    and envelope.checkpoint_id=checkpoint.id
    and envelope.checkpoint_digest=checkpoint.checkpoint_digest
    and envelope.context_manifest_digest=checkpoint.context_manifest_digest
    and claim.job_id=job.id and claim.attempt_id=attempt.id
    and claim.authorization_id=auth.id and claim.operation=operation_
    and claim.fencing_token=job.fencing_token
    and claim.adapter_digest=continuity.jsonb_digest(jsonb_build_object(
      'adapter','projection-aware-close-postgres','revision',1))
    and claim.execution_identity=attempt.worker_label||':'||job.fencing_token::text
    and claim.resources=jsonb_build_array(jsonb_build_object(
      'mode','write','resource','work:'||project_id_::text||':'||work_item_id_::text
        ||':projection-close:'||run_id_::text))
    and claim.claim_digest=continuity.jsonb_digest(jsonb_build_object(
      'job_id',job.id::text,'operation',claim.operation,
      'effect_digest',claim.effect_digest,
      'authorization_digest',claim.authorization_digest,
      'idempotency_key',claim.idempotency_key,'resources',claim.resources,
      'execution_identity',claim.execution_identity,
      'fencing_token',claim.fencing_token,'adapter_digest',claim.adapter_digest))
    and auth.work_item_id=item.id and auth.plan_id=plan.id and auth.state='consumed'
    and auth.plan_digest=plan_digest_ and auth.effect_digest=claim.effect_digest
    and auth.consumed_by='projection-aware-close/v1'
    and auth.authorization_digest=claim.authorization_digest
    and auth.allowed_resources=array['work:'||project_id_::text||':'||work_item_id_::text
      ||':projection-close:'||run_id_::text]
    and auth.allowed_effects=array['database-write']
    and cardinality(auth.provider_refs)=0 and cardinality(auth.secret_ref_ids)=0
    and auth.scope->'data_classifications'='[]'::jsonb
    and auth.expires_at>=auth.consumed_at
    and effect.status='completed' and effect.result_digest is not null
    and claim.claimed_at<=auth.consumed_at and auth.consumed_at<=effect.completed_at
    and claim.effect_digest=continuity.jsonb_digest(jsonb_build_object(
      'effect','database-write','operation',operation_,
      'resource','work:'||project_id_::text||':'||work_item_id_::text
        ||':projection-close:'||run_id_::text,
      'result_digest',effect.result_digest))
    and effect.adapter_evidence_digest=continuity.jsonb_digest(jsonb_build_object(
      'close_receipt_digest',close_receipt.receipt_digest,
      'projection_receipt_digest',projection.receipt_digest,
      'completed_work_record_digest',expected_digest_))
    and checkpoint.project_id=project_id_ and checkpoint.work_item_id=item.id
    and checkpoint.task_plan_id=plan.id and checkpoint.job_id=job.id
    and checkpoint.id=(select latest_checkpoint.id from work.checkpoint latest_checkpoint
      where latest_checkpoint.realm_id=realm_id_ and latest_checkpoint.job_id=job.id
      order by latest_checkpoint.created_at desc,latest_checkpoint.id desc limit 1)
    and checkpoint.plan_steps=work.task_plan_execution_order(plan.steps)
    and checkpoint.completed_steps=checkpoint.plan_steps
    and cardinality(checkpoint.pending_steps)=0
    and (select array_agg(result.key order by result.key)
      from jsonb_each_text(checkpoint.step_results) result)
      =(select array_agg(step order by step) from unnest(checkpoint.plan_steps) step)
    and not exists(select 1 from jsonb_each_text(checkpoint.step_results) result
      where result.value !~ '^sha256:[0-9a-f]{64}$')
    and checkpoint.step_results->>job.step_id=effect.result_digest
    and close_receipt.project_id=project_id_ and close_receipt.work_item_id=item.id
    and close_receipt.run_id=run.id and close_receipt.job_id=job.id
    and close_receipt.attempt_id=attempt.id and close_receipt.close_status='closed'
    and close_receipt.session_id=event.session_id and close_receipt.client_id=event.client_id
    and close_receipt.receipt_body->>'schema'='zekam-session-close-receipt/v1'
    and close_receipt.receipt_body->>'receipt_id'=close_receipt.id::text
    and close_receipt.receipt_body->>'realm_id'=realm_id_::text
    and close_receipt.receipt_body->>'project_id'=project_id_::text
    and close_receipt.receipt_body->>'work_item_id'=item.id::text
    and close_receipt.receipt_body->>'run_id'=run.id::text
    and close_receipt.receipt_body->>'job_id'=job.id::text
    and close_receipt.receipt_body->>'attempt_id'=attempt.id::text
    and close_receipt.receipt_body->>'status'='closed'
    and close_receipt.receipt_body->'grants_authority'='false'::jsonb
    and jsonb_typeof(close_receipt.receipt_body->'verified_outcomes')='array'
    and jsonb_array_length(close_receipt.receipt_body->'verified_outcomes')>0
    and close_receipt.receipt_body->'pending_steps'='[]'::jsonb
    and close_receipt.receipt_body->'next_safe_action'='null'::jsonb
    and close_receipt.receipt_body->>'policy_digest'=plan.policy_digest
    and (close_receipt.receipt_body->>'fencing_token')::bigint=job.fencing_token
    and close_receipt.receipt_body->'checkpoint_ref'->>'digest'=checkpoint.checkpoint_digest
    and close_receipt.receipt_body->>'envelope_digest'=envelope.envelope_digest
    and close_receipt.receipt_body->>'source_digest'=expected_source_digest_
    and projection.project_id=project_id_ and projection.work_item_id=item.id
    and projection.source_digest=expected_source_digest_
    and projection.projection_digest=expected_projection_digest_
    and projection.projection_ref='projection/active-work'
    and projection.source_ref='work-item/'||item.id::text||'/revision/'||expected_revision_::text
    and projection.generator_version='projection-aware-close/v1'
    and projection.id=(select latest_projection.id
      from continuity.projection_generation_receipt latest_projection
      where latest_projection.realm_id=realm_id_ and latest_projection.project_id=project_id_
        and latest_projection.work_item_id=item.id
        and latest_projection.projection_ref='projection/active-work'
      order by latest_projection.generated_at desc,latest_projection.id desc limit 1)
    and prior_projection.source_ref='work-item/'||item.id::text
      ||'/revision/'||current_work_revision_::text
    and prior_projection.source_digest=current_projection_source_digest_
    and prior_projection.projection_digest=current_projection_digest_
    and prior_projection.generator_version='memory-continuity-shadow/v1'
    and hydration.fresh and hydration.complete
    and hydration.receipt_body ?& array['schema','receipt_id','realm_id','project_id',
      'work_item_id','run_id','session_id','client_id','plan_ref','checkpoint_ref',
      'source_digest','policy_digest','migration_digest','inventory_digest','context_digest',
      'required_selections','optional_selections','omissions','token_budget','tokens_used',
      'freshness','projection_refs','hydration_event_digest','created_at','fresh','complete',
      'grants_authority']
    and hydration.receipt_body->>'schema'='zekam-session-hydration-receipt/v1'
    and hydration.receipt_body->>'receipt_id'=hydration.id::text
    and hydration.receipt_body->>'realm_id'=realm_id_::text
    and hydration.receipt_body->>'project_id'=project_id_::text
    and hydration.receipt_body->>'work_item_id'=item.id::text
    and hydration.receipt_body->>'run_id'=run.id::text
    and hydration.receipt_body->>'session_id'=close_receipt.session_id
    and hydration.receipt_body->>'client_id'=close_receipt.client_id
    and hydration.receipt_body->>'plan_ref'='work-plan:'||plan.id::text
    and hydration.receipt_body->>'checkpoint_ref' in
      (checkpoint.checkpoint_key,'db:work.checkpoint/'||checkpoint.id::text)
    and hydration.receipt_body->>'source_digest'=current_projection_source_digest_
    and hydration.receipt_body->>'policy_digest'=plan.policy_digest
    and hydration.receipt_body->>'migration_digest'=current_migration_digest_
    and hydration.receipt_body->>'context_digest'=envelope.context_manifest_digest
    and hydration.receipt_body->'fresh'='true'::jsonb
    and hydration.receipt_body->'complete'='true'::jsonb
    and hydration.receipt_body->'grants_authority'='false'::jsonb
    and (hydration.receipt_body->>'created_at')::timestamptz=hydration.created_at
    and jsonb_typeof(hydration.receipt_body->'freshness')='array'
    and jsonb_array_length(hydration.receipt_body->'freshness')>0
    and not exists(select 1 from jsonb_array_elements(
      hydration.receipt_body->'freshness') freshness_dimension
      where not (freshness_dimension ?& array[
          'name','observed_digest','expected_digest','current'])
        or freshness_dimension->>'name' !~ '^[a-z][a-z0-9_.:-]{0,95}$'
        or freshness_dimension->>'observed_digest' !~ '^sha256:[0-9a-f]{64}$'
        or freshness_dimension->>'expected_digest' !~ '^sha256:[0-9a-f]{64}$'
        or freshness_dimension->'current' is distinct from 'true'::jsonb
        or freshness_dimension->>'observed_digest'
          is distinct from freshness_dimension->>'expected_digest')
    and exists(select 1 from jsonb_array_elements(
      hydration.receipt_body->'freshness') freshness_dimension
      where freshness_dimension->>'name'='source'
        and freshness_dimension->>'observed_digest'=current_projection_source_digest_
        and freshness_dimension->>'expected_digest'=current_projection_source_digest_)
    and exists(select 1 from jsonb_array_elements(
      hydration.receipt_body->'freshness') freshness_dimension
      where freshness_dimension->>'name'='policy'
        and freshness_dimension->>'observed_digest'=plan.policy_digest
        and freshness_dimension->>'expected_digest'=plan.policy_digest)
    and exists(select 1 from jsonb_array_elements(
      hydration.receipt_body->'freshness') freshness_dimension
      where freshness_dimension->>'name'='migration'
        and freshness_dimension->>'observed_digest'=current_migration_digest_
        and freshness_dimension->>'expected_digest'=current_migration_digest_)
    and exists(select 1 from jsonb_array_elements(
      hydration.receipt_body->'freshness') freshness_dimension
      where freshness_dimension->>'name'='context'
        and freshness_dimension->>'observed_digest'=envelope.context_manifest_digest
        and freshness_dimension->>'expected_digest'=envelope.context_manifest_digest)
    and not exists(select 1 from jsonb_array_elements(
      hydration.receipt_body->'omissions') omission
      where omission->'required'='true'::jsonb)
    and exists(select 1 from jsonb_array_elements(
      hydration.receipt_body->'projection_refs') projection_ref
      where projection_ref->>'digest'=prior_projection.receipt_digest)
    and exists(select 1 from continuity.session_lifecycle_event hydration_event
      join continuity.lifecycle_delivery_outbox hydration_outbox
        on hydration_outbox.realm_id=hydration_event.realm_id
        and hydration_outbox.event_id=hydration_event.id
      where hydration_event.realm_id=realm_id_
        and hydration_event.project_id=project_id_
        and hydration_event.work_item_id=item.id and hydration_event.run_id=run.id
        and hydration_event.session_id=close_receipt.session_id
        and hydration_event.client_id=close_receipt.client_id
        and hydration_event.event_type='hydration_required'
        and hydration_event.event_digest=hydration.receipt_body->>'hydration_event_digest'
        and hydration_event.event_digest=continuity.jsonb_digest(hydration_event.event_body)
        and hydration_event.event_body->>'plan_ref'=hydration.receipt_body->>'plan_ref'
        and hydration_event.event_body->>'checkpoint_ref'
          =hydration.receipt_body->>'checkpoint_ref'
        and hydration_event.event_body->>'source_revision'=source_head_
        and hydration_outbox.created_at=hydration_event.ingested_at
        and hydration_event.ingested_at<=hydration.created_at
        and hydration_outbox.completed_at<=hydration.created_at
        and hydration_event.sequence<event.sequence
        and hydration_outbox.state='completed')
    and exists(select 1 from security.authorization hydration_auth
      where hydration_auth.realm_id=realm_id_
        and hydration_auth.work_item_id=item.id and hydration_auth.plan_id=plan.id
        and hydration_auth.state='consumed'
        and hydration_auth.consumed_by='memory-continuity/v1'
        and hydration_auth.plan_digest=hydration_plan.plan_digest
        and hydration_auth.effect_digest=hydration_effect.effect_digest
        and hydration_auth.allowed_resources=array[hydration_effect.resource]
        and hydration_auth.allowed_effects=array['database-write']
        and cardinality(hydration_auth.provider_refs)=0
        and cardinality(hydration_auth.secret_ref_ids)=0
        and hydration_auth.scope->'data_classifications'='[]'::jsonb
        and hydration_auth.expires_at>=hydration_auth.consumed_at
        and hydration_auth.consumed_at<=now_)
    and close_receipt.receipt_body->>'migration_digest'=current_migration_digest_
    and close_receipt.receipt_body->>'context_digest'=envelope.context_manifest_digest
    and event.project_id=project_id_ and event.work_item_id=item.id and event.run_id=run.id
    and event.event_type='pre_close' and event.event_digest=continuity.jsonb_digest(event.event_body)
    and event.id=(select latest_event.id from continuity.session_lifecycle_event latest_event
      where latest_event.realm_id=realm_id_ and latest_event.project_id=project_id_
        and latest_event.work_item_id=item.id and latest_event.run_id=run.id
        and latest_event.session_id=close_receipt.session_id
        and latest_event.client_id=close_receipt.client_id
      order by latest_event.sequence desc,latest_event.id desc limit 1)
    and outbox.state='completed'
    and outbox.created_at=event.ingested_at
    and event.ingested_at<=close_receipt.created_at
    and event.event_body->>'checkpoint_ref'=close_receipt.receipt_body->'checkpoint_ref'->>'ref'
    and event.event_body->>'plan_ref'='work-plan:'||plan.id::text
    and event.event_body->>'source_revision'=source_head_
    and outbox.payload_digest=continuity.jsonb_digest(jsonb_build_object(
      'event_digest',event.event_digest,'plan_digest',outbox.plan_digest))
    and outbox.terminal_receipt_digest=close_receipt.receipt_digest
    and exists(select 1 from security.authorization lifecycle_auth
      where lifecycle_auth.realm_id=realm_id_
        and lifecycle_auth.work_item_id=item.id and lifecycle_auth.plan_id=plan.id
        and lifecycle_auth.state='consumed'
        and lifecycle_auth.consumed_by='client-lifecycle-bridge/v1'
        and lifecycle_auth.plan_digest=outbox.plan_digest
        and lifecycle_auth.allowed_resources=array['continuity:session:'||event.session_id]
        and lifecycle_auth.allowed_effects=array['database-write']
        and cardinality(lifecycle_auth.provider_refs)=0
        and cardinality(lifecycle_auth.secret_ref_ids)=0
        and lifecycle_auth.scope->'data_classifications'='[]'::jsonb
        and lifecycle_auth.expires_at>=lifecycle_auth.consumed_at
        and lifecycle_auth.consumed_at<=now_)
    and outbox.completed_at=effect.completed_at
    and close_receipt.created_at=projection.generated_at
    and close_receipt.created_at<=effect.completed_at
    and hydration.created_at<=close_receipt.created_at
    and checkpoint.created_at<=close_receipt.created_at
    and auth.consumed_at<=effect.completed_at and effect.completed_at<=now_
    and not exists(select 1 from continuity.gap_recovery_reference gap
      where gap.realm_id=realm_id_ and gap.project_id=project_id_
        and gap.work_item_id=item.id and gap.state<>'resolved'
        and (gap.run_id is null or gap.run_id=run.id))
    and not exists(select 1 from memory.compiler_watermark_claim watermark
      where watermark.realm_id=realm_id_ and watermark.project_id=project_id_
        and watermark.work_item_id=item.id and watermark.run_id=run.id
        and watermark.state<>'completed')
    and coalesce((select latest_compaction.status from continuity.compaction_receipt latest_compaction
      where latest_compaction.realm_id=realm_id_ and latest_compaction.project_id=project_id_
        and latest_compaction.work_item_id=item.id and latest_compaction.run_id=run.id
        and latest_compaction.session_id=close_receipt.session_id
        and latest_compaction.client_id=close_receipt.client_id
      order by latest_compaction.created_at desc,latest_compaction.id desc limit 1),'completed')
      ='completed'
    and not exists(select 1 from continuity.session_lifecycle_event pending_event
      left join continuity.lifecycle_delivery_outbox pending_outbox
        on pending_outbox.realm_id=pending_event.realm_id
        and pending_outbox.event_id=pending_event.id
      where pending_event.realm_id=realm_id_ and pending_event.project_id=project_id_
        and pending_event.work_item_id=item.id and pending_event.run_id=run.id
        and pending_event.session_id=close_receipt.session_id
        and pending_event.client_id=close_receipt.client_id
        and (pending_outbox.id is null or pending_outbox.state<>'completed'))
    and not exists(select 1 from runtime.claim_without_receipt pending
      where pending.realm_id=realm_id_
        and exists(select 1 from runtime.job pending_job where pending_job.realm_id=realm_id_
          and pending_job.id=pending.job_id and pending_job.run_id=run_id_));
  if exact_<>1 then
    raise exception 'projection completion exact terminal chain missing' using errcode='23514';
  end if;
  body_=jsonb_build_object(
    'schema','zekam-work-completion-admission/v1','admission_id',id_,
    'realm_id',realm_id_,'project_id',project_id_,'work_item_id',work_item_id_,
    'mode','projection-aware','expected_work_revision',expected_revision_,
    'expected_work_record_digest',expected_digest_,'plan_id',plan_id_,
    'plan_digest',plan_digest_,'job_id',job_id_,'attempt_id',attempt_id_,
    'claim_id',claim_id_,'authorization_id',authorization_id_,'run_id',run_id_,
    'close_receipt_id',close_receipt_id_,'projection_receipt_id',projection_receipt_id_,
    'pre_close_outbox_id',pre_close_outbox_id_,'checkpoint_id',checkpoint_id_,
    'effect_receipt_id',effect_receipt_id_,'operation',operation_,
    'transaction_id',pg_current_xact_id()::text,'admitted_at',now_,'grants_authority',false);
  admission_digest_=continuity.jsonb_digest(body_);
  insert into work.completion_admission(id,realm_id,project_id,work_item_id,mode,
    expected_work_revision,expected_work_record_digest,plan_id,plan_digest,job_id,
    attempt_id,claim_id,authorization_id,run_id,close_receipt_id,projection_receipt_id,
    pre_close_outbox_id,checkpoint_id,effect_receipt_id,operation,transaction_id,
    admission_body,admission_digest,admitted_at,grants_authority)
  values(id_,realm_id_,project_id_,work_item_id_,'projection-aware',expected_revision_,
    expected_digest_,plan_id_,plan_digest_,job_id_,attempt_id_,claim_id_,authorization_id_,
    run_id_,close_receipt_id_,projection_receipt_id_,pre_close_outbox_id_,checkpoint_id_,
    effect_receipt_id_,operation_,pg_current_xact_id(),body_,admission_digest_,now_,false);
  return id_;
end $$;

create function work.admit_control_plane_completion(
  realm_id_ uuid, project_id_ uuid, work_item_id_ uuid,
  expected_revision_ integer, expected_digest_ text,
  plan_id_ uuid, plan_digest_ text, job_id_ uuid, attempt_id_ uuid,
  claim_id_ uuid, authorization_id_ uuid, checkpoint_id_ uuid,
  effect_receipt_id_ uuid, operation_ text,
  source_authorization_id_ uuid, source_authorization_digest_ text,
  source_claim_id_ uuid, source_claim_digest_ text,
  source_effect_receipt_id_ uuid, source_operation_ text, source_consumed_by_ text,
  source_effect_digest_ text, source_adapter_digest_ text,
  source_adapter_evidence_digest_ text, source_resources_ text[], source_effects_ text[],
  source_data_classifications_ text[], completion_evidence_ jsonb,
  request_digest_ text, evidence_digest_ text
) returns uuid
language plpgsql security definer
set search_path=pg_catalog,core,work,runtime,security,continuity as $$
declare id_ uuid:=gen_random_uuid(); now_ timestamptz:=statement_timestamp(); body_ jsonb;
  request_body_ jsonb;
  exact_ integer; admission_digest_ text;
begin
  if realm_id_ is distinct from core.current_realm_id() then
    raise exception 'control-plane completion realm scope drift' using errcode='42501';
  end if;
  perform work.lock_control_plane_completion_scope(
    realm_id_,project_id_,work_item_id_,plan_id_,job_id_,attempt_id_);
  if operation_<>'control-plane-completion' then
    raise exception 'control-plane completion operation drift' using errcode='23514';
  end if;
  request_body_=jsonb_build_object(
    'schema','zekam-control-plane-completion-request/v1',
    'project_id',project_id_::text,'work_item_id',work_item_id_::text,
    'task_plan_id',plan_id_::text,'job_id',job_id_::text,'attempt_id',attempt_id_::text,
    'checkpoint_id',checkpoint_id_::text,
    'source_authorization_id',source_authorization_id_::text,
    'source_authorization_digest',source_authorization_digest_,
    'source_claim_id',source_claim_id_::text,'source_claim_digest',source_claim_digest_,
    'source_effect_receipt_id',source_effect_receipt_id_::text,
    'source_operation',source_operation_,'source_consumed_by',source_consumed_by_,
    'source_effect_digest',source_effect_digest_,
    'source_adapter_digest',source_adapter_digest_,
    'source_adapter_evidence_digest',source_adapter_evidence_digest_,
    'source_resources',source_resources_,'source_effects',source_effects_,
    'source_data_classifications',source_data_classifications_,
    'evidence_digest',evidence_digest_,'grants_authority',false);
  if jsonb_typeof(completion_evidence_)<>'array'
    or jsonb_array_length(completion_evidence_)<>1
    or evidence_digest_<>continuity.jsonb_digest(completion_evidence_)
    or request_digest_<>continuity.jsonb_digest(request_body_) then
    raise exception 'control-plane completion request/evidence digest drift'
      using errcode='23514';
  end if;
  select count(*) into exact_ from work.work_item item
  join work.task_plan plan on plan.realm_id=item.realm_id and plan.id=plan_id_
  join runtime.job job on job.realm_id=item.realm_id and job.id=job_id_
  join runtime.job_attempt attempt on attempt.realm_id=item.realm_id and attempt.id=attempt_id_
  join runtime.effect_claim claim on claim.realm_id=item.realm_id and claim.id=claim_id_
  join security.authorization auth on auth.realm_id=item.realm_id and auth.id=authorization_id_
  join runtime.effect_receipt effect on effect.realm_id=item.realm_id
    and effect.id=effect_receipt_id_ and effect.claim_id=claim.id
  join security.authorization source_auth on source_auth.realm_id=item.realm_id
    and source_auth.id=source_authorization_id_
  join runtime.effect_claim source_claim on source_claim.realm_id=item.realm_id
    and source_claim.id=source_claim_id_ and source_claim.authorization_id=source_auth.id
  join runtime.effect_receipt source_effect on source_effect.realm_id=item.realm_id
    and source_effect.id=source_effect_receipt_id_ and source_effect.claim_id=source_claim.id
  join work.checkpoint checkpoint on checkpoint.realm_id=item.realm_id and checkpoint.id=checkpoint_id_
  where item.realm_id=realm_id_ and item.project_id=project_id_ and item.id=work_item_id_
    and item.type='maintenance' and item.state='verification'
    and item.revision+1=expected_revision_
    and not exists(select 1 from jsonb_array_elements(item.acceptance_criteria) criterion
      where criterion->'verified' is distinct from 'true'::jsonb)
    and plan.project_id=project_id_ and plan.work_item_id=item.id
    and plan.plan_digest=plan_digest_
    and plan.id=(select current_plan.id from work.task_plan current_plan
      where current_plan.realm_id=realm_id_ and current_plan.work_item_id=work_item_id_
      order by current_plan.revision desc,current_plan.id desc limit 1)
    and job.project_id=project_id_ and job.work_item_id=item.id and job.plan_id=plan.id
    and job.run_id is null and job.state='completed'
    and job.id=(select latest_job.id from runtime.job latest_job
      where latest_job.realm_id=realm_id_ and latest_job.work_item_id=item.id
        and latest_job.plan_id=plan.id
      order by latest_job.created_at desc,latest_job.id desc limit 1)
    and attempt.job_id=job.id and attempt.outcome='succeeded'
    and attempt.fencing_token=job.fencing_token and attempt.finished_at is not null
    and attempt.id=(select latest_attempt.id from runtime.job_attempt latest_attempt
      where latest_attempt.realm_id=realm_id_ and latest_attempt.job_id=job.id
      order by latest_attempt.attempt_number desc,latest_attempt.id desc limit 1)
    and source_auth.work_item_id=item.id and source_auth.plan_id=plan.id
    and source_auth.plan_digest=plan_digest_ and source_auth.state='consumed'
    and source_auth.authorization_digest=source_authorization_digest_
    and source_auth.consumed_by=source_consumed_by_
    and source_auth.effect_digest=source_effect_digest_
    and source_auth.allowed_resources=source_resources_
    and source_auth.allowed_effects=source_effects_
    and source_auth.provider_refs='{}'::text[] and source_auth.secret_ref_ids='{}'::uuid[]
    and source_auth.scope->'allowed_resources'=to_jsonb(source_resources_)
    and source_auth.scope->'allowed_effects'=to_jsonb(source_effects_)
    and source_auth.scope->'provider_refs'='[]'::jsonb
    and source_auth.scope->'secret_ref_ids'='[]'::jsonb
    and source_auth.scope->'data_classifications'=to_jsonb(source_data_classifications_)
    and source_claim.job_id=job.id and source_claim.attempt_id=attempt.id
    and source_claim.operation=job.step_id and source_claim.operation=source_operation_
    and source_claim.claim_digest=source_claim_digest_
    and source_claim.effect_digest=source_effect_digest_
    and source_claim.authorization_digest=source_authorization_digest_
    and source_claim.adapter_digest=source_adapter_digest_
    and source_claim.fencing_token=job.fencing_token
    and source_claim.execution_identity=attempt.worker_label||':'||job.fencing_token::text
    and source_claim.resources=(select jsonb_agg(jsonb_build_object(
      'resource',resource,'mode','write') order by resource)
      from unnest(source_resources_) resource)
    and source_claim.claim_digest=continuity.jsonb_digest(jsonb_build_object(
      'job_id',job.id::text,'operation',source_claim.operation,
      'effect_digest',source_claim.effect_digest,
      'authorization_digest',source_claim.authorization_digest,
      'idempotency_key',source_claim.idempotency_key,'resources',source_claim.resources,
      'execution_identity',source_claim.execution_identity,
      'fencing_token',source_claim.fencing_token,
      'adapter_digest',source_claim.adapter_digest))
    and source_effect.status='completed'
    and source_effect.result_digest=attempt.result_digest
    and source_effect.adapter_evidence_digest=source_adapter_evidence_digest_
    and completion_evidence_=jsonb_build_array(jsonb_build_object(
      'kind','runtime-receipt','reference',source_effect.id::text,
      'digest',source_effect.result_digest))
    and attempt.started_at<=source_claim.claimed_at
    and source_auth.issued_at<=source_claim.claimed_at
    and source_claim.claimed_at<=source_auth.consumed_at
    and source_auth.consumed_at<=source_effect.completed_at
    and source_effect.completed_at<=attempt.finished_at
    and source_auth.expires_at>=source_auth.consumed_at
    and claim.job_id=job.id and claim.attempt_id=attempt.id and claim.operation=operation_
    and claim.idempotency_key=request_digest_
    and claim.authorization_id=auth.id and claim.fencing_token=job.fencing_token
    and claim.execution_identity=attempt.worker_label||':'||job.fencing_token::text
    and claim.effect_digest=plan.effect_digest
    and claim.adapter_digest=continuity.jsonb_digest(jsonb_build_object(
      'adapter','control-plane-completion-postgres','revision',1))
    and claim.resources=jsonb_build_array(jsonb_build_object(
      'resource','work:'||project_id_::text||':'||item.id::text
        ||':control-plane-completion','mode','write'))
    and jsonb_array_length(claim.resources)>0
    and claim.resources=(select jsonb_agg(jsonb_build_object(
      'resource',resource,'mode','write') order by resource)
      from unnest(auth.allowed_resources) resource)
    and claim.claim_digest=continuity.jsonb_digest(jsonb_build_object(
      'job_id',job.id::text,'operation',claim.operation,
      'effect_digest',claim.effect_digest,
      'authorization_digest',claim.authorization_digest,
      'idempotency_key',claim.idempotency_key,'resources',claim.resources,
      'execution_identity',claim.execution_identity,
      'fencing_token',claim.fencing_token,'adapter_digest',claim.adapter_digest))
    and auth.work_item_id=item.id and auth.plan_id=plan.id and auth.state='consumed'
    and auth.actor_id=source_auth.actor_id
    and auth.plan_digest=plan_digest_ and auth.effect_digest=claim.effect_digest
    and auth.consumed_by='control-plane-completion/v1'
    and auth.authorization_digest=claim.authorization_digest
    and (select array_agg(resource order by resource)
      from unnest(auth.allowed_resources) resource)
      =(select array_agg(resource order by resource)
      from (select distinct value->>'resource' resource
        from jsonb_array_elements(claim.resources) value) resources)
    and auth.allowed_effects=array['database-write']
    and auth.allowed_resources=array[
      'work:'||project_id_::text||':'||item.id::text||':control-plane-completion']
    and cardinality(auth.provider_refs)=0 and cardinality(auth.secret_ref_ids)=0
    and auth.scope->'data_classifications'='[]'::jsonb
    and auth.issued_at<=claim.claimed_at
    and auth.expires_at>=auth.consumed_at
    and effect.status='completed' and effect.result_digest=attempt.result_digest
    and effect.adapter_evidence_digest=continuity.jsonb_digest(jsonb_build_object(
      'schema','zekam-control-plane-completion-adapter-evidence/v2',
      'work_item_id',item.id::text,
      'completed_work_record_digest',expected_digest_,
      'checkpoint_digest',checkpoint.checkpoint_digest,
      'plan_digest',plan_digest_,'operation',operation_,
      'source_authorization_id',source_authorization_id_::text,
      'source_claim_id',source_claim_id_::text,
      'source_effect_receipt_id',source_effect_receipt_id_::text,
      'request_digest',request_digest_,'evidence_digest',evidence_digest_))
    and claim.claimed_at<=auth.consumed_at and auth.consumed_at<=effect.completed_at
    and effect.completed_at<=now_
    and checkpoint.project_id=project_id_ and checkpoint.work_item_id=item.id
    and checkpoint.task_plan_id=plan.id and checkpoint.job_id=job.id
    and checkpoint.id=(select latest_checkpoint.id from work.checkpoint latest_checkpoint
      where latest_checkpoint.realm_id=realm_id_ and latest_checkpoint.job_id=job.id
      order by latest_checkpoint.created_at desc,latest_checkpoint.id desc limit 1)
    and checkpoint.plan_steps=work.task_plan_execution_order(plan.steps)
    and checkpoint.completed_steps=checkpoint.plan_steps
    and cardinality(checkpoint.pending_steps)=0
    and (select array_agg(result.key order by result.key)
      from jsonb_each_text(checkpoint.step_results) result)
      =(select array_agg(step order by step) from unnest(checkpoint.plan_steps) step)
    and not exists(select 1 from jsonb_each_text(checkpoint.step_results) result
      where result.value !~ '^sha256:[0-9a-f]{64}$')
    and checkpoint.step_results->>job.step_id=attempt.result_digest
    and not exists(select 1 from jsonb_each_text(checkpoint.step_results) result
      where result.value<>attempt.result_digest)
    and source_effect.completed_at<=checkpoint.created_at
    and checkpoint.created_at<=attempt.finished_at
    and attempt.finished_at<=claim.claimed_at
    and checkpoint.created_at<=claim.claimed_at
    and not exists(select 1 from runtime.lease active_lease
      where active_lease.realm_id=realm_id_ and active_lease.job_id=job.id)
    and not exists(select 1 from runtime.resource_lock active_lock
      where active_lock.realm_id=realm_id_ and active_lock.job_id=job.id)
    and not exists(select 1 from runtime.lease other_lease
      join runtime.job other_job on other_job.realm_id=other_lease.realm_id
        and other_job.id=other_lease.job_id
      where other_job.realm_id=realm_id_ and other_job.work_item_id=item.id
        and other_job.id<>job.id)
    and not exists(select 1 from runtime.resource_lock other_lock
      join runtime.job other_job on other_job.realm_id=other_lock.realm_id
        and other_job.id=other_lock.job_id
      where other_job.realm_id=realm_id_ and other_job.work_item_id=item.id
        and other_job.id<>job.id)
    and not exists(select 1 from runtime.job other_job
      where other_job.realm_id=realm_id_ and other_job.work_item_id=item.id
        and other_job.id<>job.id
        and other_job.state in ('ready','running','blocked','recovery-required'))
    and not exists(select 1 from runtime.claim_without_receipt pending
      where pending.realm_id=realm_id_ and exists(select 1 from runtime.job pending_job
        where pending_job.realm_id=realm_id_ and pending_job.id=pending.job_id
          and pending_job.work_item_id=item.id))
    and not exists(select 1 from runtime.execution_run run
      where run.realm_id=realm_id_ and run.work_item_id=item.id)
    and not exists(select 1 from continuity.session_lifecycle_event event
      where event.realm_id=realm_id_ and event.work_item_id=item.id)
    and not exists(select 1 from continuity.session_hydration_receipt hydration
      where hydration.realm_id=realm_id_ and hydration.work_item_id=item.id)
    and not exists(select 1 from continuity.session_close_receipt close_receipt
      where close_receipt.realm_id=realm_id_ and close_receipt.work_item_id=item.id)
    and not exists(select 1 from continuity.projection_generation_receipt projection
      where projection.realm_id=realm_id_ and projection.work_item_id=item.id)
    and not exists(select 1 from continuity.compaction_receipt compaction
      where compaction.realm_id=realm_id_ and compaction.work_item_id=item.id)
    and not exists(select 1 from continuity.memory_contract_evaluation evaluation
      where evaluation.realm_id=realm_id_ and evaluation.work_item_id=item.id)
    and not exists(select 1 from continuity.gap_recovery_reference gap
      where gap.realm_id=realm_id_ and gap.work_item_id=item.id)
    and not exists(select 1 from memory.compiler_watermark_claim watermark
      where watermark.realm_id=realm_id_ and watermark.work_item_id=item.id);
  if exact_<>1 then
    raise exception 'control-plane completion exact terminal chain missing' using errcode='23514';
  end if;
  body_=jsonb_build_object(
    'schema','zekam-work-completion-admission/v1','admission_id',id_,
    'realm_id',realm_id_,'project_id',project_id_,'work_item_id',work_item_id_,
    'mode','control-plane','expected_work_revision',expected_revision_,
    'expected_work_record_digest',expected_digest_,'plan_id',plan_id_,
    'plan_digest',plan_digest_,'job_id',job_id_,'attempt_id',attempt_id_,
    'claim_id',claim_id_,'authorization_id',authorization_id_,
    'checkpoint_id',checkpoint_id_,'effect_receipt_id',effect_receipt_id_,
    'operation',operation_,'source_authorization_id',source_authorization_id_,
    'source_authorization_digest',source_authorization_digest_,
    'source_claim_id',source_claim_id_,'source_claim_digest',source_claim_digest_,
    'source_effect_receipt_id',source_effect_receipt_id_,
    'source_operation',source_operation_,'source_consumed_by',source_consumed_by_,
    'source_effect_digest',source_effect_digest_,
    'source_adapter_digest',source_adapter_digest_,
    'source_adapter_evidence_digest',source_adapter_evidence_digest_,
    'source_resources',source_resources_,'source_effects',source_effects_,
    'source_data_classifications',source_data_classifications_,
    'completion_evidence',completion_evidence_,'request_digest',request_digest_,
    'evidence_digest',evidence_digest_,'transaction_id',pg_current_xact_id()::text,
    'admitted_at',now_,'grants_authority',false);
  admission_digest_=continuity.jsonb_digest(body_);
  insert into work.completion_admission(id,realm_id,project_id,work_item_id,mode,
    expected_work_revision,expected_work_record_digest,plan_id,plan_digest,job_id,
    attempt_id,claim_id,authorization_id,checkpoint_id,effect_receipt_id,operation,
    source_authorization_id,source_authorization_digest,source_claim_id,source_claim_digest,
    source_effect_receipt_id,source_operation,source_consumed_by,source_effect_digest,
    source_adapter_digest,source_adapter_evidence_digest,source_resources,source_effects,
    source_data_classifications,completion_evidence,request_digest,evidence_digest,
    transaction_id,admission_body,admission_digest,admitted_at,grants_authority)
  values(id_,realm_id_,project_id_,work_item_id_,'control-plane',expected_revision_,
    expected_digest_,plan_id_,plan_digest_,job_id_,attempt_id_,claim_id_,authorization_id_,
    checkpoint_id_,effect_receipt_id_,operation_,
    source_authorization_id_,source_authorization_digest_,source_claim_id_,source_claim_digest_,
    source_effect_receipt_id_,source_operation_,source_consumed_by_,source_effect_digest_,
    source_adapter_digest_,source_adapter_evidence_digest_,source_resources_,source_effects_,
    source_data_classifications_,completion_evidence_,request_digest_,evidence_digest_,
    pg_current_xact_id(),body_,admission_digest_,now_,false);
  return id_;
end $$;

create function work.enforce_completed_admission() returns trigger
language plpgsql security definer
set search_path=pg_catalog,work,continuity,runtime as $$
declare required_mode_ text; admission_id_ uuid;
begin
  if old.state='completed' or new.state<>'completed' then return new; end if;
  if exists(select 1 from runtime.execution_run run
      where run.realm_id=new.realm_id and run.work_item_id=new.id)
    or exists(select 1 from continuity.session_lifecycle_event event
      where event.realm_id=new.realm_id and event.work_item_id=new.id)
    or exists(select 1 from continuity.session_hydration_receipt hydration
      where hydration.realm_id=new.realm_id and hydration.work_item_id=new.id)
    or exists(select 1 from continuity.session_close_receipt close_receipt
      where close_receipt.realm_id=new.realm_id and close_receipt.work_item_id=new.id)
    or exists(select 1 from continuity.projection_generation_receipt projection
      where projection.realm_id=new.realm_id and projection.work_item_id=new.id)
    or exists(select 1 from continuity.compaction_receipt compaction
      where compaction.realm_id=new.realm_id and compaction.work_item_id=new.id)
    or exists(select 1 from continuity.memory_contract_evaluation evaluation
      where evaluation.realm_id=new.realm_id and evaluation.work_item_id=new.id)
    or exists(select 1 from continuity.gap_recovery_reference gap
      where gap.realm_id=new.realm_id and gap.work_item_id=new.id)
    or exists(select 1 from memory.compiler_watermark_claim watermark
      where watermark.realm_id=new.realm_id and watermark.work_item_id=new.id) then
    required_mode_:='projection-aware';
  elsif new.type='maintenance' then
    required_mode_:='control-plane';
  else
    raise exception 'completed transition requires projection-aware continuity admission'
      using errcode='42501';
  end if;
  select id into admission_id_ from work.completion_admission
    where realm_id=new.realm_id and project_id=new.project_id and work_item_id=new.id
      and mode=required_mode_ and expected_work_revision=new.revision
      and expected_work_record_digest=new.record_digest
      and (mode<>'control-plane' or (
        completion_evidence=new.acceptance_evidence
        and evidence_digest=continuity.jsonb_digest(new.acceptance_evidence)))
      and transaction_id=pg_current_xact_id() and consumed_at is null
    order by admitted_at,id for update;
  if admission_id_ is null then
    raise exception 'raw completed transition lacks exact completion admission'
      using errcode='42501';
  end if;
  update work.completion_admission set consumed_at=statement_timestamp()
    where realm_id=new.realm_id and id=admission_id_ and consumed_at is null;
  if not found then
    raise exception 'completion admission consumption race' using errcode='40001';
  end if;
  return new;
end $$;
create trigger work_completed_admission_guard before update of state on work.work_item
  for each row execute function work.enforce_completed_admission();

create function work.reject_completed_insert() returns trigger
language plpgsql security invoker set search_path=pg_catalog,work as $$
begin
  if new.state='completed' then
    raise exception 'raw completed Work insert is forbidden' using errcode='42501';
  end if;
  return new;
end $$;
create trigger work_completed_insert_guard before insert on work.work_item
  for each row execute function work.reject_completed_insert();

revoke all on work.completion_admission from public,zekam_app;
grant select on work.completion_admission to zekam_app;
revoke all on function work.enforce_completion_admission_body() from public;
revoke all on function work.enforce_completion_admission_update() from public;
revoke all on function work.enforce_completed_admission() from public;
revoke all on function work.reject_completed_insert() from public;
revoke all on function continuity.enforce_exact_close_receipt() from public;
revoke all on function runtime.reject_late_projection_close_claim() from public;
revoke all on function continuity.enforce_hydration_authorization() from public;
revoke all on function work.task_plan_execution_order(jsonb) from public;
revoke all on function continuity.lock_projection_closure_scope(uuid,uuid,uuid,uuid,uuid,uuid)
  from public;
revoke all on function work.lock_control_plane_completion_scope(uuid,uuid,uuid,uuid,uuid,uuid)
  from public;
revoke all on function work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,
  uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text) from public;
revoke all on function work.admit_control_plane_completion(uuid,uuid,uuid,integer,text,uuid,text,
  uuid,uuid,uuid,uuid,uuid,uuid,text,uuid,text,uuid,text,uuid,text,text,text,text,text,
  text[],text[],text[],jsonb,text,text) from public;
grant execute on function continuity.lock_projection_closure_scope(uuid,uuid,uuid,uuid,uuid,uuid)
  to zekam_app;
grant execute on function work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,
  uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text) to zekam_app;
grant execute on function work.admit_control_plane_completion(uuid,uuid,uuid,integer,text,uuid,text,
  uuid,uuid,uuid,uuid,uuid,uuid,text,uuid,text,uuid,text,uuid,text,text,text,text,text,
  text[],text[],text[],jsonb,text,text) to zekam_app;
