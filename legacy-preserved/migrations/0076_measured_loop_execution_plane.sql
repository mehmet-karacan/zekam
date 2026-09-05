-- Additive measured loop, topology and reversible execution authority records.

create table runtime.optimization_objective (
  id uuid not null,
  realm_id uuid not null,
  project_id uuid not null,
  work_item_id uuid not null,
  plan_id uuid not null,
  source_revision text not null,
  objective_body jsonb not null check (jsonb_typeof(objective_body)='object'),
  objective_digest text not null check (objective_digest ~ '^sha256:[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,id),
  unique (realm_id,objective_digest),
  foreign key (realm_id,project_id) references projects.project(realm_id,id),
  foreign key (realm_id,work_item_id) references work.work_item(realm_id,id),
  foreign key (realm_id,plan_id) references work.task_plan(realm_id,id)
);

create table runtime.validator_asset_manifest (
  id uuid not null,
  realm_id uuid not null,
  objective_id uuid not null,
  builder_assignment_id uuid not null,
  verifier_assignment_id uuid not null,
  manifest_body jsonb not null check (jsonb_typeof(manifest_body)='object'),
  manifest_digest text not null check (manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,id),
  unique (realm_id,manifest_digest),
  check (builder_assignment_id<>verifier_assignment_id),
  foreign key (realm_id,objective_id) references runtime.optimization_objective(realm_id,id),
  foreign key (realm_id,builder_assignment_id) references agents.assignment(realm_id,id),
  foreign key (realm_id,verifier_assignment_id) references agents.assignment(realm_id,id)
);

create table runtime.loop_policy_v2 (
  realm_id uuid not null,
  loop_id uuid not null,
  objective_id uuid not null,
  validator_manifest_id uuid not null,
  stable_objective_digest text not null check (stable_objective_digest ~ '^sha256:[0-9a-f]{64}$'),
  stall_limit integer not null check (stall_limit between 1 and 100),
  diagnostic_patience integer not null check (diagnostic_patience between 0 and 100),
  progress_token_budget integer not null check (progress_token_budget between 64 and 32768),
  minimum_value_per_cost double precision not null check (
    minimum_value_per_cost<>'NaN'::double precision
    and minimum_value_per_cost<'Infinity'::double precision
    and minimum_value_per_cost>=0
  ),
  policy_body jsonb not null check (jsonb_typeof(policy_body)='object'),
  policy_digest text not null check (policy_digest ~ '^sha256:[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,loop_id),
  unique (realm_id,policy_digest),
  foreign key (realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key (realm_id,objective_id) references runtime.optimization_objective(realm_id,id),
  foreign key (realm_id,validator_manifest_id)
    references runtime.validator_asset_manifest(realm_id,id)
);

create table runtime.measurement_evidence (
  id uuid not null,
  realm_id uuid not null,
  loop_id uuid not null,
  attempt_id uuid not null,
  producer_assignment_id uuid not null,
  verifier_assignment_id uuid not null,
  evidence_body jsonb not null check (jsonb_typeof(evidence_body)='object'),
  evidence_digest text not null check (evidence_digest ~ '^sha256:[0-9a-f]{64}$'),
  observed_at timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,id),
  unique (realm_id,evidence_digest),
  check (producer_assignment_id<>verifier_assignment_id),
  foreign key (realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key (realm_id,attempt_id) references runtime.loop_attempt(realm_id,id),
  foreign key (realm_id,producer_assignment_id) references agents.assignment(realm_id,id),
  foreign key (realm_id,verifier_assignment_id) references agents.assignment(realm_id,id)
);

create table runtime.loop_progress_packet (
  id uuid not null,
  realm_id uuid not null,
  loop_id uuid not null,
  attempt_id uuid not null,
  ordinal integer not null check (ordinal>0),
  measurement_evidence_id uuid not null,
  packet_body jsonb not null check (jsonb_typeof(packet_body)='object'),
  packet_digest text not null check (packet_digest ~ '^sha256:[0-9a-f]{64}$'),
  progress_state text not null check (progress_state in (
    'improved','target-reached','plateau','regressed','invalid'
  )),
  metric_vector_digest text not null check (metric_vector_digest ~ '^sha256:[0-9a-f]{64}$'),
  progress_decision_body jsonb not null check (jsonb_typeof(progress_decision_body)='object'),
  progress_decision_digest text not null
    check (progress_decision_digest ~ '^sha256:[0-9a-f]{64}$'),
  improved boolean not null,
  stop_reason text,
  omission_count integer not null default 0 check (omission_count>=0),
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,id),
  unique (realm_id,loop_id,attempt_id),
  unique (realm_id,packet_digest),
  foreign key (realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key (realm_id,attempt_id) references runtime.loop_attempt(realm_id,id),
  foreign key (realm_id,measurement_evidence_id)
    references runtime.measurement_evidence(realm_id,id)
);

create table runtime.loop_attempt_job (
  realm_id uuid not null,
  loop_id uuid not null,
  ordinal integer not null check (ordinal>0),
  predecessor_attempt_id uuid,
  progress_packet_digest text,
  job_id uuid not null,
  idempotency_digest text not null check (idempotency_digest ~ '^sha256:[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,loop_id,ordinal),
  unique (realm_id,job_id),
  unique (realm_id,idempotency_digest),
  check ((ordinal=1 and predecessor_attempt_id is null and progress_packet_digest is null)
    or (ordinal>1 and predecessor_attempt_id is not null
      and progress_packet_digest ~ '^sha256:[0-9a-f]{64}$')),
  foreign key (realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key (realm_id,predecessor_attempt_id) references runtime.loop_attempt(realm_id,id),
  foreign key (realm_id,job_id) references runtime.job(realm_id,id)
);

create table runtime.loop_attempt_novelty (
  realm_id uuid not null,
  loop_id uuid not null,
  attempt_id uuid not null,
  ordinal integer not null check (ordinal>0),
  objective_digest text not null check (objective_digest ~ '^sha256:[0-9a-f]{64}$'),
  validator_asset_manifest_digest text not null
    check (validator_asset_manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
  novelty_digest text not null check (novelty_digest ~ '^sha256:[0-9a-f]{64}$'),
  progress_packet_digest text,
  metric_vector_digest text,
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,attempt_id),
  unique (realm_id,loop_id,ordinal),
  unique (realm_id,loop_id,novelty_digest),
  check ((ordinal=1 and progress_packet_digest is null and metric_vector_digest is null)
    or (ordinal>1 and progress_packet_digest ~ '^sha256:[0-9a-f]{64}$'
      and metric_vector_digest ~ '^sha256:[0-9a-f]{64}$')),
  foreign key (realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key (realm_id,attempt_id) references runtime.loop_attempt(realm_id,id)
);

create table runtime.loop_control_event (
  id uuid not null,
  realm_id uuid not null,
  loop_id uuid not null,
  state text not null check (state in ('active','paused','draining','cancelled')),
  plan_digest text not null check (plan_digest ~ '^sha256:[0-9a-f]{64}$'),
  authorization_id uuid not null,
  authorization_digest text not null check (authorization_digest ~ '^sha256:[0-9a-f]{64}$'),
  reason_digest text not null check (reason_digest ~ '^sha256:[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,id),
  foreign key (realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key (authorization_id) references security.authorization(id)
);

create table runtime.execution_topology_decision (
  id uuid not null,
  realm_id uuid not null,
  project_id uuid not null,
  work_item_id uuid not null,
  plan_id uuid not null,
  assessment_body jsonb not null check (jsonb_typeof(assessment_body)='object'),
  assessment_digest text not null check (assessment_digest ~ '^sha256:[0-9a-f]{64}$'),
  selected_pattern text not null check (selected_pattern in (
    'direct','single-pass','bounded-loop','tournament','graph','queue-human-review','blocked'
  )),
  decision_body jsonb not null check (jsonb_typeof(decision_body)='object'),
  decision_digest text not null check (decision_digest ~ '^sha256:[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,id),
  unique (realm_id,decision_digest),
  foreign key (realm_id,project_id) references projects.project(realm_id,id),
  foreign key (realm_id,work_item_id) references work.work_item(realm_id,id),
  foreign key (realm_id,plan_id) references work.task_plan(realm_id,id)
);

create table runtime.graph_execution_receipt (
  id uuid not null,
  realm_id uuid not null,
  topology_decision_id uuid not null,
  graph_root_id uuid not null,
  receipt_body jsonb not null check (jsonb_typeof(receipt_body)='object'),
  receipt_digest text not null check (receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
  fake_parallelism boolean not null,
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,id),
  unique (realm_id,receipt_digest),
  foreign key (realm_id,topology_decision_id)
    references runtime.execution_topology_decision(realm_id,id),
  foreign key (realm_id,graph_root_id) references agents.graph_root(realm_id,id)
);

create table runtime.tournament_plan (
  id uuid not null,
  realm_id uuid not null,
  topology_decision_id uuid not null,
  selector_assignment_id uuid not null,
  selector_model_id text not null,
  selector_execution_identity text not null,
  plan_body jsonb not null check (jsonb_typeof(plan_body)='object'),
  plan_digest text not null check (plan_digest ~ '^sha256:[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,id),
  unique (realm_id,plan_digest),
  foreign key (realm_id,topology_decision_id)
    references runtime.execution_topology_decision(realm_id,id),
  foreign key (realm_id,selector_assignment_id) references agents.assignment(realm_id,id)
);

create table runtime.loop_change_set (
  id uuid not null,
  realm_id uuid not null,
  loop_id uuid not null,
  attempt_id uuid not null,
  change_body jsonb not null check (jsonb_typeof(change_body)='object'),
  change_digest text not null check (change_digest ~ '^sha256:[0-9a-f]{64}$'),
  inverse_patch_digest text not null check (inverse_patch_digest ~ '^sha256:[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,id),
  unique (realm_id,loop_id,attempt_id),
  unique (realm_id,change_digest),
  foreign key (realm_id,loop_id) references runtime.loop_policy(realm_id,id),
  foreign key (realm_id,attempt_id) references runtime.loop_attempt(realm_id,id)
);

create table runtime.loop_rollback_receipt (
  id uuid not null,
  realm_id uuid not null,
  change_set_id uuid not null,
  receipt_body jsonb not null check (jsonb_typeof(receipt_body)='object'),
  receipt_digest text not null check (receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,id),
  unique (realm_id,change_set_id),
  unique (realm_id,receipt_digest),
  foreign key (realm_id,change_set_id) references runtime.loop_change_set(realm_id,id)
);

create table runtime.scaffolding_ablation (
  id uuid not null,
  realm_id uuid not null,
  project_id uuid not null,
  work_item_id uuid not null,
  plan_id uuid not null,
  ablation_body jsonb not null check (jsonb_typeof(ablation_body)='object'),
  ablation_digest text not null check (ablation_digest ~ '^sha256:[0-9a-f]{64}$'),
  decision text not null check (decision in ('keep-baseline','deprecation-candidate')),
  created_at timestamptz not null default clock_timestamp(),
  primary key (realm_id,id),
  unique (realm_id,ablation_digest),
  foreign key (realm_id,project_id) references projects.project(realm_id,id),
  foreign key (realm_id,work_item_id) references work.work_item(realm_id,id),
  foreign key (realm_id,plan_id) references work.task_plan(realm_id,id)
);

create function runtime.assert_measured_payload_safe(p_value jsonb) returns void
language plpgsql immutable set search_path=pg_catalog as $$
declare item record; forbidden constant text:=
  '^(raw[-_])?(prompt|response|transcript|secret|pii|credential|private[-_]reasoning|patch[-_]body|test[-_]log)s?$';
begin
  if p_value is null or octet_length(p_value::text)>262144 then
    raise exception 'measured payload bounded JSON ister' using errcode='22023';
  end if;
  if p_value::text ~ '-----BEGIN ([A-Z ]+ )?PRIVATE KEY-----'
    or p_value::text ~ '\m(AKIA|ASIA)[0-9A-Z]{16}\M'
    or p_value::text ~ '\mgh[pousr]_[A-Za-z0-9]{30,}\M'
    or p_value::text ~* '\mauthorization\M[[:space:]]*[:=][[:space:]]*["'']?(bearer|basic)[[:space:]]+[^[:space:]]{8,}'
    or p_value::text ~* '\m(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd|credential)\M[[:space:]]*[:=][[:space:]]*["''][^"''[:space:]]{8,}["'']'
    or p_value::text ~* '\m[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\M' then
    raise exception 'measured payload secret veya PII degeri tasiyamaz' using errcode='42501';
  end if;
  if jsonb_typeof(p_value)='object' then
    for item in select key,value from jsonb_each(p_value) loop
      if lower(item.key) ~ forbidden then
        raise exception 'measured payload private/raw key tasiyamaz' using errcode='42501';
      end if;
      perform runtime.assert_measured_payload_safe(item.value);
    end loop;
  elsif jsonb_typeof(p_value)='array' then
    for item in select value from jsonb_array_elements(p_value) loop
      perform runtime.assert_measured_payload_safe(item.value);
    end loop;
  end if;
end $$;

create function runtime.store_measured_loop_contract(
  p_objective_id uuid,p_loop_id uuid,p_validator_manifest_id uuid,
  p_objective_body jsonb,p_objective_digest text,p_source_revision text,
  p_builder_assignment_id uuid,p_verifier_assignment_id uuid,
  p_manifest_body jsonb,p_manifest_digest text,p_policy_body jsonb,p_policy_digest text,
  p_stall_limit integer,p_diagnostic_patience integer,p_progress_token_budget integer,
  p_minimum_value_per_cost double precision
) returns boolean language plpgsql security definer
set search_path=pg_catalog,runtime,work,agents,core as $$
declare rid uuid:=core.current_realm_id(); lp runtime.loop_policy%rowtype; inserted integer;
begin
  perform runtime.assert_measured_payload_safe(p_objective_body);
  perform runtime.assert_measured_payload_safe(p_manifest_body);
  perform runtime.assert_measured_payload_safe(p_policy_body);
  select * into lp from runtime.loop_policy where realm_id=rid and id=p_loop_id;
  if not found or lp.assignment_id<>p_builder_assignment_id
    or lp.validator_assignment_id<>p_verifier_assignment_id
    or p_builder_assignment_id=p_verifier_assignment_id then
    raise exception 'measured loop contract assignment binding drift' using errcode='42501';
  end if;
  if p_objective_body->>'objective_id'<>p_objective_id::text
    or p_objective_body->>'validator_asset_manifest_digest'<>p_manifest_digest
    or p_objective_body->>'measurement_plan_digest'<>p_policy_body#>>'{measured_v2,measurement_plan_digest}'
    or p_manifest_body->>'manifest_id'<>p_validator_manifest_id::text
    or p_manifest_body->>'objective_id'<>p_objective_id::text
    or p_manifest_body->>'source_revision'<>p_source_revision
    or p_manifest_body->>'builder_assignment_id'<>p_builder_assignment_id::text
    or p_manifest_body->>'verifier_assignment_id'<>p_verifier_assignment_id::text
    or p_policy_body#>>'{measured_v2,objective_id}'<>p_objective_id::text
    or p_policy_body#>>'{measured_v2,stable_objective_digest}'<>p_objective_digest
    or p_policy_body#>>'{measured_v2,validator_manifest_id}'<>p_validator_manifest_id::text
    or p_policy_body#>>'{measured_v2,validator_asset_manifest_digest}'<>p_manifest_digest then
    raise exception 'measured loop objective manifest policy binding drift' using errcode='42501';
  end if;
  if exists(
    select 1 from agents.assignment_resource ar
    join jsonb_array_elements(coalesce(p_manifest_body->'assets','[]'::jsonb)) asset
      on ar.resource=asset->>'logical_ref'
    where ar.realm_id=rid and ar.assignment_id=p_builder_assignment_id and ar.mode='write'
  ) then
    raise exception 'builder validator asset write scope disinda olmali' using errcode='42501';
  end if;
  insert into runtime.optimization_objective(id,realm_id,project_id,work_item_id,plan_id,
    source_revision,objective_body,objective_digest)
  values(p_objective_id,rid,lp.project_id,lp.work_item_id,lp.plan_id,p_source_revision,
    p_objective_body,p_objective_digest) on conflict(realm_id,objective_digest) do nothing;
  get diagnostics inserted=row_count;
  if inserted=0 and not exists(select 1 from runtime.optimization_objective
      where realm_id=rid and id=p_objective_id and objective_digest=p_objective_digest) then
    raise exception 'optimization objective idempotency drift' using errcode='23505'; end if;
  insert into runtime.validator_asset_manifest(id,realm_id,objective_id,builder_assignment_id,
    verifier_assignment_id,manifest_body,manifest_digest)
  values(p_validator_manifest_id,rid,p_objective_id,p_builder_assignment_id,
    p_verifier_assignment_id,p_manifest_body,p_manifest_digest)
  on conflict(realm_id,manifest_digest) do nothing;
  insert into runtime.loop_policy_v2(realm_id,loop_id,objective_id,validator_manifest_id,
    stable_objective_digest,stall_limit,diagnostic_patience,progress_token_budget,
    minimum_value_per_cost,policy_body,policy_digest)
  values(rid,p_loop_id,p_objective_id,p_validator_manifest_id,p_objective_digest,p_stall_limit,
    p_diagnostic_patience,p_progress_token_budget,p_minimum_value_per_cost,
    p_policy_body,p_policy_digest) on conflict(realm_id,loop_id) do nothing;
  if not exists(select 1 from runtime.loop_policy_v2 where realm_id=rid and loop_id=p_loop_id
      and policy_digest=p_policy_digest and stable_objective_digest=p_objective_digest) then
    raise exception 'measured loop policy v2 idempotency drift' using errcode='23505'; end if;
  return inserted=1;
end $$;

create function runtime.store_loop_progress(
  p_evidence_id uuid,p_packet_id uuid,p_loop_id uuid,p_attempt_id uuid,
  p_producer_assignment_id uuid,p_verifier_assignment_id uuid,
  p_evidence_body jsonb,p_evidence_digest text,p_observed_at timestamptz,
  p_ordinal integer,p_packet_body jsonb,p_packet_digest text,p_improved boolean,
  p_stop_reason text,p_omission_count integer,p_progress_state text,
  p_progress_decision_body jsonb,p_progress_decision_digest text,p_metric_vector_digest text
) returns boolean language plpgsql security definer
set search_path=pg_catalog,runtime,agents,core as $$
declare rid uuid:=core.current_realm_id(); at runtime.loop_attempt%rowtype;
  lp runtime.loop_policy%rowtype; inserted integer;
begin
  perform runtime.assert_measured_payload_safe(p_evidence_body);
  perform runtime.assert_measured_payload_safe(p_packet_body);
  perform runtime.assert_measured_payload_safe(p_progress_decision_body);
  select * into at from runtime.loop_attempt where realm_id=rid and id=p_attempt_id;
  select * into lp from runtime.loop_policy where realm_id=rid and id=p_loop_id;
  if at.loop_id is distinct from p_loop_id or at.ordinal<>p_ordinal
    or lp.assignment_id<>p_producer_assignment_id
    or lp.validator_assignment_id<>p_verifier_assignment_id
    or p_producer_assignment_id=p_verifier_assignment_id then
    raise exception 'loop progress immutable attempt/verifier binding drift' using errcode='42501';
  end if;
  if coalesce((p_evidence_body->>'producer_self_report')::boolean,true)
    or p_evidence_body->>'producer_assignment_id'<>p_producer_assignment_id::text
    or p_evidence_body->>'verifier_assignment_id'<>p_verifier_assignment_id::text
    or exists(select 1 from jsonb_array_elements(coalesce(
      p_evidence_body->'measurements','[]'::jsonb)) measurement
      where coalesce((measurement->>'producer_self_report')::boolean,true)
        or measurement->>'measurement_identity'=measurement->>'verifier_identity') then
    raise exception 'loop progress external producer ve verifier evidence ister' using errcode='42501';
  end if;
  if p_packet_body#>>'{current_metric_vector,progress_state}'<>p_progress_state
    or (p_progress_state in ('improved','target-reached'))<>p_improved
    or p_progress_decision_body->>'packet_digest'<>p_packet_digest
    or p_progress_decision_body->>'progress_state'<>p_progress_state
    or coalesce((p_progress_decision_body->>'progress_counted')::boolean,false)<>p_improved
    or coalesce((p_progress_decision_body->>'allow_next_attempt')::boolean,false)
       <> (p_stop_reason is null) then
    raise exception 'loop progress state/vector binding drift' using errcode='42501';
  end if;
  insert into runtime.measurement_evidence(id,realm_id,loop_id,attempt_id,
    producer_assignment_id,verifier_assignment_id,evidence_body,evidence_digest,observed_at)
  values(p_evidence_id,rid,p_loop_id,p_attempt_id,p_producer_assignment_id,
    p_verifier_assignment_id,p_evidence_body,p_evidence_digest,p_observed_at)
  on conflict(realm_id,evidence_digest) do nothing;
  insert into runtime.loop_progress_packet(id,realm_id,loop_id,attempt_id,ordinal,
    measurement_evidence_id,packet_body,packet_digest,progress_state,metric_vector_digest,
    progress_decision_body,progress_decision_digest,improved,stop_reason,omission_count)
  values(p_packet_id,rid,p_loop_id,p_attempt_id,p_ordinal,p_evidence_id,p_packet_body,
    p_packet_digest,p_progress_state,p_metric_vector_digest,p_progress_decision_body,
    p_progress_decision_digest,p_improved,
    p_stop_reason,p_omission_count)
  on conflict(realm_id,loop_id,attempt_id) do nothing;
  get diagnostics inserted=row_count;
  if not exists(select 1 from runtime.loop_progress_packet where realm_id=rid
      and loop_id=p_loop_id and attempt_id=p_attempt_id and packet_digest=p_packet_digest) then
    raise exception 'loop progress idempotency drift' using errcode='23505'; end if;
  return inserted=1;
end $$;

create function runtime.store_topology_decision(
  p_id uuid,p_project_id uuid,p_work_item_id uuid,p_plan_id uuid,
  p_assessment_body jsonb,p_assessment_digest text,p_selected_pattern text,
  p_decision_body jsonb,p_decision_digest text
) returns boolean language plpgsql security definer
set search_path=pg_catalog,runtime,work,core as $$
declare rid uuid:=core.current_realm_id(); inserted integer;
begin
  perform runtime.assert_measured_payload_safe(p_assessment_body);
  perform runtime.assert_measured_payload_safe(p_decision_body);
  if not exists(select 1 from work.task_plan where realm_id=rid and id=p_plan_id
      and project_id=p_project_id and work_item_id=p_work_item_id) then
    raise exception 'topology decision exact TaskPlan ister' using errcode='42501'; end if;
  insert into runtime.execution_topology_decision(id,realm_id,project_id,work_item_id,
    plan_id,assessment_body,assessment_digest,selected_pattern,decision_body,decision_digest)
  values(p_id,rid,p_project_id,p_work_item_id,p_plan_id,p_assessment_body,p_assessment_digest,
    p_selected_pattern,p_decision_body,p_decision_digest)
  on conflict(realm_id,decision_digest) do nothing;
  get diagnostics inserted=row_count;
  if not exists(select 1 from runtime.execution_topology_decision where realm_id=rid
      and id=p_id and decision_digest=p_decision_digest) then
    raise exception 'topology decision idempotency drift' using errcode='23505'; end if;
  return inserted=1;
end $$;

create function runtime.store_graph_execution_receipt(
  p_id uuid,p_topology_decision_id uuid,p_graph_root_id uuid,
  p_receipt_body jsonb,p_receipt_digest text,p_fake_parallelism boolean
) returns boolean language plpgsql security definer
set search_path=pg_catalog,runtime,agents,work,core as $$
declare rid uuid:=core.current_realm_id(); inserted integer;
begin
  perform runtime.assert_measured_payload_safe(p_receipt_body);
  if p_fake_parallelism or not exists(
    select 1 from runtime.execution_topology_decision d
    join agents.graph_root g on g.realm_id=d.realm_id and g.id=p_graph_root_id
    join runtime.execution_run r on r.realm_id=g.realm_id and r.id=g.run_id
    where d.realm_id=rid and d.id=p_topology_decision_id and d.selected_pattern='graph'
      and r.plan_id=d.plan_id
  ) then raise exception 'graph receipt exact graph topology ve gercek parallel evidence ister'
    using errcode='42501'; end if;
  insert into runtime.graph_execution_receipt(id,realm_id,topology_decision_id,graph_root_id,
    receipt_body,receipt_digest,fake_parallelism)
  values(p_id,rid,p_topology_decision_id,p_graph_root_id,p_receipt_body,p_receipt_digest,false)
  on conflict(realm_id,receipt_digest) do nothing;
  get diagnostics inserted=row_count;
  if not exists(select 1 from runtime.graph_execution_receipt where realm_id=rid
      and id=p_id and receipt_digest=p_receipt_digest) then
    raise exception 'graph receipt idempotency drift' using errcode='23505'; end if;
  return inserted=1;
end $$;

create function runtime.store_tournament_plan(
  p_id uuid,p_topology_decision_id uuid,p_selector_assignment_id uuid,
  p_selector_model_id text,p_selector_execution_identity text,
  p_plan_body jsonb,p_plan_digest text
) returns boolean language plpgsql security definer
set search_path=pg_catalog,runtime,agents,core as $$
declare rid uuid:=core.current_realm_id(); inserted integer;
begin
  perform runtime.assert_measured_payload_safe(p_plan_body);
  if btrim(p_selector_model_id)='' or btrim(p_selector_execution_identity)=''
    or not exists(select 1 from runtime.execution_topology_decision
      where realm_id=rid and id=p_topology_decision_id and selected_pattern='tournament')
    or not exists(select 1 from agents.assignment where realm_id=rid
      and id=p_selector_assignment_id and role in ('verifier','reviewer','critic'))
    or exists(select 1 from jsonb_array_elements(coalesce(
      p_plan_body->'candidate_assignments','[]'::jsonb)) candidate
      where candidate->>'assignment_id'=p_selector_assignment_id::text
        or candidate->>'model_id'=p_selector_model_id
        or candidate->>'execution_identity'=p_selector_execution_identity) then
    raise exception 'tournament selector independent identity ister' using errcode='42501';
  end if;
  insert into runtime.tournament_plan(id,realm_id,topology_decision_id,
    selector_assignment_id,selector_model_id,selector_execution_identity,plan_body,plan_digest)
  values(p_id,rid,p_topology_decision_id,p_selector_assignment_id,p_selector_model_id,
    p_selector_execution_identity,p_plan_body,p_plan_digest)
  on conflict(realm_id,plan_digest) do nothing;
  get diagnostics inserted=row_count;
  if not exists(select 1 from runtime.tournament_plan where realm_id=rid
      and id=p_id and plan_digest=p_plan_digest) then
    raise exception 'tournament plan idempotency drift' using errcode='23505'; end if;
  return inserted=1;
end $$;

create function runtime.store_loop_change_set(
  p_id uuid,p_loop_id uuid,p_attempt_id uuid,p_change_body jsonb,
  p_change_digest text,p_inverse_patch_digest text
) returns boolean language plpgsql security definer
set search_path=pg_catalog,runtime,core as $$
declare rid uuid:=core.current_realm_id(); inserted integer;
begin
  perform runtime.assert_measured_payload_safe(p_change_body);
  if not exists(select 1 from runtime.loop_attempt where realm_id=rid and id=p_attempt_id
      and loop_id=p_loop_id) then
    raise exception 'loop change set exact attempt ister' using errcode='42501'; end if;
  insert into runtime.loop_change_set(id,realm_id,loop_id,attempt_id,change_body,
    change_digest,inverse_patch_digest)
  values(p_id,rid,p_loop_id,p_attempt_id,p_change_body,p_change_digest,p_inverse_patch_digest)
  on conflict(realm_id,loop_id,attempt_id) do nothing;
  get diagnostics inserted=row_count;
  if not exists(select 1 from runtime.loop_change_set where realm_id=rid and id=p_id
      and change_digest=p_change_digest and inverse_patch_digest=p_inverse_patch_digest) then
    raise exception 'loop change set idempotency drift' using errcode='23505'; end if;
  return inserted=1;
end $$;

create function runtime.store_loop_rollback_receipt(
  p_id uuid,p_change_set_id uuid,p_receipt_body jsonb,p_receipt_digest text
) returns boolean language plpgsql security definer
set search_path=pg_catalog,runtime,core as $$
declare rid uuid:=core.current_realm_id(); inserted integer;
begin
  perform runtime.assert_measured_payload_safe(p_receipt_body);
  insert into runtime.loop_rollback_receipt(id,realm_id,change_set_id,receipt_body,receipt_digest)
  values(p_id,rid,p_change_set_id,p_receipt_body,p_receipt_digest)
  on conflict(realm_id,change_set_id) do nothing;
  get diagnostics inserted=row_count;
  if not exists(select 1 from runtime.loop_rollback_receipt where realm_id=rid
      and id=p_id and receipt_digest=p_receipt_digest) then
    raise exception 'loop rollback receipt idempotency drift' using errcode='23505'; end if;
  return inserted=1;
end $$;

create function runtime.store_scaffolding_ablation(
  p_id uuid,p_project_id uuid,p_work_item_id uuid,p_plan_id uuid,
  p_ablation_body jsonb,p_ablation_digest text,p_decision text
) returns boolean language plpgsql security definer
set search_path=pg_catalog,runtime,work,core as $$
declare rid uuid:=core.current_realm_id(); inserted integer;
begin
  perform runtime.assert_measured_payload_safe(p_ablation_body);
  if not exists(select 1 from work.task_plan where realm_id=rid and id=p_plan_id
      and project_id=p_project_id and work_item_id=p_work_item_id)
    or coalesce((p_ablation_body->>'auto_delete')::boolean,true)
    or p_ablation_body->>'status'<>'review-required' then
    raise exception 'scaffolding ablation review-required ve no-auto-delete olmali'
      using errcode='42501'; end if;
  insert into runtime.scaffolding_ablation(id,realm_id,project_id,work_item_id,plan_id,
    ablation_body,ablation_digest,decision)
  values(p_id,rid,p_project_id,p_work_item_id,p_plan_id,p_ablation_body,
    p_ablation_digest,p_decision) on conflict(realm_id,ablation_digest) do nothing;
  get diagnostics inserted=row_count;
  if not exists(select 1 from runtime.scaffolding_ablation where realm_id=rid and id=p_id
      and ablation_digest=p_ablation_digest) then
    raise exception 'scaffolding ablation idempotency drift' using errcode='23505'; end if;
  return inserted=1;
end $$;

create function runtime.bind_loop_attempt_job(
  p_loop_id uuid,p_ordinal integer,p_predecessor_attempt_id uuid,
  p_progress_packet_digest text,p_job_id uuid,p_idempotency_digest text
) returns boolean language plpgsql security definer
set search_path=pg_catalog,runtime,core as $$
declare rid uuid:=core.current_realm_id(); inserted integer; j runtime.job%rowtype;
begin
  select * into j from runtime.job where realm_id=rid and id=p_job_id;
  if not found or j.max_attempts<>1 or j.state<>'ready'
    or j.payload->>'loop_id'<>p_loop_id::text
    or (j.payload->>'ordinal')::integer<>p_ordinal then
    raise exception 'loop attempt job one-job-per-attempt binding drift' using errcode='42501';
  end if;
  if exists(select 1 from runtime.loop_terminal where realm_id=rid and loop_id=p_loop_id) then
    raise exception 'terminal loop yeni attempt job uretemez' using errcode='42501';
  end if;
  if coalesce((select state from runtime.loop_control_event where realm_id=rid
      and loop_id=p_loop_id order by created_at desc,id desc limit 1),'active')<>'active' then
    raise exception 'paused/draining/cancelled loop yeni attempt job uretemez'
      using errcode='42501';
  end if;
  if p_ordinal>1 and not exists(select 1 from runtime.loop_progress_packet
      where realm_id=rid and loop_id=p_loop_id and attempt_id=p_predecessor_attempt_id
        and packet_digest=p_progress_packet_digest) then
    raise exception 'next loop attempt exact progress packet ister' using errcode='42501';
  end if;
  insert into runtime.loop_attempt_job(realm_id,loop_id,ordinal,predecessor_attempt_id,
    progress_packet_digest,job_id,idempotency_digest)
  values(rid,p_loop_id,p_ordinal,p_predecessor_attempt_id,p_progress_packet_digest,
    p_job_id,p_idempotency_digest) on conflict(realm_id,loop_id,ordinal) do nothing;
  get diagnostics inserted=row_count;
  if not exists(select 1 from runtime.loop_attempt_job where realm_id=rid and loop_id=p_loop_id
      and ordinal=p_ordinal and job_id=p_job_id and idempotency_digest=p_idempotency_digest) then
    raise exception 'loop attempt job idempotency drift' using errcode='23505'; end if;
  return inserted=1;
end $$;

create function runtime.record_loop_control_event(
  p_id uuid,p_loop_id uuid,p_state text,p_authorization_id uuid,
  p_authorization_digest text,p_reason_digest text
) returns uuid language plpgsql security definer
set search_path=pg_catalog,runtime,security,core as $$
declare rid uuid:=core.current_realm_id(); lp runtime.loop_policy%rowtype;
  auth security.authorization%rowtype;
begin
  select * into lp from runtime.loop_policy where realm_id=rid and id=p_loop_id;
  select * into auth from security.authorization where realm_id=rid and id=p_authorization_id
    for update;
  if not found or auth.state<>'issued' or auth.expires_at<=clock_timestamp()
    or auth.authorization_digest<>p_authorization_digest
    or auth.work_item_id<>lp.work_item_id or auth.plan_id<>lp.plan_id
    or auth.plan_digest<>lp.plan_digest or not 'database-write'=any(auth.allowed_effects)
    or not ('loop:'||p_loop_id::text)=any(auth.allowed_resources) then
    raise exception 'loop control exact one-shot authorization ister' using errcode='42501';
  end if;
  insert into runtime.loop_control_event(id,realm_id,loop_id,state,plan_digest,
    authorization_id,authorization_digest,reason_digest)
  values(p_id,rid,p_loop_id,p_state,lp.plan_digest,p_authorization_id,
    p_authorization_digest,p_reason_digest);
  update security.authorization set state='consumed',consumed_at=clock_timestamp(),
    consumed_by='runtime.loop-control' where id=p_authorization_id;
  return p_id;
end $$;

create function runtime.admit_loop_attempt_current(
  p_attempt_id uuid,p_loop_id uuid,p_predecessor_attempt_id uuid,
  p_semantic_request_digest text,p_prompt_digest text,p_context_digest text,
  p_action_digest text,p_binding_digest text,p_source_revision text,p_plan_digest text,
  p_policy_revision_digest text,p_validator_spec_digest text,p_reserved_input_tokens bigint,
  p_reserved_output_tokens bigint,p_reserved_cost_micros bigint,p_evidence_ids uuid[],
  p_delta_digest text,p_attempt_ordinal integer,p_objective_digest text,
  p_validator_asset_manifest_digest text,p_progress_packet_digest text,
  p_metric_vector_digest text,p_novelty_digest text
) returns table(admitted boolean,attempt_id uuid,ordinal integer,terminal_state text,reason text)
language plpgsql security definer set search_path=pg_catalog,runtime,core as $$
declare rid uuid:=core.current_realm_id(); v2 runtime.loop_policy_v2%rowtype;
  expected_ordinal integer; response record; existing runtime.loop_attempt%rowtype;
begin
  select * into v2 from runtime.loop_policy_v2 where realm_id=rid and loop_id=p_loop_id;
  if not found then
    return query select * from runtime.admit_loop_attempt(
      p_attempt_id,p_loop_id,p_predecessor_attempt_id,p_semantic_request_digest,
      p_prompt_digest,p_context_digest,p_action_digest,p_binding_digest,p_source_revision,
      p_plan_digest,p_policy_revision_digest,p_validator_spec_digest,p_reserved_input_tokens,
      p_reserved_output_tokens,p_reserved_cost_micros,p_evidence_ids,p_delta_digest);
    return;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(rid::text||':'||p_loop_id::text,0));
  select count(*)+1 into expected_ordinal from runtime.loop_attempt
    where realm_id=rid and loop_id=p_loop_id;
  select attempt.* into existing from runtime.loop_attempt_novelty novelty
    join runtime.loop_attempt attempt on attempt.realm_id=novelty.realm_id
      and attempt.id=novelty.attempt_id
    where novelty.realm_id=rid and novelty.loop_id=p_loop_id
      and novelty.novelty_digest=p_novelty_digest;
  if found then
    if existing.id<>p_attempt_id or existing.ordinal<>p_attempt_ordinal
      or existing.predecessor_attempt_id is distinct from p_predecessor_attempt_id
      or existing.source_revision<>p_source_revision or existing.plan_digest<>p_plan_digest
      or existing.policy_revision_digest<>p_policy_revision_digest then
      raise exception 'measured loop novelty duplicate baska attempt ile tekrarlandi'
        using errcode='23505';
    end if;
    return query select true,existing.id,existing.ordinal,null::text,'idempotent replay'::text;
    return;
  end if;
  if p_attempt_ordinal<>expected_ordinal or p_objective_digest<>v2.stable_objective_digest
    or p_validator_asset_manifest_digest<>(v2.policy_body#>>'{measured_v2,validator_asset_manifest_digest}')
    or p_novelty_digest is null or p_novelty_digest!~'^sha256:[0-9a-f]{64}$'
    or not exists(select 1 from runtime.loop_attempt_job j
      join runtime.job job on job.realm_id=j.realm_id and job.id=j.job_id
      where j.realm_id=rid and j.loop_id=p_loop_id and j.ordinal=expected_ordinal
        and j.predecessor_attempt_id is not distinct from p_predecessor_attempt_id
        and j.progress_packet_digest is not distinct from p_progress_packet_digest
        and job.max_attempts=1 and job.state in ('ready','running')) then
    raise exception 'measured loop admission exact objective/job/ordinal binding ister'
      using errcode='42501';
  end if;
  if expected_ordinal=1 then
    if p_predecessor_attempt_id is not null or p_progress_packet_digest is not null
      or p_metric_vector_digest is not null then
      raise exception 'ilk measured loop attempt progress packet tasiyamaz' using errcode='42501';
    end if;
  elsif not exists(select 1 from runtime.loop_progress_packet packet
      where packet.realm_id=rid and packet.loop_id=p_loop_id
        and packet.attempt_id=p_predecessor_attempt_id
        and packet.packet_digest=p_progress_packet_digest
        and packet.metric_vector_digest=p_metric_vector_digest
        and coalesce((packet.progress_decision_body->>'allow_next_attempt')::boolean,false)) then
    raise exception 'measured loop attempt 2+ exact fresh progress packet ister'
      using errcode='42501';
  end if;
  select * into response from runtime.admit_loop_attempt(
    p_attempt_id,p_loop_id,p_predecessor_attempt_id,p_semantic_request_digest,
    p_prompt_digest,p_context_digest,p_action_digest,p_binding_digest,p_source_revision,
    p_plan_digest,p_policy_revision_digest,p_validator_spec_digest,p_reserved_input_tokens,
    p_reserved_output_tokens,p_reserved_cost_micros,p_evidence_ids,p_delta_digest);
  if response.admitted then
    insert into runtime.loop_attempt_novelty(realm_id,loop_id,attempt_id,ordinal,
      objective_digest,validator_asset_manifest_digest,novelty_digest,
      progress_packet_digest,metric_vector_digest)
    values(rid,p_loop_id,response.attempt_id,response.ordinal,p_objective_digest,
      p_validator_asset_manifest_digest,p_novelty_digest,p_progress_packet_digest,
      p_metric_vector_digest);
  end if;
  return query select response.admitted,response.attempt_id,response.ordinal,
    response.terminal_state,response.reason;
end $$;

create function runtime.complete_loop_attempt_current(
  p_outcome_id uuid,p_attempt_id uuid,p_outcome text,p_validator_spec_digest text,
  p_result_invocation_id uuid,p_verifier_invocation_id uuid,p_effect_receipt_id uuid,
  p_actual_input_tokens bigint,p_actual_output_tokens bigint,p_actual_cost_micros bigint,
  p_progress_packet_digest text,p_metric_vector_digest text,
  p_progress_decision_digest text,p_metric_evidence_refs text[],p_progress_state text
) returns text language plpgsql security definer
set search_path=pg_catalog,runtime,core as $$
declare rid uuid:=core.current_realm_id(); at runtime.loop_attempt%rowtype;
  v2 runtime.loop_policy_v2%rowtype; packet runtime.loop_progress_packet%rowtype;
  evidence runtime.measurement_evidence%rowtype; observed_refs text[]; allow_next boolean;
  prior runtime.loop_attempt_outcome%rowtype; prior_state text;
begin
  select * into at from runtime.loop_attempt where realm_id=rid and id=p_attempt_id;
  if not found then raise exception 'loop attempt bulunamadi' using errcode='42501'; end if;
  select * into v2 from runtime.loop_policy_v2 where realm_id=rid and loop_id=at.loop_id;
  if not found then
    return runtime.complete_loop_attempt(p_outcome_id,p_attempt_id,p_outcome,
      p_validator_spec_digest,p_result_invocation_id,p_verifier_invocation_id,
      p_effect_receipt_id,p_actual_input_tokens,p_actual_output_tokens,p_actual_cost_micros);
  end if;
  select * into packet from runtime.loop_progress_packet where realm_id=rid
    and loop_id=at.loop_id and attempt_id=at.id;
  if not found then raise exception 'measured loop completion progress packet ister'
    using errcode='42501'; end if;
  select * into evidence from runtime.measurement_evidence where realm_id=rid
    and id=packet.measurement_evidence_id;
  select array_agg(value->>'evidence_ref' order by value->>'evidence_ref') into observed_refs
    from jsonb_array_elements(coalesce(evidence.evidence_body->'measurements','[]'::jsonb)) value;
  allow_next:=coalesce((packet.progress_decision_body->>'allow_next_attempt')::boolean,false);
  if packet.packet_digest<>p_progress_packet_digest
    or packet.metric_vector_digest<>p_metric_vector_digest
    or packet.progress_decision_digest<>p_progress_decision_digest
    or packet.progress_state<>p_progress_state
    or coalesce(observed_refs,array[]::text[]) is distinct from p_metric_evidence_refs
    or p_metric_evidence_refs is distinct from array(
      select distinct ref from unnest(coalesce(p_metric_evidence_refs,array[]::text[])) ref order by ref)
    or coalesce((evidence.evidence_body->>'producer_self_report')::boolean,true)
    or (p_outcome='passed' and p_progress_state<>'target-reached')
    or (p_outcome='retryable-failure' and not allow_next)
    or (p_outcome in ('blocked','manual-review') and allow_next) then
    raise exception 'measured loop completion result/verifier/metric/progress binding drift'
      using errcode='42501';
  end if;
  select * into prior from runtime.loop_attempt_outcome
    where realm_id=rid and attempt_id=p_attempt_id;
  if found then
    if row(prior.outcome,prior.validator_spec_digest,prior.result_invocation_id,
      prior.verifier_invocation_id,prior.effect_receipt_id,prior.actual_input_tokens,
      prior.actual_output_tokens,prior.actual_cost_micros) is distinct from
      row(p_outcome,p_validator_spec_digest,p_result_invocation_id,p_verifier_invocation_id,
      p_effect_receipt_id,p_actual_input_tokens,p_actual_output_tokens,p_actual_cost_micros) then
      raise exception 'measured loop completion replay binding drift' using errcode='23505';
    end if;
    select state into prior_state from runtime.loop_terminal
      where realm_id=rid and loop_id=at.loop_id;
    return coalesce(prior_state,'active');
  end if;
  return runtime.complete_loop_attempt(p_outcome_id,p_attempt_id,p_outcome,
    p_validator_spec_digest,p_result_invocation_id,p_verifier_invocation_id,
    p_effect_receipt_id,p_actual_input_tokens,p_actual_output_tokens,p_actual_cost_micros);
end $$;

do $$ declare target text; begin foreach target in array array[
  'runtime.optimization_objective','runtime.validator_asset_manifest',
  'runtime.loop_policy_v2','runtime.measurement_evidence','runtime.loop_progress_packet',
  'runtime.loop_attempt_job','runtime.loop_attempt_novelty','runtime.loop_control_event',
  'runtime.execution_topology_decision',
  'runtime.graph_execution_receipt','runtime.tournament_plan','runtime.loop_change_set',
  'runtime.loop_rollback_receipt','runtime.scaffolding_ablation'
] loop
  execute format('alter table %s enable row level security',target);
  execute format('alter table %s force row level security',target);
  execute format('create policy scope_select on %s for select using (realm_id=core.current_realm_id())',target);
  execute format('create trigger immutable_rows before update or delete on %s for each statement execute function core.deny_mutation()',target);
  execute format('revoke all on %s from public',target);
  execute format('grant select on %s to zekam_app',target);
end loop; end $$;

revoke all on function runtime.store_measured_loop_contract(uuid,uuid,uuid,jsonb,text,text,
  uuid,uuid,jsonb,text,jsonb,text,integer,integer,integer,double precision),
  runtime.store_loop_progress(uuid,uuid,uuid,uuid,uuid,uuid,jsonb,text,timestamptz,
    integer,jsonb,text,boolean,text,integer,text,jsonb,text,text),
  runtime.bind_loop_attempt_job(uuid,integer,uuid,text,uuid,text),
  runtime.record_loop_control_event(uuid,uuid,text,uuid,text,text),
  runtime.admit_loop_attempt_current(uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,
    bigint,bigint,bigint,uuid[],text,integer,text,text,text,text,text),
  runtime.complete_loop_attempt_current(uuid,uuid,text,text,uuid,uuid,uuid,bigint,bigint,bigint,
    text,text,text,text[],text),
  runtime.store_topology_decision(uuid,uuid,uuid,uuid,jsonb,text,text,jsonb,text),
  runtime.store_graph_execution_receipt(uuid,uuid,uuid,jsonb,text,boolean),
  runtime.store_tournament_plan(uuid,uuid,uuid,text,text,jsonb,text),
  runtime.store_loop_change_set(uuid,uuid,uuid,jsonb,text,text),
  runtime.store_loop_rollback_receipt(uuid,uuid,jsonb,text),
  runtime.store_scaffolding_ablation(uuid,uuid,uuid,uuid,jsonb,text,text),
  runtime.assert_measured_payload_safe(jsonb) from public;
revoke execute on function runtime.admit_loop_attempt(uuid,uuid,uuid,text,text,text,text,text,text,
  text,text,text,bigint,bigint,bigint,uuid[],text),
  runtime.complete_loop_attempt(uuid,uuid,text,text,uuid,uuid,uuid,bigint,bigint,bigint)
  from zekam_app;
grant execute on function runtime.store_measured_loop_contract(uuid,uuid,uuid,jsonb,text,text,
  uuid,uuid,jsonb,text,jsonb,text,integer,integer,integer,double precision),
  runtime.store_loop_progress(uuid,uuid,uuid,uuid,uuid,uuid,jsonb,text,timestamptz,
    integer,jsonb,text,boolean,text,integer,text,jsonb,text,text),
  runtime.bind_loop_attempt_job(uuid,integer,uuid,text,uuid,text),
  runtime.record_loop_control_event(uuid,uuid,text,uuid,text,text),
  runtime.admit_loop_attempt_current(uuid,uuid,uuid,text,text,text,text,text,text,text,text,text,
    bigint,bigint,bigint,uuid[],text,integer,text,text,text,text,text),
  runtime.complete_loop_attempt_current(uuid,uuid,text,text,uuid,uuid,uuid,bigint,bigint,bigint,
    text,text,text,text[],text),
  runtime.store_topology_decision(uuid,uuid,uuid,uuid,jsonb,text,text,jsonb,text),
  runtime.store_graph_execution_receipt(uuid,uuid,uuid,jsonb,text,boolean),
  runtime.store_tournament_plan(uuid,uuid,uuid,text,text,jsonb,text),
  runtime.store_loop_change_set(uuid,uuid,uuid,jsonb,text,text),
  runtime.store_loop_rollback_receipt(uuid,uuid,jsonb,text),
  runtime.store_scaffolding_ablation(uuid,uuid,uuid,uuid,jsonb,text,text) to zekam_app;
