do $$ begin
  if exists(select 1 from runtime.loop_attempt_novelty where novelty_body is not null) then
    raise exception '0077 rollback refused: canonical novelty audit data exists';
  end if;
end $$;

drop function if exists runtime.admit_loop_attempt_current_v3(uuid,uuid,uuid,text,text,text,text,
  text,text,text,text,text,bigint,bigint,bigint,uuid[],text,integer,text,text,text,text,text,jsonb);
drop index if exists runtime.effect_claim_authorization_once;
drop trigger if exists assignment_resource_validator_asset_guard on agents.assignment_resource;
drop function if exists runtime.protect_validator_asset_from_builder_write();
drop trigger if exists loop_policy_v2_canonical on runtime.loop_policy_v2;
drop function if exists runtime.enforce_loop_policy_v2_canonical();
drop trigger if exists validator_asset_manifest_canonical on runtime.validator_asset_manifest;
drop function if exists runtime.enforce_validator_asset_manifest_canonical();
drop trigger if exists optimization_objective_canonical on runtime.optimization_objective;
drop function if exists runtime.enforce_optimization_objective_canonical();
drop function if exists runtime.assert_loop_policy_v2_body(
  jsonb,uuid,uuid,uuid,uuid,text,integer,integer,integer,double precision);
drop function if exists runtime.assert_validator_asset_manifest_body(jsonb,uuid,uuid,uuid,uuid);
drop function if exists runtime.assert_optimization_objective_body(jsonb,uuid,uuid,uuid,uuid,uuid);
drop function if exists runtime.assert_loop_novelty_body(jsonb,text,text);
drop index if exists runtime.loop_attempt_novelty_failure_once;
drop index if exists runtime.loop_attempt_novelty_patch_once;
drop index if exists runtime.loop_attempt_novelty_hypothesis_once;
alter table runtime.loop_attempt_novelty
  drop constraint if exists loop_attempt_novelty_body_pair,
  drop column if exists novelty_body_digest,
  drop column if exists novelty_body;

create or replace function runtime.record_loop_control_event(
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

create or replace function runtime.admit_loop_attempt_current(
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
