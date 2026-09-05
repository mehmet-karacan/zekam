-- The strict 0051 guard can return only when every realm has contiguous one-based history.

lock table hooks.spec_revision in access exclusive mode;
alter table hooks.spec_revision no force row level security;

do $$
begin
  if exists(
    select 1
    from hooks.spec_revision
    group by realm_id,hook_id
    having min(revision)<>1 or count(*)<>max(revision)::bigint
  ) then
    raise exception '0059 rollback refused: hook revision history is not 0051-compatible'
      using errcode='55000';
  end if;
end $$;

create or replace function hooks.enforce_spec_revision() returns trigger
language plpgsql security invoker set search_path=pg_catalog,hooks,security,models,runtime as $$
declare expected jsonb; declare profile record;
begin
  perform pg_advisory_xact_lock(hashtextextended(new.realm_id::text||':'||new.hook_id,0));
  if not exists(select 1 from hooks.spec_revision prior
      where prior.realm_id=new.realm_id and prior.hook_digest=new.hook_digest)
    and new.revision<>(select coalesce(max(prior.revision),0)+1 from hooks.spec_revision prior
      where prior.realm_id=new.realm_id and prior.hook_id=new.hook_id) then
    raise exception 'hook spec revision monotonic olmali' using errcode='23514';
  end if;
  select name,profile_digest into profile from security.permission_profile_revision
    where realm_id=new.realm_id and id=new.permission_profile_revision_id;
  if profile.name is distinct from new.permission_profile_name
    or profile.profile_digest is distinct from new.permission_profile_digest then
    raise exception 'hook permission profile exact binding mismatch' using errcode='23514';
  end if;
  expected:=jsonb_build_object(
    'schema','zekam-hook-spec-revision/v1','id',new.id::text,'realm_id',new.realm_id::text,
    'hook_id',new.hook_id,'revision',new.revision,'event_type',new.event_type,
    'required',new.required,'source_layer',new.source_layer,'timeout_ms',new.timeout_ms,
    'execution_mode',new.execution_mode,'input_schema_digest',new.input_schema_digest,
    'output_schema_digest',new.output_schema_digest,
    'permission_profile_name',new.permission_profile_name,
    'permission_profile_digest',new.permission_profile_digest,
    'failure_policy',new.failure_policy,
    'created_at',runtime.environment_canonical_timestamp(new.created_at),
    'grants_authority',false);
  if not hooks.json_schema_supported(new.input_schema)
    or not hooks.json_schema_supported(new.output_schema)
    or new.input_schema_digest<>models.capability_runtime_jsonb_digest(new.input_schema)
    or new.output_schema_digest<>models.capability_runtime_jsonb_digest(new.output_schema)
    or new.hook_body<>expected
    or new.hook_digest<>models.capability_runtime_jsonb_digest(expected) then
    raise exception 'hook spec schema/body/digest drift' using errcode='23514';
  end if;
  return new;
end $$;

alter table hooks.spec_revision force row level security;
