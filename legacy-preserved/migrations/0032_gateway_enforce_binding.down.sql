create or replace function models.activate_gateway_enforce(p_policy_digest text) returns void
language plpgsql security invoker set search_path=pg_catalog,models,core as $$
declare rid uuid := core.current_realm_id();
begin
  if p_policy_digest !~ '^sha256:[0-9a-f]{64}$' then raise exception 'policy digest invalid' using errcode='23514'; end if;
  perform pg_advisory_xact_lock(hashtextextended(rid::text,0));
  if exists(select 1 from models.invocation_audit where realm_id=rid and disposition in ('unbound','bypass')) then
    raise exception 'gateway enforce requires zero unbound/bypass audit' using errcode='23514';
  end if;
  insert into models.gateway_policy(realm_id,mode,policy_digest) values(rid,'enforce',p_policy_digest)
  on conflict(realm_id) do update set mode='enforce',policy_digest=excluded.policy_digest,activated_at=now();
end $$;
create or replace function models.enforce_gateway_attempt() returns trigger language plpgsql security invoker
set search_path=pg_catalog,models,core as $$
declare policy_mode text; manifest_status text;
begin
  select mode into policy_mode from models.gateway_policy where realm_id=new.realm_id;
  if coalesce(policy_mode,'audit')='enforce' and new.state in ('sent','response-received','parsed','verified') then
    select binding_status into manifest_status from models.request_manifest where realm_id=new.realm_id and id=new.manifest_id;
    if manifest_status is distinct from 'bound' or new.effect_claim_id is null or new.authorization_id is null then
      raise exception 'gateway enforce exact bound manifest/claim/authorization ister' using errcode='42501';
    end if;
  end if;
  return new;
end $$;
drop trigger if exists manifest_execution_envelope_check on models.request_manifest;
drop function if exists models.enforce_manifest_execution_envelope();
alter table models.request_manifest drop constraint if exists request_manifest_execution_envelope_same_realm;
alter table models.request_manifest drop constraint if exists request_manifest_execution_envelope_digest;
alter table models.request_manifest drop column if exists execution_envelope_digest;
alter table models.request_manifest drop column if exists execution_envelope_id;
create or replace function models.enforce_manifest_missing_bindings() returns trigger language plpgsql as $$
declare expected text[];
begin
  select coalesce(array_agg(name order by name),'{}'::text[]) into expected from (values
    ('assignment_id',new.assignment_id is null),('authorization_scope_digest',new.authorization_scope_digest is null),
    ('checkpoint_digest',new.checkpoint_digest is null),('context_manifest_digest',new.context_manifest_digest is null),
    ('context_packet_digest',new.context_packet_digest is null),('max_cost_micros',new.max_cost_micros is null),
    ('max_input_tokens',new.max_input_tokens is null),('max_output_tokens',new.max_output_tokens is null),
    ('output_schema_digest',new.output_schema_digest is null),('policy_digest',new.policy_digest is null),
    ('role',new.role is null),('route_decision_digest',new.route_decision_digest is null),
    ('route_expires_at',new.route_expires_at is null),('run_id',new.run_id is null),
    ('source_revision',new.source_revision is null)
  ) as fields(name,missing) where missing;
  if new.missing_bindings<>expected then
    raise exception 'missing bindings manifest alanlariyla exact eslesmeli' using errcode='23514';
  end if;
  return new;
end $$;
