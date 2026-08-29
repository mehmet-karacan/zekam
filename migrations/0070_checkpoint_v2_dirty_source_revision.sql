do $$
declare definition_ text; patched_ text;
begin
  select pg_get_functiondef(procedure.oid) into strict definition_
  from pg_proc procedure join pg_namespace namespace on namespace.oid=procedure.pronamespace
  where namespace.nspname='work' and procedure.proname='checkpoint_v2_header_guard';
  patched_:=replace(
    definition_,
    'row(new.project_id,new.source_revision,new.capability_profile_digest,',
    'row(new.project_id,(case when new.source_revision ~ ''^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'' then substring(new.source_revision from 5 for 40) else new.source_revision end),new.capability_profile_digest,'
  );
  if patched_ is not distinct from definition_ then
    raise exception '0070 checkpoint v2 dirty source patch target missing';
  end if;
  execute patched_;
end $$;
