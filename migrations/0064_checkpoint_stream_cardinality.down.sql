do $migration$
begin
  if exists (
    select 1 from work.checkpoint
    where job_id is not null
    group by realm_id, job_id
    having count(*) > 1
  ) then
    raise exception '0064 rollback refused: per-job checkpoint stream evidence exists'
      using errcode = '55000';
  end if;
end $migration$;

drop index work.checkpoint_job_latest_idx;

alter table work.checkpoint
  add constraint checkpoint_job_unique unique (realm_id, job_id);
