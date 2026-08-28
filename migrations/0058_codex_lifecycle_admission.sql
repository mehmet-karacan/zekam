-- Codex generic lifecycle ingest may commit only with one exact governed terminal chain.

create table client.codex_lifecycle_admission (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  lifecycle_event_id uuid not null,
  entry_digest text not null,
  continuity_event_id uuid not null,
  delivery_outbox_id uuid not null,
  hook_receipt_id uuid not null,
  job_id uuid not null,
  attempt_id uuid not null,
  envelope_id uuid not null,
  authorization_id uuid not null,
  claim_id uuid not null,
  effect_receipt_id uuid not null,
  work_plan_digest text not null,
  effect_plan_digest text not null,
  effect_plan_body jsonb not null,
  effect_digest text not null,
  source_digest text not null,
  policy_digest text not null,
  migration_digest text not null,
  envelope_digest text not null,
  terminal_hook_receipt_digest text not null,
  result_formula_digest text not null,
  binding_digest text not null,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,lifecycle_event_id), unique(realm_id,entry_digest),
  foreign key(realm_id,lifecycle_event_id) references client.lifecycle_event(realm_id,id),
  foreign key(realm_id,continuity_event_id)
    references continuity.session_lifecycle_event(realm_id,id),
  foreign key(realm_id,delivery_outbox_id)
    references continuity.lifecycle_delivery_outbox(realm_id,id),
  foreign key(realm_id,job_id) references runtime.job(realm_id,id),
  foreign key(realm_id,attempt_id) references runtime.job_attempt(realm_id,id),
  foreign key(realm_id,envelope_id) references runtime.execution_envelope(realm_id,id),
  foreign key(realm_id,hook_receipt_id) references hooks.result_receipt(realm_id,id),
  foreign key(authorization_id) references security.authorization(id),
  foreign key(claim_id) references runtime.effect_claim(id),
  foreign key(effect_receipt_id) references runtime.effect_receipt(id),
  check(entry_digest ~ '^sha256:[0-9a-f]{64}$'
    and jsonb_typeof(effect_plan_body)='object'
    and work_plan_digest ~ '^sha256:[0-9a-f]{64}$'
    and effect_plan_digest ~ '^sha256:[0-9a-f]{64}$'
    and effect_digest ~ '^sha256:[0-9a-f]{64}$'
    and source_digest ~ '^sha256:[0-9a-f]{64}$'
    and policy_digest ~ '^sha256:[0-9a-f]{64}$'
    and migration_digest ~ '^sha256:[0-9a-f]{64}$'
    and envelope_digest ~ '^sha256:[0-9a-f]{64}$'
    and terminal_hook_receipt_digest ~ '^sha256:[0-9a-f]{64}$'
    and result_formula_digest ~ '^sha256:[0-9a-f]{64}$'
    and binding_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(not grants_authority)
);

create function client.lock_codex_lifecycle_scope(
  realm_id_ uuid,job_id_ uuid,attempt_id_ uuid,authorization_id_ uuid
)
returns timestamptz language plpgsql security definer set search_path=pg_catalog as $$
declare project_id_ uuid; work_item_id_ uuid;
begin
  if realm_id_ is distinct from core.current_realm_id() then
    raise exception 'Codex lifecycle lock realm scope drift' using errcode='42501';
  end if;
  select job.project_id,job.work_item_id into project_id_,work_item_id_
    from runtime.job job where job.realm_id=realm_id_ and job.id=job_id_;
  if project_id_ is null or work_item_id_ is null then
    raise exception 'Codex lifecycle lock exact job scope missing' using errcode='23514';
  end if;
  lock table core.schema_migrations in share mode;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(realm_id_::text||':'||work_item_id_::text,0));
  perform 1 from projects.source_binding binding
    where binding.realm_id=realm_id_ and binding.project_id=project_id_
    for update;
  if not found then raise exception 'Codex lifecycle source binding missing'
    using errcode='23514'; end if;
  perform 1 from work.work_item item where item.realm_id=realm_id_
    and item.project_id=project_id_ and item.id=work_item_id_ for update;
  if not found then raise exception 'Codex lifecycle Work scope missing'
    using errcode='23514'; end if;
  perform 1 from runtime.job job join runtime.job_attempt attempt
    on attempt.realm_id=job.realm_id and attempt.job_id=job.id
    where job.realm_id=realm_id_ and job.id=job_id_ and attempt.id=attempt_id_
    for update of job,attempt;
  if not found then raise exception 'Codex lifecycle runtime parent lock missing'
    using errcode='23514'; end if;
  if authorization_id_ is not null then
    perform 1 from security.authorization auth join core.actor actor
      on actor.realm_id=auth.realm_id and actor.id=auth.actor_id
      where auth.realm_id=realm_id_ and auth.id=authorization_id_
      for update of auth,actor;
    if not found then raise exception 'Codex lifecycle authorization parent lock missing'
      using errcode='23514'; end if;
  end if;
  return clock_timestamp();
end $$;
revoke all on function client.lock_codex_lifecycle_scope(uuid,uuid,uuid,uuid) from public;
grant execute on function client.lock_codex_lifecycle_scope(uuid,uuid,uuid,uuid) to zekam_app;
grant execute on function work.task_plan_execution_order(jsonb) to zekam_app;

create function client.enforce_codex_lifecycle_admission() returns trigger
language plpgsql security definer set search_path=pg_catalog as $$
declare kind_ text; realm_ uuid; event_id_ uuid; work_item_id_ uuid; job_id_ uuid;
declare lock_moment_ timestamptz;
begin
  if tg_table_name='codex_lifecycle_admission' then
    realm_:=new.realm_id; event_id_:=new.lifecycle_event_id;
  else
    realm_:=new.realm_id; event_id_:=new.id;
  end if;
  if realm_ is distinct from core.current_realm_id() then
    raise exception 'Codex lifecycle admission realm scope drift' using errcode='42501';
  end if;
  select stream.client_kind into strict kind_ from client.lifecycle_event event
    join client.lifecycle_stream stream on stream.realm_id=event.realm_id
      and stream.id=event.stream_id
    where event.realm_id=realm_ and event.id=event_id_;
  if kind_<>'codex' then
    if tg_table_name='codex_lifecycle_admission' then
      raise exception 'Codex admission yalniz exact Codex lifecycle eventine baglanabilir'
        using errcode='23514';
    end if;
    return new;
  end if;
  select job.id,job.work_item_id into job_id_,work_item_id_
    from client.codex_lifecycle_admission admission
    join runtime.job job on job.realm_id=admission.realm_id and job.id=admission.job_id
    where admission.realm_id=realm_ and admission.lifecycle_event_id=event_id_;
  if work_item_id_ is null then
    raise exception 'Codex lifecycle generic ingest governed admission olmadan commit edilemez'
      using errcode='23514';
  end if;
  lock_moment_:=client.lock_codex_lifecycle_scope(realm_,job_id_,(
    select admission.attempt_id from client.codex_lifecycle_admission admission
    where admission.realm_id=realm_ and admission.lifecycle_event_id=event_id_),(
    select admission.authorization_id from client.codex_lifecycle_admission admission
    where admission.realm_id=realm_ and admission.lifecycle_event_id=event_id_));
  if not exists(
    select 1 from client.codex_lifecycle_admission admission
    join client.lifecycle_event lifecycle_event on lifecycle_event.realm_id=admission.realm_id
      and lifecycle_event.id=admission.lifecycle_event_id
    join runtime.job job on job.realm_id=admission.realm_id and job.id=admission.job_id
    join runtime.job_attempt attempt on attempt.realm_id=admission.realm_id
      and attempt.id=admission.attempt_id and attempt.job_id=job.id
    join runtime.execution_envelope envelope on envelope.realm_id=admission.realm_id
      and envelope.id=admission.envelope_id and envelope.job_id=job.id
      and envelope.attempt_id=attempt.id
    join runtime.effect_claim claim on claim.realm_id=admission.realm_id
      and claim.id=admission.claim_id and claim.job_id=job.id and claim.attempt_id=attempt.id
    join runtime.effect_receipt effect_receipt on effect_receipt.realm_id=admission.realm_id
      and effect_receipt.id=admission.effect_receipt_id
      and effect_receipt.claim_id=claim.id and effect_receipt.status='completed'
    join security.authorization auth on auth.realm_id=admission.realm_id
      and auth.id=admission.authorization_id and auth.id=claim.authorization_id
    join core.actor authorization_actor on authorization_actor.realm_id=auth.realm_id
      and authorization_actor.id=auth.actor_id
    join continuity.session_lifecycle_event continuity_event
      on continuity_event.realm_id=admission.realm_id
      and continuity_event.id=admission.continuity_event_id
    join continuity.lifecycle_delivery_outbox outbox on outbox.realm_id=admission.realm_id
      and outbox.id=admission.delivery_outbox_id and outbox.event_id=continuity_event.id
    join hooks.result_receipt hook_receipt on hook_receipt.realm_id=admission.realm_id
      and hook_receipt.id=admission.hook_receipt_id
    join hooks.invocation hook_invocation on hook_invocation.realm_id=admission.realm_id
      and hook_invocation.id=hook_receipt.invocation_id
    join hooks.session_binding hook_session on hook_session.realm_id=admission.realm_id
      and hook_session.id=hook_invocation.session_binding_id
    join client.lifecycle_ack lifecycle_ack on lifecycle_ack.realm_id=admission.realm_id
      and lifecycle_ack.event_id=admission.lifecycle_event_id
    join client.lifecycle_stream stream on stream.realm_id=admission.realm_id
      and stream.id=lifecycle_event.stream_id
    join work.task_plan task_plan on task_plan.realm_id=admission.realm_id
      and task_plan.id=job.plan_id
    join lateral(select step.value as body from jsonb_array_elements(task_plan.steps) step
      where step.value->>'step_id'=job.step_id) plan_step on true
    join lateral(select checkpoint.* from work.checkpoint checkpoint
      where checkpoint.realm_id=job.realm_id and checkpoint.job_id=job.id
      order by checkpoint.created_at desc,checkpoint.id desc limit 1) checkpoint on true
    join runtime.execution_run run on run.realm_id=admission.realm_id and run.id=job.run_id
    join projects.source_binding source_binding on source_binding.realm_id=admission.realm_id
      and source_binding.project_id=job.project_id
    join lateral(select revision.revision,revision.tree_digest
      from projects.source_revision revision
      where revision.realm_id=source_binding.realm_id and revision.binding_id=source_binding.id
      order by revision.observed_at desc,revision.id desc limit 1) source on true
    join lateral(select continuity.jsonb_digest(to_jsonb(checksum)) as migration_digest
      from core.schema_migrations order by version desc limit 1) migration on true
   where admission.realm_id=realm_ and admission.lifecycle_event_id=event_id_
     and admission.entry_digest ~ '^sha256:[0-9a-f]{64}$'
     and lifecycle_event.event_digest=continuity.jsonb_digest(lifecycle_event.payload)
     and lifecycle_ack.local_event_digest=lifecycle_event.event_digest
     and lifecycle_ack.canonical_digest=continuity.jsonb_digest(jsonb_build_object(
       'realm_id',lifecycle_event.realm_id::text,'stream_id',lifecycle_event.stream_id::text,
       'event_id',lifecycle_event.id::text,'local_event_digest',lifecycle_event.event_digest))
     and stream.client_kind='codex' and stream.session_id=continuity_event.session_id
     and lifecycle_event.payload->>'schema'='zekam-client-lifecycle-event/v1'
     and (select count(*) from jsonb_object_keys(lifecycle_event.payload))=11
     and lifecycle_event.payload->>'client_id'=stream.client_instance_id
     and lifecycle_event.payload->>'client_kind'='codex'
     and lifecycle_event.payload->>'session_id'=stream.session_id
     and (lifecycle_event.payload->>'sequence')::bigint=lifecycle_event.sequence
     and (lifecycle_event.payload->>'previous_digest') is not distinct from lifecycle_event.previous_digest
     and lifecycle_event.payload->>'payload_digest'=continuity_event.event_body->>'payload_digest'
     and (lifecycle_event.payload->>'occurred_at')::timestamptz=lifecycle_event.occurred_at
     and lifecycle_event.payload->'transcript_included'='false'::jsonb
     and lifecycle_event.payload->'grants_authority'='false'::jsonb
     and lifecycle_event.payload->>'event_type'=case continuity_event.event_type
       when 'session_start' then 'session.created'
       when 'pre_compaction' then 'session.compacting'
       when 'post_compaction' then 'session.compacted'
       when 'pre_close' then 'session.status'
       when 'post_close' then 'session.deleted' else null end
     and lifecycle_event.sequence=continuity_event.sequence
     and continuity_event.client_id='codex'
     and continuity_event.project_id=job.project_id
     and continuity_event.work_item_id=job.work_item_id
     and continuity_event.run_id=job.run_id
     and continuity_event.event_digest=continuity.jsonb_digest(continuity_event.event_body)
     and continuity_event.event_body->>'source_revision'=envelope.source_revision
     and continuity_event.event_body->>'plan_ref'='work-plan:'||job.plan_id::text
     and continuity_event.event_body->>'context_ref'='context-packet:'||envelope.context_packet_id::text
     and ((envelope.checkpoint_disposition='not-applicable-genesis'
       and continuity_event.event_body->'checkpoint_ref'='null'::jsonb)
       or (envelope.checkpoint_disposition='bound'
         and continuity_event.event_body->>'checkpoint_ref'='checkpoint:'||envelope.checkpoint_id::text)
       or (envelope.checkpoint_disposition='bound-v2'
         and continuity_event.event_body->>'checkpoint_ref'='checkpoint-v2:'||envelope.checkpoint_v2_id::text))
     and continuity_event.idempotency_key=claim.idempotency_key
     and outbox.payload_digest=continuity.jsonb_digest(jsonb_build_object(
       'event_digest',continuity_event.event_digest,'plan_digest',outbox.plan_digest))
     and job.state='completed' and attempt.outcome='succeeded'
     and job.kind='mutation' and job.max_attempts=1 and job.attempt_count=1
     and job.required_capabilities=array['client.lifecycle.codex-drain']::text[]
     and job.read_resources='{}'::text[] and cardinality(job.write_resources)=1
     and job.payload->>'schema'='zekam-codex-lifecycle-job/v1'
     and job.payload->>'authorization_id'=admission.authorization_id::text
     and (select count(*) from jsonb_object_keys(job.payload))=2
     and attempt.result_digest=effect_receipt.result_digest
     and checkpoint.task_plan_id=job.plan_id and checkpoint.work_item_id=job.work_item_id
     and checkpoint.project_id=job.project_id and checkpoint.source_revision=task_plan.source_revision
     and checkpoint.plan_steps=work.task_plan_execution_order(task_plan.steps)
     and checkpoint.completed_steps||checkpoint.pending_steps=checkpoint.plan_steps
     and job.step_id=any(checkpoint.completed_steps)
     and (select array_agg(result.key order by result.key)
       from jsonb_each_text(checkpoint.step_results) result)
       =(select array_agg(step order by step) from unnest(checkpoint.completed_steps) step)
     and not exists(select 1 from jsonb_each_text(checkpoint.step_results) result
       where result.value !~ '^sha256:[0-9a-f]{64}$')
     and checkpoint.step_results->>job.step_id=effect_receipt.result_digest
     and checkpoint.grants_authority=false
     and task_plan.plan_digest=admission.work_plan_digest
     and task_plan.plan_digest=continuity.jsonb_digest(jsonb_build_object(
       'work_item_id',task_plan.work_item_id::text,'project_id',task_plan.project_id::text,
       'revision',task_plan.revision,'source_revision',task_plan.source_revision,
       'policy_digest',task_plan.policy_digest,'steps',task_plan.steps,
       'effect_digest',task_plan.effect_digest,'grants_authority',false))
     and run.client_id='codex' and run.session_id=continuity_event.session_id
     and run.project_id=job.project_id and run.work_item_id=job.work_item_id
     and run.plan_id=job.plan_id and run.source_revision=envelope.source_revision
     and run.policy_digest=envelope.policy_digest
     and envelope.run_id=run.id and run.id=job.run_id
     and envelope.source_revision=task_plan.source_revision
     and envelope.policy_digest=task_plan.policy_digest
     and envelope.envelope_digest=admission.envelope_digest
     and envelope.id=(select latest.id from runtime.execution_envelope latest
       where latest.realm_id=envelope.realm_id and latest.job_id=envelope.job_id
         and latest.attempt_id=envelope.attempt_id
       order by latest.request_ordinal desc,latest.created_at desc,latest.id desc limit 1)
     and envelope.source_revision=source.revision
     and source.tree_digest=admission.source_digest
     and envelope.policy_digest=admission.policy_digest
     and migration.migration_digest=admission.migration_digest
     and task_plan.id=(select current_plan.id from work.task_plan current_plan
       where current_plan.realm_id=task_plan.realm_id
         and current_plan.work_item_id=task_plan.work_item_id
       order by current_plan.revision desc,current_plan.id desc limit 1)
     and envelope.fencing_token=claim.fencing_token
     and envelope.job_id=claim.job_id and envelope.attempt_id=claim.attempt_id
     and job.fencing_token=claim.fencing_token
     and attempt.fencing_token=claim.fencing_token
      and auth.state='consumed'
      and authorization_actor.status='active' and authorization_actor.kind='human'
      and auth.consumed_by='client-lifecycle-bridge/v1'
      and auth.plan_digest=admission.effect_plan_digest
     and admission.effect_plan_digest=continuity.jsonb_digest(admission.effect_plan_body)
     and admission.effect_plan_body->>'schema'='zekam-lifecycle-bridge-plan/v1'
     and (select count(*) from jsonb_object_keys(admission.effect_plan_body))=14
     and admission.effect_plan_body->>'event_digest'=continuity_event.event_digest
     and admission.effect_plan_body->>'hook_payload_digest'=hook_invocation.input_digest
     and admission.effect_plan_body->>'client_contract_digest'=
       'sha256:e688a17271134e25ef233bfda7095308311afc48a7bee825bd720e3e93571147'
     and (admission.effect_plan_body->>'hook_generation')::integer=hook_invocation.generation
     and admission.effect_plan_body->>'hook_set_digest'=hook_session.hook_set_digest
     and admission.effect_plan_body->'hook_ids'=jsonb_build_array(
       (select spec.hook_id from hooks.spec_revision spec
         where spec.realm_id=hook_invocation.realm_id and spec.id=hook_invocation.spec_revision_id))
     and admission.effect_plan_body->>'idempotency_key'=continuity_event.idempotency_key
     and admission.effect_plan_body->>'resource'=job.write_resources[1]
     and admission.effect_plan_body->>'source_digest'=admission.source_digest
     and admission.effect_plan_body->>'policy_digest'=admission.policy_digest
     and admission.effect_plan_body->>'migration_digest'=admission.migration_digest
     and admission.effect_plan_body->>'effect_digest'=admission.effect_digest
     and admission.effect_plan_body->'grants_authority'='false'::jsonb
      and auth.effect_digest=admission.effect_digest
      and auth.authorization_digest=claim.authorization_digest
      and auth.work_item_id=job.work_item_id and auth.plan_id=job.plan_id
      and auth.allowed_resources=job.write_resources
      and auth.allowed_effects=array['database-write']::text[]
      and cardinality(auth.provider_refs)=0
      and cardinality(auth.secret_ref_ids)=0
      and auth.scope=jsonb_build_object(
       'allowed_resources',to_jsonb(job.write_resources),
       'allowed_effects',jsonb_build_array('database-write'),
       'provider_refs','[]'::jsonb,'secret_ref_ids','[]'::jsonb,
       'data_classifications',jsonb_build_array('internal'))
      and auth.risk=plan_step.body->>'risk'
      and auth.risk='high'
     and plan_step.body->>'effect'='database-write'
     and plan_step.body->'logical_resources'=to_jsonb(job.write_resources)
     and claim.effect_digest=admission.effect_digest
     and claim.operation='client-lifecycle-drain'
     and claim.adapter_digest=continuity.jsonb_digest(jsonb_build_object(
       'adapter','claimedwork-codex-lifecycle','version',1))
     and claim.resources=jsonb_build_array(jsonb_build_object(
       'resource',job.write_resources[1],'mode','write'))
     and cardinality(job.read_resources)=0 and cardinality(job.write_resources)=1
     and claim.execution_identity=attempt.worker_label||':'||attempt.fencing_token::text
     and claim.claim_digest=continuity.jsonb_digest(jsonb_build_object(
       'job_id',claim.job_id::text,'operation',claim.operation,
       'effect_digest',claim.effect_digest,'authorization_digest',claim.authorization_digest,
       'idempotency_key',claim.idempotency_key,'resources',claim.resources,
       'execution_identity',claim.execution_identity,'fencing_token',claim.fencing_token,
       'adapter_digest',claim.adapter_digest))
     and outbox.state='completed'
     and outbox.plan_digest=admission.effect_plan_digest
     and outbox.terminal_receipt_digest=admission.terminal_hook_receipt_digest
     and hook_receipt.status='completed'
     and hook_receipt.effect_performed=false and hook_receipt.grants_authority=false
     and hook_receipt.output_digest=admission.terminal_hook_receipt_digest
     and hook_receipt.output_digest=continuity.jsonb_digest(hook_receipt.output_body)
     and jsonb_typeof(hook_receipt.output_body->'command'->'compiler_enqueue')='boolean'
     and hook_invocation.event_type=continuity_event.event_type
     and hook_invocation.input_digest=continuity.jsonb_digest(hook_invocation.input_body)
     and hook_invocation.input_body->'lifecycle'=continuity_event.event_body
     and continuity.jsonb_digest(hook_invocation.input_body->'data')
       =continuity_event.event_body->>'payload_digest'
     and hook_session.session_ref='codex:'||continuity_event.session_id||':'||admission.entry_digest
     and (continuity_event.event_type<>'pre_compaction'
       or (hook_receipt.output_body->'command'->>'compiler_enqueue')::boolean is true)
     and effect_receipt.adapter_evidence_digest=continuity.jsonb_digest(jsonb_build_object(
       'adapter','claimedwork-codex-lifecycle/v1','entry_digest',admission.entry_digest,
       'plan_digest',admission.effect_plan_digest,
       'terminal_hook_receipt_digest',admission.terminal_hook_receipt_digest))
     and admission.result_formula_digest=continuity.jsonb_digest(jsonb_build_object(
       'schema','zekam-client-lifecycle-effect-result/v1',
       'entry_digest',admission.entry_digest,
       'canonical_ack_digest',lifecycle_ack.canonical_digest,
       'bridge_result_digest',continuity.jsonb_digest(jsonb_build_object(
         'schema','zekam-client-lifecycle-bridge-result/v1',
         'plan_digest',admission.effect_plan_digest,
         'event_digest',continuity_event.event_digest,
         'event_id',continuity_event.id::text,'outbox_id',outbox.id::text,
         'hook_receipt_id',hook_receipt.id::text,
         'hook_output_digest',hook_receipt.output_digest,'grants_authority',false)),
       'terminal_hook_receipt_digest',hook_receipt.output_digest,
       'compiler_enqueue',(hook_receipt.output_body->'command'->>'compiler_enqueue')::boolean,
       'grants_authority',false))
     and effect_receipt.result_digest=admission.result_formula_digest
     and effect_receipt.failure_category is null and effect_receipt.failure_digest is null
     and effect_receipt.token_count=0 and effect_receipt.cost_micros=0
     and effect_receipt.latency_ms>=0
     and admission.binding_digest=continuity.jsonb_digest(jsonb_build_object(
       'schema','zekam-codex-lifecycle-governed-admission/v1',
       'lifecycle_event_id',admission.lifecycle_event_id::text,
       'entry_digest',admission.entry_digest,
       'continuity_event_id',admission.continuity_event_id::text,
       'delivery_outbox_id',admission.delivery_outbox_id::text,
       'hook_receipt_id',admission.hook_receipt_id::text,
       'job_id',admission.job_id::text,'attempt_id',admission.attempt_id::text,
       'envelope_id',admission.envelope_id::text,
       'authorization_id',admission.authorization_id::text,
       'claim_id',admission.claim_id::text,
       'effect_receipt_id',admission.effect_receipt_id::text,
       'work_plan_digest',admission.work_plan_digest,
       'effect_plan_digest',admission.effect_plan_digest,
       'effect_plan_body',admission.effect_plan_body,
       'effect_digest',admission.effect_digest,
       'source_digest',admission.source_digest,'policy_digest',admission.policy_digest,
       'migration_digest',admission.migration_digest,
       'envelope_digest',admission.envelope_digest,
       'terminal_hook_receipt_digest',admission.terminal_hook_receipt_digest,
       'result_formula_digest',admission.result_formula_digest,
       'grants_authority',false))
      and auth.issued_at<=claim.claimed_at
      and claim.claimed_at<=auth.consumed_at
      and auth.expires_at>=auth.consumed_at
      and auth.consumed_at<=hook_invocation.created_at
     and hook_invocation.created_at<=hook_receipt.completed_at
     and hook_receipt.completed_at<=effect_receipt.completed_at
     and effect_receipt.completed_at<=checkpoint.created_at
     and checkpoint.created_at<=attempt.finished_at
     and attempt.finished_at<=admission.created_at
     and admission.created_at<=lock_moment_
     and not exists(select 1 from runtime.lease lease
       where lease.realm_id=job.realm_id and lease.job_id=job.id)
      and not exists(select 1 from runtime.resource_lock held_lock
        where held_lock.realm_id=job.realm_id and held_lock.job_id=job.id)
     and not exists(select 1 from runtime.effect_claim orphan
       where orphan.realm_id=job.realm_id and orphan.job_id=job.id
         and not exists(select 1 from runtime.effect_receipt receipt
           where receipt.realm_id=orphan.realm_id and receipt.claim_id=orphan.id))
  ) then
    raise exception 'Codex lifecycle generic ingest governed admission olmadan commit edilemez'
      using errcode='23514';
  end if;
  return new;
end $$;

create constraint trigger codex_lifecycle_admission_guard
after insert on client.lifecycle_event deferrable initially deferred
for each row execute function client.enforce_codex_lifecycle_admission();

create constraint trigger codex_lifecycle_admission_row_guard
after insert on client.codex_lifecycle_admission deferrable initially deferred
for each row execute function client.enforce_codex_lifecycle_admission();

revoke all on function client.enforce_codex_lifecycle_admission() from public;
grant execute on function client.enforce_codex_lifecycle_admission() to zekam_app;

create trigger codex_lifecycle_admission_no_mutation
before update or delete on client.codex_lifecycle_admission
for each statement execute function core.deny_mutation();

alter table client.codex_lifecycle_admission enable row level security;
alter table client.codex_lifecycle_admission force row level security;
create policy scope_select on client.codex_lifecycle_admission
for select using(realm_id=core.current_realm_id());
create policy scope_insert on client.codex_lifecycle_admission
for insert with check(realm_id=core.current_realm_id());
grant select,insert on client.codex_lifecycle_admission to zekam_app;
