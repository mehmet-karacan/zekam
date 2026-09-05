-- Pre-compact lifecycle olayini exact execution baglamina baglayan durable compiler outbox'i.

create function work.valid_compaction_checkpoint_draft(value jsonb) returns boolean
language sql immutable parallel safe set search_path=pg_catalog,work as $$
  select jsonb_typeof(value)='object'
    and octet_length(value::text)<=65536
    and (select array_agg(key order by key) from jsonb_object_keys(value) key)=array[
      'assignment_id','attempt_id','budget','client_instance_id','client_kind',
      'context_manifest_digest','context_manifest_id','context_packet_digest',
      'context_packet_id','execution_envelope_digest','execution_envelope_id',
      'grants_authority','intent_digest','job_id','job_state','journal_head_digest',
      'lifecycle_event_id','lifecycle_stream_id','observed_fencing_token',
      'observed_lease_id','occurred_at','open_effects','plan_digest','plan_effect_digest',
      'plan_id','plan_steps','policy_digest','previous_checkpoint_digest',
      'previous_checkpoint_id','previous_checkpoint_revision','project_id',
      'route_decision_digest','route_decision_id','routing_context_snapshot_id','run_id',
      'run_state','schema','selection_manifest','session_id','source_event_digest',
      'source_revision','transcript_included','work_item_id'
    ]::text[]
    and value->>'schema'='zekam-compaction-checkpoint-draft/v1'
    and value->'transcript_included'='false'::jsonb
    and value->'grants_authority'='false'::jsonb
    and jsonb_typeof(value->'plan_steps')='array'
    and jsonb_array_length(value->'plan_steps')<=128
    and jsonb_typeof(value->'open_effects')='array'
    and jsonb_array_length(value->'open_effects')<=128
    and jsonb_typeof(value->'selection_manifest')='object'
    and (value->'selection_manifest'->>'strategy')='canonical-order-first-128'
$$;

create table work.compaction_checkpoint_outbox (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  lifecycle_event_id uuid not null,
  lifecycle_stream_id uuid not null,
  run_id uuid not null,
  job_id uuid not null,
  attempt_id uuid not null,
  assignment_id uuid not null,
  execution_envelope_id uuid not null,
  route_decision_id uuid not null,
  context_manifest_id uuid not null,
  context_packet_id uuid not null,
  observed_lease_id uuid not null,
  source_event_digest text not null,
  structural_payload jsonb not null,
  payload_digest text not null,
  state text not null default 'pending',
  checkpoint_id uuid,
  checkpoint_digest text,
  failure_category text,
  created_at timestamptz not null,
  completed_at timestamptz,
  grants_authority boolean not null default false,
  unique(realm_id,id),
  unique(realm_id,lifecycle_event_id),
  foreign key(realm_id,lifecycle_event_id)
    references client.lifecycle_event(realm_id,id) on delete restrict,
  foreign key(realm_id,lifecycle_stream_id)
    references client.lifecycle_stream(realm_id,id) on delete restrict,
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id) on delete restrict,
  foreign key(realm_id,job_id) references runtime.job(realm_id,id) on delete restrict,
  foreign key(realm_id,attempt_id) references runtime.job_attempt(realm_id,id) on delete restrict,
  foreign key(realm_id,assignment_id)
    references agents.assignment(realm_id,id) on delete restrict,
  foreign key(realm_id,execution_envelope_id)
    references runtime.execution_envelope(realm_id,id) on delete restrict,
  foreign key(realm_id,route_decision_id)
    references models.model_route_decision(realm_id,id) on delete restrict,
  foreign key(realm_id,context_manifest_id)
    references work.context_manifest(realm_id,id) on delete restrict,
  foreign key(realm_id,context_packet_id)
    references work.context_packet(realm_id,id) on delete restrict,
  foreign key(realm_id,observed_lease_id)
    references runtime.lease(realm_id,id) on delete restrict,
  foreign key(realm_id,checkpoint_id)
    references work.checkpoint_v2(realm_id,id) on delete restrict,
  check(source_event_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(payload_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(checkpoint_digest is null or checkpoint_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(work.valid_compaction_checkpoint_draft(structural_payload)),
  check(state in ('pending','processing','completed','failed')),
  check((state='completed' and checkpoint_id is not null and checkpoint_digest is not null
      and completed_at is not null and failure_category is null)
    or (state='failed' and checkpoint_id is null and checkpoint_digest is null
      and completed_at is null and btrim(failure_category)<>'' )
    or (state in ('pending','processing') and checkpoint_id is null
      and checkpoint_digest is null and completed_at is null and failure_category is null)),
  check(not grants_authority)
);

create function work.compile_pre_compact_outbox() returns trigger
language plpgsql security definer
set search_path=pg_catalog,work,runtime,client,models,core as $$
declare
  stream_row record;
  run_row record;
  envelope_row record;
  plan_row record;
  intent_row record;
  route_row record;
  checkpoint_head record;
  journal_head_digest text;
  plan_step_count integer;
  open_effect_count integer;
  read_resource_count integer;
  write_resource_count integer;
  candidate_count integer;
  envelope_count integer;
  payload jsonb;
begin
  if new.payload->>'event_type' <> 'session.compacting' then
    return new;
  end if;

  select client_kind,client_instance_id,session_id into strict stream_row
    from client.lifecycle_stream
   where realm_id=new.realm_id and id=new.stream_id;

  select count(*) into candidate_count
    from runtime.execution_run
   where realm_id=new.realm_id
     and session_id=stream_row.session_id
     and client_id in (stream_row.client_kind,stream_row.client_instance_id)
     and state in ('prepared','active','reconciliation-required')
     and created_at<=new.occurred_at and deadline>new.occurred_at;
  if candidate_count <> 1 then
    raise exception 'pre-compact exact active execution binding requires one run; found %',
      candidate_count using errcode='23514';
  end if;
  select id,project_id,work_item_id,plan_id,source_revision,policy_digest
    into strict run_row
    from runtime.execution_run
   where realm_id=new.realm_id
     and session_id=stream_row.session_id
     and client_id in (stream_row.client_kind,stream_row.client_instance_id)
     and state in ('prepared','active','reconciliation-required')
     and created_at<=new.occurred_at and deadline>new.occurred_at;

  select count(distinct envelope.job_id) into envelope_count
    from runtime.execution_envelope envelope
    join runtime.job job on job.realm_id=envelope.realm_id and job.id=envelope.job_id
    join runtime.lease lease on lease.realm_id=envelope.realm_id and lease.id=envelope.lease_id
   where envelope.realm_id=new.realm_id and envelope.run_id=run_row.id
     and envelope.created_at<=new.occurred_at
     and job.state in ('running','recovery-required')
     and lease.expires_at>new.occurred_at;
  if envelope_count <> 1 then
    raise exception 'pre-compact exact active job binding requires one job; found %',
      envelope_count using errcode='23514';
  end if;
  select envelope.id,envelope.job_id,envelope.attempt_id,envelope.assignment_id,
         envelope.route_decision_id,envelope.context_manifest_id,envelope.context_packet_id,
         envelope.lease_id,envelope.envelope_digest,envelope.route_decision_digest,
         envelope.context_manifest_digest,envelope.context_packet_digest,envelope.fencing_token
    into strict envelope_row
    from runtime.execution_envelope envelope
    join runtime.job job on job.realm_id=envelope.realm_id and job.id=envelope.job_id
    join runtime.lease lease on lease.realm_id=envelope.realm_id and lease.id=envelope.lease_id
   where envelope.realm_id=new.realm_id and envelope.run_id=run_row.id
     and envelope.created_at<=new.occurred_at
     and job.state in ('running','recovery-required')
     and lease.expires_at>new.occurred_at
   order by envelope.request_ordinal desc,envelope.created_at desc,envelope.id desc limit 1;

  select plan_digest,steps,effect_digest into strict plan_row
    from work.task_plan where realm_id=new.realm_id and id=run_row.plan_id;
  select intent_digest into intent_row from work.intent
   where realm_id=new.realm_id and work_item_id=run_row.work_item_id
   order by revision desc limit 1;
  select project_context_id into strict route_row from models.model_route_decision
   where realm_id=new.realm_id and id=envelope_row.route_decision_id;
  select id,revision,checkpoint_digest into checkpoint_head
    from work.checkpoint_v2 where realm_id=new.realm_id and run_id=run_row.id
   order by revision desc,id desc limit 1;
  if intent_row.intent_digest is null then
    select intent_digest into intent_row from work.checkpoint_v2
     where realm_id=new.realm_id and run_id=run_row.id
     order by revision desc,id desc limit 1;
  end if;
  if intent_row.intent_digest is null then
    raise exception 'pre-compact structural checkpoint requires canonical intent digest'
      using errcode='23514';
  end if;
  select entry_digest into journal_head_digest from work.work_journal_entry
   where realm_id=new.realm_id and work_item_id=run_row.work_item_id
   order by sequence desc limit 1;
  if journal_head_digest is null then
    raise exception 'pre-compact structural checkpoint requires journal head'
      using errcode='23514';
  end if;
  plan_step_count=jsonb_array_length(plan_row.steps);
  select count(*) into open_effect_count
    from runtime.effect_claim claim
    where claim.realm_id=new.realm_id and claim.job_id=envelope_row.job_id
      and not exists(select 1 from runtime.effect_receipt receipt
        where receipt.realm_id=claim.realm_id and receipt.claim_id=claim.id);
  select cardinality(read_resources),cardinality(write_resources)
    into read_resource_count,write_resource_count
    from runtime.job where realm_id=new.realm_id and id=envelope_row.job_id;

  payload=jsonb_build_object(
    'schema','zekam-compaction-checkpoint-draft/v1',
    'lifecycle_event_id',new.id,
    'lifecycle_stream_id',new.stream_id,
    'source_event_digest',new.event_digest,
    'client_kind',stream_row.client_kind,
    'client_instance_id',stream_row.client_instance_id,
    'session_id',stream_row.session_id,
    'run_id',run_row.id,
    'project_id',run_row.project_id,
    'work_item_id',run_row.work_item_id,
    'plan_id',run_row.plan_id,
    'intent_digest',intent_row.intent_digest,
    'plan_digest',plan_row.plan_digest,
    'plan_steps',(
      select coalesce(jsonb_agg(step.value order by step.ordinality),'[]'::jsonb)
      from (select value,ordinality from jsonb_array_elements(plan_row.steps)
            with ordinality order by ordinality limit 128) step),
    'plan_effect_digest',plan_row.effect_digest,
    'job_id',envelope_row.job_id,
    'attempt_id',envelope_row.attempt_id,
    'assignment_id',envelope_row.assignment_id,
    'execution_envelope_id',envelope_row.id,
    'execution_envelope_digest',envelope_row.envelope_digest,
    'route_decision_id',envelope_row.route_decision_id,
    'route_decision_digest',envelope_row.route_decision_digest,
    'context_manifest_id',envelope_row.context_manifest_id,
    'context_manifest_digest',envelope_row.context_manifest_digest,
    'context_packet_id',envelope_row.context_packet_id,
    'context_packet_digest',envelope_row.context_packet_digest,
    'observed_lease_id',envelope_row.lease_id,
    'observed_fencing_token',envelope_row.fencing_token,
    'source_revision',run_row.source_revision,
    'policy_digest',run_row.policy_digest,
    'routing_context_snapshot_id',route_row.project_context_id,
    'journal_head_digest',journal_head_digest,
    'previous_checkpoint_id',checkpoint_head.id,
    'previous_checkpoint_revision',checkpoint_head.revision,
    'previous_checkpoint_digest',checkpoint_head.checkpoint_digest,
    'run_state',(select state from runtime.execution_run
      where realm_id=new.realm_id and id=run_row.id),
    'budget',(
      select jsonb_build_object(
        'input_tokens_used',input_tokens_used,
        'output_tokens_used',output_tokens_used,
        'cost_micros_used',cost_micros_used,
        'deadline',deadline)
      from runtime.execution_run where realm_id=new.realm_id and id=run_row.id),
    'job_state',(
      select jsonb_build_object(
        'step_id',step_id,'kind',kind,'state',state,
        'read_resources',read_resources[1:128],'write_resources',write_resources[1:128])
      from runtime.job where realm_id=new.realm_id and id=envelope_row.job_id),
    'open_effects',coalesce((
      select jsonb_agg(jsonb_build_object(
        'claim_id',effect.id,'effect_digest',effect.effect_digest,
        'terminal_receipt_id',null,'terminal_status',null)
        order by effect.claimed_at,effect.id)
      from (select claim.id,claim.effect_digest,claim.claimed_at
        from runtime.effect_claim claim
        where claim.realm_id=new.realm_id and claim.job_id=envelope_row.job_id
          and not exists(select 1 from runtime.effect_receipt receipt
            where receipt.realm_id=claim.realm_id and receipt.claim_id=claim.id)
        order by claim.claimed_at,claim.id limit 128) effect),'[]'::jsonb),
    'selection_manifest',jsonb_build_object(
      'strategy','canonical-order-first-128',
      'plan_steps',jsonb_build_object('total',plan_step_count,'included',least(128,plan_step_count),
        'omitted',greatest(0,plan_step_count-128)),
      'open_effects',jsonb_build_object('total',open_effect_count,
        'included',least(128,open_effect_count),'omitted',greatest(0,open_effect_count-128)),
      'read_resources',jsonb_build_object('total',read_resource_count,
        'included',least(128,read_resource_count),'omitted',greatest(0,read_resource_count-128)),
      'write_resources',jsonb_build_object('total',write_resource_count,
        'included',least(128,write_resource_count),'omitted',greatest(0,write_resource_count-128))),
    'occurred_at',new.occurred_at,
    'transcript_included',false,
    'grants_authority',false
  );

  insert into work.compaction_checkpoint_outbox(
    id,realm_id,lifecycle_event_id,lifecycle_stream_id,run_id,job_id,attempt_id,
    assignment_id,execution_envelope_id,route_decision_id,context_manifest_id,
    context_packet_id,observed_lease_id,source_event_digest,structural_payload,
    payload_digest,state,created_at,grants_authority)
  values(gen_random_uuid(),new.realm_id,new.id,new.stream_id,run_row.id,envelope_row.job_id,
    envelope_row.attempt_id,envelope_row.assignment_id,envelope_row.id,
    envelope_row.route_decision_id,envelope_row.context_manifest_id,
    envelope_row.context_packet_id,envelope_row.lease_id,new.event_digest,payload,
    'sha256:'||encode(public.digest(convert_to(payload::text,'UTF8'),'sha256'),'hex'),
    'pending',new.ingested_at,false)
  on conflict(realm_id,lifecycle_event_id) do nothing;
  return new;
end $$;

create trigger lifecycle_pre_compact_checkpoint_compile
after insert on client.lifecycle_event
for each row execute function work.compile_pre_compact_outbox();

create function work.enforce_compaction_outbox_insert() returns trigger
language plpgsql security invoker set search_path=pg_catalog,work,core as $$
declare manifest jsonb;
begin
  if new.payload_digest is distinct from
      'sha256:'||encode(public.digest(convert_to(new.structural_payload::text,'UTF8'),'sha256'),'hex')
    or row(new.lifecycle_event_id,new.lifecycle_stream_id,new.run_id,new.job_id,new.attempt_id,
      new.assignment_id,new.execution_envelope_id,new.route_decision_id,new.context_manifest_id,
      new.context_packet_id,new.observed_lease_id,new.source_event_digest) is distinct from row(
      (new.structural_payload->>'lifecycle_event_id')::uuid,
      (new.structural_payload->>'lifecycle_stream_id')::uuid,
      (new.structural_payload->>'run_id')::uuid,
      (new.structural_payload->>'job_id')::uuid,
      (new.structural_payload->>'attempt_id')::uuid,
      (new.structural_payload->>'assignment_id')::uuid,
      (new.structural_payload->>'execution_envelope_id')::uuid,
      (new.structural_payload->>'route_decision_id')::uuid,
      (new.structural_payload->>'context_manifest_id')::uuid,
      (new.structural_payload->>'context_packet_id')::uuid,
      (new.structural_payload->>'observed_lease_id')::uuid,
      new.structural_payload->>'source_event_digest') then
    raise exception 'compaction checkpoint outbox payload/column binding mismatch'
      using errcode='23514';
  end if;
  manifest=new.structural_payload->'selection_manifest';
  if (manifest#>>'{plan_steps,included}')::integer
       <> jsonb_array_length(new.structural_payload->'plan_steps')
    or (manifest#>>'{open_effects,included}')::integer
       <> jsonb_array_length(new.structural_payload->'open_effects')
    or (manifest#>>'{plan_steps,total}')::integer
       <> (manifest#>>'{plan_steps,included}')::integer
          +(manifest#>>'{plan_steps,omitted}')::integer
    or (manifest#>>'{open_effects,total}')::integer
       <> (manifest#>>'{open_effects,included}')::integer
          +(manifest#>>'{open_effects,omitted}')::integer
    or (manifest#>>'{read_resources,total}')::integer
       <> (manifest#>>'{read_resources,included}')::integer
          +(manifest#>>'{read_resources,omitted}')::integer
    or (manifest#>>'{write_resources,total}')::integer
       <> (manifest#>>'{write_resources,included}')::integer
          +(manifest#>>'{write_resources,omitted}')::integer then
    raise exception 'compaction checkpoint selection manifest mismatch'
      using errcode='23514';
  end if;
  return new;
end $$;

create trigger compaction_checkpoint_outbox_insert_guard
before insert on work.compaction_checkpoint_outbox
for each row execute function work.enforce_compaction_outbox_insert();

create function work.enforce_compaction_outbox_update() returns trigger
language plpgsql security invoker set search_path=pg_catalog,work,core as $$
declare checkpoint record;
begin
  if row(new.realm_id,new.lifecycle_event_id,new.lifecycle_stream_id,new.run_id,new.job_id,
      new.attempt_id,new.assignment_id,new.execution_envelope_id,new.route_decision_id,
      new.context_manifest_id,new.context_packet_id,new.observed_lease_id,
      new.source_event_digest,new.structural_payload,new.payload_digest,new.created_at,
      new.grants_authority) is distinct from
     row(old.realm_id,old.lifecycle_event_id,old.lifecycle_stream_id,old.run_id,old.job_id,
      old.attempt_id,old.assignment_id,old.execution_envelope_id,old.route_decision_id,
      old.context_manifest_id,old.context_packet_id,old.observed_lease_id,
      old.source_event_digest,old.structural_payload,old.payload_digest,old.created_at,
      old.grants_authority) then
    raise exception 'compaction checkpoint outbox identity is immutable' using errcode='23514';
  end if;
  if old.state='completed' or (old.state='failed' and new.state<>'processing') then
    raise exception 'compaction checkpoint outbox terminal state is immutable' using errcode='23514';
  end if;
  if not ((old.state='pending' and new.state='processing')
      or (old.state='processing' and new.state in ('completed','failed'))
      or (old.state='failed' and new.state='processing')) then
    raise exception 'compaction checkpoint outbox state transition invalid'
      using errcode='23514';
  end if;
  if new.state='completed' then
    select checkpoint_digest,run_id,job_id,attempt_id,assignment_id,execution_envelope_id,
           route_decision_id,context_manifest_id,context_packet_id
      into strict checkpoint from work.checkpoint_v2
     where realm_id=new.realm_id and id=new.checkpoint_id;
    if row(new.checkpoint_digest,new.run_id,new.job_id,new.attempt_id,new.assignment_id,
        new.execution_envelope_id,new.route_decision_id,new.context_manifest_id,
        new.context_packet_id) is distinct from
       row(checkpoint.checkpoint_digest,checkpoint.run_id,checkpoint.job_id,
        checkpoint.attempt_id,checkpoint.assignment_id,checkpoint.execution_envelope_id,
        checkpoint.route_decision_id,checkpoint.context_manifest_id,
        checkpoint.context_packet_id) then
      raise exception 'compaction checkpoint completion binding mismatch'
        using errcode='23514';
    end if;
  end if;
  return new;
end $$;

create trigger compaction_checkpoint_outbox_update_guard
before update on work.compaction_checkpoint_outbox
for each row execute function work.enforce_compaction_outbox_update();
create trigger compaction_checkpoint_outbox_delete_guard
before delete on work.compaction_checkpoint_outbox
for each statement execute function core.deny_mutation();

alter table work.compaction_checkpoint_outbox enable row level security;
alter table work.compaction_checkpoint_outbox force row level security;
create policy scope_select on work.compaction_checkpoint_outbox for select
  using(realm_id=core.current_realm_id());
create policy scope_insert on work.compaction_checkpoint_outbox for insert
  with check(realm_id=core.current_realm_id());
create policy scope_update on work.compaction_checkpoint_outbox for update
  using(realm_id=core.current_realm_id()) with check(realm_id=core.current_realm_id());

create index compaction_checkpoint_outbox_pending_idx
  on work.compaction_checkpoint_outbox(realm_id,created_at,id) where state='pending';

revoke all on function work.compile_pre_compact_outbox() from public;
grant select,update on work.compaction_checkpoint_outbox to zekam_app;
grant execute on function work.enforce_compaction_outbox_update() to zekam_app;
