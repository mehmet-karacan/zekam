create or replace function work.require_meaningful_job_checkpoint() returns trigger
language plpgsql security invoker set search_path=pg_catalog,work,runtime,core as $$
begin
  if new.state='completed' and old.state is distinct from 'completed'
    and (coalesce(new.payload->>'meaningful_step','false')='true'
      or (new.work_item_id is not null and new.plan_id is not null and new.step_id is not null)) then
    if new.run_id is null and not exists(select 1 from work.checkpoint c
        where c.realm_id=new.realm_id and c.job_id=new.id) then
      raise exception 'legacy meaningful terminal job requires checkpoint' using errcode='23514';
    elsif new.run_id is not null and not exists(
      select 1 from work.checkpoint_v2 c
      join runtime.job_attempt a on a.realm_id=c.realm_id and a.id=c.attempt_id
      where c.realm_id=new.realm_id and c.job_id=new.id and c.run_id=new.run_id
        and c.step_id=new.step_id and new.step_id=any(c.completed_steps)
        and a.attempt_number=new.attempt_count and work.validate_checkpoint_v2(c.realm_id,c.id)
        and c.revision=(select max(x.revision) from work.checkpoint_v2 x
          where x.realm_id=c.realm_id and x.checkpoint_key=c.checkpoint_key)) then
      raise exception 'run-bound meaningful terminal job requires current checkpoint v2'
        using errcode='23514';
    end if;
  end if;
  return new;
end $$;

comment on function work.require_meaningful_job_checkpoint() is null;
