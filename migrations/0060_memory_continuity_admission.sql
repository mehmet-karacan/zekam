-- Session-start lifecycle delivery requires one separate, terminal hydration admission.

create table continuity.lifecycle_hydration_admission (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  codex_admission_id uuid not null,
  continuity_event_id uuid not null,
  delivery_outbox_id uuid not null,
  hydration_receipt_id uuid not null,
  hydration_authorization_id uuid not null,
  hydration_plan_digest text not null,
  hydration_effect_digest text not null,
  hydration_apply_result_digest text not null,
  hydration_created boolean not null,
  hydration_applied_at timestamptz not null,
  binding_digest text not null,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),
  unique(realm_id,codex_admission_id),
  unique(realm_id,continuity_event_id),
  unique(realm_id,hydration_receipt_id),
  foreign key(realm_id,codex_admission_id)
    references client.codex_lifecycle_admission(realm_id,id) on delete restrict,
  foreign key(realm_id,continuity_event_id)
    references continuity.session_lifecycle_event(realm_id,id) on delete restrict,
  foreign key(realm_id,delivery_outbox_id)
    references continuity.lifecycle_delivery_outbox(realm_id,id) on delete restrict,
  foreign key(realm_id,hydration_receipt_id)
    references continuity.session_hydration_receipt(realm_id,id) on delete restrict,
  foreign key(hydration_authorization_id)
    references security.authorization(id) on delete restrict,
  check(hydration_plan_digest ~ '^sha256:[0-9a-f]{64}$'
    and hydration_effect_digest ~ '^sha256:[0-9a-f]{64}$'
    and hydration_apply_result_digest ~ '^sha256:[0-9a-f]{64}$'
    and binding_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(created_at=hydration_applied_at),
  check(not grants_authority)
);

create function continuity.utc_timestamp_text(value_ timestamptz) returns text
language sql immutable strict set search_path=pg_catalog as $$
  select pg_catalog.to_char(value_ at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS')
    ||case when extract(microseconds from value_)::bigint%1000000=0 then ''
      else '.'||pg_catalog.to_char(value_ at time zone 'UTC','US') end||'Z'
$$;

create function continuity.enforce_lifecycle_hydration_admission() returns trigger
language plpgsql security definer set search_path=pg_catalog as $$
declare realm_ uuid; admission_id_ uuid; event_id_ uuid; exact_ bigint;
begin
  if tg_table_name='codex_lifecycle_admission' then
    realm_:=new.realm_id; admission_id_:=new.id; event_id_:=new.continuity_event_id;
  else
    realm_:=new.realm_id; admission_id_:=new.codex_admission_id;
    event_id_:=new.continuity_event_id;
  end if;
  if realm_ is distinct from core.current_realm_id() then
    raise exception 'lifecycle hydration admission realm scope drift' using errcode='42501';
  end if;
  if not exists(select 1 from continuity.session_lifecycle_event event
      where event.realm_id=realm_ and event.id=event_id_ and event.event_type='session_start') then
    if tg_table_name='lifecycle_hydration_admission' then
      raise exception 'hydration admission yalniz session_start eventine baglanabilir'
        using errcode='23514';
    end if;
    return new;
  end if;

  select count(*) into exact_
  from continuity.lifecycle_hydration_admission hydration_admission
  join client.codex_lifecycle_admission codex_admission
    on codex_admission.realm_id=hydration_admission.realm_id
    and codex_admission.id=hydration_admission.codex_admission_id
  join continuity.session_lifecycle_event event
    on event.realm_id=hydration_admission.realm_id
    and event.id=hydration_admission.continuity_event_id
  join continuity.lifecycle_delivery_outbox outbox
    on outbox.realm_id=hydration_admission.realm_id
    and outbox.id=hydration_admission.delivery_outbox_id
  join continuity.session_hydration_receipt receipt
    on receipt.realm_id=hydration_admission.realm_id
    and receipt.id=hydration_admission.hydration_receipt_id
  join security.authorization auth
    on auth.realm_id=hydration_admission.realm_id
    and auth.id=hydration_admission.hydration_authorization_id
  join runtime.execution_envelope envelope
    on envelope.realm_id=codex_admission.realm_id
    and envelope.id=codex_admission.envelope_id
  join work.work_item work_item
    on work_item.realm_id=event.realm_id and work_item.id=event.work_item_id
  join projects.source_binding source_binding
    on source_binding.realm_id=event.realm_id and source_binding.project_id=event.project_id
  join lateral(select revision.revision,revision.tree_digest
    from projects.source_revision revision
    where revision.realm_id=source_binding.realm_id
      and revision.binding_id=source_binding.id
    order by revision.observed_at desc,revision.id desc limit 1) source on true
  join lateral(select coalesce(max(version),0) migration_head
    from core.schema_migrations) migration on true
  join lateral(select projection.receipt_digest,projection.projection_digest,
      projection.source_digest
    from continuity.projection_generation_receipt projection
    where projection.realm_id=event.realm_id and projection.project_id=event.project_id
      and projection.work_item_id=event.work_item_id
      and projection.projection_ref='projection/active-work'
    order by projection.generated_at desc,projection.id desc limit 1) projection on true
  where hydration_admission.realm_id=realm_
    and hydration_admission.codex_admission_id=admission_id_
    and codex_admission.continuity_event_id=event.id
    and codex_admission.delivery_outbox_id=outbox.id
    and event.id=event_id_ and event.event_type='session_start'
    and event.event_digest=continuity.jsonb_digest(event.event_body)
    and outbox.event_id=event.id and outbox.state='completed'
    and receipt.project_id=event.project_id
    and receipt.work_item_id=event.work_item_id and receipt.run_id=event.run_id
    and receipt.session_id=event.session_id and receipt.client_id=event.client_id
    and receipt.receipt_digest=continuity.jsonb_digest(receipt.receipt_body)
    and receipt.receipt_body->>'hydration_event_digest'=event.event_digest
    and receipt.receipt_body->>'plan_ref'=event.event_body->>'plan_ref'
    and receipt.receipt_body->>'source_digest'=codex_admission.source_digest
    and receipt.receipt_body->>'policy_digest'=codex_admission.policy_digest
    and receipt.receipt_body->>'migration_digest'=codex_admission.migration_digest
    and receipt.receipt_body->>'context_digest'=envelope.context_manifest_digest
    and projection.source_digest=continuity.jsonb_digest(jsonb_build_object(
      'source_head',source.revision,'source_tree_digest',source.tree_digest,
      'migration_head',migration.migration_head,
      'database_revision_digest',continuity.jsonb_digest(jsonb_build_object(
        'project_id',event.project_id,'work_item_id',event.work_item_id,
        'work_revision',work_item.revision,'work_state',work_item.state,
        'work_record_digest',work_item.record_digest))))
    and projection.projection_digest=continuity.jsonb_digest(jsonb_build_object(
      'schema','zekam-memory-continuity-public-projection/v1',
      'project_id',event.project_id,'work_item_id',event.work_item_id,
      'work_revision',work_item.revision,'work_state',work_item.state,
      'source_head',source.revision,'source_tree_digest',source.tree_digest,
      'migration_head',migration.migration_head,
      'database_revision_digest',continuity.jsonb_digest(jsonb_build_object(
        'project_id',event.project_id,'work_item_id',event.work_item_id,
        'work_revision',work_item.revision,'work_state',work_item.state,
        'work_record_digest',work_item.record_digest)),
      'source_digest',projection.source_digest,'classification','public',
      'public_filtered',true,'content_included',false,'fresh',true,
      'read_only',true,'grants_authority',false))
    and exists(select 1 from jsonb_array_elements(
      receipt.receipt_body->'projection_refs') projection_ref
      where projection_ref->>'digest'=projection.projection_digest)
    and receipt.fresh and receipt.complete
    and receipt.receipt_body->'fresh'='true'::jsonb
    and receipt.receipt_body->'complete'='true'::jsonb
    and not exists(select 1 from jsonb_array_elements(receipt.receipt_body->'omissions') omission
      where omission->'required'='true'::jsonb)
    and exists(select 1 from jsonb_array_elements(receipt.receipt_body->'freshness') dimension
      where dimension->>'name'='source'
        and dimension->>'observed_digest'=codex_admission.source_digest
        and dimension->>'expected_digest'=codex_admission.source_digest
        and dimension->'current'='true'::jsonb)
    and exists(select 1 from jsonb_array_elements(receipt.receipt_body->'freshness') dimension
      where dimension->>'name'='policy'
        and dimension->>'observed_digest'=codex_admission.policy_digest
        and dimension->>'expected_digest'=codex_admission.policy_digest
        and dimension->'current'='true'::jsonb)
    and exists(select 1 from jsonb_array_elements(receipt.receipt_body->'freshness') dimension
      where dimension->>'name'='migration'
        and dimension->>'observed_digest'=codex_admission.migration_digest
        and dimension->>'expected_digest'=codex_admission.migration_digest
        and dimension->'current'='true'::jsonb)
    and exists(select 1 from jsonb_array_elements(receipt.receipt_body->'freshness') dimension
      where dimension->>'name'='context'
        and dimension->>'observed_digest'=envelope.context_manifest_digest
        and dimension->>'expected_digest'=envelope.context_manifest_digest
        and dimension->'current'='true'::jsonb)
    and auth.work_item_id=event.work_item_id and auth.plan_id=(
      select job.plan_id from runtime.job job
      where job.realm_id=codex_admission.realm_id and job.id=codex_admission.job_id)
    and auth.state='consumed' and auth.consumed_by='memory-continuity/v1'
    and auth.plan_digest=hydration_admission.hydration_plan_digest
    and auth.effect_digest=hydration_admission.hydration_effect_digest
    and auth.allowed_resources=array['continuity:hydration:'||receipt.id::text]
    and auth.allowed_effects=array['database-write']::text[]
    and cardinality(auth.provider_refs)=0 and cardinality(auth.secret_ref_ids)=0
    and auth.scope->'data_classifications'='[]'::jsonb
    and hydration_admission.hydration_effect_digest=continuity.jsonb_digest(jsonb_build_object(
      'effect','database-write','resource','continuity:hydration:'||receipt.id::text,
      'receipt_digest',receipt.receipt_digest))
    and hydration_admission.hydration_plan_digest=continuity.jsonb_digest(jsonb_build_object(
      'schema','zekam-continuity-receipt-plan/v1','kind','hydration',
      'receipt_id',receipt.id::text,'receipt_digest',receipt.receipt_digest,
      'idempotency_key',receipt.idempotency_key,
      'resource','continuity:hydration:'||receipt.id::text,
      'source_digest',receipt.receipt_body->>'source_digest',
      'policy_digest',receipt.receipt_body->>'policy_digest',
      'migration_digest',receipt.receipt_body->>'migration_digest',
      'context_digest',receipt.receipt_body->>'context_digest',
      'effect_digest',hydration_admission.hydration_effect_digest,
      'grants_authority',false))
    and hydration_admission.hydration_apply_result_digest=continuity.jsonb_digest(
      jsonb_build_object('schema','zekam-continuity-apply-receipt/v1','kind','hydration',
        'receipt_id',receipt.id::text,'receipt_digest',receipt.receipt_digest,
        'plan_digest',hydration_admission.hydration_plan_digest,
        'authorization_id',auth.id::text,'created',hydration_admission.hydration_created,
        'applied_at',continuity.utc_timestamp_text(hydration_admission.hydration_applied_at),
        'grants_authority',false))
    and hydration_admission.binding_digest=continuity.jsonb_digest(jsonb_build_object(
      'schema','zekam-lifecycle-hydration-admission/v1',
      'codex_admission_id',codex_admission.id::text,
      'continuity_event_id',event.id::text,'delivery_outbox_id',outbox.id::text,
      'hydration_receipt_id',receipt.id::text,'hydration_receipt_digest',receipt.receipt_digest,
      'hydration_authorization_id',auth.id::text,
      'hydration_plan_digest',hydration_admission.hydration_plan_digest,
      'hydration_effect_digest',hydration_admission.hydration_effect_digest,
      'hydration_apply_result_digest',hydration_admission.hydration_apply_result_digest,
      'hydration_created',hydration_admission.hydration_created,
      'hydration_applied_at',continuity.utc_timestamp_text(
        hydration_admission.hydration_applied_at),
      'grants_authority',false))
    and auth.consumed_at<=hydration_admission.hydration_applied_at
    and hydration_admission.hydration_applied_at=hydration_admission.created_at;
  if exact_<>1 then
    raise exception 'session_start exact terminal hydration admission missing'
      using errcode='23514';
  end if;
  return new;
end $$;

create constraint trigger lifecycle_hydration_codex_guard
after insert on client.codex_lifecycle_admission deferrable initially deferred
for each row execute function continuity.enforce_lifecycle_hydration_admission();
create constraint trigger lifecycle_hydration_row_guard
after insert on continuity.lifecycle_hydration_admission deferrable initially deferred
for each row execute function continuity.enforce_lifecycle_hydration_admission();
create trigger lifecycle_hydration_no_mutation
before update or delete on continuity.lifecycle_hydration_admission
for each statement execute function core.deny_mutation();

revoke all on function continuity.enforce_lifecycle_hydration_admission() from public;
revoke all on function continuity.utc_timestamp_text(timestamptz) from public;
grant execute on function continuity.enforce_lifecycle_hydration_admission() to zekam_app;
alter table continuity.lifecycle_hydration_admission enable row level security;
alter table continuity.lifecycle_hydration_admission force row level security;
create policy scope_select on continuity.lifecycle_hydration_admission
for select using(realm_id=core.current_realm_id());
create policy scope_insert on continuity.lifecycle_hydration_admission
for insert with check(realm_id=core.current_realm_id());
grant select,insert on continuity.lifecycle_hydration_admission to zekam_app;
