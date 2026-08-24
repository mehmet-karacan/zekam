drop trigger if exists request_manifest_environment_guard on models.request_manifest;
drop function if exists models.enforce_manifest_environment_binding();
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
    ('source_revision',new.source_revision is null)
  ) as fields(name,missing) where missing;
  if new.missing_bindings<>expected then
    raise exception 'missing bindings manifest alanlariyla exact eslesmeli' using errcode='23514';
  end if;
  return new;
end $$;
alter table models.request_manifest drop column if exists hook_set_digest;
alter table models.request_manifest drop column if exists config_effective_digest;
alter table models.request_manifest drop column if exists turn_execution_snapshot_digest;
drop trigger if exists execution_envelope_turn_snapshot_guard on runtime.execution_envelope;
drop function if exists runtime.enforce_envelope_turn_snapshot();
alter table runtime.execution_envelope drop constraint if exists execution_envelope_turn_snapshot_required;
alter table runtime.execution_envelope drop constraint if exists execution_envelope_turn_snapshot_fk;
alter table runtime.execution_envelope drop column if exists turn_execution_snapshot_digest;
alter table runtime.execution_envelope drop column if exists turn_execution_snapshot_id;
drop table if exists runtime.environment_probe_evidence;
drop table if exists runtime.turn_execution_snapshot;
drop table if exists agents.assignment_environment_binding;
drop table if exists runtime.execution_environment_snapshot;
drop function if exists runtime.enforce_turn_execution_snapshot();
drop function if exists runtime.enforce_environment_snapshot_shape();
drop function if exists runtime.enforce_environment_probe_evidence();
drop function if exists agents.enforce_assignment_environment_binding();
drop function if exists runtime.environment_canonical_timestamp(timestamptz);
