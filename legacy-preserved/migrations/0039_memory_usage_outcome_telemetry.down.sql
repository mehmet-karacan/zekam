drop trigger if exists checkpoint_memory_outcome on work.checkpoint_v2_step_verification;
drop function if exists memory.capture_checkpoint_usage_outcomes();
drop function if exists memory.rebuild_last_used_projection(uuid);
drop view if exists memory.usage_effectiveness;
drop trigger if exists invocation_memory_usage on models.invocation_result;
drop function if exists memory.capture_verified_invocation_usage();
drop trigger if exists usage_last_used_projection on memory.usage_event;
drop function if exists memory.project_last_used_at();
drop trigger if exists usage_outcome_integrity on memory.usage_outcome;
drop function if exists memory.enforce_usage_outcome();
drop trigger if exists usage_event_integrity on memory.usage_event;
drop function if exists memory.enforce_usage_event();
drop table if exists memory.usage_outcome;
drop table if exists memory.usage_event;
alter table work.context_fragment
    drop constraint if exists context_fragment_realm_scoped_key;
grant update (last_used_at) on memory.record to zekam_app;
