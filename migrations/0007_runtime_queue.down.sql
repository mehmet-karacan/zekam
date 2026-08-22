-- 0007_runtime_queue geri alma betigi.
--
-- Bu betik otomatik calismaz. Kuyruk, deneme, lease, kilit, claim, receipt ve
-- yurutme olaylarini siler. Claim/receipt gecmisinin silinmesi geri alinamaz;
-- yalnizca exact authorization ile uygulanir.

drop view if exists runtime.claim_without_receipt;

drop table if exists runtime.execution_event;
drop table if exists runtime.effect_receipt;
drop table if exists runtime.effect_claim;
drop table if exists runtime.outbox_event;
drop table if exists runtime.resource_lock;
drop table if exists runtime.lease;
drop table if exists runtime.job_attempt;
drop table if exists runtime.job;

drop function if exists runtime.enforce_lock_conflict();
drop function if exists runtime.locks_conflict(text, text, text, text);
drop function if exists runtime.resource_path(text);
drop function if exists runtime.resource_rest(text);
drop function if exists runtime.resource_scope(text);
drop function if exists runtime.resource_kind(text);
drop function if exists runtime.enforce_attempt_immutability();

delete from core.schema_migrations where version = 7;
