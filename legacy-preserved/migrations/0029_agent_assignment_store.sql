-- Canonical child assignment, invocation and write ownership store.

create schema if not exists agents;
grant usage on schema agents to zekam_app;

create table agents.assignment (
    id uuid primary key,
    realm_id uuid not null references core.realm(id) on delete cascade,
    project_id uuid not null,
    work_item_id uuid not null,
    plan_id uuid,
    step_id text,
    parent_assignment_id uuid,
    role text not null,
    agent_ref text not null,
    status text not null default 'ready',
    risk text not null default 'medium',
    instruction_digest text not null,
    context_manifest_digest text not null,
    assignment_digest text not null,
    created_at timestamptz not null,
    terminal_at timestamptz,
    foreign key (realm_id,project_id) references projects.project(realm_id,id),
    foreign key (realm_id,work_item_id) references work.work_item(realm_id,id),
    foreign key (realm_id,parent_assignment_id) references agents.assignment(realm_id,id),
    unique (realm_id,id),
    unique (realm_id,assignment_digest),
    check (role in ('coordinator','researcher','builder','reviewer','critic','synthesizer','verifier')),
    check ((role='coordinator' and parent_assignment_id is null)
           or (role<>'coordinator' and parent_assignment_id is not null)),
    check (status in ('ready','active','completed','failed','blocked','cancelled')),
    check (risk in ('low','medium','high','critical')),
    check (btrim(agent_ref)<>''),
    check (instruction_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (context_manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (assignment_digest ~ '^sha256:[0-9a-f]{64}$'),
    check ((status in ('completed','failed','cancelled'))=(terminal_at is not null))
);

create table agents.assignment_resource (
    realm_id uuid not null,
    assignment_id uuid not null,
    resource text not null,
    mode text not null,
    foreign key (realm_id,assignment_id) references agents.assignment(realm_id,id),
    primary key (realm_id,assignment_id,resource,mode),
    check (mode in ('read','write')),
    check (btrim(resource)<>'')
);

create table agents.invocation (
    id uuid primary key,
    realm_id uuid not null,
    assignment_id uuid not null,
    client_id text not null,
    execution_identity text not null,
    invocation_digest text not null,
    created_at timestamptz not null,
    foreign key (realm_id,assignment_id) references agents.assignment(realm_id,id),
    unique (realm_id,id),
    unique (realm_id,invocation_digest),
    check (btrim(client_id)<>'' and btrim(execution_identity)<>''),
    check (invocation_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table agents.result_receipt (
    realm_id uuid not null,
    assignment_id uuid not null,
    invocation_id uuid not null,
    envelope_digest text not null,
    created_at timestamptz not null default now(),
    foreign key (realm_id,assignment_id) references agents.assignment(realm_id,id),
    foreign key (realm_id,invocation_id) references agents.invocation(realm_id,id),
    unique (realm_id,invocation_id),
    check (envelope_digest ~ '^sha256:[0-9a-f]{64}$')
);

alter table runtime.job add column assignment_id uuid;
alter table runtime.job add constraint job_assignment_same_realm
    foreign key (realm_id,assignment_id) references agents.assignment(realm_id,id);

create function agents.enforce_assignment_parent() returns trigger
language plpgsql security invoker set search_path=pg_catalog,agents as $$
declare p record;
begin
    if new.parent_assignment_id is null then return new; end if;
    select project_id,work_item_id,role into p from agents.assignment
      where realm_id=new.realm_id and id=new.parent_assignment_id;
    if not found or p.project_id<>new.project_id or p.work_item_id<>new.work_item_id
       or p.role<>'coordinator' then
        raise exception 'child assignment exact coordinator/work binding ister' using errcode='23514';
    end if;
    return new;
end $$;
create trigger assignment_parent_check before insert on agents.assignment
for each row execute function agents.enforce_assignment_parent();

create function agents.enforce_assignment_immutability() returns trigger
language plpgsql as $$
begin
    if old.status in ('completed','failed','cancelled') then
        raise exception 'terminal assignment degistirilemez' using errcode='42501';
    end if;
    if row(old.id,old.realm_id,old.project_id,old.work_item_id,old.plan_id,old.step_id,
           old.parent_assignment_id,old.role,old.agent_ref,old.risk,old.instruction_digest,
           old.context_manifest_digest,old.assignment_digest,old.created_at)
       is distinct from
       row(new.id,new.realm_id,new.project_id,new.work_item_id,new.plan_id,new.step_id,
           new.parent_assignment_id,new.role,new.agent_ref,new.risk,new.instruction_digest,
           new.context_manifest_digest,new.assignment_digest,new.created_at) then
       raise exception 'assignment identity degistirilemez' using errcode='42501';
    end if;
    return new;
end $$;
create trigger assignment_immutability before update on agents.assignment
for each row execute function agents.enforce_assignment_immutability();

create function agents.enforce_write_ownership() returns trigger
language plpgsql security invoker set search_path=pg_catalog,agents,runtime as $$
declare owner record;
begin
    if new.mode<>'write' then return new; end if;
    -- Realm icindeki ownership kararlarini transaction boyunca siraya alir;
    -- iki paralel INSERT ayni MVCC snapshot'inda birbirini kaciramaz.
    perform pg_advisory_xact_lock(hashtextextended(new.realm_id::text,0));
    if (select role from agents.assignment where realm_id=new.realm_id and id=new.assignment_id)<>'builder' then
        raise exception 'yalniz builder write resource sahibi olabilir' using errcode='23514';
    end if;
    select ar.resource,a.id into owner
    from agents.assignment_resource ar join agents.assignment a
      on a.realm_id=ar.realm_id and a.id=ar.assignment_id
    where ar.realm_id=new.realm_id and ar.mode='write' and ar.assignment_id<>new.assignment_id
      and a.status in ('ready','active','blocked')
      and runtime.locks_conflict(new.resource,'write',ar.resource,'write') limit 1;
    if found then
      raise exception 'aktif builder write ownership catismasi: % / %',new.resource,owner.resource
        using errcode='55P03';
    end if;
    return new;
end $$;
create trigger assignment_resource_owner before insert on agents.assignment_resource
for each row execute function agents.enforce_write_ownership();

create function agents.enforce_runtime_lock_assignment() returns trigger
language plpgsql security invoker set search_path=pg_catalog,agents,runtime as $$
declare aid uuid;
begin
    select assignment_id into aid from runtime.job where realm_id=new.realm_id and id=new.job_id;
    if aid is null then return new; end if; -- legacy/non-agent jobs remain compatible
    if not exists (select 1 from agents.assignment_resource ar
                   where ar.realm_id=new.realm_id and ar.assignment_id=aid
                     and ar.resource=new.resource and ar.mode=new.mode) then
      raise exception 'runtime lock assignment tarafindan exact declare edilmedi' using errcode='42501';
    end if;
    return new;
end $$;
create trigger resource_lock_assignment_check before insert or update on runtime.resource_lock
for each row execute function agents.enforce_runtime_lock_assignment();
create trigger resource_lock_identity_no_update before update on runtime.resource_lock
for each statement execute function core.deny_mutation();

create function agents.enforce_result_binding() returns trigger
language plpgsql security invoker set search_path=pg_catalog,agents as $$
declare arole text; declare invocation_assignment uuid;
begin
    select role into arole from agents.assignment where realm_id=new.realm_id and id=new.assignment_id;
    select assignment_id into invocation_assignment from agents.invocation
      where realm_id=new.realm_id and id=new.invocation_id;
    if arole='coordinator' then raise exception 'koordinator child sonucu uretemez' using errcode='23514'; end if;
    if invocation_assignment is distinct from new.assignment_id then
      raise exception 'invocation/result assignment uyusmuyor' using errcode='23514';
    end if;
    if not agents.verifier_gate_satisfied(new.realm_id,new.assignment_id) then
      raise exception 'yuksek riskli assignment bagimsiz verifier ister' using errcode='23514';
    end if;
    return new;
end $$;
create trigger result_binding_check before insert on agents.result_receipt
for each row execute function agents.enforce_result_binding();

create function agents.verifier_gate_satisfied(p_realm uuid,p_assignment uuid) returns boolean
language sql stable security invoker set search_path=pg_catalog,agents as $$
 select case when a.risk not in ('high','critical') then true else exists (
   select 1 from agents.assignment v
   where v.realm_id=a.realm_id and v.parent_assignment_id=a.parent_assignment_id
     and v.role='verifier' and v.agent_ref<>a.agent_ref
     and exists (select 1 from agents.invocation vi where vi.realm_id=v.realm_id and vi.assignment_id=v.id)
     and not exists (
       select 1 from agents.invocation bi join agents.invocation vi
         on vi.realm_id=bi.realm_id and vi.assignment_id=v.id
       where bi.realm_id=a.realm_id and bi.assignment_id=a.id
         and bi.execution_identity=vi.execution_identity))
 end from agents.assignment a where a.realm_id=p_realm and a.id=p_assignment
$$;

create trigger assignment_no_delete before delete on agents.assignment
for each statement execute function core.deny_mutation();
create trigger assignment_resource_no_update before update on agents.assignment_resource
for each statement execute function core.deny_mutation();
create trigger assignment_resource_no_delete before delete on agents.assignment_resource
for each statement execute function core.deny_mutation();
create trigger invocation_no_update before update or delete on agents.invocation
for each statement execute function core.deny_mutation();
create trigger result_no_update before update or delete on agents.result_receipt
for each statement execute function core.deny_mutation();

do $$ declare target text; begin foreach target in array array[
 'agents.assignment','agents.assignment_resource','agents.invocation','agents.result_receipt'
] loop
 execute format('alter table %s enable row level security',target);
 execute format('alter table %s force row level security',target);
 execute format('create policy scope_select on %s for select using (realm_id=core.current_realm_id())',target);
 execute format('create policy scope_insert on %s for insert with check (realm_id=core.current_realm_id())',target);
end loop; end $$;
create policy scope_update on agents.assignment for update using (realm_id=core.current_realm_id())
 with check (realm_id=core.current_realm_id());

grant select,insert,update on agents.assignment to zekam_app;
grant select,insert on agents.assignment_resource,agents.invocation,agents.result_receipt to zekam_app;
grant execute on function agents.verifier_gate_satisfied(uuid,uuid) to zekam_app;

create function agents.enforce_job_assignment_immutability() returns trigger
language plpgsql as $$
begin
  if old.assignment_id is distinct from new.assignment_id then
    raise exception 'job assignment kimligi degistirilemez' using errcode='42501';
  end if;
  return new;
end $$;
create trigger job_assignment_immutability before update of assignment_id on runtime.job
for each row execute function agents.enforce_job_assignment_immutability();
