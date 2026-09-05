-- 0003_project_registry geri alma betigi.
--
-- Bu betik otomatik calismaz. Yalnizca exact authorization ile uygulanir ve
-- butun proje kayitlarini, alias'lari, binding'leri, source revision'lari ve
-- capability profillerini siler.

drop table if exists projects.integration_state;
drop table if exists projects.capability_profile;
drop table if exists projects.source_revision;
drop table if exists projects.source_binding_local;
drop table if exists projects.source_binding;
drop table if exists projects.project_alias;
drop table if exists projects.project;

delete from core.schema_migrations where version = 3;
