drop trigger if exists runtime_residency_no_delete on agents.runtime_residency;
drop trigger if exists reload_attempt_no_mutation on agents.reload_attempt;
drop trigger if exists residency_event_no_mutation on agents.residency_event;
drop trigger if exists assignment_runtime_snapshot_no_mutation on agents.assignment_runtime_snapshot;
drop trigger if exists residency_event_publish on agents.residency_event;
drop trigger if exists assignment_runtime_snapshot_guard on agents.assignment_runtime_snapshot;
drop function if exists agents.publish_residency_event();
drop function if exists agents.reload_runtime_residency(uuid,uuid,text,uuid,uuid,text,
  timestamptz,text,jsonb);
drop function if exists agents.transition_runtime_residency(uuid,uuid,text,timestamptz,text);
drop function if exists agents.register_runtime_residency(uuid,uuid,uuid,uuid,text,timestamptz);
drop function if exists agents.append_residency_event(uuid,uuid,uuid,text,integer,text,timestamptz);
drop function if exists agents.enforce_assignment_runtime_snapshot();
drop table if exists agents.reload_attempt;
drop table if exists agents.residency_event;
drop table if exists agents.runtime_residency;
drop table if exists agents.assignment_runtime_snapshot;

do $$
begin
  if exists(select 1 from agents.assignment where status='recovery-required') then
    raise exception 'recovery-required assignment varken residency migration geri alinamaz';
  end if;
end $$;
alter table agents.assignment
  drop constraint if exists assignment_terminal_residency_check,
  drop constraint if exists assignment_status_residency_check;
alter table agents.assignment
  add constraint assignment_status_check check (
    status in ('ready','active','completed','failed','blocked','cancelled')
  ),
  add constraint assignment_terminal_check check (
    (status in ('completed','failed','cancelled'))=(terminal_at is not null)
  );
