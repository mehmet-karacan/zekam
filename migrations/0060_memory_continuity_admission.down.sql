do $$
begin
  execute 'alter table continuity.lifecycle_hydration_admission no force row level security';
  if exists(select 1 from continuity.lifecycle_hydration_admission) then
    raise exception '0060 rollback refused: lifecycle hydration admission audit data exists'
      using errcode='55000';
  end if;
end $$;

drop trigger if exists lifecycle_hydration_codex_guard on client.codex_lifecycle_admission;
drop trigger if exists lifecycle_hydration_row_guard
  on continuity.lifecycle_hydration_admission;
drop function if exists continuity.enforce_lifecycle_hydration_admission();
drop table if exists continuity.lifecycle_hydration_admission;
drop function if exists continuity.utc_timestamp_text(timestamptz);
