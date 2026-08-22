-- 0004_realm_bootstrap_functions geri alma betigi.
--
-- Bu betik otomatik calismaz. Fonksiyonlar kaldirildiktan sonra uygulama realm
-- bootstrap yapamaz; yalnizca exact authorization ile uygulanir.

drop function if exists core.ensure_realm(text, text);
drop function if exists core.find_realm_id(text);

delete from core.schema_migrations where version = 4;
