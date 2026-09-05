drop table if exists knowledge.content_unit;
drop table if exists knowledge.normalized_document;
drop trigger if exists version_requires_completed_ingestion on knowledge.source_version;
drop function if exists knowledge.require_completed_ingestion();
drop table if exists knowledge.source_version;
drop trigger if exists ingestion_stage_order_guard on knowledge.ingestion_job;
drop function if exists knowledge.enforce_stage_order();
drop table if exists knowledge.ingestion_job;
drop table if exists knowledge.source;
drop table if exists knowledge.artifact;

delete from core.schema_migrations where version = 12;
