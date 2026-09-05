-- Permit an already-authorized one-shot recovery envelope to bind the exact
-- historical route/provider evidence that was current when the old effect ran.
do $$
declare definition_ text; patched_ text;
begin
  select pg_get_functiondef(procedure.oid) into strict definition_
  from pg_proc procedure join pg_namespace namespace on namespace.oid=procedure.pronamespace
  where namespace.nspname='runtime' and procedure.proname='enforce_execution_envelope';
  patched_:=replace(
    definition_,
    'or pb.expires_at<=statement_timestamp()' || chr(10) ||
    '    or new.route_expires_at<=statement_timestamp() then',
    'or ((pb.expires_at<=statement_timestamp() or new.route_expires_at<=statement_timestamp()) and not exists(select 1 from runtime.recovery_envelope_admission recovery_admission where recovery_admission.realm_id=new.realm_id and recovery_admission.old_job_id=new.job_id and recovery_admission.old_attempt_id=new.attempt_id and recovery_admission.old_lease_id=new.lease_id and recovery_admission.old_fencing_token=new.fencing_token and recovery_admission.transaction_id=pg_current_xact_id() and recovery_admission.consumed_at is not null)) then'
  );
  if patched_ is not distinct from definition_ then
    raise exception '0068 execution envelope historical route patch target missing';
  end if;
  execute patched_;
end $$;

