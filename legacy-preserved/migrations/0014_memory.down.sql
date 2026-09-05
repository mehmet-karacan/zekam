drop trigger if exists record_immutable_content_guard on memory.record;
drop function if exists memory.enforce_immutable_content();
drop table if exists memory.embedding;
drop table if exists memory.relation;
drop table if exists memory.record;
drop table if exists memory.candidate;

delete from core.schema_migrations where version = 14;
