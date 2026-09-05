-- One-shot admission for binding the missing envelope of an expired,
-- receiptless effect from a separately claimed recovery job.

create table runtime.recovery_envelope_admission (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  old_job_id uuid not null,
  old_attempt_id uuid not null,
  old_lease_id uuid not null,
  old_fencing_token bigint not null check(old_fencing_token>0),
  old_claim_id uuid not null,
  recovery_job_id uuid not null,
  recovery_attempt_id uuid not null,
  recovery_claim_id uuid not null,
  resource text not null check(btrim(resource)<>''),
  transaction_id xid8 not null default pg_current_xact_id(),
  expires_at timestamptz not null,
  consumed_at timestamptz,
  created_at timestamptz not null default statement_timestamp(),
  unique(realm_id,id),
  unique(realm_id,old_job_id,old_attempt_id,old_claim_id),
  foreign key(realm_id,old_job_id) references runtime.job(realm_id,id),
  foreign key(realm_id,old_attempt_id) references runtime.job_attempt(realm_id,id),
  foreign key(realm_id,old_claim_id) references runtime.effect_claim(realm_id,id),
  foreign key(realm_id,recovery_job_id) references runtime.job(realm_id,id),
  foreign key(realm_id,recovery_attempt_id) references runtime.job_attempt(realm_id,id),
  foreign key(realm_id,recovery_claim_id) references runtime.effect_claim(realm_id,id),
  check(consumed_at is null or consumed_at>=created_at)
);

create function runtime.consume_recovery_envelope_admission(
  realm_id_ uuid, job_id_ uuid, attempt_id_ uuid, lease_id_ uuid, fencing_token_ bigint
) returns boolean
language plpgsql security definer
set search_path=pg_catalog,core,runtime as $$
declare admission_id_ uuid;
begin
  if realm_id_ is distinct from core.current_realm_id() then
    raise exception 'recovery envelope admission realm drift' using errcode='42501';
  end if;
  select admission.id into admission_id_
  from runtime.recovery_envelope_admission admission
  join runtime.job old_job on old_job.realm_id=admission.realm_id
    and old_job.id=admission.old_job_id
  join runtime.job_attempt old_attempt on old_attempt.realm_id=admission.realm_id
    and old_attempt.id=admission.old_attempt_id and old_attempt.job_id=old_job.id
  join runtime.lease old_lease on old_lease.realm_id=admission.realm_id
    and old_lease.id=admission.old_lease_id and old_lease.job_id=old_job.id
    and old_lease.attempt_id=old_attempt.id
  join runtime.effect_claim old_claim on old_claim.realm_id=admission.realm_id
    and old_claim.id=admission.old_claim_id and old_claim.job_id=old_job.id
    and old_claim.attempt_id=old_attempt.id
  join runtime.job recovery_job on recovery_job.realm_id=admission.realm_id
    and recovery_job.id=admission.recovery_job_id
  join runtime.job_attempt recovery_attempt on recovery_attempt.realm_id=admission.realm_id
    and recovery_attempt.id=admission.recovery_attempt_id
    and recovery_attempt.job_id=recovery_job.id
  join runtime.lease recovery_lease on recovery_lease.realm_id=admission.realm_id
    and recovery_lease.job_id=recovery_job.id
    and recovery_lease.attempt_id=recovery_attempt.id
  join runtime.effect_claim recovery_claim on recovery_claim.realm_id=admission.realm_id
    and recovery_claim.id=admission.recovery_claim_id
    and recovery_claim.job_id=recovery_job.id
    and recovery_claim.attempt_id=recovery_attempt.id
  where admission.realm_id=realm_id_ and admission.old_job_id=job_id_
    and admission.old_attempt_id=attempt_id_ and admission.old_lease_id=lease_id_
    and admission.old_fencing_token=fencing_token_
    and admission.transaction_id=pg_current_xact_id()
    and admission.consumed_at is null and admission.expires_at>statement_timestamp()
    and old_job.state in ('running','recovery-required')
    and old_job.fencing_token=fencing_token_
    and old_attempt.fencing_token=fencing_token_ and old_attempt.outcome is null
    and old_lease.fencing_token=fencing_token_
    and old_lease.expires_at<=statement_timestamp()
    and old_claim.fencing_token=fencing_token_
    and not exists(select 1 from runtime.effect_receipt old_receipt
      where old_receipt.realm_id=old_claim.realm_id and old_receipt.claim_id=old_claim.id)
    and recovery_job.state='running' and recovery_job.max_attempts=1
    and recovery_attempt.outcome is null
    and recovery_attempt.fencing_token=recovery_job.fencing_token
    and recovery_lease.fencing_token=recovery_job.fencing_token
    and recovery_lease.expires_at>statement_timestamp()
    and recovery_claim.fencing_token=recovery_job.fencing_token
    and recovery_claim.operation='reconcile-recovery'
    and exists(select 1 from jsonb_array_elements(recovery_claim.resources) entry
      where entry->>'resource'=admission.resource and entry->>'mode'='write')
    and not exists(select 1 from runtime.effect_receipt recovery_receipt
      where recovery_receipt.realm_id=recovery_claim.realm_id
        and recovery_receipt.claim_id=recovery_claim.id)
  for update of admission;
  if admission_id_ is null then return false; end if;
  update runtime.recovery_envelope_admission
    set consumed_at=statement_timestamp()
    where realm_id=realm_id_ and id=admission_id_ and consumed_at is null;
  return found;
end $$;

do $$
declare definition_ text; patched_ text;
begin
  select pg_get_functiondef(procedure.oid) into strict definition_
  from pg_proc procedure join pg_namespace namespace on namespace.oid=procedure.pronamespace
  where namespace.nspname='runtime' and procedure.proname='enforce_execution_envelope';
  patched_:=replace(
    definition_,
    'or l.expires_at<=statement_timestamp() then',
    'or (l.expires_at<=statement_timestamp() and not runtime.consume_recovery_envelope_admission(new.realm_id,new.job_id,new.attempt_id,new.lease_id,new.fencing_token)) then'
  );
  if patched_ is not distinct from definition_ then
    raise exception '0067 execution envelope patch target missing';
  end if;
  execute patched_;
end $$;

do $$
declare function_name_ text; definition_ text; patched_ text;
declare recovery_predicate_ text := $predicate$(attempt.outcome='succeeded' or
  (attempt.outcome='recovery-required' and exists(
    select 1 from runtime.recovery_envelope_admission admission
    join runtime.effect_claim old_claim on old_claim.realm_id=admission.realm_id
      and old_claim.id=admission.old_claim_id and old_claim.job_id=admission.old_job_id
      and old_claim.attempt_id=admission.old_attempt_id
    join runtime.effect_receipt old_receipt on old_receipt.realm_id=old_claim.realm_id
      and old_receipt.claim_id=old_claim.id and old_receipt.status='completed'
    join runtime.job recovery_job on recovery_job.realm_id=admission.realm_id
      and recovery_job.id=admission.recovery_job_id and recovery_job.state='completed'
    join runtime.job_attempt recovery_attempt on recovery_attempt.realm_id=admission.realm_id
      and recovery_attempt.id=admission.recovery_attempt_id
      and recovery_attempt.job_id=recovery_job.id and recovery_attempt.outcome='succeeded'
    join runtime.effect_claim recovery_claim on recovery_claim.realm_id=admission.realm_id
      and recovery_claim.id=admission.recovery_claim_id
      and recovery_claim.job_id=recovery_job.id
      and recovery_claim.attempt_id=recovery_attempt.id
    join runtime.effect_receipt recovery_receipt on recovery_receipt.realm_id=admission.realm_id
      and recovery_receipt.claim_id=recovery_claim.id and recovery_receipt.status='completed'
    where admission.realm_id=job.realm_id and admission.old_job_id=job.id
      and admission.old_attempt_id=attempt.id and admission.consumed_at is not null)))$predicate$;
begin
  foreach function_name_ in array array[
    'lock_control_plane_completion_scope','admit_control_plane_completion',
    'enforce_completed_admission'
  ] loop
    select pg_get_functiondef(procedure.oid) into strict definition_
    from pg_proc procedure join pg_namespace namespace on namespace.oid=procedure.pronamespace
    where namespace.nspname='work' and procedure.proname=function_name_;
    patched_:=replace(definition_, 'attempt.outcome = ''succeeded''', recovery_predicate_);
    patched_:=replace(patched_, 'attempt.outcome=''succeeded''', recovery_predicate_);
    if function_name_='admit_control_plane_completion' then
      definition_:=patched_;
      patched_:=regexp_replace(
        patched_,
        'and job\.id=\(select latest_job\.id.*?limit 1\)',
        'and (job.id=(select latest_job.id from runtime.job latest_job where latest_job.realm_id=realm_id_ and latest_job.work_item_id=item.id and latest_job.plan_id=plan.id order by latest_job.created_at desc,latest_job.id desc limit 1) or exists(select 1 from runtime.recovery_envelope_admission recovery_admission where recovery_admission.realm_id=job.realm_id and recovery_admission.old_job_id=job.id and recovery_admission.recovery_job_id=(select latest_job.id from runtime.job latest_job where latest_job.realm_id=realm_id_ and latest_job.work_item_id=item.id and latest_job.plan_id=plan.id order by latest_job.created_at desc,latest_job.id desc limit 1) and recovery_admission.consumed_at is not null))',
        's'
      );
      if patched_ is not distinct from definition_ then
        raise exception '0067 recovered latest-job patch target missing';
      end if;
      patched_:=regexp_replace(
        patched_,
        'source_effect\.completed_at\s*<=\s*checkpoint\.created_at',
        '(source_effect.completed_at<=checkpoint.created_at or (attempt.outcome=''recovery-required'' and exists(select 1 from runtime.recovery_envelope_admission recovery_admission where recovery_admission.realm_id=job.realm_id and recovery_admission.old_job_id=job.id and recovery_admission.old_attempt_id=attempt.id and recovery_admission.old_claim_id=source_claim.id and recovery_admission.consumed_at is not null)))'
      );
    end if;
    if patched_ is not distinct from definition_ then
      raise exception '0067 control-plane recovery patch target missing: %',function_name_;
    end if;
    execute patched_;
  end loop;
end $$;

revoke all on function runtime.consume_recovery_envelope_admission(uuid,uuid,uuid,uuid,bigint)
  from public;
grant execute on function runtime.consume_recovery_envelope_admission(uuid,uuid,uuid,uuid,bigint)
  to zekam_app;
grant select, insert on runtime.recovery_envelope_admission to zekam_app;
