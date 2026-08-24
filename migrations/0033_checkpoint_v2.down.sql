create or replace function work.require_meaningful_job_checkpoint() returns trigger
language plpgsql security invoker set search_path=pg_catalog,work,runtime,core as $$
begin
  if new.state='completed' and old.state is distinct from 'completed'
    and (coalesce(new.payload->>'meaningful_step','false')='true'
      or (new.work_item_id is not null and new.plan_id is not null and new.step_id is not null))
    and not exists(select 1 from work.checkpoint c where c.realm_id=new.realm_id and c.job_id=new.id) then
    raise exception 'meaningful terminal job requires checkpoint' using errcode='23514';
  end if;
  return new;
end $$;
drop function if exists work.checkpoint_v2_child_deferred_guard() cascade;
drop function if exists work.checkpoint_v2_header_deferred_guard() cascade;
drop function if exists work.validate_checkpoint_v2(uuid,uuid) cascade;
drop function if exists work.checkpoint_v2_header_guard() cascade;
drop table if exists work.checkpoint_v2_open_effect;
drop table if exists work.checkpoint_v2_step_verification;
drop table if exists work.checkpoint_v2_step_receipt;
drop table if exists work.checkpoint_v2_step_result;
drop table if exists work.checkpoint_v2;
