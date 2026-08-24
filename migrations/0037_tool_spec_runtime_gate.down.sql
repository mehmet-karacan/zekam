alter table models.request_manifest drop constraint if exists request_manifest_compiled_tool_set_fk;
drop trigger if exists request_manifest_tool_payload_guard on models.request_manifest;
drop function if exists tools.enforce_model_visible_tool_payload();
drop trigger if exists turn_execution_snapshot_tool_set_guard on runtime.turn_execution_snapshot;
drop function if exists tools.enforce_turn_compiled_set();
alter table runtime.turn_execution_snapshot drop constraint if exists turn_execution_snapshot_compiled_tool_set_fk;
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
    ('config_effective_digest',new.execution_envelope_id is not null
      and new.config_effective_digest is null),
    ('hook_set_digest',new.execution_envelope_id is not null and new.hook_set_digest is null)
  ) as fields(name,missing) where missing;
  if new.missing_bindings<>expected then
    raise exception 'missing bindings manifest alanlariyla exact eslesmeli' using errcode='23514';
  end if;
  return new;
end $$;
alter table models.request_manifest
  drop constraint if exists request_manifest_tool_visible_payload_digest_format;
alter table models.request_manifest
  drop constraint if exists request_manifest_tool_visible_payload_mode_check;
alter table models.request_manifest drop column if exists tool_visible_payload_mode;
alter table models.request_manifest drop column if exists tool_visible_payload_digest;
create or replace function models.enforce_manifest_environment_binding() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, runtime as $$
declare e record; t record; env record;
begin
    if new.execution_envelope_id is null then return new; end if;
    select turn_execution_snapshot_digest into e from runtime.execution_envelope
      where realm_id = new.realm_id and id = new.execution_envelope_id;
    select execution_environment_snapshot_digest, exposed_tool_set_digest,
      config_effective_digest, hook_set_digest into t
      from runtime.turn_execution_snapshot
      where realm_id = new.realm_id and turn_snapshot_digest = e.turn_execution_snapshot_digest;
    select permission_profile_digest into env
      from runtime.execution_environment_snapshot
      where realm_id = new.realm_id and snapshot_digest = t.execution_environment_snapshot_digest;
    if row(new.turn_execution_snapshot_digest,new.environment_digest,new.permission_profile_digest,
           new.tool_set_digest,new.config_effective_digest,new.hook_set_digest)
       is distinct from
       row(e.turn_execution_snapshot_digest,t.execution_environment_snapshot_digest,
           env.permission_profile_digest,t.exposed_tool_set_digest,
           t.config_effective_digest,t.hook_set_digest) then
      raise exception 'model manifest environment/permission/tool/config binding drift'
        using errcode = '23514';
    end if;
    return new;
end
$$;
drop table if exists tools.dispatch_gate_evidence;
drop function if exists tools.enforce_dispatch_gate_evidence();
drop table if exists tools.compiled_set;
drop function if exists tools.enforce_compiled_set();
drop table if exists tools.runtime_revision;
drop function if exists tools.enforce_runtime_revision();
drop table if exists tools.spec_revision;
drop function if exists tools.enforce_spec_revision();
drop schema if exists tools;
