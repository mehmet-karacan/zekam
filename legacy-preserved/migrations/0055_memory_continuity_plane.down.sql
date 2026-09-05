alter table hooks.spec_revision drop constraint if exists spec_revision_event_type_check;
alter table hooks.spec_revision add constraint spec_revision_event_type_check check(event_type in (
  'session.start','session.end','user.input.submitted','turn.start','turn.stop','pre.tool',
  'post.tool','permission.request','pre.compact','post.compact','checkpoint.created',
  'agent.spawned','agent.completed','recovery.required'
));

drop trigger if exists compiler_candidate_deny_delete on memory.compiler_candidate;
drop table if exists memory.compiler_candidate_promotion;
drop table if exists memory.compiler_candidate_review;
drop trigger if exists compiler_candidate_supersession on memory.compiler_candidate;
drop trigger if exists compiler_candidate_update on memory.compiler_candidate;
drop function if exists memory.enforce_compiler_candidate_supersession();
drop function if exists memory.enforce_compiler_candidate_update();
drop function if exists memory.enforce_compiler_candidate_review_insert();
drop function if exists memory.enforce_compiler_candidate_promotion_insert();
drop trigger if exists compiler_watermark_deny_delete on memory.compiler_watermark_claim;
drop trigger if exists compiler_watermark_update on memory.compiler_watermark_claim;
drop function if exists memory.enforce_compiler_watermark_update();
drop table if exists memory.compiler_candidate_source;
drop index if exists memory.compiler_candidate_current_idx;
drop table if exists memory.compiler_candidate;
alter table if exists memory.compiler_watermark_claim
  drop constraint if exists compiler_watermark_run_same_realm;
drop table if exists memory.compiler_run;
drop table if exists memory.compiler_watermark_claim;
drop schema if exists continuity cascade;
