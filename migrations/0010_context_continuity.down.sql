drop trigger if exists meaningful_job_checkpoint_guard on runtime.job;
drop function if exists work.require_meaningful_job_checkpoint();
drop table if exists work.finalized_handoff;
drop table if exists work.continuity_snapshot;
drop table if exists work.checkpoint;
alter table work.task_plan drop constraint if exists task_plan_realm_scoped_key;
drop trigger if exists journal_chain_guard on work.work_journal_entry;
drop function if exists work.enforce_journal_chain();
drop table if exists work.work_journal_entry;
drop table if exists work.context_manifest;
drop function if exists work.enforce_checkpoint_plan_partition();

delete from core.schema_migrations where version = 10;
