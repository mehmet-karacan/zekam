drop table if exists research.plan_candidate;
drop function if exists research.require_actionable_report();
drop table if exists research.report;
drop function if exists research.require_independent_verifier();
drop function if exists research.require_subagent_result();
drop table if exists research.role_result;
drop table if exists research.source_snapshot;
drop table if exists research.question;
drop table if exists research.intake_resolution;

delete from core.schema_migrations where version = 11;
