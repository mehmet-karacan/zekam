"""Additive Work dependency history and effect admission, operational v8."""

import sqlite3

MIGRATION_NAME = "operational-work-dependency-v8"

WORK_DEPENDENCY_DDL = r"""
create table work_dependency_event (
    work_item_id text not null references work_item(id),
    depends_on_work_id text not null references work_item(id),
    work_revision integer not null,
    action text not null check(action in ('added','removed')),
    event_digest text not null unique references work_event(event_digest),
    created_at text not null,
    primary key(work_item_id,depends_on_work_id,work_revision),
    foreign key(work_item_id,work_revision) references work_revision(work_item_id,revision),
    check(work_item_id<>depends_on_work_id)
) strict, without rowid;
create view work_dependency_current as
select e.work_item_id,e.depends_on_work_id,e.work_revision,e.event_digest
from work_dependency_event e
where e.action='added' and not exists (
    select 1 from work_dependency_event later
    where later.work_item_id=e.work_item_id
    and later.depends_on_work_id=e.depends_on_work_id
    and later.work_revision>e.work_revision
);
create trigger work_dependency_event_no_update before update on work_dependency_event begin
    select raise(abort,'work dependency history immutable');
end;
create trigger work_dependency_event_no_delete before delete on work_dependency_event begin
    select raise(abort,'work dependency history immutable');
end;
create trigger work_dependency_event_scope before insert on work_dependency_event begin
    select case when not exists (
        select 1 from work_item w join work_item dep on dep.id=new.depends_on_work_id
        join work_event ev on ev.event_digest=new.event_digest
        where w.id=new.work_item_id and w.project_id=dep.project_id
        and w.revision=new.work_revision and w.state in ('proposed','ready','blocked')
        and ev.work_item_id=w.id and ev.revision=w.revision
        and ev.event_kind='dependency-'||new.action
    ) then raise(abort,'work dependency scope/revision drift') end;
    select case when new.action='removed' and not exists (
        select 1 from work_dependency_current d
        where d.work_item_id=new.work_item_id and d.depends_on_work_id=new.depends_on_work_id
    ) then raise(abort,'work dependency missing') end;
    select case when new.action='added' and exists (
        select 1 from work_dependency_current d
        where d.work_item_id=new.work_item_id and d.depends_on_work_id=new.depends_on_work_id
    ) then raise(abort,'work dependency duplicate') end;
    select case when new.action='added' and exists (
        with recursive reachable(id) as (
            select new.depends_on_work_id
            union
            select d.depends_on_work_id from work_dependency_current d
            join reachable r on d.work_item_id=r.id
        ) select 1 from reachable where id=new.work_item_id
    ) then raise(abort,'work dependency cycle') end;
end;
create trigger work_dependency_state_guard before update of state on work_item
when new.state<>old.state and new.state in ('ready','active','verification','completed') begin
    select case when exists (
        select 1 from work_dependency_current d
        join work_item dep on dep.id=d.depends_on_work_id
        where d.work_item_id=new.id and (dep.state<>'completed' or dep.evidence_digest is null)
    ) then raise(abort,'work dependency incomplete') end;
end;
"""

DEPENDENCIES_COMPLETE = """
not exists (
    select 1 from work_dependency_current wd
    join work_item dependency on dependency.id=wd.depends_on_work_id
    where wd.work_item_id=b.work_item_id
    and (dependency.state<>'completed' or dependency.evidence_digest is null)
)
"""

for _table in ("local_lease", "local_effect_claim"):
    WORK_DEPENDENCY_DDL += (
        f"create trigger {_table}_work_dependencies before insert on {_table}\n"
        "when exists(select 1 from local_job_binding b where b.local_job_id=new.job_id) begin\n"
        " select case when exists(select 1 from local_job_binding b\n"
        " where b.local_job_id=new.job_id and not (" + DEPENDENCIES_COMPLETE + "))\n"
        " then raise(abort,'work dependency incomplete') end;\nend;\n"
    )


def current_binding_query(connection: sqlite3.Connection) -> str:
    """Extend the immutable v7 query only when the v8 canonical view exists."""
    from zekam.infrastructure.sqlite.local_job_binding import _CURRENT_BINDING_QUERY

    if (
        connection.execute(
            "select 1 from sqlite_master where type='view' and name='work_dependency_current'"
        ).fetchone()
        is None
    ):
        return _CURRENT_BINDING_QUERY
    return _CURRENT_BINDING_QUERY + " and " + DEPENDENCIES_COMPLETE


def has_unfinished_work_dependencies(connection: sqlite3.Connection, work_item_id: str) -> bool:
    if (
        connection.execute(
            "select 1 from sqlite_master where type='view' and name='work_dependency_current'"
        ).fetchone()
        is None
    ):
        return False
    return (
        connection.execute(
            "select 1 from work_dependency_current d join work_item dep on dep.id=d.depends_on_work_id"
            " where d.work_item_id=? and (dep.state<>'completed' or dep.evidence_digest is null) limit 1",
            (work_item_id,),
        ).fetchone()
        is not None
    )
