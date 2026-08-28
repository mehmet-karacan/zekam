do $$
declare definition_ text; restored_ text;
begin
  if exists(
    select 1 from runtime.recovery_envelope_admission admission
    join runtime.execution_envelope envelope on envelope.realm_id=admission.realm_id
      and envelope.job_id=admission.old_job_id and envelope.attempt_id=admission.old_attempt_id
    where admission.consumed_at is not null
      and envelope.created_at>envelope.route_expires_at
  ) then
    raise exception '0068 rollback refused: historical recovery envelope exists';
  end if;
  select pg_get_functiondef(procedure.oid) into strict definition_
  from pg_proc procedure join pg_namespace namespace on namespace.oid=procedure.pronamespace
  where namespace.nspname='runtime' and procedure.proname='enforce_execution_envelope';
  restored_:=replace(
    definition_,
    'or ((pb.expires_at<=statement_timestamp() or new.route_expires_at<=statement_timestamp()) and not exists(select 1 from runtime.recovery_envelope_admission recovery_admission where recovery_admission.realm_id=new.realm_id and recovery_admission.old_job_id=new.job_id and recovery_admission.old_attempt_id=new.attempt_id and recovery_admission.old_lease_id=new.lease_id and recovery_admission.old_fencing_token=new.fencing_token and recovery_admission.transaction_id=pg_current_xact_id() and recovery_admission.consumed_at is not null)) then',
    'or pb.expires_at<=statement_timestamp()' || chr(10) ||
    '    or new.route_expires_at<=statement_timestamp() then'
  );
  if restored_ is not distinct from definition_ then
    raise exception '0068 rollback execution envelope historical route patch target missing';
  end if;
  execute restored_;
end $$;

