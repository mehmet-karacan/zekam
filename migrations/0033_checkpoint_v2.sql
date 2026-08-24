-- Structural, append-only checkpoint v2 with canonical execution/effect evidence.
create table work.checkpoint_v2 (
  id uuid primary key, realm_id uuid not null references core.realm(id) on delete restrict,
  checkpoint_key text not null, revision integer not null,
  previous_checkpoint_id uuid, previous_checkpoint_digest text,
  project_id uuid not null, work_item_id uuid not null, task_plan_id uuid not null,
  intent_digest text not null, plan_digest text not null, step_id text not null,
  run_id uuid not null, job_id uuid not null, attempt_id uuid not null,
  assignment_id uuid not null, execution_envelope_id uuid not null,
  execution_envelope_digest text not null, route_decision_id uuid not null,
  route_decision_digest text not null, context_manifest_id uuid not null,
  context_manifest_digest text not null, context_packet_id uuid not null,
  context_packet_digest text not null, source_revision text not null,
  policy_digest text not null, routing_context_snapshot_id uuid not null,
  capability_profile_digest text not null,
  dependency_snapshot_digest text not null, migration_head_digest text not null,
  architecture_digest text not null, rules_digest text not null,
  test_suite_digest text not null, model_inventory_digest text not null,
  journal_head_digest text not null, plan_steps text[] not null,
  completed_steps text[] not null, pending_steps text[] not null,
  logical_read_resources text[] not null, logical_write_resources text[] not null,
  sandbox_disposition text not null, sandbox_id text, base_revision text,
  patch_digest text, dirty_state_digest text, test_and_eval_refs jsonb not null,
  observed_lease_id uuid not null, observed_fencing_token bigint not null,
  tokens_used integer not null, cost_micros_used bigint not null, attempts_used integer not null,
  deadline timestamptz not null, rollback_recovery jsonb not null,
  next_safe_action jsonb, resumability text not null,
  grants_authority boolean not null default false,
  carries_active_lease boolean not null default false,
  approval_inherited boolean not null default false,
  created_at timestamptz not null, checkpoint_digest text not null,
  unique(realm_id,id), unique(realm_id,checkpoint_digest),
  unique(realm_id,checkpoint_key,revision),
  foreign key(realm_id,previous_checkpoint_id) references work.checkpoint_v2(realm_id,id),
  foreign key(realm_id,project_id) references projects.project(realm_id,id),
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id),
  foreign key(realm_id,task_plan_id) references work.task_plan(realm_id,id),
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id),
  foreign key(realm_id,job_id) references runtime.job(realm_id,id),
  foreign key(realm_id,attempt_id) references runtime.job_attempt(realm_id,id),
  foreign key(realm_id,assignment_id) references agents.assignment(realm_id,id),
  foreign key(realm_id,execution_envelope_id) references runtime.execution_envelope(realm_id,id),
  foreign key(realm_id,route_decision_id) references models.model_route_decision(realm_id,id),
  foreign key(realm_id,context_manifest_id) references work.context_manifest(realm_id,id),
  foreign key(realm_id,context_packet_id) references work.context_packet(realm_id,id),
  foreign key(realm_id,routing_context_snapshot_id)
    references projects.routing_context_snapshot(realm_id,id),
  foreign key(realm_id,observed_lease_id) references runtime.lease(realm_id,id),
  check(revision>0 and tokens_used>=0 and cost_micros_used>=0 and attempts_used>=0),
  check((revision=1 and previous_checkpoint_id is null and previous_checkpoint_digest is null)
    or (revision>1 and previous_checkpoint_id is not null and previous_checkpoint_digest is not null)),
  check(sandbox_disposition in ('not-applicable','clean','dirty')),
  check((sandbox_disposition='not-applicable' and sandbox_id is null and base_revision is null
      and patch_digest is null and dirty_state_digest is null)
    or (sandbox_disposition='clean' and sandbox_id is not null and base_revision is not null
      and patch_digest is null and dirty_state_digest is null)
    or (sandbox_disposition='dirty' and sandbox_id is not null and base_revision is not null
      and patch_digest is not null and dirty_state_digest is not null)),
  check(resumability in ('safe-continue','reconciliation-required','manual-review','blocked')),
  check(jsonb_typeof(test_and_eval_refs)='array' and jsonb_typeof(rollback_recovery)='array'
    and (next_safe_action is null or jsonb_typeof(next_safe_action)='object')),
  check(observed_fencing_token>0),
  check(not grants_authority and not carries_active_lease and not approval_inherited),
  check(checkpoint_digest ~ '^sha256:[0-9a-f]{64}$'
    and intent_digest ~ '^sha256:[0-9a-f]{64}$' and plan_digest ~ '^sha256:[0-9a-f]{64}$'
    and execution_envelope_digest ~ '^sha256:[0-9a-f]{64}$'
    and route_decision_digest ~ '^sha256:[0-9a-f]{64}$'
    and context_manifest_digest ~ '^sha256:[0-9a-f]{64}$'
    and context_packet_digest ~ '^sha256:[0-9a-f]{64}$'
    and policy_digest ~ '^sha256:[0-9a-f]{64}$'
    and capability_profile_digest ~ '^sha256:[0-9a-f]{64}$'
    and dependency_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'
    and migration_head_digest ~ '^sha256:[0-9a-f]{64}$'
    and architecture_digest ~ '^sha256:[0-9a-f]{64}$'
    and rules_digest ~ '^sha256:[0-9a-f]{64}$'
    and test_suite_digest ~ '^sha256:[0-9a-f]{64}$'
    and model_inventory_digest ~ '^sha256:[0-9a-f]{64}$'
    and journal_head_digest ~ '^sha256:[0-9a-f]{64}$'
    and (previous_checkpoint_digest is null
      or previous_checkpoint_digest ~ '^sha256:[0-9a-f]{64}$')
    and (patch_digest is null or patch_digest ~ '^sha256:[0-9a-f]{64}$')
    and (dirty_state_digest is null or dirty_state_digest ~ '^sha256:[0-9a-f]{64}$'))
);

create table work.checkpoint_v2_step_result (
  realm_id uuid not null, checkpoint_id uuid not null, step_id text not null,
  effect_kind text not null, result_digest text not null,
  job_id uuid not null, attempt_id uuid not null, assignment_id uuid not null,
  execution_envelope_id uuid not null, execution_envelope_digest text not null,
  primary key(realm_id,checkpoint_id,step_id),
  foreign key(realm_id,checkpoint_id) references work.checkpoint_v2(realm_id,id),
  foreign key(realm_id,job_id) references runtime.job(realm_id,id),
  foreign key(realm_id,attempt_id) references runtime.job_attempt(realm_id,id),
  foreign key(realm_id,assignment_id) references agents.assignment(realm_id,id),
  foreign key(realm_id,execution_envelope_id)
    references runtime.execution_envelope(realm_id,id),
  check(effect_kind in ('none','file-write','database-write','network-call','provider-call',
    'git-commit','git-push','process-run')),
  check(result_digest ~ '^sha256:[0-9a-f]{64}$'
    and execution_envelope_digest ~ '^sha256:[0-9a-f]{64}$')
);
create table work.checkpoint_v2_step_receipt (
  realm_id uuid not null, checkpoint_id uuid not null, step_id text not null,
  claim_id uuid not null, receipt_id uuid not null,
  primary key(realm_id,checkpoint_id,step_id,receipt_id),
  foreign key(realm_id,checkpoint_id,step_id)
    references work.checkpoint_v2_step_result(realm_id,checkpoint_id,step_id),
  foreign key(realm_id,claim_id) references runtime.effect_claim(realm_id,id),
  foreign key(realm_id,receipt_id) references runtime.effect_receipt(realm_id,id)
);
create table work.checkpoint_v2_step_verification (
  realm_id uuid not null, checkpoint_id uuid not null, step_id text not null,
  verifier_assignment_id uuid not null, verifier_invocation_id uuid not null,
  envelope_digest text not null,
  primary key(realm_id,checkpoint_id,step_id,verifier_invocation_id),
  foreign key(realm_id,checkpoint_id,step_id)
    references work.checkpoint_v2_step_result(realm_id,checkpoint_id,step_id),
  foreign key(realm_id,verifier_assignment_id) references agents.assignment(realm_id,id),
  foreign key(realm_id,verifier_invocation_id) references agents.invocation(realm_id,id),
  check(envelope_digest ~ '^sha256:[0-9a-f]{64}$')
);
create table work.checkpoint_v2_open_effect (
  realm_id uuid not null, checkpoint_id uuid not null, claim_id uuid not null,
  state text not null, effect_digest text not null,
  primary key(realm_id,checkpoint_id,claim_id),
  foreign key(realm_id,checkpoint_id) references work.checkpoint_v2(realm_id,id),
  foreign key(realm_id,claim_id) references runtime.effect_claim(realm_id,id),
  check(state in ('started-no-terminal-receipt','failed-reconciliation','unknown')),
  check(effect_digest ~ '^sha256:[0-9a-f]{64}$')
);

create function work.checkpoint_v2_header_guard() returns trigger
language plpgsql security invoker set search_path=pg_catalog,work,runtime,agents,models as $$
declare p record; r record; j record; a record; at record; rd record; cm record; cp record;
  ee record; routing_context record; migration_head record; journal_head record;
  observed_lease record;
  previous record; planned text[];
begin
  perform pg_advisory_xact_lock(hashtextextended(new.realm_id::text||new.checkpoint_key,0));
  if exists(select 1 from work.checkpoint_v2 existing
      where existing.realm_id=new.realm_id and existing.id=new.id
        and existing.checkpoint_key=new.checkpoint_key and existing.revision=new.revision
        and existing.checkpoint_digest=new.checkpoint_digest) then
    return new;
  end if;
  select id,revision,checkpoint_digest into previous from work.checkpoint_v2
    where realm_id=new.realm_id and checkpoint_key=new.checkpoint_key
    order by revision desc limit 1;
  if (not found and new.revision<>1) or (found and row(new.revision,new.previous_checkpoint_id,
      new.previous_checkpoint_digest) is distinct from
      row(previous.revision+1,previous.id,previous.checkpoint_digest)) then
    raise exception 'checkpoint v2 revision chain drift' using errcode='40001';
  end if;
  select project_id,work_item_id,source_revision,policy_digest,plan_digest,steps into p
    from work.task_plan where realm_id=new.realm_id and id=new.task_plan_id;
  select array_agg(x.value order by x.value) into planned
    from jsonb_array_elements(p.steps) step,
      lateral (select step->>'step_id' as value) x;
  select project_id,work_item_id,plan_id,source_revision,policy_digest,deadline,
    input_tokens_used+output_tokens_used token_used,cost_micros_used,state into r
    from runtime.execution_run where realm_id=new.realm_id and id=new.run_id;
  select project_id,work_item_id,plan_id,step_id,assignment_id,run_id,read_resources,
    write_resources into j from runtime.job where realm_id=new.realm_id and id=new.job_id;
  select job_id,attempt_number into at from runtime.job_attempt
    where realm_id=new.realm_id and id=new.attempt_id;
  select project_id,work_item_id,plan_id,step_id,risk into a from agents.assignment
    where realm_id=new.realm_id and id=new.assignment_id;
  select evidence_digest into rd from models.model_route_decision
    where realm_id=new.realm_id and id=new.route_decision_id;
  select manifest_digest into cm from work.context_manifest
    where realm_id=new.realm_id and id=new.context_manifest_id;
  select manifest_id,manifest_digest,packet_digest into cp from work.context_packet
    where realm_id=new.realm_id and id=new.context_packet_id;
  select project_id,source_revision,capability_profile_digest,dependency_digest,
    architecture_digest,rules_digest,suite_digest,inventory_digest,policy_digest
    into routing_context from projects.routing_context_snapshot
    where realm_id=new.realm_id and id=new.routing_context_snapshot_id;
  select version,checksum into migration_head from core.schema_migrations
    order by version desc limit 1;
  select entry_digest into journal_head from work.work_journal_entry
    where realm_id=new.realm_id and work_item_id=new.work_item_id
    order by sequence desc limit 1;
  select run_id,job_id,attempt_id,assignment_id,route_decision_id,route_decision_digest,
    context_manifest_id,context_manifest_digest,context_packet_id,context_packet_digest,
    source_revision,policy_digest,lease_id,fencing_token,envelope_digest into ee
    from runtime.execution_envelope
    where realm_id=new.realm_id and id=new.execution_envelope_id;
  select job_id,attempt_id,fencing_token into observed_lease from runtime.lease
    where realm_id=new.realm_id and id=new.observed_lease_id;
  if row(p.project_id,p.work_item_id,p.source_revision,p.policy_digest,p.plan_digest)
      is distinct from row(new.project_id,new.work_item_id,new.source_revision,new.policy_digest,
      new.plan_digest)
    or row(r.project_id,r.work_item_id,r.plan_id,r.source_revision,r.policy_digest,r.deadline)
      is distinct from row(new.project_id,new.work_item_id,new.task_plan_id,new.source_revision,
      new.policy_digest,new.deadline)
    or row(j.project_id,j.work_item_id,j.plan_id,j.step_id,j.assignment_id,j.run_id)
      is distinct from row(new.project_id,new.work_item_id,new.task_plan_id,new.step_id,
      new.assignment_id,new.run_id)
    or at.job_id is distinct from new.job_id
    or row(a.project_id,a.work_item_id,a.plan_id,a.step_id) is distinct from
      row(new.project_id,new.work_item_id,new.task_plan_id,new.step_id)
    or rd.evidence_digest is distinct from new.route_decision_digest
    or cm.manifest_digest is distinct from new.context_manifest_digest
    or row(cp.manifest_id,cp.manifest_digest,cp.packet_digest) is distinct from
      row(new.context_manifest_id,new.context_manifest_digest,new.context_packet_digest)
    or row(routing_context.project_id,routing_context.source_revision,
      routing_context.capability_profile_digest,routing_context.dependency_digest,
      routing_context.architecture_digest,routing_context.rules_digest,
      routing_context.suite_digest,routing_context.inventory_digest,routing_context.policy_digest)
      is distinct from row(new.project_id,new.source_revision,new.capability_profile_digest,
      new.dependency_snapshot_digest,new.architecture_digest,new.rules_digest,
      new.test_suite_digest,new.model_inventory_digest,new.policy_digest)
    or models.capability_runtime_jsonb_digest(to_jsonb(migration_head.checksum))
      is distinct from new.migration_head_digest
    or journal_head.entry_digest is distinct from new.journal_head_digest
    or row(ee.run_id,ee.job_id,ee.attempt_id,ee.assignment_id,ee.route_decision_id,
      ee.route_decision_digest,ee.context_manifest_id,ee.context_manifest_digest,
      ee.context_packet_id,ee.context_packet_digest,ee.source_revision,ee.policy_digest,
      ee.envelope_digest) is distinct from
      row(new.run_id,new.job_id,new.attempt_id,new.assignment_id,new.route_decision_id,
      new.route_decision_digest,new.context_manifest_id,new.context_manifest_digest,
      new.context_packet_id,new.context_packet_digest,new.source_revision,new.policy_digest,
      new.execution_envelope_digest)
    or planned is distinct from (select array_agg(x order by x) from unnest(new.plan_steps) x)
    or planned is distinct from (select array_agg(x order by x)
      from unnest(new.completed_steps||new.pending_steps) x)
    or new.completed_steps&&new.pending_steps
    or new.tokens_used<>r.token_used or new.cost_micros_used<>r.cost_micros_used
    or new.attempts_used<>at.attempt_number
    or new.logical_read_resources is distinct from j.read_resources
    or new.logical_write_resources is distinct from j.write_resources then
    raise exception 'checkpoint v2 canonical scope/partition/budget drift' using errcode='23514';
  end if;
  if row(observed_lease.job_id,observed_lease.attempt_id,observed_lease.fencing_token)
      is distinct from row(new.job_id,new.attempt_id,new.observed_fencing_token) then
    raise exception 'checkpoint v2 observed lease drift' using errcode='23514';
  end if;
  if row(new.observed_lease_id,new.observed_fencing_token)
      is distinct from row(ee.lease_id,ee.fencing_token) then
    raise exception 'checkpoint v2 envelope lease observation drift' using errcode='23514';
  end if;
  if (cardinality(new.pending_steps)=0 and new.next_safe_action is not null)
    or (cardinality(new.pending_steps)>0 and
      (new.next_safe_action is null
        or not (new.next_safe_action->>'step_id'=any(new.pending_steps)))) then
    raise exception 'checkpoint v2 next safe action partition drift' using errcode='23514';
  end if;
  if exists(select 1 from jsonb_array_elements_text(new.test_and_eval_refs) value
      where value !~ '^sha256:[0-9a-f]{64}$') then
    raise exception 'checkpoint v2 test/eval digest malformed' using errcode='23514';
  end if;
  return new;
end $$;
create trigger checkpoint_v2_header_check before insert on work.checkpoint_v2
for each row execute function work.checkpoint_v2_header_guard();

create function work.validate_checkpoint_v2(p_realm uuid,p_checkpoint uuid) returns boolean
language plpgsql security invoker set search_path=pg_catalog,work,runtime,agents as $$
declare c record;
begin
  select * into c from work.checkpoint_v2 where realm_id=p_realm and id=p_checkpoint;
  if not found then return false; end if;
  if (select array_agg(step_id order by step_id) from work.checkpoint_v2_step_result
      where realm_id=p_realm and checkpoint_id=p_checkpoint) is distinct from
     (select array_agg(x order by x) from unnest(c.completed_steps) x) then return false; end if;
  if exists(select 1 from work.checkpoint_v2_step_result sr
      join lateral (select step->>'effect' effect_kind
        from work.task_plan p, jsonb_array_elements(p.steps) step
        where p.realm_id=c.realm_id and p.id=c.task_plan_id
          and step->>'step_id'=sr.step_id) planned on true
      where sr.realm_id=p_realm and sr.checkpoint_id=p_checkpoint
        and sr.effect_kind is distinct from planned.effect_kind) then return false; end if;
  if exists(select 1 from work.checkpoint_v2_step_result sr
      left join runtime.job sj on sj.realm_id=sr.realm_id and sj.id=sr.job_id
      left join runtime.job_attempt sa on sa.realm_id=sr.realm_id and sa.id=sr.attempt_id
      left join agents.assignment assigned
        on assigned.realm_id=sr.realm_id and assigned.id=sr.assignment_id
      left join runtime.execution_envelope se
        on se.realm_id=sr.realm_id and se.id=sr.execution_envelope_id
      where sr.realm_id=p_realm and sr.checkpoint_id=p_checkpoint
        and (row(sj.project_id,sj.work_item_id,sj.plan_id,sj.step_id,sj.assignment_id,sj.run_id)
          is distinct from row(c.project_id,c.work_item_id,c.task_plan_id,sr.step_id,
            sr.assignment_id,c.run_id)
          or sa.job_id is distinct from sr.job_id
          or row(assigned.project_id,assigned.work_item_id,assigned.plan_id,assigned.step_id)
            is distinct from row(c.project_id,c.work_item_id,c.task_plan_id,sr.step_id)
          or row(se.run_id,se.job_id,se.attempt_id,se.assignment_id,se.envelope_digest)
            is distinct from row(c.run_id,sr.job_id,sr.attempt_id,sr.assignment_id,
              sr.execution_envelope_digest))) then return false; end if;
  if exists(select 1 from work.checkpoint_v2_step_result sr
      where sr.realm_id=p_realm and sr.checkpoint_id=p_checkpoint and sr.effect_kind<>'none'
      and not exists(select 1 from work.checkpoint_v2_step_receipt cr
        join runtime.effect_claim cl on cl.realm_id=cr.realm_id and cl.id=cr.claim_id
        join runtime.effect_receipt er on er.realm_id=cr.realm_id and er.id=cr.receipt_id
        where cr.realm_id=sr.realm_id and cr.checkpoint_id=sr.checkpoint_id
          and cr.step_id=sr.step_id and cl.job_id=sr.job_id and cl.attempt_id=sr.attempt_id
          and er.claim_id=cl.id and er.status='completed' and er.result_digest=sr.result_digest))
    then return false; end if;
  if exists(select 1 from work.checkpoint_v2_step_receipt cr
      join work.checkpoint_v2_step_result sr
        on sr.realm_id=cr.realm_id and sr.checkpoint_id=cr.checkpoint_id
          and sr.step_id=cr.step_id
      join runtime.effect_claim cl on cl.realm_id=cr.realm_id and cl.id=cr.claim_id
      join runtime.effect_receipt er on er.realm_id=cr.realm_id and er.id=cr.receipt_id
      where cr.realm_id=p_realm and cr.checkpoint_id=p_checkpoint
        and (row(cl.job_id,cl.attempt_id,er.claim_id,er.status,er.result_digest)
          is distinct from row(sr.job_id,sr.attempt_id,cl.id,'completed',sr.result_digest)))
    then return false; end if;
  if exists(select 1 from work.checkpoint_v2_step_result sr
      join agents.assignment assigned
        on assigned.realm_id=sr.realm_id and assigned.id=sr.assignment_id
      where sr.realm_id=p_realm and sr.checkpoint_id=p_checkpoint
        and assigned.risk in ('high','critical') and not exists(
          select 1 from work.checkpoint_v2_step_verification v
          join agents.assignment va on va.realm_id=v.realm_id and va.id=v.verifier_assignment_id
          join agents.invocation vi on vi.realm_id=v.realm_id and vi.id=v.verifier_invocation_id
          join agents.result_receipt rr on rr.realm_id=v.realm_id and rr.invocation_id=vi.id
          where v.realm_id=sr.realm_id and v.checkpoint_id=sr.checkpoint_id
            and v.step_id=sr.step_id and va.role='verifier' and va.agent_ref<>assigned.agent_ref
            and row(va.parent_assignment_id,va.project_id,va.work_item_id,va.plan_id,va.step_id)
              is not distinct from row(assigned.parent_assignment_id,assigned.project_id,
                assigned.work_item_id,assigned.plan_id,assigned.step_id)
            and rr.envelope_digest=v.envelope_digest
            and not exists(select 1 from agents.invocation bi
              where bi.realm_id=assigned.realm_id and bi.assignment_id=assigned.id
                and bi.execution_identity=vi.execution_identity))) then return false; end if;
  if exists(select 1 from work.checkpoint_v2_step_verification v
      join work.checkpoint_v2_step_result sr
        on sr.realm_id=v.realm_id and sr.checkpoint_id=v.checkpoint_id and sr.step_id=v.step_id
      join agents.assignment assigned
        on assigned.realm_id=sr.realm_id and assigned.id=sr.assignment_id
      join agents.assignment va on va.realm_id=v.realm_id and va.id=v.verifier_assignment_id
      join agents.invocation vi on vi.realm_id=v.realm_id and vi.id=v.verifier_invocation_id
      join agents.result_receipt rr on rr.realm_id=v.realm_id and rr.invocation_id=vi.id
      where v.realm_id=p_realm and v.checkpoint_id=p_checkpoint
        and (va.role<>'verifier' or va.agent_ref=assigned.agent_ref
          or row(va.parent_assignment_id,va.project_id,va.work_item_id,va.plan_id,va.step_id)
            is distinct from row(assigned.parent_assignment_id,assigned.project_id,
              assigned.work_item_id,assigned.plan_id,assigned.step_id)
          or vi.assignment_id is distinct from va.id
          or rr.assignment_id is distinct from va.id
          or rr.envelope_digest is distinct from v.envelope_digest
          or exists(select 1 from agents.invocation bi
            where bi.realm_id=assigned.realm_id and bi.assignment_id=assigned.id
              and bi.execution_identity=vi.execution_identity))) then return false; end if;
  if (select array_agg(cl.id order by cl.id) from runtime.effect_claim cl
      where cl.realm_id=p_realm and cl.job_id=c.job_id and cl.attempt_id=c.attempt_id
        and not exists(select 1 from runtime.effect_receipt er where er.claim_id=cl.id))
    is distinct from (select array_agg(oe.claim_id order by oe.claim_id)
      from work.checkpoint_v2_open_effect oe
      where oe.realm_id=p_realm and oe.checkpoint_id=p_checkpoint) then return false; end if;
  if exists(select 1 from work.checkpoint_v2_open_effect oe
      join runtime.effect_claim cl on cl.realm_id=oe.realm_id and cl.id=oe.claim_id
      where oe.realm_id=p_realm and oe.checkpoint_id=p_checkpoint
        and (cl.job_id<>c.job_id or cl.attempt_id<>c.attempt_id
          or cl.effect_digest<>oe.effect_digest or exists(
            select 1 from runtime.effect_receipt er where er.claim_id=cl.id))) then return false; end if;
  if (exists(select 1 from work.checkpoint_v2_open_effect oe
        where oe.realm_id=p_realm and oe.checkpoint_id=p_checkpoint)
      and c.resumability<>'reconciliation-required')
    or (not exists(select 1 from work.checkpoint_v2_open_effect oe
        where oe.realm_id=p_realm and oe.checkpoint_id=p_checkpoint)
      and c.resumability='reconciliation-required') then return false; end if;
  return true;
end $$;

create function work.checkpoint_v2_header_deferred_guard() returns trigger language plpgsql as $$
begin
  if not work.validate_checkpoint_v2(new.realm_id,new.id) then
    raise exception 'checkpoint v2 receipt/verification/open-effect completeness drift'
      using errcode='23514';
  end if;
  return new;
end $$;
create function work.checkpoint_v2_child_deferred_guard() returns trigger language plpgsql as $$
begin
  if not work.validate_checkpoint_v2(new.realm_id,new.checkpoint_id) then
    raise exception 'checkpoint v2 receipt/verification/open-effect completeness drift'
      using errcode='23514';
  end if;
  return new;
end $$;
create constraint trigger checkpoint_v2_complete after insert on work.checkpoint_v2
deferrable initially deferred for each row execute function work.checkpoint_v2_header_deferred_guard();
create constraint trigger checkpoint_v2_result_complete after insert on work.checkpoint_v2_step_result
deferrable initially deferred for each row execute function work.checkpoint_v2_child_deferred_guard();
create constraint trigger checkpoint_v2_receipt_complete after insert on work.checkpoint_v2_step_receipt
deferrable initially deferred for each row execute function work.checkpoint_v2_child_deferred_guard();
create constraint trigger checkpoint_v2_verification_complete after insert on work.checkpoint_v2_step_verification
deferrable initially deferred for each row execute function work.checkpoint_v2_child_deferred_guard();
create constraint trigger checkpoint_v2_open_complete after insert on work.checkpoint_v2_open_effect
deferrable initially deferred for each row execute function work.checkpoint_v2_child_deferred_guard();

create or replace function work.require_meaningful_job_checkpoint() returns trigger
language plpgsql security invoker set search_path=pg_catalog,work,runtime,core as $$
begin
  if new.state='completed' and old.state is distinct from 'completed'
    and (coalesce(new.payload->>'meaningful_step','false')='true'
      or (new.work_item_id is not null and new.plan_id is not null and new.step_id is not null)) then
    if new.run_id is null and not exists(select 1 from work.checkpoint c
        where c.realm_id=new.realm_id and c.job_id=new.id) then
      raise exception 'legacy meaningful terminal job requires checkpoint' using errcode='23514';
    elsif new.run_id is not null and not exists(
      select 1 from work.checkpoint_v2 c
      join runtime.job_attempt a on a.realm_id=c.realm_id and a.id=c.attempt_id
      where c.realm_id=new.realm_id and c.job_id=new.id and c.run_id=new.run_id
        and c.step_id=new.step_id and new.step_id=any(c.completed_steps)
        and a.attempt_number=new.attempt_count and work.validate_checkpoint_v2(c.realm_id,c.id)
        and c.revision=(select max(x.revision) from work.checkpoint_v2 x
          where x.realm_id=c.realm_id and x.checkpoint_key=c.checkpoint_key)) then
      raise exception 'run-bound meaningful terminal job requires current checkpoint v2'
        using errcode='23514';
    end if;
  end if;
  return new;
end $$;

do $$ declare target text; begin foreach target in array array[
 'work.checkpoint_v2','work.checkpoint_v2_step_result','work.checkpoint_v2_step_receipt',
 'work.checkpoint_v2_step_verification','work.checkpoint_v2_open_effect'] loop
 execute format('alter table %s enable row level security',target);
 execute format('alter table %s force row level security',target);
 execute format('create policy scope_select on %s for select using(realm_id=core.current_realm_id())',target);
 execute format('create policy scope_insert on %s for insert with check(realm_id=core.current_realm_id())',target);
 execute format('create trigger no_mutation before update or delete on %s for each statement execute function core.deny_mutation()',target);
end loop; end $$;
grant select,insert on work.checkpoint_v2,work.checkpoint_v2_step_result,
 work.checkpoint_v2_step_receipt,work.checkpoint_v2_step_verification,
 work.checkpoint_v2_open_effect to zekam_app;
grant execute on function work.validate_checkpoint_v2(uuid,uuid) to zekam_app;
