-- Failure gozlemi, ders adayi, skill yasam dongusu ve olculu dongu kaydi.

create table skills.failure_occurrence (
    id uuid primary key,
    realm_id uuid not null,
    project_id uuid,
    occurrence_key text not null,
    evidence_digest text not null,
    run_ref text not null,
    failure_category text not null,
    observed_at timestamptz not null,
    constraint occurrence_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    -- Ayni kanit iki kez sayilmaz: cift sayim burada engellenir.
    constraint occurrence_evidence_unique unique (realm_id, occurrence_key, evidence_digest),
    constraint occurrence_realm_scoped_key unique (realm_id, id)
);

create table skills.learning_candidate (
    id uuid primary key,
    realm_id uuid not null,
    occurrence_key text not null,
    target text not null,
    proposal text not null,
    author_ref text not null,
    root_cause text,
    root_cause_verified_by text,
    root_cause_digest text,
    critical boolean not null default false,
    approved boolean,
    verifier_ref text,
    decision_reason text,
    created_at timestamptz not null,
    constraint learning_realm_scoped_key unique (realm_id, id),
    constraint learning_target check (target in ('test', 'eval', 'guidance', 'skill')),
    -- Kok neden ucusu birlikte doldurulur veya hic doldurulmaz.
    constraint learning_root_cause_complete check (
        (root_cause is null and root_cause_verified_by is null and root_cause_digest is null)
        or (root_cause is not null and root_cause_verified_by is not null
            and root_cause_digest is not null)
    ),
    -- Onaylanmis ders dogrulanmis kok neden ve bagimsiz verifier ister.
    constraint learning_approved_needs_root_cause check (
        approved is not true or root_cause is not null
    ),
    constraint learning_verifier_independent check (
        verifier_ref is null or verifier_ref is distinct from author_ref
    )
);

create table skills.skill (
    id uuid primary key,
    realm_id uuid not null,
    project_id uuid,
    name text not null,
    body_digest text not null,
    state text not null,
    revision integer not null,
    author_ref text not null,
    evaluation_digest text,
    approved_by text,
    rollback_plan text,
    self_promoted boolean not null default false,
    created_at timestamptz not null,
    constraint skill_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    -- Ayni govde iki kez kaydedilmez.
    constraint skill_body_unique unique (realm_id, body_digest),
    constraint skill_realm_scoped_key unique (realm_id, id),
    constraint skill_state check (
        state in ('candidate', 'evaluated', 'active', 'deprecated', 'retired')
    ),
    constraint skill_revision check (revision > 0),
    -- Skill kendi kendini aktif registry'ye yazamaz.
    constraint skill_no_self_promotion check (self_promoted = false),
    -- Aktivasyon olcum, bagimsiz onay ve rollback plani ister.
    constraint skill_active_requires_gates check (
        state <> 'active'
        or (
            evaluation_digest is not null
            and approved_by is not null
            and approved_by is distinct from author_ref
            and rollback_plan is not null
            and length(btrim(rollback_plan)) > 0
        )
    )
);

create table skills.skill_evaluation (
    id uuid primary key,
    realm_id uuid not null,
    skill_id uuid not null,
    fixtures jsonb not null,
    trials integer not null,
    successes integer not null,
    baseline_success_rate double precision not null,
    evaluator_ref text not null,
    verifier_ref text not null,
    evaluation_digest text not null,
    created_at timestamptz not null,
    constraint evaluation_skill_same_realm
        foreign key (realm_id, skill_id) references skills.skill (realm_id, id) on delete cascade,
    constraint evaluation_unique unique (realm_id, evaluation_digest),
    constraint evaluation_fixtures_array check (jsonb_typeof(fixtures) = 'array'),
    constraint evaluation_fixtures_present check (jsonb_array_length(fixtures) > 0),
    -- En az bes deneme; basari sayisi denemeyi asamaz.
    constraint evaluation_trials check (trials >= 5 and successes between 0 and trials),
    constraint evaluation_baseline check (baseline_success_rate between 0 and 1),
    -- Degerlendiren ve dogrulayan ayni kimlik olamaz.
    constraint evaluation_independent check (evaluator_ref is distinct from verifier_ref)
);

create table skills.loop_iteration (
    id uuid primary key,
    realm_id uuid not null,
    work_item_id uuid not null,
    iteration integer not null,
    score double precision not null,
    cost_units integer not null,
    verified boolean not null,
    stop_reason text,
    created_at timestamptz not null,
    constraint iteration_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint iteration_unique unique (realm_id, work_item_id, iteration),
    constraint iteration_positive check (iteration > 0 and cost_units >= 0),
    constraint iteration_stop_reason check (
        stop_reason is null
        or stop_reason in ('goal-reached', 'iteration-budget', 'cost-budget',
                           'no-progress', 'blocked')
    )
);

do $$
declare target text;
begin
    foreach target in array array[
        'skills.failure_occurrence', 'skills.learning_candidate', 'skills.skill',
        'skills.skill_evaluation', 'skills.loop_iteration'
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
    end loop;
end
$$;

-- Ders karari ve skill durumu ilerler; gozlem ve olcum degismez.
create policy scope_update on skills.learning_candidate for update
    using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_update on skills.skill for update
    using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());

create trigger deny_update before update on skills.failure_occurrence
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on skills.failure_occurrence
    for each statement execute function core.deny_mutation();
create trigger deny_update before update on skills.skill_evaluation
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on skills.skill_evaluation
    for each statement execute function core.deny_mutation();
create trigger deny_update before update on skills.loop_iteration
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on skills.loop_iteration
    for each statement execute function core.deny_mutation();

-- Aktiflesen skill'in olcumu baseline'i gecmis olmalidir.
create function skills.require_improving_evaluation() returns trigger
language plpgsql security invoker set search_path = pg_catalog, skills, core as $$
declare
    improved boolean;
begin
    if new.state <> 'active' then
        return new;
    end if;
    select (e.successes::double precision / e.trials) > e.baseline_success_rate
      into improved
    from skills.skill_evaluation e
    where e.realm_id = new.realm_id and e.evaluation_digest = new.evaluation_digest;
    if improved is not true then
        raise exception 'baseline gecmeyen skill aktive edilemez' using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger skill_requires_improving_evaluation
    before insert or update of state on skills.skill
    for each row execute function skills.require_improving_evaluation();

create index occurrence_key_idx on skills.failure_occurrence (realm_id, occurrence_key);
create index learning_occurrence_idx on skills.learning_candidate (realm_id, occurrence_key);
create index skill_state_idx on skills.skill (realm_id, state);
create index iteration_work_idx on skills.loop_iteration (realm_id, work_item_id, iteration);

grant select, insert on skills.failure_occurrence, skills.learning_candidate,
    skills.skill, skills.skill_evaluation, skills.loop_iteration to zekam_app;
grant update (approved, verifier_ref, decision_reason, root_cause, root_cause_verified_by,
    root_cause_digest) on skills.learning_candidate to zekam_app;
grant update (state, evaluation_digest, approved_by, rollback_plan) on skills.skill to zekam_app;
grant execute on function skills.require_improving_evaluation() to zekam_app;
