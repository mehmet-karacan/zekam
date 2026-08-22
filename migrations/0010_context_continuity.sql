-- Context compiler, append-only journal, checkpoint ve authority-free handoff.

create table work.context_manifest (
    id uuid primary key,
    realm_id uuid not null,
    project_id uuid not null,
    work_item_id uuid not null,
    token_budget integer not null,
    selected jsonb not null,
    omitted jsonb not null,
    candidate_fingerprint text not null,
    manifest_digest text not null,
    grants_authority boolean not null default false,
    created_at timestamptz not null,
    constraint context_manifest_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint context_manifest_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint context_manifest_unique unique (realm_id, manifest_digest),
    constraint context_manifest_budget check (token_budget > 0),
    constraint context_manifest_json check (
        jsonb_typeof(selected) = 'array' and jsonb_typeof(omitted) = 'array'
    ),
    constraint context_manifest_no_authority check (grants_authority = false)
);

create table work.work_journal_entry (
    id uuid primary key,
    realm_id uuid not null,
    project_id uuid not null,
    work_item_id uuid not null,
    sequence integer not null,
    event_kind text not null,
    payload_digest text not null,
    previous_digest text,
    truncated boolean not null,
    entry_digest text not null,
    created_at timestamptz not null,
    constraint journal_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint journal_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint journal_sequence_unique unique (realm_id, work_item_id, sequence),
    constraint journal_entry_unique unique (realm_id, work_item_id, entry_digest),
    constraint journal_sequence_positive check (sequence > 0),
    constraint journal_previous_required check (
        (sequence = 1 and previous_digest is null)
        or (sequence > 1 and previous_digest is not null)
    )
);

create function work.enforce_journal_chain() returns trigger
language plpgsql security invoker set search_path = pg_catalog, work, core as $$
declare
    current_head text;
begin
    select entry_digest into current_head
    from work.work_journal_entry
    where realm_id = new.realm_id and work_item_id = new.work_item_id
    order by sequence desc limit 1;
    if (current_head is null and (new.sequence <> 1 or new.previous_digest is not null))
       or (current_head is not null and new.previous_digest is distinct from current_head) then
        raise exception 'work journal optimistic head mismatch' using errcode = '40001';
    end if;
    return new;
end
$$;

create trigger journal_chain_guard
    before insert on work.work_journal_entry
    for each row execute function work.enforce_journal_chain();

alter table work.task_plan
    add constraint task_plan_realm_scoped_key unique (realm_id, id);

create table work.checkpoint (
    id uuid primary key,
    checkpoint_key text not null,
    realm_id uuid not null,
    project_id uuid not null,
    work_item_id uuid not null,
    task_plan_id uuid not null,
    job_id uuid,
    source_revision text not null,
    plan_steps text[] not null,
    completed_steps text[] not null,
    pending_steps text[] not null,
    step_results jsonb not null,
    context_manifest_digest text not null,
    journal_head_digest text not null,
    next_safe_action text not null,
    checkpoint_digest text not null,
    grants_authority boolean not null default false,
    created_at timestamptz not null,
    constraint checkpoint_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint checkpoint_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint checkpoint_plan_same_realm
        foreign key (realm_id, task_plan_id) references work.task_plan (realm_id, id)
        on delete restrict,
    constraint checkpoint_job_same_realm
        foreign key (realm_id, job_id) references runtime.job (realm_id, id)
        on delete restrict,
    constraint checkpoint_unique unique (realm_id, checkpoint_digest),
    constraint checkpoint_key_unique unique (realm_id, checkpoint_key),
    constraint checkpoint_job_unique unique (realm_id, job_id),
    constraint checkpoint_partition_disjoint check (
        not (completed_steps && pending_steps)
    ),
    constraint checkpoint_results_object check (jsonb_typeof(step_results) = 'object'),
    constraint checkpoint_no_authority check (grants_authority = false)
);

create function work.enforce_checkpoint_plan_partition() returns trigger
language plpgsql security invoker set search_path = pg_catalog, work, core as $$
declare
    planned text[];
    plan_source text;
begin
    select array_agg(step->>'step_id' order by step->>'step_id'), source_revision
      into planned, plan_source
    from work.task_plan, jsonb_array_elements(steps) step
    where id = new.task_plan_id and realm_id = new.realm_id
    group by source_revision;
    if planned is null
       or plan_source <> new.source_revision
       or planned <> (
           select array_agg(value order by value)
           from unnest(new.plan_steps) value
       )
       or planned <> (
           select array_agg(value order by value)
           from unnest(new.completed_steps || new.pending_steps) value
       )
       or (select array_agg(key order by key) from jsonb_object_keys(new.step_results) key)
          is distinct from
          (select array_agg(value order by value) from unnest(new.completed_steps) value) then
        raise exception 'checkpoint exact plan/source partition mismatch' using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger checkpoint_plan_partition_guard
    before insert on work.checkpoint
    for each row execute function work.enforce_checkpoint_plan_partition();

create table work.continuity_snapshot (
    id uuid primary key,
    realm_id uuid not null,
    project_id uuid not null,
    work_item_id uuid not null,
    checkpoint_id uuid not null references work.checkpoint (id) on delete restrict,
    checkpoint_digest text not null,
    journal_head_digest text not null,
    context_manifest_digest text not null,
    source_revision text not null,
    first_reads text[] not null,
    next_safe_actions text[] not null,
    evidence_refs jsonb not null,
    snapshot_digest text not null,
    grants_authority boolean not null default false,
    carries_active_lease boolean not null default false,
    approval_inherited boolean not null default false,
    created_at timestamptz not null,
    constraint snapshot_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint snapshot_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint snapshot_unique unique (realm_id, snapshot_digest),
    constraint snapshot_evidence_array check (jsonb_typeof(evidence_refs) = 'array'),
    constraint snapshot_no_authority check (
        not grants_authority and not carries_active_lease and not approval_inherited
    )
);

create table work.finalized_handoff (
    id uuid primary key,
    realm_id uuid not null,
    project_id uuid not null,
    work_item_id uuid not null,
    snapshot_id uuid not null references work.continuity_snapshot (id) on delete restrict,
    from_client text not null,
    to_client text not null,
    from_model_ref text not null,
    to_model_ref text not null,
    snapshot_digest text not null,
    checkpoint_digest text not null,
    source_revision text not null,
    handoff_digest text not null,
    transcript_included boolean not null default false,
    grants_authority boolean not null default false,
    carries_active_lease boolean not null default false,
    approval_inherited boolean not null default false,
    reacquire_required boolean not null default true,
    created_at timestamptz not null,
    constraint handoff_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint handoff_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint handoff_unique unique (realm_id, handoff_digest),
    constraint handoff_no_authority check (
        not transcript_included and not grants_authority and not carries_active_lease
        and not approval_inherited and reacquire_required
    )
);

create function work.require_meaningful_job_checkpoint() returns trigger
language plpgsql security invoker set search_path = pg_catalog, work, runtime, core as $$
begin
    if new.state = 'completed'
       and old.state is distinct from 'completed'
       and (
           coalesce(new.payload->>'meaningful_step', 'false') = 'true'
           or (new.work_item_id is not null and new.plan_id is not null and new.step_id is not null)
       )
       and not exists (
           select 1 from work.checkpoint c
           where c.realm_id = new.realm_id and c.job_id = new.id
       ) then
        raise exception 'meaningful terminal job requires checkpoint' using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger meaningful_job_checkpoint_guard
    before update of state on runtime.job
    for each row execute function work.require_meaningful_job_checkpoint();

do $$
declare target text;
begin
    foreach target in array array[
        'work.context_manifest', 'work.work_journal_entry', 'work.checkpoint',
        'work.continuity_snapshot', 'work.finalized_handoff'
    ] loop
        execute format('alter table %s enable row level security', target);
        execute format('alter table %s force row level security', target);
        execute format(
            'create policy scope_select on %s for select using (realm_id = core.current_realm_id())',
            target
        );
        execute format(
            'create policy scope_insert on %s for insert with check (realm_id = core.current_realm_id())',
            target
        );
        execute format(
            'create trigger deny_update before update on %s '
            'for each statement execute function core.deny_mutation()', target
        );
        execute format(
            'create trigger deny_delete before delete on %s '
            'for each statement execute function core.deny_mutation()', target
        );
    end loop;
end
$$;

grant select, insert on work.context_manifest, work.work_journal_entry, work.checkpoint,
    work.continuity_snapshot, work.finalized_handoff to zekam_app;
grant execute on function work.enforce_journal_chain() to zekam_app;
grant execute on function work.enforce_checkpoint_plan_partition() to zekam_app;
grant execute on function work.require_meaningful_job_checkpoint() to zekam_app;
