do $$
declare definition_ text; restored_ text;
begin
  select pg_get_functiondef(procedure.oid) into strict definition_
  from pg_proc procedure join pg_namespace namespace on namespace.oid=procedure.pronamespace
  where namespace.nspname='work' and procedure.proname='checkpoint_v2_header_guard';
  restored_:=regexp_replace(
    definition_,
    'row\(new\.project_id,\s*\(?case\s+when\s+new\.source_revision.*?end\)?,\s*new\.capability_profile_digest,',
    'row(new.project_id,new.source_revision,new.capability_profile_digest,',
    'is'
  );
  if restored_ is not distinct from definition_ then
    raise exception '0070 checkpoint v2 dirty source rollback target missing';
  end if;
  execute restored_;
end $$;
