-- A long-running job may advance through multiple immutable checkpoints.
-- Projection close already selects the latest checkpoint; keep every prior
-- checkpoint as historical evidence instead of mutating it in place.

do $migration$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'work.checkpoint'::regclass
      and conname = 'checkpoint_job_unique'
      and contype = 'u'
  ) then
    raise exception '0064 refused: checkpoint_job_unique baseline missing'
      using errcode = '55000';
  end if;
end $migration$;

alter table work.checkpoint drop constraint checkpoint_job_unique;

create index checkpoint_job_latest_idx
  on work.checkpoint (realm_id, job_id, created_at desc, id desc)
  where job_id is not null;

comment on index work.checkpoint_job_latest_idx is
  '0064 immutable per-job checkpoint stream ordered for latest-checkpoint admission';
