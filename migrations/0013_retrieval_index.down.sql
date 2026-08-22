drop trigger if exists embedding_profile_guard on knowledge.chunk_embedding;
drop function if exists knowledge.enforce_embedding_profile();
drop table if exists knowledge.chunk_embedding;
drop table if exists knowledge.chunk;
drop table if exists knowledge.embedding_profile;
drop table if exists knowledge.chunk_profile;

delete from core.schema_migrations where version = 13;
