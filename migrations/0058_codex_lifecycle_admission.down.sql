do $$
begin
  execute 'alter table client.codex_lifecycle_admission no force row level security';
  if exists(select 1 from client.codex_lifecycle_admission) then
    raise exception '0058 rollback refused: Codex lifecycle admission audit data exists'
      using errcode='55000';
  end if;
end $$;

drop trigger if exists codex_lifecycle_admission_guard on client.lifecycle_event;
drop trigger if exists codex_lifecycle_admission_row_guard
  on client.codex_lifecycle_admission;
drop function if exists client.enforce_codex_lifecycle_admission();
drop function if exists client.lock_codex_lifecycle_scope(uuid,uuid,uuid,uuid);
revoke execute on function work.task_plan_execution_order(jsonb) from zekam_app;
drop table if exists client.codex_lifecycle_admission;
