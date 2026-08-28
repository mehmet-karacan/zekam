do $$
begin
  if exists(select 1 from runtime.recovery_envelope_admission where consumed_at is not null) then
    raise exception '0067 rollback refused: consumed recovery envelope admission exists';
  end if;
end $$;

do $$
declare definition_ text; restored_ text;
begin
  select pg_get_functiondef(procedure.oid) into strict definition_
  from pg_proc procedure join pg_namespace namespace on namespace.oid=procedure.pronamespace
  where namespace.nspname='runtime' and procedure.proname='enforce_execution_envelope';
  restored_:=replace(
    definition_,
    'or (l.expires_at<=statement_timestamp() and not runtime.consume_recovery_envelope_admission(new.realm_id,new.job_id,new.attempt_id,new.lease_id,new.fencing_token)) then',
    'or l.expires_at<=statement_timestamp() then'
  );
  if restored_ is not distinct from definition_ then
    raise exception '0067 rollback execution envelope patch target missing';
  end if;
  execute restored_;
end $$;

do $$
declare function_name_ text; definition_ text; restored_ text;
begin
  foreach function_name_ in array array[
    'lock_control_plane_completion_scope','admit_control_plane_completion',
    'enforce_completed_admission'
  ] loop
    select pg_get_functiondef(procedure.oid) into strict definition_
    from pg_proc procedure join pg_namespace namespace on namespace.oid=procedure.pronamespace
    where namespace.nspname='work' and procedure.proname=function_name_;
    restored_:=regexp_replace(
      definition_,
      '\(\s*attempt\.outcome\s*=\s*''succeeded''\s+or\s+\(\s*attempt\.outcome\s*=\s*''recovery-required''\s+and\s+exists\s*\(.*?admission\.consumed_at\s+is\s+not\s+null\s*\)\s*\)\s*\)',
      'attempt.outcome=''succeeded''',
      'is'
    );
    if function_name_='admit_control_plane_completion' then
      restored_:=regexp_replace(
        restored_,
        'and\s+\(\s*job\.id\s*=\s*\(\s*select latest_job\.id.*?recovery_admission\.consumed_at\s+is\s+not\s+null\s*\)\s*\)',
        'and job.id=(select latest_job.id from runtime.job latest_job where latest_job.realm_id=realm_id_ and latest_job.work_item_id=item.id and latest_job.plan_id=plan.id order by latest_job.created_at desc,latest_job.id desc limit 1)',
        'is'
      );
      restored_:=regexp_replace(
        restored_,
        '\(\s*source_effect\.completed_at\s*<=\s*checkpoint\.created_at\s+or\s+\(\s*attempt\.outcome\s*=\s*''recovery-required''\s+and\s+exists\s*\(.*?recovery_admission\.consumed_at\s+is\s+not\s+null\s*\)\s*\)\s*\)',
        'source_effect.completed_at<=checkpoint.created_at',
        'is'
      );
    end if;
    if restored_ is not distinct from definition_ then
      raise exception '0067 rollback control-plane recovery patch target missing: %',function_name_;
    end if;
    execute restored_;
  end loop;
end $$;

drop function runtime.consume_recovery_envelope_admission(uuid,uuid,uuid,uuid,bigint);
drop table runtime.recovery_envelope_admission;
