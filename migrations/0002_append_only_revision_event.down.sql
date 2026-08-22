-- 0002_append_only_revision_event geri alma betigi.
--
-- Bu betik otomatik calismaz. Yalnizca exact authorization ile uygulanir ve
-- butun revision/event tarihcesini siler.

drop table if exists core.event;
drop table if exists core.revision;

drop function if exists core.enforce_revision_chain();
drop function if exists core.deny_mutation();

delete from core.schema_migrations where version = 2;
