-- Projection close writes its terminal effect receipt and consumes its exact
-- completion admission in the same transaction before the run-bound job is
-- finalized. Accept that stronger atomic chain without weakening checkpoint-v2
-- requirements for any other run-bound job.

create or replace function work.require_meaningful_job_checkpoint() returns trigger
language plpgsql security invoker set search_path=pg_catalog,work,runtime,core as $$
begin
  if new.state='completed' and old.state is distinct from 'completed'
    and (coalesce(new.payload->>'meaningful_step','false')='true'
      or (new.work_item_id is not null and new.plan_id is not null and new.step_id is not null)) then
    if new.run_id is null and not exists(select 1 from work.checkpoint c
        where c.realm_id=new.realm_id and c.job_id=new.id) then
      raise exception 'legacy meaningful terminal job requires checkpoint' using errcode='23514';
    elsif new.run_id is not null and not (
      (new.step_id='apply-atomic-close' and exists(
        select 1 from work.completion_admission admission
        join runtime.effect_receipt receipt on receipt.realm_id=admission.realm_id
          and receipt.id=admission.effect_receipt_id
        join work.checkpoint checkpoint on checkpoint.realm_id=admission.realm_id
          and checkpoint.id=admission.checkpoint_id
        join work.work_item item on item.realm_id=admission.realm_id
          and item.id=admission.work_item_id
        where admission.realm_id=new.realm_id and admission.job_id=new.id
          and admission.plan_id=new.plan_id and admission.work_item_id=new.work_item_id
          and admission.mode='projection-aware' and admission.operation='projection-aware-close'
          and admission.consumed_at is not null and item.state='completed'
          and receipt.status='completed' and checkpoint.job_id=new.id
          and checkpoint.task_plan_id=new.plan_id
          and checkpoint.completed_steps=array_remove(checkpoint.plan_steps,new.step_id)
          and checkpoint.pending_steps=array[new.step_id]
          and (select array_agg(result.key order by result.key)
            from jsonb_each_text(checkpoint.step_results) result)
            =(select array_agg(step order by step)
              from unnest(array_remove(checkpoint.plan_steps,new.step_id)) step)
      )) or exists(
        select 1 from work.checkpoint_v2 c
        join runtime.job_attempt a on a.realm_id=c.realm_id and a.id=c.attempt_id
        where c.realm_id=new.realm_id and c.job_id=new.id and c.run_id=new.run_id
          and c.step_id=new.step_id and new.step_id=any(c.completed_steps)
          and a.attempt_number=new.attempt_count and work.validate_checkpoint_v2(c.realm_id,c.id)
          and c.revision=(select max(x.revision) from work.checkpoint_v2 x
            where x.realm_id=c.realm_id and x.checkpoint_key=c.checkpoint_key)
      )) then
      raise exception 'run-bound meaningful terminal job requires current checkpoint v2 or atomic projection admission'
        using errcode='23514';
    end if;
  end if;
  return new;
end $$;

comment on function work.require_meaningful_job_checkpoint() is
  '0065 run-bound v2 gate with exact atomic projection-close compatibility';
