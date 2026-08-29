do $$
declare definition_ text; restored_ text;
begin
  select pg_get_functiondef(procedure.oid) into strict definition_
  from pg_proc procedure join pg_namespace namespace on namespace.oid=procedure.pronamespace
  where namespace.nspname='work' and procedure.proname='admit_control_plane_completion';
  restored_:=regexp_replace(
    definition_,
    '\(\s*source_effect\.completed_at\s*<=\s*checkpoint\.created_at\s+or\s+exists\s*\(.*?recovery_admission\.old_claim_id\s*=\s*source_claim\.id.*?recovery_admission\.consumed_at\s+is\s+not\s+null\s*\)\s*\)',
    '(source_effect.completed_at<=checkpoint.created_at or (attempt.outcome=''recovery-required'' and exists(select 1 from runtime.recovery_envelope_admission recovery_admission where recovery_admission.realm_id=job.realm_id and recovery_admission.old_job_id=job.id and recovery_admission.old_attempt_id=attempt.id and recovery_admission.old_claim_id=source_claim.id and recovery_admission.consumed_at is not null)))',
    'is'
  );
  if restored_ is not distinct from definition_
    and position('source_effect.completed_at' in definition_)>0 then
    raise exception '0069 rollback recovery completion terminal-chain patch target missing';
  end if;
  if restored_ is distinct from definition_ then
    execute restored_;
  end if;
end $$;
