alter table models.model_route_decision
  drop constraint if exists route_catalog_same_realm,
  drop constraint if exists route_catalog_binding,
  drop column if exists catalog_provider_id,
  drop column if exists catalog_digest,
  drop column if exists catalog_snapshot_digest,
  drop column if exists catalog_snapshot_id;
drop trigger if exists request_catalog_guard on models.request_manifest;
drop function if exists models.enforce_request_catalog();
alter table models.request_manifest
  drop constraint if exists request_catalog_same_realm,
  drop constraint if exists request_catalog_binding,
  drop column if exists catalog_provider_id,
  drop column if exists catalog_digest,
  drop column if exists catalog_snapshot_digest,
  drop column if exists catalog_snapshot_id;
create or replace function models.enforce_manifest_missing_bindings() returns trigger
language plpgsql as $$
declare expected text[];
begin
  select coalesce(array_agg(name order by name),'{}'::text[]) into expected from (values
    ('assignment_id',new.assignment_id is null),
    ('authorization_scope_digest',new.authorization_scope_digest is null),
    ('checkpoint_digest',new.checkpoint_digest is null),
    ('context_fragment_set_digest',new.context_fragment_set_digest is null),
    ('context_manifest_digest',new.context_manifest_digest is null),
    ('context_packet_digest',new.context_packet_digest is null),
    ('execution_envelope_digest',new.execution_envelope_digest is null),
    ('execution_envelope_id',new.execution_envelope_id is null),
    ('max_cost_micros',new.max_cost_micros is null),
    ('max_input_tokens',new.max_input_tokens is null),
    ('max_output_tokens',new.max_output_tokens is null),
    ('model_visible_payload_digest',new.model_visible_payload_digest is null),
    ('output_schema_digest',new.output_schema_digest is null),
    ('policy_digest',new.policy_digest is null),
    ('role',new.role is null),
    ('route_decision_digest',new.route_decision_digest is null),
    ('route_expires_at',new.route_expires_at is null),
    ('run_id',new.run_id is null),
    ('source_revision',new.source_revision is null),
    ('turn_execution_snapshot_digest',new.execution_envelope_id is not null
      and new.turn_execution_snapshot_digest is null),
    ('environment_digest',new.execution_envelope_id is not null and new.environment_digest is null),
    ('permission_profile_digest',new.execution_envelope_id is not null
      and new.permission_profile_digest is null),
    ('tool_set_digest',new.execution_envelope_id is not null and new.tool_set_digest is null),
    ('tool_visible_payload_digest',new.execution_envelope_id is not null
      and new.tool_visible_payload_digest is null),
    ('tool_visible_payload_mode',new.execution_envelope_id is not null
      and new.tool_visible_payload_mode is null),
    ('config_effective_digest',new.execution_envelope_id is not null
      and new.config_effective_digest is null),
    ('hook_set_digest',new.execution_envelope_id is not null and new.hook_set_digest is null)
  ) as fields(name,missing) where missing;
  if new.missing_bindings<>expected then
    raise exception 'missing bindings manifest alanlariyla exact eslesmeli' using errcode='23514';
  end if;
  return new;
end $$;
create or replace function models.enforce_route_decision() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models,projects,core as $$
declare policy_role text; policy_layer text; policy_digest_ text;
declare context_project uuid; target_digest text;
begin
  select role,target_layer,policy_digest into policy_role,policy_layer,policy_digest_
    from models.routing_role_policy where realm_id=new.realm_id and id=new.role_policy_id;
  if policy_role is distinct from new.role or policy_layer is distinct from new.target_layer
    or policy_digest_ is distinct from new.routing_policy_digest then
    raise exception 'route decision role policy drift' using errcode='42501';
  end if;
  if new.project_context_id is not null then
    select project_id into context_project from projects.routing_context_snapshot
      where realm_id=new.realm_id and id=new.project_context_id;
    if context_project is distinct from new.project_id then
      raise exception 'route decision project context mismatch' using errcode='42501';
    end if;
  end if;
  select snapshot_digest into target_digest from models.execution_target_snapshot
    where realm_id=new.realm_id and id=new.execution_target_id;
  if target_digest is distinct from new.execution_target_digest then
    raise exception 'route decision execution target drift' using errcode='42501';
  end if;
  return new;
end $$;
drop table if exists models.catalog_snapshot;
drop function if exists models.enforce_catalog_snapshot();
drop function if exists models.catalog_snapshot_immutable();
drop function if exists models.catalog_jsonb_digest(jsonb);
drop function if exists models.catalog_canonical_json(jsonb);
