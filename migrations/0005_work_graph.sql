-- Work Graph: Work Item, iliski grafi, Intent, Decision ve Task Plan.
--
-- Kanonik gercek burasidir. Vector, FTS, dashboard veya Markdown projection bu
-- kayitlarin yerine gecemez.
--
-- Tasarim:
--   * `work.work_item` mutable "head" satiridir ve optimistic concurrency icin
--     `revision` tasir. Tam tarihce `core.revision` hash zincirinde durur.
--   * Iliskiler ayni proje icinde olmak zorundadir (bilesik FK).
--   * `depends-on` ve `parent-of` iliskileri asiklik trigger'i ile korunur.
--   * Intent, Decision ve Task Plan append-only revision kayitlaridir.
--   * `completed` durumu acceptance evidence olmadan yazilamaz (DB constraint).
--
-- Geri alma: 0005_work_graph.down.sql

-- 1. Work Item ---------------------------------------------------------------------

create table work.work_item (
    id                   uuid        primary key,
    realm_id             uuid        not null,
    project_id           uuid        not null,
    external_number      text,
    type                 text        not null,
    state                text        not null default 'proposed',
    title                text        not null,
    summary              text        not null default '',
    revision             integer     not null default 1,
    acceptance_criteria  jsonb       not null default '[]'::jsonb,
    acceptance_evidence  jsonb       not null default '[]'::jsonb,
    record_digest        text        not null,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    constraint work_item_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint work_item_realm_scoped_key unique (realm_id, id),
    constraint work_item_project_scoped_key unique (realm_id, project_id, id),
    constraint work_item_type_allowed check (
        type in ('request', 'defect', 'task', 'subtask', 'decision', 'research',
                 'idea', 'maintenance')
    ),
    constraint work_item_state_allowed check (
        state in ('proposed', 'ready', 'active', 'blocked', 'verification',
                  'completed', 'cancelled', 'archived')
    ),
    constraint work_item_title_not_blank check (btrim(title) <> ''),
    constraint work_item_revision_positive check (revision >= 1),
    constraint work_item_digest_format check (record_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint work_item_acceptance_criteria_is_array
        check (jsonb_typeof(acceptance_criteria) = 'array'),
    constraint work_item_acceptance_evidence_is_array
        check (jsonb_typeof(acceptance_evidence) = 'array'),
    constraint work_item_completed_requires_evidence
        check (state <> 'completed' or jsonb_array_length(acceptance_evidence) > 0),
    constraint work_item_external_number_format
        check (external_number is null or external_number ~ '^[A-Za-z0-9._-]{1,64}$')
);

comment on table work.work_item is
    'Kanonik is kaydi. Tam tarihce core.revision zincirindedir; bu satir head projeksiyonudur.';
comment on constraint work_item_completed_requires_evidence on work.work_item is
    'Acceptance evidence olmadan completed yazilamaz. Markdown checklist kanit degildir.';

create unique index work_item_external_number_idx
    on work.work_item (realm_id, project_id, external_number)
    where external_number is not null;

create index work_item_state_idx on work.work_item (realm_id, project_id, state);
create index work_item_type_idx on work.work_item (realm_id, project_id, type);
create index work_item_updated_idx on work.work_item (realm_id, updated_at desc);
create index work_item_title_trgm_idx on work.work_item using gin (title gin_trgm_ops);

create trigger work_item_touch_updated_at
    before update on work.work_item
    for each row execute function core.touch_updated_at();

-- 2. Iliski grafi --------------------------------------------------------------------

create table work.work_relation (
    id          uuid        primary key,
    realm_id    uuid        not null,
    project_id  uuid        not null,
    source_id   uuid        not null,
    target_id   uuid        not null,
    kind        text        not null,
    created_at  timestamptz not null default now(),
    constraint relation_source_same_project
        foreign key (realm_id, project_id, source_id)
        references work.work_item (realm_id, project_id, id) on delete cascade,
    constraint relation_target_same_project
        foreign key (realm_id, project_id, target_id)
        references work.work_item (realm_id, project_id, id) on delete cascade,
    constraint relation_kind_allowed check (
        kind in ('depends-on', 'blocks', 'parent-of', 'duplicates', 'relates-to', 'supersedes')
    ),
    constraint relation_no_self_reference check (source_id <> target_id),
    constraint relation_unique unique (realm_id, source_id, target_id, kind)
);

comment on table work.work_relation is
    'Is iliskileri. Cross-project iliski bilesik FK ile veritabani seviyesinde reddedilir.';

create index relation_source_idx on work.work_relation (realm_id, source_id, kind);
create index relation_target_idx on work.work_relation (realm_id, target_id, kind);

-- Asiklik: yalnizca yon tasiyan iliskiler icin.
create or replace function work.enforce_acyclic_relation()
returns trigger
language plpgsql
as $$
declare
    cyclic boolean;
begin
    if new.kind not in ('depends-on', 'parent-of') then
        return new;
    end if;

    with recursive reachable(node) as (
        select new.target_id
        union
        select r.target_id
        from work.work_relation r
        join reachable on r.source_id = reachable.node
        where r.realm_id = new.realm_id
          and r.kind = new.kind
    )
    select exists (select 1 from reachable where node = new.source_id) into cyclic;

    if cyclic then
        raise exception 'iliski dongusu reddedildi: % -> % (%)',
            new.source_id, new.target_id, new.kind
            using errcode = '23514';
    end if;
    return new;
end;
$$;

comment on function work.enforce_acyclic_relation() is
    'depends-on ve parent-of graflarinin acyclic kalmasini garanti eder.';

create trigger relation_acyclic_check
    before insert on work.work_relation
    for each row execute function work.enforce_acyclic_relation();

-- 3. Intent ---------------------------------------------------------------------------

create table work.intent (
    id            uuid        primary key,
    realm_id      uuid        not null,
    work_item_id  uuid        not null,
    revision      integer     not null,
    goal          text        not null,
    non_goals     jsonb       not null default '[]'::jsonb,
    outcomes      jsonb       not null default '[]'::jsonb,
    constraints   jsonb       not null default '[]'::jsonb,
    intent_digest text        not null,
    created_at    timestamptz not null default now(),
    constraint intent_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint intent_revision_unique unique (realm_id, work_item_id, revision),
    constraint intent_revision_positive check (revision >= 1),
    constraint intent_goal_not_blank check (btrim(goal) <> ''),
    constraint intent_digest_format check (intent_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint intent_non_goals_is_array check (jsonb_typeof(non_goals) = 'array'),
    constraint intent_outcomes_is_array check (jsonb_typeof(outcomes) = 'array'),
    constraint intent_constraints_is_array check (jsonb_typeof(constraints) = 'array')
);

comment on table work.intent is
    'Isin amaci, kapsam disi maddeleri, beklenen sonuclari ve kisitlari. Append-only.';

create index intent_current_idx on work.intent (realm_id, work_item_id, revision desc);

create trigger intent_deny_update
    before update on work.intent
    for each statement execute function core.deny_mutation();
create trigger intent_deny_delete
    before delete on work.intent
    for each statement execute function core.deny_mutation();

-- 4. Decision ---------------------------------------------------------------------------

create table work.decision (
    id              uuid        primary key,
    realm_id        uuid        not null,
    work_item_id    uuid        not null,
    revision        integer     not null,
    question        text        not null,
    chosen_option   text        not null,
    alternatives    jsonb       not null default '[]'::jsonb,
    criteria        jsonb       not null default '[]'::jsonb,
    rationale       text        not null,
    evidence        jsonb       not null default '[]'::jsonb,
    decision_digest text        not null,
    decided_at      timestamptz not null default now(),
    constraint decision_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint decision_revision_unique unique (realm_id, work_item_id, revision),
    constraint decision_revision_positive check (revision >= 1),
    constraint decision_question_not_blank check (btrim(question) <> ''),
    constraint decision_chosen_not_blank check (btrim(chosen_option) <> ''),
    constraint decision_rationale_not_blank check (btrim(rationale) <> ''),
    constraint decision_digest_format check (decision_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint decision_alternatives_is_array check (jsonb_typeof(alternatives) = 'array'),
    constraint decision_criteria_is_array check (jsonb_typeof(criteria) = 'array'),
    constraint decision_evidence_is_array check (jsonb_typeof(evidence) = 'array')
);

comment on table work.decision is
    'Karar kaydi: soru, secilen secenek, alternatifler, kriterler, gerekce ve kanit. Append-only.';

create index decision_current_idx on work.decision (realm_id, work_item_id, revision desc);

create trigger decision_deny_update
    before update on work.decision
    for each statement execute function core.deny_mutation();
create trigger decision_deny_delete
    before delete on work.decision
    for each statement execute function core.deny_mutation();

-- 5. Task Plan ----------------------------------------------------------------------------

create table work.task_plan (
    id               uuid        primary key,
    realm_id         uuid        not null,
    project_id       uuid        not null,
    work_item_id     uuid        not null,
    revision         integer     not null,
    source_revision  text        not null,
    policy_digest    text        not null,
    steps            jsonb       not null,
    effect_digest    text        not null,
    plan_digest      text        not null,
    grants_authority boolean     not null default false,
    created_at       timestamptz not null default now(),
    constraint plan_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint plan_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint plan_revision_unique unique (realm_id, work_item_id, revision),
    constraint plan_revision_positive check (revision >= 1),
    constraint plan_steps_is_array check (jsonb_typeof(steps) = 'array'),
    constraint plan_steps_not_empty check (jsonb_array_length(steps) > 0),
    constraint plan_source_revision_not_blank check (btrim(source_revision) <> ''),
    constraint plan_policy_digest_format check (policy_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint plan_effect_digest_format check (effect_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint plan_digest_format check (plan_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint plan_grants_no_authority check (grants_authority = false)
);

comment on table work.task_plan is
    'Exact adim planlari. Plan yetki vermez; authorization ayri kayittir.';
comment on constraint plan_grants_no_authority on work.task_plan is
    'Plan kaydinin kendisi hicbir kosulda authority tasimaz.';

create index plan_current_idx on work.task_plan (realm_id, work_item_id, revision desc);
create index plan_effect_idx on work.task_plan (realm_id, effect_digest);

create trigger plan_deny_update
    before update on work.task_plan
    for each statement execute function core.deny_mutation();
create trigger plan_deny_delete
    before delete on work.task_plan
    for each statement execute function core.deny_mutation();

-- 6. Row-level security ---------------------------------------------------------------------

do $$
declare
    target text;
begin
    foreach target in array array[
        'work.work_item',
        'work.work_relation',
        'work.intent',
        'work.decision',
        'work.task_plan'
    ]
    loop
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

create policy scope_update on work.work_item
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_delete on work.work_item
    for delete using (realm_id = core.current_realm_id());

create policy scope_delete on work.work_relation
    for delete using (realm_id = core.current_realm_id());

-- 7. Yetkiler --------------------------------------------------------------------------------

grant select, insert, update, delete on work.work_item to zekam_app;
grant select, insert, delete on work.work_relation to zekam_app;
grant select, insert on work.intent, work.decision, work.task_plan to zekam_app;
