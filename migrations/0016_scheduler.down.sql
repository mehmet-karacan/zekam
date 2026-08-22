drop table if exists ops.scheduler_incident;
drop table if exists ops.daily_report;
drop table if exists ops.incoming_document;
drop table if exists ops.job_run;
drop table if exists ops.job_definition;

delete from core.schema_migrations where version = 16;
