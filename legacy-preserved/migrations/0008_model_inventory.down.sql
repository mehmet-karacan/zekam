-- 0008_model_inventory geri alma betigi.
--
-- Bu betik otomatik calismaz. Model envanterini, probe gecmisini, sozlesme
-- kontrollerini, karantina olaylarini ve raporlari siler; yalnizca exact
-- authorization ile uygulanir.

drop table if exists models.health_report;
drop table if exists models.quarantine_event;
drop table if exists models.capability_check;
drop table if exists models.health_probe;
drop table if exists models.model_inventory;

delete from core.schema_migrations where version = 8;
