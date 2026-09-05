-- 0001_core_baseline geri alma betigi.
--
-- Bu betik otomatik calismaz. Yalnizca exact authorization ile ve yedek
-- dogrulandiktan sonra uygulanir. Butun realm ve actor kayitlarini siler.

drop table if exists core.actor;
drop table if exists core.realm;

drop function if exists core.touch_updated_at();
drop function if exists core.assert_realm_selected();
drop function if exists core.current_realm_id();

drop schema if exists ops cascade;
drop schema if exists security cascade;
drop schema if exists skills cascade;
drop schema if exists memory cascade;
drop schema if exists knowledge cascade;
drop schema if exists research cascade;
drop schema if exists models cascade;
drop schema if exists runtime cascade;
drop schema if exists work cascade;
drop schema if exists projects cascade;

-- core schema'si migration ledger'ini tasidigi icin birakilir.
-- Rol paylasilan bir nesnedir; yalnizca acik talep uzerine dusurulur:
--   drop role if exists zekam_app;

delete from core.schema_migrations where version = 1;
