-- Allow control-plane completion to consume a run-bound source effect only
-- when its terminal run and current checkpoint-v2 prove the same execution.

create or replace function work.lock_control_plane_completion_scope(
  realm_id_ uuid, project_id_ uuid, work_item_id_ uuid, plan_id_ uuid,
  job_id_ uuid, attempt_id_ uuid
) returns timestamptz
language plpgsql security definer
set search_path=pg_catalog,core,projects,work,runtime,security,continuity,memory as $$
declare now_ timestamptz:=statement_timestamp();
begin
  if realm_id_ is distinct from core.current_realm_id() then
    raise exception 'control-plane completion realm scope drift' using errcode='42501';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(
    realm_id_::text||':'||project_id_::text||':'||work_item_id_::text||':'||plan_id_::text,0));
  perform 1 from projects.source_binding
    where realm_id=realm_id_ and project_id=project_id_ for update;
  if not found then raise exception 'control-plane source binding missing' using errcode='P0002'; end if;
  perform 1 from work.work_item
    where realm_id=realm_id_ and project_id=project_id_ and id=work_item_id_ for update;
  if not found then raise exception 'control-plane work missing' using errcode='P0002'; end if;
  perform 1 from runtime.job job
    join runtime.job_attempt attempt on attempt.realm_id=job.realm_id and attempt.id=attempt_id_
    left join runtime.execution_run run on run.realm_id=job.realm_id and run.id=job.run_id
    where job.realm_id=realm_id_ and job.id=job_id_ and job.project_id=project_id_
      and job.work_item_id=work_item_id_ and job.plan_id=plan_id_
      and job.state='completed' and attempt.job_id=job.id and attempt.outcome='succeeded'
      and attempt.fencing_token=job.fencing_token
      and (job.run_id is null or (run.state='completed' and exists(
        select 1 from work.checkpoint_v2 checkpoint
        where checkpoint.realm_id=job.realm_id and checkpoint.job_id=job.id
          and checkpoint.run_id=job.run_id and checkpoint.attempt_id=attempt.id
          and checkpoint.task_plan_id=job.plan_id and checkpoint.step_id=job.step_id
          and checkpoint.assignment_id=job.assignment_id
          and checkpoint.execution_envelope_id is not null
          and job.step_id=any(checkpoint.completed_steps)
          and work.validate_checkpoint_v2(checkpoint.realm_id,checkpoint.id)
          and checkpoint.revision=(select max(latest.revision) from work.checkpoint_v2 latest
            where latest.realm_id=checkpoint.realm_id
              and latest.checkpoint_key=checkpoint.checkpoint_key))))
    for update of job,attempt;
  if not found then
    raise exception 'control-plane exact runtime chain missing' using errcode='23514';
  end if;
  return now_;
end $$;

-- A maintenance preparation Work owns a real execution_run, but its close is
-- still a control-plane close.  Keep projection-aware precedence for every
-- other runtime chain and admit this shape only when the same terminal job has
-- a validated current checkpoint-v2.
create or replace function work.enforce_completed_admission() returns trigger
language plpgsql security definer
set search_path=pg_catalog,work,continuity,runtime as $$
declare required_mode_ text; admission_id_ uuid;
begin
  if old.state='completed' or new.state<>'completed' then return new; end if;
  if new.type='maintenance' and exists(
      select 1 from runtime.job job
      join runtime.execution_run run on run.realm_id=job.realm_id and run.id=job.run_id
      join runtime.job_attempt attempt on attempt.realm_id=job.realm_id
        and attempt.job_id=job.id and attempt.outcome='succeeded'
      join work.checkpoint_v2 checkpoint on checkpoint.realm_id=job.realm_id
        and checkpoint.job_id=job.id and checkpoint.run_id=run.id
        and checkpoint.attempt_id=attempt.id and checkpoint.task_plan_id=job.plan_id
        and checkpoint.step_id=job.step_id and checkpoint.assignment_id=job.assignment_id
      where job.realm_id=new.realm_id and job.work_item_id=new.id
        and job.state='completed' and run.state='completed'
        and checkpoint.execution_envelope_id is not null
        and job.step_id=any(checkpoint.completed_steps)
        and work.validate_checkpoint_v2(checkpoint.realm_id,checkpoint.id)
        and checkpoint.revision=(select max(latest.revision) from work.checkpoint_v2 latest
          where latest.realm_id=checkpoint.realm_id
            and latest.checkpoint_key=checkpoint.checkpoint_key)) then
    required_mode_:='control-plane';
  elsif exists(select 1 from runtime.execution_run run
      where run.realm_id=new.realm_id and run.work_item_id=new.id)
    or exists(select 1 from continuity.session_lifecycle_event event
      where event.realm_id=new.realm_id and event.work_item_id=new.id)
    or exists(select 1 from continuity.session_hydration_receipt hydration
      where hydration.realm_id=new.realm_id and hydration.work_item_id=new.id)
    or exists(select 1 from continuity.session_close_receipt close_receipt
      where close_receipt.realm_id=new.realm_id and close_receipt.work_item_id=new.id)
    or exists(select 1 from continuity.projection_generation_receipt projection
      where projection.realm_id=new.realm_id and projection.work_item_id=new.id)
    or exists(select 1 from continuity.compaction_receipt compaction
      where compaction.realm_id=new.realm_id and compaction.work_item_id=new.id)
    or exists(select 1 from continuity.memory_contract_evaluation evaluation
      where evaluation.realm_id=new.realm_id and evaluation.work_item_id=new.id)
    or exists(select 1 from continuity.gap_recovery_reference gap
      where gap.realm_id=new.realm_id and gap.work_item_id=new.id)
    or exists(select 1 from memory.compiler_watermark_claim watermark
      where watermark.realm_id=new.realm_id and watermark.work_item_id=new.id) then
    required_mode_:='projection-aware';
  elsif new.type='maintenance' then
    required_mode_:='control-plane';
  else
    raise exception 'completed transition requires projection-aware continuity admission'
      using errcode='42501';
  end if;
  select id into admission_id_ from work.completion_admission
    where realm_id=new.realm_id and project_id=new.project_id and work_item_id=new.id
      and mode=required_mode_ and expected_work_revision=new.revision
      and expected_work_record_digest=new.record_digest
      and (mode<>'control-plane' or (
        completion_evidence=new.acceptance_evidence
        and evidence_digest=continuity.jsonb_digest(new.acceptance_evidence)))
      and transaction_id=pg_current_xact_id() and consumed_at is null
    order by admitted_at,id for update;
  if admission_id_ is null then
    raise exception 'raw completed transition lacks exact completion admission'
      using errcode='42501';
  end if;
  update work.completion_admission set consumed_at=statement_timestamp()
    where realm_id=new.realm_id and id=admission_id_ and consumed_at is null;
  if not found then
    raise exception 'completion admission consumption race' using errcode='40001';
  end if;
  return new;
end $$;

do $$
declare definition_ text; patched_ text;
begin
  select pg_get_functiondef(procedure.oid) into strict definition_
  from pg_proc procedure join pg_namespace namespace on namespace.oid=procedure.pronamespace
  where namespace.nspname='work' and procedure.proname='admit_control_plane_completion';
  patched_:=replace(definition_,
    'and job.run_id is null and job.state=''completed''',
    'and job.state=''completed'' and (job.run_id is null or exists(select 1 from runtime.execution_run source_run join work.checkpoint_v2 source_checkpoint on source_checkpoint.realm_id=source_run.realm_id and source_checkpoint.run_id=source_run.id and source_checkpoint.job_id=job.id and source_checkpoint.attempt_id=attempt.id and source_checkpoint.task_plan_id=job.plan_id and source_checkpoint.step_id=job.step_id where source_run.realm_id=job.realm_id and source_run.id=job.run_id and source_run.state=''completed'' and job.step_id=any(source_checkpoint.completed_steps) and work.validate_checkpoint_v2(source_checkpoint.realm_id,source_checkpoint.id)))');
  patched_:=replace(patched_,
    'and not exists(select 1 from runtime.execution_run run' || chr(10) ||
    '      where run.realm_id=realm_id_ and run.work_item_id=item.id)',
    'and not exists(select 1 from runtime.execution_run run where run.realm_id=realm_id_ and run.work_item_id=item.id and (job.run_id is null or run.id<>job.run_id or run.state<>''completed''))');
  if patched_ is not distinct from definition_ then
    raise exception '0066 admit_control_plane_completion patch target missing';
  end if;
  execute patched_;
end $$;
