"""Additive v7 bridge from runtime jobs to canonical Work and Run records.

Existing operational rows and migrations stay immutable. Installation uses the
admitted operational migration boundary; rollback restores its verified backup.
"""

LOCAL_JOB_BINDING_DDL = r"""
create table local_job_binding (
    local_job_id text primary key references local_job(id),
    project_id text not null references project(id),
    work_item_id text not null references work_item(id),
    work_revision integer not null check(work_revision>0),
    run_id text not null references run(id),
    run_step_id text references run_step(id),
    source_snapshot_id text references source_snapshot(id),
    config_revision_id text not null references config_revision(id),
    config_digest text not null check(length(config_digest)=71
        and substr(config_digest,1,7)='sha256:'
        and substr(config_digest,8) not glob '*[^0-9a-f]*'),
    plan_digest text not null check(length(plan_digest)=71
        and substr(plan_digest,1,7)='sha256:'
        and substr(plan_digest,8) not glob '*[^0-9a-f]*'),
    binding_digest text not null check(length(binding_digest)=71
        and substr(binding_digest,1,7)='sha256:'
        and substr(binding_digest,8) not glob '*[^0-9a-f]*'),
    created_at text not null,
    foreign key(work_item_id,work_revision) references work_revision(work_item_id,revision)
) strict;
create index local_job_binding_run_idx on local_job_binding(run_id,local_job_id);
create trigger local_job_binding_no_update before update on local_job_binding begin
    select raise(abort,'local_job_binding append-only');
end;
create trigger local_job_binding_no_delete before delete on local_job_binding begin
    select raise(abort,'local_job_binding append-only');
end;
create trigger local_job_binding_admit before insert on local_job_binding begin
    select case when not exists (
        select 1 from work_item w
        join project p on p.id=w.project_id
        join run r on r.work_item_id=w.id
        join config_revision c on c.id=r.config_revision_id
        join local_job j on j.id=new.local_job_id
        where p.id=new.project_id and p.status='active'
        and j.state='ready' and j.attempt_count=0
        and w.id=new.work_item_id and w.revision=new.work_revision
        and w.state in ('ready','active','verification')
        and r.id=new.run_id and r.status in ('planned','running')
        and r.plan_digest=new.plan_digest
        and r.source_snapshot_id is new.source_snapshot_id
        and c.id=new.config_revision_id and c.active=1
        and c.config_digest=new.config_digest
        and json_extract(j.payload_json,'$.work_binding_digest')=new.binding_digest
        and (new.run_step_id is null or exists (
            select 1 from run_step s where s.id=new.run_step_id and s.run_id=r.id
            and s.status in ('pending','ready','running')
        ))
        and (new.source_snapshot_id is null or exists (
            select 1 from source_snapshot s join source_binding b on b.id=s.source_binding_id
            where s.id=new.source_snapshot_id and b.project_id=p.id and b.active=1
        ))
    ) then raise(abort,'local_job_binding canonical tuple drift') end;
end;
create trigger local_job_binding_payload_immutable before update of payload_json on local_job
when exists(select 1 from local_job_binding b where b.local_job_id=old.id)
and new.payload_json<>old.payload_json begin
    select raise(abort,'bound local_job payload immutable');
end;
"""

# Lease/effect admission always re-reads current canonical authority. The bridge
# holds references, never a copied grant, lease or second operational state.
_CURRENT_BINDING_QUERY = r"""
select 1 from local_job_binding b
join work_item w on w.id=b.work_item_id and w.project_id=b.project_id
join project p on p.id=b.project_id
join run r on r.id=b.run_id and r.work_item_id=w.id
join config_revision c on c.id=b.config_revision_id and c.id=r.config_revision_id
join local_job j on j.id=b.local_job_id
where b.local_job_id=new.job_id and p.status='active'
and w.revision=b.work_revision and w.state in ('ready','active','verification')
and r.status in ('planned','running') and r.plan_digest=b.plan_digest
and r.source_snapshot_id is b.source_snapshot_id
and c.active=1 and c.config_digest=b.config_digest
and b.binding_digest=json_extract(j.payload_json,'$.work_binding_digest')
and (b.run_step_id is null or exists (
    select 1 from run_step s where s.id=b.run_step_id and s.run_id=r.id
    and s.status in ('pending','ready','running')
    and not exists (
        select 1 from run_step_dependency d
        join run_step dep on dep.id=d.depends_on_step_id
        where d.run_step_id=s.id and dep.status<>'succeeded'
    )
))
and (b.source_snapshot_id is null or exists (
    select 1 from source_snapshot s join source_binding sb on sb.id=s.source_binding_id
    where s.id=b.source_snapshot_id and sb.project_id=p.id and sb.active=1
))
"""

for _table in ("local_lease", "local_effect_claim"):
    LOCAL_JOB_BINDING_DDL += (
        f"create trigger {_table}_work_binding_current before insert on {_table}\n"
        "when json_type((select payload_json from local_job where id=new.job_id),\n"
        " '$.work_binding_digest') is not null begin\n"
        f" select case when not exists ({_CURRENT_BINDING_QUERY})\n"
        " then raise(abort,'local_job_binding current authority required') end;\n"
        "end;\n"
    )

MIGRATION_NAME = "operational-local-job-binding-v7"
