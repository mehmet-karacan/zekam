-- Bind enforce-mode request manifests to one current canonical execution envelope.
alter table models.request_manifest add column execution_envelope_id uuid;
alter table models.request_manifest add column execution_envelope_digest text;
alter table models.request_manifest add constraint request_manifest_execution_envelope_same_realm
  foreign key(realm_id,execution_envelope_id) references runtime.execution_envelope(realm_id,id);
alter table models.request_manifest add constraint request_manifest_execution_envelope_digest
  check(execution_envelope_digest is null or execution_envelope_digest ~ '^sha256:[0-9a-f]{64}$');

create or replace function models.enforce_manifest_missing_bindings() returns trigger language plpgsql as $$
declare expected text[];
begin
  select coalesce(array_agg(name order by name),'{}'::text[]) into expected from (values
    ('assignment_id',new.assignment_id is null),('authorization_scope_digest',new.authorization_scope_digest is null),
    ('checkpoint_digest',new.checkpoint_digest is null),('context_manifest_digest',new.context_manifest_digest is null),
    ('context_packet_digest',new.context_packet_digest is null),
    ('execution_envelope_digest',new.execution_envelope_digest is null),
    ('execution_envelope_id',new.execution_envelope_id is null),
    ('max_cost_micros',new.max_cost_micros is null),('max_input_tokens',new.max_input_tokens is null),
    ('max_output_tokens',new.max_output_tokens is null),('output_schema_digest',new.output_schema_digest is null),
    ('policy_digest',new.policy_digest is null),('role',new.role is null),
    ('route_decision_digest',new.route_decision_digest is null),('route_expires_at',new.route_expires_at is null),
    ('run_id',new.run_id is null),('source_revision',new.source_revision is null)
  ) as fields(name,missing) where missing;
  if new.missing_bindings<>expected then
    raise exception 'missing bindings manifest alanlariyla exact eslesmeli' using errcode='23514';
  end if;
  return new;
end $$;

create function models.enforce_manifest_execution_envelope() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models,runtime,agents as $$
declare bound record; policy_mode text;
begin
  select mode into policy_mode from models.gateway_policy where realm_id=new.realm_id;
  if coalesce(policy_mode,'audit')='enforce'
      and (new.execution_envelope_id is null or cardinality(new.missing_bindings)>0) then
    raise exception 'gateway enforce unbound/envelopeless manifest reddi' using errcode='42501';
  end if;
  if new.execution_envelope_id is null then return new; end if;
  select e.envelope_digest,e.run_id,e.job_id,e.attempt_id,e.assignment_id,e.role,
    e.route_decision_digest,e.model_id,e.provider_ref,e.context_manifest_digest,
    e.context_packet_digest,e.checkpoint_digest,e.source_revision,e.policy_digest,
    e.payload_digest,e.authorization_scope_digest,e.output_schema_digest,
    e.max_input_tokens,e.max_output_tokens,e.max_cost_micros,e.deadline,e.route_expires_at,
    r.project_id,r.work_item_id,r.plan_id,r.state run_state,j.step_id,j.state job_state,
    a.status assignment_state,l.expires_at lease_expires_at into bound
    from runtime.execution_envelope e
    join runtime.execution_run r on r.realm_id=e.realm_id and r.id=e.run_id
    join runtime.job j on j.realm_id=e.realm_id and j.id=e.job_id
    join agents.assignment a on a.realm_id=e.realm_id and a.id=e.assignment_id
    join runtime.lease l on l.realm_id=e.realm_id and l.id=e.lease_id
    where e.realm_id=new.realm_id and e.id=new.execution_envelope_id;
  if not found or row(bound.envelope_digest,bound.run_id,bound.job_id,bound.attempt_id,
      bound.assignment_id,bound.role,bound.route_decision_digest,bound.model_id,
      bound.provider_ref,bound.context_manifest_digest,bound.context_packet_digest,
      bound.checkpoint_digest,bound.source_revision,bound.policy_digest,bound.payload_digest,
      bound.authorization_scope_digest,bound.output_schema_digest,bound.max_input_tokens,
      bound.max_output_tokens,bound.max_cost_micros,bound.deadline,bound.route_expires_at,
      bound.project_id,bound.work_item_id,bound.plan_id,bound.step_id) is distinct from
    row(new.execution_envelope_digest,new.run_id,new.job_id,new.attempt_id,new.assignment_id,
      new.role,new.route_decision_digest,new.model_id,new.provider_ref,new.context_manifest_digest,
      new.context_packet_digest,new.checkpoint_digest,new.source_revision,new.policy_digest,
      new.payload_digest,new.authorization_scope_digest,new.output_schema_digest,
      new.max_input_tokens,new.max_output_tokens,new.max_cost_micros,new.deadline,
      new.route_expires_at,new.project_id,new.work_item_id,new.plan_id,new.step_id)
    or bound.run_state<>'active' or bound.job_state<>'running'
    or bound.assignment_state<>'active' or bound.lease_expires_at<=statement_timestamp()
    or bound.route_expires_at<=statement_timestamp() or bound.deadline<=statement_timestamp() then
    raise exception 'model manifest execution envelope exact/current binding drift' using errcode='42501';
  end if;
  return new;
end $$;
create trigger manifest_execution_envelope_check before insert on models.request_manifest
for each row execute function models.enforce_manifest_execution_envelope();

create or replace function models.enforce_gateway_attempt() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models,runtime,agents,core as $$
declare policy_mode text; manifest_status text; envelope_id uuid; current_envelope boolean;
begin
  select mode into policy_mode from models.gateway_policy where realm_id=new.realm_id;
  if coalesce(policy_mode,'audit')='enforce'
      and new.state in ('sent','response-received','parsed','verified') then
    select binding_status,execution_envelope_id into manifest_status,envelope_id
      from models.request_manifest where realm_id=new.realm_id and id=new.manifest_id;
    select exists(
      select 1 from runtime.execution_envelope e
      join runtime.execution_run r on r.realm_id=e.realm_id and r.id=e.run_id
      join runtime.job j on j.realm_id=e.realm_id and j.id=e.job_id
      join agents.assignment a on a.realm_id=e.realm_id and a.id=e.assignment_id
      join runtime.lease l on l.realm_id=e.realm_id and l.id=e.lease_id
      where e.realm_id=new.realm_id and e.id=envelope_id and r.state='active'
        and j.state='running' and a.status='active' and l.expires_at>statement_timestamp()
        and e.route_expires_at>statement_timestamp() and e.deadline>statement_timestamp()
    ) into current_envelope;
    if manifest_status is distinct from 'bound' or not coalesce(current_envelope,false)
        or new.effect_claim_id is null or new.authorization_id is null then
      raise exception 'gateway enforce exact current envelope/claim/authorization ister'
        using errcode='42501';
    end if;
  end if;
  return new;
end $$;

create or replace function models.activate_gateway_enforce(p_policy_digest text) returns void
language plpgsql security invoker set search_path=pg_catalog,models,core as $$
declare rid uuid := core.current_realm_id();
begin
  if p_policy_digest !~ '^sha256:[0-9a-f]{64}$' then raise exception 'policy digest invalid' using errcode='23514'; end if;
  perform pg_advisory_xact_lock(hashtextextended(rid::text,0));
  if exists(select 1 from models.invocation_audit where realm_id=rid
      and (disposition in ('unbound','bypass') or manifest_id is null))
    or exists(select 1 from models.request_manifest where realm_id=rid
      and (execution_envelope_id is null or binding_status<>'bound')) then
    raise exception 'gateway enforce requires zero unbound/bypass/envelopeless evidence' using errcode='23514';
  end if;
  insert into models.gateway_policy(realm_id,mode,policy_digest) values(rid,'enforce',p_policy_digest)
  on conflict(realm_id) do update set mode='enforce',policy_digest=excluded.policy_digest,activated_at=now();
end $$;
