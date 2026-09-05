do $$
begin
  execute 'alter table work.completion_admission no force row level security';
  if exists(select 1 from work.completion_admission) then
    raise exception '0057 rollback refused: completion admission audit data exists'
      using errcode='55000';
  end if;
end $$;

drop trigger if exists work_completed_admission_guard on work.work_item;
drop function if exists work.enforce_completed_admission();
drop trigger if exists work_completed_insert_guard on work.work_item;
drop function if exists work.reject_completed_insert();
drop function if exists work.admit_control_plane_completion(uuid,uuid,uuid,integer,text,uuid,text,
  uuid,uuid,uuid,uuid,uuid,uuid,text,uuid,text,uuid,text,uuid,text,text,text,text,text,
  text[],text[],text[],jsonb,text,text);
drop function if exists work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,
  uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text);
drop function if exists work.lock_control_plane_completion_scope(uuid,uuid,uuid,uuid,uuid,uuid);
drop function if exists continuity.lock_projection_closure_scope(uuid,uuid,uuid,uuid,uuid,uuid);
drop trigger if exists projection_close_claim_order_guard on runtime.effect_claim;
drop function if exists runtime.reject_late_projection_close_claim();
drop trigger if exists hydration_authorization_guard on continuity.session_hydration_receipt;
drop function if exists continuity.enforce_hydration_authorization();
drop function if exists work.task_plan_execution_order(jsonb);
drop trigger if exists close_exact_guard on continuity.session_close_receipt;
drop function if exists continuity.enforce_exact_close_receipt();
drop table if exists work.completion_admission;
drop function if exists work.enforce_completion_admission_update();
drop function if exists work.enforce_completion_admission_body();
