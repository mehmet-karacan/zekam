-- 0005_work_graph geri alma betigi.
--
-- Bu betik otomatik calismaz. Butun Work Item, iliski, Intent, Decision ve plan
-- kayitlarini siler; yalnizca exact authorization ile uygulanir.

drop table if exists work.task_plan;
drop table if exists work.decision;
drop table if exists work.intent;
drop table if exists work.work_relation;
drop table if exists work.work_item;

drop function if exists work.enforce_acyclic_relation();

delete from core.schema_migrations where version = 5;
