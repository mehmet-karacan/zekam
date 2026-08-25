-- Canonical bounded loop policy, evidence registry and dispatch ledger.

create function runtime.loop_effect_class(p_effect_kind text) returns text
language sql immutable strict as $$
  select case p_effect_kind
    when 'none' then 'read-only' when 'provider-call' then 'model-call'
    when 'process-run' then 'tool-call' when 'file-write' then 'file-write'
    when 'git-commit' then 'file-write' when 'git-push' then 'deploy'
    when 'database-write' then 'migration-apply'
    when 'network-call' then 'external-message' else null end
$$;

create table runtime.loop_policy (
  id uuid primary key, realm_id uuid not null, project_id uuid not null,
  work_item_id uuid not null, plan_id uuid not null, step_id text not null,
  assignment_id uuid not null, context_manifest_id uuid not null,
  validator_assignment_id uuid not null, max_attempts integer not null,
  max_tokens bigint not null, max_cost_micros bigint not null,
  deadline timestamptz not null, validator_spec_digest text not null,
  required_delta text[] not null, forbidden_effects text[] not null,
  terminal_states text[] not null, source_revision text not null,
  context_manifest_digest text not null, plan_digest text not null,
  policy_revision_digest text not null, canonical_effect_kind text not null,
  created_at timestamptz not null, canonical_body jsonb not null,
  policy_digest text not null, grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,policy_digest),
  unique(realm_id,project_id,work_item_id,plan_id,step_id,policy_revision_digest),
  foreign key(realm_id,project_id) references projects.project(realm_id,id),
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id),
  foreign key(realm_id,plan_id) references work.task_plan(realm_id,id),
  foreign key(realm_id,assignment_id) references agents.assignment(realm_id,id),
  foreign key(realm_id,context_manifest_id) references work.context_manifest(realm_id,id),
  foreign key(realm_id,validator_assignment_id) references agents.assignment(realm_id,id),
  check(max_attempts between 1 and 100 and max_tokens>0 and max_cost_micros>0),
  check(deadline>created_at and btrim(step_id)<>'' and btrim(source_revision)<>''),
  check(validator_spec_digest ~ '^sha256:[0-9a-f]{64}$'
    and context_manifest_digest ~ '^sha256:[0-9a-f]{64}$'
    and plan_digest ~ '^sha256:[0-9a-f]{64}$'
    and policy_revision_digest ~ '^sha256:[0-9a-f]{64}$'
    and policy_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(required_delta<@array['different-patch-digest','new-evidence',
    'new-failure-diagnosis','revised-plan']::text[] and cardinality(required_delta)>0),
  check(forbidden_effects<@array['read-only','model-call','tool-call','file-write',
    'deploy','migration-apply','external-message']::text[]),
  check(terminal_states=array['blocked','budget-exhausted','manual-review','passed']::text[]),
  check(canonical_effect_kind in ('none','file-write','database-write','network-call',
    'provider-call','git-commit','git-push','process-run')),
  check(jsonb_typeof(canonical_body)='object' and not grants_authority)
);

create table runtime.loop_delta_evidence (
  id uuid primary key, realm_id uuid not null, loop_id uuid not null,
  kind text not null, source_kind text not null, source_id uuid not null,
  evidence_digest text not null, source_created_at timestamptz not null,
  registered_at timestamptz not null, unique(realm_id,id),
  unique(realm_id,loop_id,kind,source_kind,source_id),
  foreign key(realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  check(kind in ('different-patch-digest','new-evidence','new-failure-diagnosis','revised-plan')),
  check(source_kind in ('checkpoint-patch','agent-result','verifier-diagnosis','task-plan')),
  check(evidence_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table runtime.loop_attempt (
  id uuid primary key, realm_id uuid not null, loop_id uuid not null,
  predecessor_attempt_id uuid, ordinal integer not null,
  semantic_request_digest text not null, prompt_digest text not null,
  context_digest text not null, action_digest text not null, binding_digest text not null,
  source_revision text not null, plan_digest text not null,
  policy_revision_digest text not null, validator_spec_digest text not null,
  canonical_effect_kind text not null, effect_class text not null,
  reserved_input_tokens bigint not null, reserved_output_tokens bigint not null,
  reserved_cost_micros bigint not null, delta_digest text not null,
  admitted_at timestamptz not null, unique(realm_id,id), unique(realm_id,loop_id,ordinal),
  foreign key(realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key(realm_id,predecessor_attempt_id) references runtime.loop_attempt(realm_id,id),
  check(ordinal>0 and reserved_input_tokens>=0 and reserved_output_tokens>=0
    and reserved_input_tokens+reserved_output_tokens>0 and reserved_cost_micros>=0),
  check(canonical_effect_kind in ('none','file-write','database-write','network-call',
    'provider-call','git-commit','git-push','process-run')),
  check(effect_class in ('read-only','model-call','tool-call','file-write','deploy',
    'migration-apply','external-message')),
  check(semantic_request_digest ~ '^sha256:[0-9a-f]{64}$'
    and prompt_digest ~ '^sha256:[0-9a-f]{64}$'
    and context_digest ~ '^sha256:[0-9a-f]{64}$'
    and action_digest ~ '^sha256:[0-9a-f]{64}$'
    and binding_digest ~ '^sha256:[0-9a-f]{64}$'
    and plan_digest ~ '^sha256:[0-9a-f]{64}$'
    and policy_revision_digest ~ '^sha256:[0-9a-f]{64}$'
    and validator_spec_digest ~ '^sha256:[0-9a-f]{64}$'
    and delta_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table runtime.loop_attempt_delta (
  realm_id uuid not null, loop_id uuid not null, attempt_id uuid not null, evidence_id uuid not null,
  primary key(realm_id,attempt_id,evidence_id), unique(realm_id,loop_id,evidence_id),
  foreign key(realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key(realm_id,attempt_id) references runtime.loop_attempt(realm_id,id),
  foreign key(realm_id,evidence_id) references runtime.loop_delta_evidence(realm_id,id)
);

create table runtime.loop_dispatch_binding (
  realm_id uuid not null, loop_id uuid not null, attempt_id uuid not null,
  surface text not null, dispatch_id uuid not null,
  semantic_request_digest text not null, action_digest text not null,
  bound_at timestamptz not null,
  primary key(realm_id,surface,dispatch_id),
  foreign key(realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key(realm_id,attempt_id) references runtime.loop_attempt(realm_id,id),
  check(surface in ('agent','model','tool')),
  check(semantic_request_digest ~ '^sha256:[0-9a-f]{64}$'
    and action_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table runtime.loop_attempt_outcome (
  id uuid primary key, realm_id uuid not null, loop_id uuid not null, attempt_id uuid not null,
  outcome text not null, validator_spec_digest text not null,
  result_invocation_id uuid not null, verifier_invocation_id uuid not null,
  validator_result_digest text not null, effect_receipt_id uuid,
  actual_input_tokens bigint not null, actual_output_tokens bigint not null,
  actual_cost_micros bigint not null, completed_at timestamptz not null,
  unique(realm_id,id), unique(realm_id,attempt_id), unique(realm_id,effect_receipt_id),
  foreign key(realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key(realm_id,attempt_id) references runtime.loop_attempt(realm_id,id),
  foreign key(realm_id,result_invocation_id) references agents.invocation(realm_id,id),
  foreign key(realm_id,verifier_invocation_id) references agents.invocation(realm_id,id),
  foreign key(realm_id,effect_receipt_id) references runtime.effect_receipt(realm_id,id),
  check(outcome in ('retryable-failure','passed','blocked','manual-review')),
  check(validator_spec_digest ~ '^sha256:[0-9a-f]{64}$'
    and validator_result_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(actual_input_tokens>=0 and actual_output_tokens>=0 and actual_cost_micros>=0)
);

create table runtime.loop_checkpoint (
  id uuid primary key, realm_id uuid not null, loop_id uuid not null,
  attempt_id uuid not null, outcome_id uuid not null, state text not null,
  result_digest text not null, validator_result_digest text not null,
  effect_receipt_id uuid, actual_input_tokens bigint not null,
  actual_output_tokens bigint not null, actual_cost_micros bigint not null,
  checkpoint_body jsonb not null, checkpoint_digest text not null,
  created_at timestamptz not null, unique(realm_id,id), unique(realm_id,attempt_id),
  unique(realm_id,checkpoint_digest),
  foreign key(realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key(realm_id,attempt_id) references runtime.loop_attempt(realm_id,id),
  foreign key(realm_id,outcome_id) references runtime.loop_attempt_outcome(realm_id,id),
  foreign key(realm_id,effect_receipt_id) references runtime.effect_receipt(realm_id,id),
  check(state in ('retryable-failure','passed','blocked','manual-review')),
  check(result_digest ~ '^sha256:[0-9a-f]{64}$'
    and validator_result_digest ~ '^sha256:[0-9a-f]{64}$'
    and checkpoint_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(actual_input_tokens>=0 and actual_output_tokens>=0 and actual_cost_micros>=0
    and jsonb_typeof(checkpoint_body)='object')
);

create table runtime.loop_terminal (
  id uuid primary key, realm_id uuid not null, loop_id uuid not null, attempt_id uuid,
  checkpoint_id uuid,
  state text not null, reason text not null, evidence_digest text not null,
  terminal_at timestamptz not null, unique(realm_id,id), unique(realm_id,loop_id),
  foreign key(realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key(realm_id,attempt_id) references runtime.loop_attempt(realm_id,id),
  foreign key(realm_id,checkpoint_id) references runtime.loop_checkpoint(realm_id,id),
  check(state in ('passed','blocked','budget-exhausted','manual-review')),
  check(btrim(reason)<>'' and evidence_digest ~ '^sha256:[0-9a-f]{64}$')
);

create function runtime.create_loop_policy(
  p_id uuid,p_assignment_id uuid,p_context_manifest_id uuid,p_validator_assignment_id uuid,
  p_max_attempts integer,p_max_tokens bigint,p_max_cost_micros bigint,p_deadline timestamptz,
  p_validator_spec_digest text,p_required_delta text[],p_forbidden_effects text[],
  p_policy_revision_digest text
) returns table(loop_id uuid,policy_digest text,inserted boolean)
language plpgsql security definer set search_path=pg_catalog,runtime,work,agents,core as $$
declare rid uuid:=core.current_realm_id(); moment timestamptz:=clock_timestamp();
  a record; v record; p record; m record; step jsonb; body jsonb; pd text; existing uuid;
begin
  select * into a from agents.assignment where realm_id=rid and id=p_assignment_id;
  if not found or a.role='coordinator' or a.status not in ('ready','active')
     or a.plan_id is null or a.step_id is null
     or not exists(select 1 from work.work_item wi where wi.realm_id=rid
       and wi.id=a.work_item_id and wi.project_id=a.project_id) then
    raise exception 'loop policy canonical child assignment ister' using errcode='42501'; end if;
  select * into p from work.task_plan where realm_id=rid and id=a.plan_id;
  if not found or row(p.project_id,p.work_item_id) is distinct from row(a.project_id,a.work_item_id)
     or p.revision<>(select max(x.revision) from work.task_plan x
       where x.realm_id=rid and x.work_item_id=p.work_item_id) then
    raise exception 'loop policy current exact task plan ister' using errcode='42501'; end if;
  select value into step from jsonb_array_elements(p.steps) value where value->>'step_id'=a.step_id;
  if step is null or runtime.loop_effect_class(step->>'effect') is null then
    raise exception 'loop policy canonical plan step/effect ister' using errcode='42501'; end if;
  select * into m from work.context_manifest where realm_id=rid and id=p_context_manifest_id;
  if not found or row(m.project_id,m.work_item_id,m.manifest_digest)
     is distinct from row(a.project_id,a.work_item_id,a.context_manifest_digest) then
    raise exception 'loop policy exact context manifest ister' using errcode='42501'; end if;
  select * into v from agents.assignment where realm_id=rid and id=p_validator_assignment_id;
  if not found or v.role<>'verifier' or v.status not in ('ready','active')
     or v.agent_ref=a.agent_ref or v.parent_assignment_id is distinct from a.parent_assignment_id
     or row(v.project_id,v.work_item_id,v.plan_id,v.step_id,v.context_manifest_digest,v.instruction_digest)
        is distinct from row(a.project_id,a.work_item_id,a.plan_id,a.step_id,
          a.context_manifest_digest,p_validator_spec_digest) then
    raise exception 'loop policy independent exact validator assignment ister' using errcode='42501'; end if;
  if p_deadline<=moment then raise exception 'loop deadline gecmis' using errcode='22023'; end if;
  if p_required_delta is distinct from array(select distinct x from unnest(p_required_delta) x order by x)
     or p_forbidden_effects is distinct from array(select distinct x from unnest(p_forbidden_effects) x order by x) then
    raise exception 'loop policy arraylari canonical unique sirali olmali' using errcode='22023';
  end if;
  body:=jsonb_build_object('schema','zekam-loop-policy/v1','id',p_id::text,
    'realm_id',rid::text,'project_id',a.project_id::text,'work_item_id',a.work_item_id::text,
    'plan_id',a.plan_id::text,'step_id',a.step_id,'assignment_id',a.id::text,
    'context_manifest_id',m.id::text,'validator_assignment_id',v.id::text,
    'max_attempts',p_max_attempts,'max_tokens',p_max_tokens,'max_cost_micros',p_max_cost_micros,
    'deadline',p_deadline,'validator_spec_digest',p_validator_spec_digest,
    'required_delta',to_jsonb(p_required_delta),'forbidden_effects',to_jsonb(p_forbidden_effects),
    'terminal_states',to_jsonb(array['blocked','budget-exhausted','manual-review','passed']::text[]),
    'source_revision',p.source_revision,'context_manifest_digest',m.manifest_digest,
    'plan_digest',p.plan_digest,'policy_revision_digest',p_policy_revision_digest,
    'canonical_effect_kind',step->>'effect','created_at',moment,'grants_authority',false);
  pd:='sha256:'||encode(public.digest(convert_to(body::text,'UTF8'),'sha256'),'hex');
  insert into runtime.loop_policy(id,realm_id,project_id,work_item_id,plan_id,step_id,
    assignment_id,context_manifest_id,validator_assignment_id,max_attempts,max_tokens,
    max_cost_micros,deadline,validator_spec_digest,required_delta,forbidden_effects,
    terminal_states,source_revision,context_manifest_digest,plan_digest,policy_revision_digest,
    canonical_effect_kind,created_at,canonical_body,policy_digest,grants_authority)
  values(p_id,rid,a.project_id,a.work_item_id,a.plan_id,a.step_id,a.id,m.id,v.id,
    p_max_attempts,p_max_tokens,p_max_cost_micros,p_deadline,p_validator_spec_digest,
    p_required_delta,p_forbidden_effects,array['blocked','budget-exhausted','manual-review','passed'],
    p.source_revision,m.manifest_digest,p.plan_digest,p_policy_revision_digest,
    step->>'effect',moment,body,pd,false)
  on conflict(realm_id,project_id,work_item_id,plan_id,step_id,policy_revision_digest)
  do nothing returning id into existing;
  if existing is null then
    select candidate.id,candidate.policy_digest into existing,pd from runtime.loop_policy candidate
      where candidate.realm_id=rid and candidate.project_id=a.project_id
        and candidate.work_item_id=a.work_item_id and candidate.plan_id=a.plan_id
        and candidate.step_id=a.step_id and candidate.policy_revision_digest=p_policy_revision_digest;
    return query select existing,pd,false;
  else return query select existing,pd,true; end if;
end $$;

create function runtime.register_loop_delta_evidence(
  p_id uuid,p_loop_id uuid,p_kind text,p_source_id uuid
) returns text language plpgsql security definer
set search_path=pg_catalog,runtime,work,agents,core as $$
declare rid uuid:=core.current_realm_id(); moment timestamptz:=clock_timestamp();
  lp runtime.loop_policy%rowtype; d text; sk text; sc timestamptz;
begin
  select * into lp from runtime.loop_policy where realm_id=rid and id=p_loop_id;
  if not found or not p_kind=any(lp.required_delta) then
    raise exception 'loop evidence policy/kind reddi' using errcode='42501'; end if;
  if p_kind='different-patch-digest' then
    select patch_digest,created_at into d,sc from work.checkpoint_v2
      where realm_id=rid and id=p_source_id and project_id=lp.project_id
        and work_item_id=lp.work_item_id and task_plan_id=lp.plan_id and step_id=lp.step_id
        and source_revision=lp.source_revision and plan_digest=lp.plan_digest
        and sandbox_disposition='dirty' and work.validate_checkpoint_v2(realm_id,id);
    sk:='checkpoint-patch';
  elsif p_kind='revised-plan' then
    select candidate.plan_digest,candidate.created_at into d,sc from work.task_plan candidate
      join work.task_plan original on original.realm_id=candidate.realm_id and original.id=lp.plan_id
      where candidate.realm_id=rid and candidate.id=p_source_id
        and candidate.project_id=lp.project_id and candidate.work_item_id=lp.work_item_id
        and candidate.revision>original.revision and candidate.plan_digest<>original.plan_digest;
    sk:='task-plan';
  elsif p_kind='new-evidence' then
    select rr.envelope_digest,rr.created_at into d,sc from agents.result_receipt rr
      join agents.assignment a on a.realm_id=rr.realm_id and a.id=rr.assignment_id
      where rr.realm_id=rid and rr.invocation_id=p_source_id and a.project_id=lp.project_id
        and a.work_item_id=lp.work_item_id and a.plan_id=lp.plan_id and a.step_id=lp.step_id
        and a.role in ('researcher','reviewer','verifier','critic');
    sk:='agent-result';
  elsif p_kind='new-failure-diagnosis' then
    select rr.envelope_digest,rr.created_at into d,sc from agents.result_receipt rr
      join agents.assignment a on a.realm_id=rr.realm_id and a.id=rr.assignment_id
      where rr.realm_id=rid and rr.invocation_id=p_source_id and a.project_id=lp.project_id
        and a.work_item_id=lp.work_item_id and a.plan_id=lp.plan_id and a.step_id=lp.step_id
        and a.role in ('verifier','critic') and a.id<>lp.assignment_id;
    sk:='verifier-diagnosis';
  else raise exception 'loop evidence kind desteklenmiyor' using errcode='22023'; end if;
  if d is null then raise exception 'canonical loop evidence bulunamadi' using errcode='42501'; end if;
  insert into runtime.loop_delta_evidence(id,realm_id,loop_id,kind,source_kind,source_id,
    evidence_digest,source_created_at,registered_at)
  values(p_id,rid,p_loop_id,p_kind,sk,p_source_id,d,sc,moment)
  on conflict(realm_id,loop_id,kind,source_kind,source_id) do nothing;
  return d;
end $$;

create function runtime.admit_loop_attempt(
  p_attempt_id uuid,p_loop_id uuid,p_predecessor_attempt_id uuid,
  p_semantic_request_digest text,p_prompt_digest text,p_context_digest text,
  p_action_digest text,p_binding_digest text,p_source_revision text,p_plan_digest text,
  p_policy_revision_digest text,p_validator_spec_digest text,p_reserved_input_tokens bigint,
  p_reserved_output_tokens bigint,p_reserved_cost_micros bigint,p_evidence_ids uuid[],
  p_delta_digest text
) returns table(admitted boolean,attempt_id uuid,ordinal integer,terminal_state text,reason text)
language plpgsql security definer set search_path=pg_catalog,runtime,work,agents,core as $$
declare rid uuid:=core.current_realm_id(); moment timestamptz:=clock_timestamp();
  lp runtime.loop_policy%rowtype; terminal runtime.loop_terminal%rowtype;
  attempt_count integer; reserved_tokens bigint; reserved_cost bigint; latest uuid;
  latest_at timestamptz; effect_kind text; effect_class text; term text; why text;
  computed_semantic_digest text; computed_binding_digest text; computed_delta_digest text;
  canonical_value text;
begin
  perform pg_advisory_xact_lock(hashtextextended(rid::text||':'||p_loop_id::text,0));
  select * into lp from runtime.loop_policy where realm_id=rid and id=p_loop_id for update;
  if not found then raise exception 'loop policy bulunamadi' using errcode='42501'; end if;
  select * into terminal from runtime.loop_terminal where realm_id=rid and loop_id=p_loop_id;
  if found then return query select false,null::uuid,null::integer,terminal.state,
    'loop terminal state kapali'::text; return; end if;
  select value->>'effect' into effect_kind from work.task_plan p,
    lateral jsonb_array_elements(p.steps) value
    where p.realm_id=rid and p.id=lp.plan_id and p.project_id=lp.project_id
      and p.work_item_id=lp.work_item_id and p.plan_digest=lp.plan_digest
      and p.source_revision=lp.source_revision and value->>'step_id'=lp.step_id
      and p.revision=(select max(x.revision) from work.task_plan x
        where x.realm_id=rid and x.work_item_id=lp.work_item_id);
  if effect_kind is null or effect_kind<>lp.canonical_effect_kind
     or not exists(select 1 from agents.assignment a where a.realm_id=rid and a.id=lp.assignment_id
       and a.status in ('ready','active') and a.plan_id=lp.plan_id and a.step_id=lp.step_id)
     or not exists(select 1 from work.context_manifest m where m.realm_id=rid
       and m.id=lp.context_manifest_id and m.manifest_digest=lp.context_manifest_digest)
     or not exists(select 1 from agents.assignment v where v.realm_id=rid
       and v.id=lp.validator_assignment_id and v.status in ('ready','active')
       and v.role='verifier' and v.instruction_digest=lp.validator_spec_digest) then
    raise exception 'loop policy canonical scope/currentness drift' using errcode='42501'; end if;
  if row(lp.source_revision,lp.plan_digest,lp.policy_revision_digest,lp.validator_spec_digest,
         lp.context_manifest_digest) is distinct from
     row(p_source_revision,p_plan_digest,p_policy_revision_digest,p_validator_spec_digest,
         p_context_digest) then
    raise exception 'loop attempt policy/source/context/validator drift' using errcode='42501'; end if;
  canonical_value:='{"action_digest":'||to_json(p_action_digest)::text||
    ',"context_digest":'||to_json(p_context_digest)::text||
    ',"prompt_digest":'||to_json(p_prompt_digest)::text||'}';
  computed_semantic_digest:='sha256:'||encode(public.digest(
    convert_to(canonical_value,'UTF8'),'sha256'),'hex');
  canonical_value:='{"plan_digest":'||to_json(p_plan_digest)::text||
    ',"policy_revision_digest":'||to_json(p_policy_revision_digest)::text||
    ',"predecessor_attempt_id":'||coalesce(to_json(p_predecessor_attempt_id::text)::text,'null')||
    ',"source_revision":'||to_json(p_source_revision)::text||
    ',"validator_spec_digest":'||to_json(p_validator_spec_digest)::text||'}';
  computed_binding_digest:='sha256:'||encode(public.digest(
    convert_to(canonical_value,'UTF8'),'sha256'),'hex');
  if p_evidence_ids is distinct from array(
      select distinct eid from unnest(coalesce(p_evidence_ids,array[]::uuid[])) eid order by eid
    ) then
    raise exception 'loop evidence kimlikleri canonical unique sirali olmali' using errcode='22023';
  end if;
  select '['||coalesce(string_agg(to_json(eid::text)::text,',' order by eid),'')||']'
    into canonical_value from unnest(coalesce(p_evidence_ids,array[]::uuid[])) eid;
  computed_delta_digest:='sha256:'||encode(public.digest(
    convert_to(canonical_value,'UTF8'),'sha256'),'hex');
  if row(p_semantic_request_digest,p_binding_digest,p_delta_digest) is distinct from
     row(computed_semantic_digest,computed_binding_digest,computed_delta_digest) then
    raise exception 'loop attempt supplied digest canonical body ile uyusmuyor' using errcode='42501';
  end if;
  effect_class:=runtime.loop_effect_class(effect_kind);
  select count(*),coalesce(sum(reserved_input_tokens+reserved_output_tokens),0),
    coalesce(sum(reserved_cost_micros),0) into attempt_count,reserved_tokens,reserved_cost
    from runtime.loop_attempt counted
    where counted.realm_id=rid and counted.loop_id=p_loop_id;
  select candidate.id,candidate.admitted_at into latest,latest_at
    from runtime.loop_attempt candidate
    where candidate.realm_id=rid and candidate.loop_id=p_loop_id
    order by candidate.ordinal desc limit 1;
  if effect_class=any(lp.forbidden_effects) then term:='manual-review'; why:='forbidden canonical effect';
  elsif moment>=lp.deadline or attempt_count>=lp.max_attempts
    or reserved_tokens+p_reserved_input_tokens+p_reserved_output_tokens>lp.max_tokens
    or reserved_cost+p_reserved_cost_micros>lp.max_cost_micros then
    term:='budget-exhausted'; why:='attempt/token/cost/deadline budget exhausted'; end if;
  if term is null and ((attempt_count=0 and p_predecessor_attempt_id is not null)
    or (attempt_count>0 and p_predecessor_attempt_id is distinct from latest)) then
    raise exception 'loop predecessor latest attempt ile exact eslesmiyor' using errcode='42501'; end if;
  if term is null and attempt_count>0 and not exists(
    select 1 from runtime.loop_attempt_outcome prior_outcome
    where prior_outcome.realm_id=rid and prior_outcome.attempt_id=latest) then
    term:='manual-review'; why:='previous attempt terminal receipt tasimiyor'; end if;
  if term is null and exists(select 1 from runtime.loop_attempt prior_attempt
      where prior_attempt.realm_id=rid and prior_attempt.loop_id=p_loop_id
        and prior_attempt.semantic_request_digest=p_semantic_request_digest) then
    if cardinality(coalesce(p_evidence_ids,array[]::uuid[]))=0 or exists(
      select 1 from unnest(p_evidence_ids) eid left join runtime.loop_delta_evidence e
        on e.realm_id=rid and e.id=eid and e.loop_id=p_loop_id
      where e.id is null or not e.kind=any(lp.required_delta) or e.source_created_at<=latest_at
        or exists(select 1 from runtime.loop_attempt_delta used where used.realm_id=rid
          and used.loop_id=p_loop_id and used.evidence_id=eid)) then
      term:='blocked'; why:='same semantic request canonical new evidence delta tasimiyor'; end if;
  elsif attempt_count=0 and cardinality(coalesce(p_evidence_ids,array[]::uuid[]))>0 then
    raise exception 'ilk loop attempt evidence delta tasiyamaz' using errcode='42501'; end if;
  if term is not null then
    insert into runtime.loop_terminal(id,realm_id,loop_id,attempt_id,state,reason,evidence_digest,terminal_at)
    values(gen_random_uuid(),rid,p_loop_id,null,term,why,'sha256:'||encode(public.digest(
      convert_to(p_loop_id::text||':'||term||':'||why,'UTF8'),'sha256'),'hex'),moment);
    return query select false,null::uuid,null::integer,term,why; return; end if;
  insert into runtime.loop_attempt(id,realm_id,loop_id,predecessor_attempt_id,ordinal,
    semantic_request_digest,prompt_digest,context_digest,action_digest,binding_digest,
    source_revision,plan_digest,policy_revision_digest,validator_spec_digest,
    canonical_effect_kind,effect_class,reserved_input_tokens,reserved_output_tokens,
    reserved_cost_micros,delta_digest,admitted_at)
  values(p_attempt_id,rid,p_loop_id,p_predecessor_attempt_id,attempt_count+1,
    p_semantic_request_digest,p_prompt_digest,p_context_digest,p_action_digest,p_binding_digest,
    p_source_revision,p_plan_digest,p_policy_revision_digest,p_validator_spec_digest,
    effect_kind,effect_class,p_reserved_input_tokens,p_reserved_output_tokens,
    p_reserved_cost_micros,p_delta_digest,moment);
  insert into runtime.loop_attempt_delta(realm_id,loop_id,attempt_id,evidence_id)
    select rid,p_loop_id,p_attempt_id,eid from unnest(coalesce(p_evidence_ids,array[]::uuid[])) eid;
  return query select true,p_attempt_id,attempt_count+1,null::text,'admitted'::text;
end $$;

create function runtime.complete_loop_attempt(
  p_outcome_id uuid,p_attempt_id uuid,p_outcome text,p_validator_spec_digest text,
  p_result_invocation_id uuid,p_verifier_invocation_id uuid,p_effect_receipt_id uuid,
  p_actual_input_tokens bigint,p_actual_output_tokens bigint,p_actual_cost_micros bigint
) returns text language plpgsql security definer set search_path=pg_catalog,runtime,agents,core as $$
declare rid uuid:=core.current_realm_id(); moment timestamptz:=clock_timestamp();
  at runtime.loop_attempt%rowtype; lp runtime.loop_policy%rowtype; validator_digest text;
  result_digest text; effect_ok boolean; term text; spent_tokens bigint; spent_cost bigint;
  checkpoint_id uuid:=gen_random_uuid(); checkpoint_body jsonb; checkpoint_digest text;
begin
  select * into at from runtime.loop_attempt where realm_id=rid and id=p_attempt_id;
  if not found then raise exception 'loop attempt bulunamadi' using errcode='42501'; end if;
  perform pg_advisory_xact_lock(hashtextextended(rid::text||':'||at.loop_id::text,0));
  select * into lp from runtime.loop_policy where realm_id=rid and id=at.loop_id for update;
  if exists(select 1 from runtime.loop_terminal where realm_id=rid and loop_id=at.loop_id)
    or exists(select 1 from runtime.loop_attempt_outcome where realm_id=rid and attempt_id=p_attempt_id)
    then raise exception 'loop attempt zaten terminal/outcome kayitli' using errcode='23505'; end if;
  if p_validator_spec_digest<>lp.validator_spec_digest or p_validator_spec_digest<>at.validator_spec_digest
    then raise exception 'loop validator spec drift' using errcode='42501'; end if;
  select rr.envelope_digest into result_digest from agents.invocation i
    join agents.result_receipt rr on rr.realm_id=i.realm_id and rr.invocation_id=i.id
    where i.realm_id=rid and i.id=p_result_invocation_id and i.assignment_id=lp.assignment_id
      and rr.created_at>=at.admitted_at;
  if result_digest is null then raise exception 'canonical loop result receipt bulunamadi' using errcode='42501'; end if;
  select rr.envelope_digest into validator_digest from agents.invocation i
    join agents.result_receipt rr on rr.realm_id=i.realm_id and rr.invocation_id=i.id
    where i.realm_id=rid and i.id=p_verifier_invocation_id
      and i.assignment_id=lp.validator_assignment_id and rr.created_at>=at.admitted_at
      and i.execution_identity<>(select execution_identity from agents.invocation
        where realm_id=rid and id=p_result_invocation_id);
  if validator_digest is null then
    raise exception 'independent canonical validator receipt bulunamadi' using errcode='42501'; end if;
  if at.effect_class='read-only' then
    if p_effect_receipt_id is not null then
      raise exception 'read-only loop effect receipt tasiyamaz' using errcode='42501'; end if;
  else
    select exists(select 1 from runtime.effect_receipt er
      join runtime.effect_claim ec on ec.realm_id=er.realm_id and ec.id=er.claim_id
      join runtime.job j on j.realm_id=ec.realm_id and j.id=ec.job_id
      join runtime.job_attempt ja on ja.realm_id=ec.realm_id and ja.id=ec.attempt_id
      where er.realm_id=rid and er.id=p_effect_receipt_id and er.status='completed'
        and er.completed_at>=at.admitted_at and ec.claimed_at>=at.admitted_at
        and j.assignment_id=lp.assignment_id and j.project_id=lp.project_id
        and j.work_item_id=lp.work_item_id and j.plan_id=lp.plan_id and j.step_id=lp.step_id
        and ja.job_id=j.id and ja.attempt_number=j.attempt_count
        and ja.fencing_token=j.fencing_token
        and exists(select 1 from runtime.loop_dispatch_binding b
          where b.realm_id=rid and b.attempt_id=at.id and (
            (b.surface='tool' and b.dispatch_id=ec.id)
            or (b.surface='agent' and b.dispatch_id=p_result_invocation_id)
            or (b.surface='model' and exists(select 1 from models.invocation_attempt mi
              where mi.realm_id=rid and mi.id=b.dispatch_id and mi.effect_claim_id=ec.id))
          ))) into effect_ok;
    if not coalesce(effect_ok,false) then
      raise exception 'canonical effect claim/receipt bulunamadi' using errcode='42501'; end if;
  end if;
  insert into runtime.loop_attempt_outcome(id,realm_id,loop_id,attempt_id,outcome,
    validator_spec_digest,result_invocation_id,verifier_invocation_id,validator_result_digest,
    effect_receipt_id,actual_input_tokens,actual_output_tokens,actual_cost_micros,completed_at)
  values(p_outcome_id,rid,at.loop_id,p_attempt_id,p_outcome,p_validator_spec_digest,
    p_result_invocation_id,p_verifier_invocation_id,validator_digest,p_effect_receipt_id,
    p_actual_input_tokens,p_actual_output_tokens,p_actual_cost_micros,moment);
  checkpoint_body:=jsonb_build_object('schema','zekam-loop-checkpoint/v1',
    'id',checkpoint_id::text,'loop_id',at.loop_id::text,'attempt_id',at.id::text,
    'outcome_id',p_outcome_id::text,'state',p_outcome,'result_digest',result_digest,
    'validator_result_digest',validator_digest,
    'effect_receipt_id',case when p_effect_receipt_id is null then null else p_effect_receipt_id::text end,
    'actual_input_tokens',p_actual_input_tokens,'actual_output_tokens',p_actual_output_tokens,
    'actual_cost_micros',p_actual_cost_micros,'created_at',moment);
  checkpoint_digest:='sha256:'||encode(public.digest(
    convert_to(checkpoint_body::text,'UTF8'),'sha256'),'hex');
  insert into runtime.loop_checkpoint(id,realm_id,loop_id,attempt_id,outcome_id,state,
    result_digest,validator_result_digest,effect_receipt_id,actual_input_tokens,
    actual_output_tokens,actual_cost_micros,checkpoint_body,checkpoint_digest,created_at)
  values(checkpoint_id,rid,at.loop_id,at.id,p_outcome_id,p_outcome,result_digest,
    validator_digest,p_effect_receipt_id,p_actual_input_tokens,p_actual_output_tokens,
    p_actual_cost_micros,checkpoint_body,checkpoint_digest,moment);
  term:=case p_outcome when 'passed' then 'passed' when 'blocked' then 'blocked'
    when 'manual-review' then 'manual-review' else null end;
  if p_actual_input_tokens>at.reserved_input_tokens or p_actual_output_tokens>at.reserved_output_tokens
    or p_actual_cost_micros>at.reserved_cost_micros then term:='manual-review'; end if;
  select coalesce(sum(actual_input_tokens+actual_output_tokens),0),
    coalesce(sum(actual_cost_micros),0) into spent_tokens,spent_cost
    from runtime.loop_attempt_outcome where realm_id=rid and loop_id=at.loop_id;
  if term is null and (spent_tokens>=lp.max_tokens or spent_cost>=lp.max_cost_micros
    or moment>=lp.deadline) then term:='budget-exhausted'; end if;
  if term is not null then
    insert into runtime.loop_terminal(id,realm_id,loop_id,attempt_id,checkpoint_id,state,reason,evidence_digest,terminal_at)
    values(gen_random_uuid(),rid,at.loop_id,p_attempt_id,checkpoint_id,term,
      case when term='budget-exhausted' then 'actual usage/deadline budget exhausted'
        when p_actual_input_tokens>at.reserved_input_tokens
          or p_actual_output_tokens>at.reserved_output_tokens
          or p_actual_cost_micros>at.reserved_cost_micros then 'actual usage reserved budgeti asti'
        else 'independent validator terminal outcome' end,
      'sha256:'||encode(public.digest(convert_to(p_attempt_id::text||':'||p_outcome||':'||
        validator_digest||':'||result_digest||':'||coalesce(p_effect_receipt_id::text,''),
        'UTF8'),'sha256'),'hex'),moment); end if;
  return coalesce(term,'active');
end $$;

create function runtime.bind_loop_dispatch(
  p_attempt_id uuid,p_surface text,p_dispatch_id uuid
) returns void language plpgsql security definer set search_path=pg_catalog,runtime,core as $$
declare rid uuid:=core.current_realm_id(); at runtime.loop_attempt%rowtype;
begin
  select * into at from runtime.loop_attempt where realm_id=rid and id=p_attempt_id;
  if not found or exists(select 1 from runtime.loop_attempt_outcome
      where realm_id=rid and attempt_id=p_attempt_id)
     or exists(select 1 from runtime.loop_terminal where realm_id=rid and loop_id=at.loop_id) then
    raise exception 'loop dispatch yalniz acik admitted attempt ister' using errcode='42501';
  end if;
  insert into runtime.loop_dispatch_binding(realm_id,loop_id,attempt_id,surface,dispatch_id,
    semantic_request_digest,action_digest,bound_at)
  values(rid,at.loop_id,at.id,p_surface,p_dispatch_id,at.semantic_request_digest,
    at.action_digest,clock_timestamp());
end $$;

create function runtime.enforce_agent_loop_dispatch() returns trigger
language plpgsql security invoker set search_path=pg_catalog,runtime,agents as $$
declare current_assignment agents.assignment%rowtype; binding runtime.loop_dispatch_binding%rowtype;
begin
  select * into current_assignment from agents.assignment
    where realm_id=new.realm_id and id=new.assignment_id;
  select * into binding from runtime.loop_dispatch_binding
    where realm_id=new.realm_id and surface='agent' and dispatch_id=new.id;
  if found then
    if not exists(select 1 from runtime.loop_attempt at
      join runtime.loop_policy lp on lp.realm_id=at.realm_id and lp.id=at.loop_id
      where at.realm_id=new.realm_id and at.id=binding.attempt_id
        and new.assignment_id in (lp.assignment_id,lp.validator_assignment_id)
        and ((new.assignment_id=lp.assignment_id
              and at.prompt_digest=current_assignment.instruction_digest)
          or (new.assignment_id=lp.validator_assignment_id
              and lp.validator_spec_digest=current_assignment.instruction_digest))
        and at.context_digest=current_assignment.context_manifest_digest
        and not exists(select 1 from runtime.loop_attempt_outcome o
          where o.realm_id=at.realm_id and o.attempt_id=at.id)) then
      raise exception 'agent invocation loop binding mismatch' using errcode='42501';
    end if;
  elsif exists(select 1 from agents.invocation prior
    where prior.realm_id=new.realm_id
      and prior.assignment_id=new.assignment_id
      and prior.id<>new.id) then
    raise exception 'repeated semantic agent dispatch canonical loop admission ister'
      using errcode='42501';
  end if;
  return new;
end $$;
create trigger agent_invocation_loop_gate before insert on agents.invocation
  for each row execute function runtime.enforce_agent_loop_dispatch();

create function runtime.enforce_model_loop_dispatch() returns trigger
language plpgsql security invoker set search_path=pg_catalog,runtime,models as $$
declare manifest models.request_manifest%rowtype; binding runtime.loop_dispatch_binding%rowtype;
begin
  select * into manifest from models.request_manifest
    where realm_id=new.realm_id and id=new.manifest_id;
  select * into binding from runtime.loop_dispatch_binding
    where realm_id=new.realm_id and surface='model' and dispatch_id=new.id;
  if found then
    if not exists(select 1 from runtime.loop_attempt at
      join runtime.loop_policy lp on lp.realm_id=at.realm_id and lp.id=at.loop_id
      where at.realm_id=new.realm_id and at.id=binding.attempt_id
        and row(lp.project_id,lp.work_item_id,lp.plan_id,lp.step_id)
          is not distinct from row(manifest.project_id,manifest.work_item_id,
            manifest.plan_id,manifest.step_id)
        and at.action_digest=manifest.payload_digest
        and not exists(select 1 from runtime.loop_attempt_outcome o
          where o.realm_id=at.realm_id and o.attempt_id=at.id)) then
      raise exception 'model invocation loop binding mismatch' using errcode='42501';
    end if;
  elsif manifest.source_label not in ('model-benchmark','model-campaign') and exists(
    select 1 from models.invocation_attempt prior
    join models.request_manifest pm on pm.realm_id=prior.realm_id and pm.id=prior.manifest_id
    where prior.realm_id=new.realm_id
      and row(pm.project_id,pm.work_item_id,pm.plan_id,pm.step_id,pm.model_id,
              pm.provider_ref,pm.payload_digest,pm.output_schema_digest,
              pm.authorization_scope_digest,pm.context_manifest_digest,pm.source_revision)
        is not distinct from row(manifest.project_id,manifest.work_item_id,manifest.plan_id,
          manifest.step_id,manifest.model_id,manifest.provider_ref,manifest.payload_digest,
          manifest.output_schema_digest,manifest.authorization_scope_digest,
          manifest.context_manifest_digest,manifest.source_revision)) then
    raise exception 'repeated semantic model dispatch canonical loop admission ister'
      using errcode='42501';
  end if;
  return new;
end $$;
create trigger model_invocation_loop_gate before insert on models.invocation_attempt
  for each row execute function runtime.enforce_model_loop_dispatch();

create function runtime.enforce_tool_loop_dispatch() returns trigger
language plpgsql security invoker set search_path=pg_catalog,runtime,tools as $$
declare binding runtime.loop_dispatch_binding%rowtype; current_job record;
begin
  select j.* into current_job from runtime.effect_claim c
    join runtime.job j on j.realm_id=c.realm_id and j.id=c.job_id
    where c.realm_id=new.realm_id and c.id=new.effect_claim_id;
  select * into binding from runtime.loop_dispatch_binding
    where realm_id=new.realm_id and surface='tool' and dispatch_id=new.effect_claim_id;
  if found then
    if not exists(select 1 from runtime.loop_attempt at
      join runtime.loop_policy lp on lp.realm_id=at.realm_id and lp.id=at.loop_id
      where at.realm_id=new.realm_id and at.id=binding.attempt_id
        and row(lp.project_id,lp.work_item_id,lp.plan_id,lp.step_id,lp.assignment_id)
          is not distinct from row(current_job.project_id,current_job.work_item_id,
            current_job.plan_id,current_job.step_id,current_job.assignment_id)
        and at.action_digest=new.input_digest
        and not exists(select 1 from runtime.loop_attempt_outcome o
          where o.realm_id=at.realm_id and o.attempt_id=at.id)) then
      raise exception 'tool dispatch loop binding mismatch' using errcode='42501';
    end if;
  elsif exists(select 1 from tools.dispatch_gate_evidence prior
    join runtime.effect_claim pc on pc.realm_id=prior.realm_id and pc.id=prior.effect_claim_id
    join runtime.job pj on pj.realm_id=pc.realm_id and pj.id=pc.job_id
    where prior.realm_id=new.realm_id and prior.tool_id=new.tool_id
      and prior.input_digest=new.input_digest
      and row(pj.project_id,pj.work_item_id,pj.plan_id,pj.step_id,pj.assignment_id)
        is not distinct from row(current_job.project_id,current_job.work_item_id,
          current_job.plan_id,current_job.step_id,current_job.assignment_id)) then
    raise exception 'repeated semantic tool dispatch canonical loop admission ister'
      using errcode='42501';
  end if;
  return new;
end $$;
create trigger tool_dispatch_loop_gate before insert on tools.dispatch_gate_evidence
  for each row execute function runtime.enforce_tool_loop_dispatch();

create function runtime.interrupt_loop_attempt(p_attempt_id uuid,p_failure_digest text)
returns text language plpgsql security definer set search_path=pg_catalog,runtime,core as $$
declare rid uuid:=core.current_realm_id(); at runtime.loop_attempt%rowtype; moment timestamptz:=clock_timestamp();
begin
  select * into at from runtime.loop_attempt where realm_id=rid and id=p_attempt_id;
  if not found then raise exception 'loop attempt bulunamadi' using errcode='42501'; end if;
  perform pg_advisory_xact_lock(hashtextextended(rid::text||':'||at.loop_id::text,0));
  if exists(select 1 from runtime.loop_attempt_outcome where realm_id=rid and attempt_id=p_attempt_id)
    or exists(select 1 from runtime.loop_terminal where realm_id=rid and loop_id=at.loop_id) then
    raise exception 'loop attempt interruption replay reddi' using errcode='23505'; end if;
  insert into runtime.loop_terminal(id,realm_id,loop_id,attempt_id,state,reason,evidence_digest,terminal_at)
  values(gen_random_uuid(),rid,at.loop_id,p_attempt_id,'manual-review',
    'effect basladi fakat canonical terminal receipt yok',p_failure_digest,moment);
  return 'manual-review';
end $$;

do $$ declare target text; begin foreach target in array array[
  'runtime.loop_policy','runtime.loop_delta_evidence','runtime.loop_attempt',
  'runtime.loop_attempt_delta','runtime.loop_dispatch_binding',
  'runtime.loop_attempt_outcome','runtime.loop_checkpoint','runtime.loop_terminal'
] loop
  execute format('alter table %s enable row level security',target);
  execute format('alter table %s force row level security',target);
  execute format('create policy scope_select on %s for select using (realm_id=core.current_realm_id())',target);
end loop; end $$;

create trigger loop_policy_no_mutation before update or delete on runtime.loop_policy
  for each statement execute function core.deny_mutation();
create trigger loop_delta_evidence_no_mutation before update or delete on runtime.loop_delta_evidence
  for each statement execute function core.deny_mutation();
create trigger loop_attempt_no_mutation before update or delete on runtime.loop_attempt
  for each statement execute function core.deny_mutation();
create trigger loop_attempt_delta_no_mutation before update or delete on runtime.loop_attempt_delta
  for each statement execute function core.deny_mutation();
create trigger loop_dispatch_binding_no_mutation before update or delete on runtime.loop_dispatch_binding
  for each statement execute function core.deny_mutation();
create trigger loop_attempt_outcome_no_mutation before update or delete on runtime.loop_attempt_outcome
  for each statement execute function core.deny_mutation();
create trigger loop_checkpoint_no_mutation before update or delete on runtime.loop_checkpoint
  for each statement execute function core.deny_mutation();
create trigger loop_terminal_no_mutation before update or delete on runtime.loop_terminal
  for each statement execute function core.deny_mutation();

revoke all on runtime.loop_policy,runtime.loop_delta_evidence,runtime.loop_attempt,
  runtime.loop_attempt_delta,runtime.loop_dispatch_binding,runtime.loop_attempt_outcome,
  runtime.loop_checkpoint,runtime.loop_terminal from public;
grant select on runtime.loop_policy,runtime.loop_delta_evidence,runtime.loop_attempt,
  runtime.loop_attempt_delta,runtime.loop_dispatch_binding,runtime.loop_attempt_outcome,
  runtime.loop_checkpoint,runtime.loop_terminal to zekam_app;
revoke all on function runtime.loop_effect_class(text),
  runtime.create_loop_policy(uuid,uuid,uuid,uuid,integer,bigint,bigint,timestamptz,text,text[],text[],text),
  runtime.register_loop_delta_evidence(uuid,uuid,text,uuid),
  runtime.admit_loop_attempt(uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,bigint,bigint,bigint,uuid[],text),
  runtime.complete_loop_attempt(uuid,uuid,text,text,uuid,uuid,uuid,bigint,bigint,bigint),
  runtime.bind_loop_dispatch(uuid,text,uuid),
  runtime.interrupt_loop_attempt(uuid,text) from public;
grant execute on function runtime.loop_effect_class(text),
  runtime.create_loop_policy(uuid,uuid,uuid,uuid,integer,bigint,bigint,timestamptz,text,text[],text[],text),
  runtime.register_loop_delta_evidence(uuid,uuid,text,uuid),
  runtime.admit_loop_attempt(uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,bigint,bigint,bigint,uuid[],text),
  runtime.complete_loop_attempt(uuid,uuid,text,text,uuid,uuid,uuid,bigint,bigint,bigint),
  runtime.bind_loop_dispatch(uuid,text,uuid),
  runtime.interrupt_loop_attempt(uuid,text) to zekam_app;
