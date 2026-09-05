drop trigger if exists request_context_fragment_binding_guard on models.request_manifest;
drop function if exists models.enforce_request_context_fragment_binding();

drop trigger if exists deny_delete on work.context_fragment;
drop trigger if exists deny_update on work.context_fragment;
drop trigger if exists context_fragment_scope_guard on work.context_fragment;
drop policy if exists scope_insert on work.context_fragment;
drop policy if exists scope_select on work.context_fragment;
drop table if exists work.context_fragment;
drop function if exists work.enforce_context_fragment_scope();
drop trigger if exists context_fragment_set_complete_guard on work.context_fragment_set;
drop function if exists work.enforce_context_fragment_set_complete();
drop trigger if exists context_fragment_set_scope_guard on work.context_fragment_set;
drop function if exists work.enforce_context_fragment_set_scope();
drop trigger if exists deny_delete on work.context_fragment_set;
drop trigger if exists deny_update on work.context_fragment_set;
drop policy if exists scope_insert on work.context_fragment_set;
drop policy if exists scope_select on work.context_fragment_set;
drop table if exists work.context_fragment_set;

alter table models.request_manifest
    drop constraint if exists request_manifest_visible_payload_exact,
    drop constraint if exists request_manifest_visible_payload_digest_format,
    drop constraint if exists request_manifest_fragment_set_digest_format,
    drop column if exists model_visible_payload_digest,
    drop column if exists context_fragment_set_digest;

create or replace function models.enforce_manifest_missing_bindings() returns trigger
language plpgsql as $$
declare expected text[];
begin
  select coalesce(array_agg(name order by name),'{}'::text[]) into expected from (values
    ('assignment_id',new.assignment_id is null),
    ('authorization_scope_digest',new.authorization_scope_digest is null),
    ('checkpoint_digest',new.checkpoint_digest is null),
    ('context_manifest_digest',new.context_manifest_digest is null),
    ('context_packet_digest',new.context_packet_digest is null),
    ('execution_envelope_digest',new.execution_envelope_digest is null),
    ('execution_envelope_id',new.execution_envelope_id is null),
    ('max_cost_micros',new.max_cost_micros is null),
    ('max_input_tokens',new.max_input_tokens is null),
    ('max_output_tokens',new.max_output_tokens is null),
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
