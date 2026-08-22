-- 0006_security_governance geri alma betigi.
--
-- Bu betik otomatik calismaz. Policy, capability, SecretRef metadata, yetki
-- ledgeri, outbound kayitlari ve denetim gecmisini siler. Denetim gecmisinin
-- silinmesi geri alinamaz; yalnizca exact authorization ile uygulanir.

drop table if exists security.audit_event;
drop table if exists security.outbound_request;
drop table if exists security.authorization;
drop table if exists security.secret_ref;
drop table if exists security.capability;
drop table if exists security.policy;

drop function if exists security.enforce_authorization_transition();

delete from core.schema_migrations where version = 6;
