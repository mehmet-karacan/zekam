drop trigger if exists lifecycle_pre_compact_checkpoint_compile on client.lifecycle_event;
drop function if exists work.compile_pre_compact_outbox();
drop trigger if exists compaction_checkpoint_outbox_insert_guard
  on work.compaction_checkpoint_outbox;
drop function if exists work.enforce_compaction_outbox_insert();
drop trigger if exists compaction_checkpoint_outbox_update_guard
  on work.compaction_checkpoint_outbox;
drop trigger if exists compaction_checkpoint_outbox_delete_guard
  on work.compaction_checkpoint_outbox;
drop function if exists work.enforce_compaction_outbox_update();
drop table if exists work.compaction_checkpoint_outbox;
drop function if exists work.valid_compaction_checkpoint_draft(jsonb);
