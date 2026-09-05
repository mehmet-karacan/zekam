-- Dogal dil intake cozumu, kanitli arastirma ve authority-free plan candidate.

create table research.intake_resolution (
    id uuid primary key,
    realm_id uuid not null,
    project_id uuid,
    request_class text not null,
    request_digest text not null,
    resolution_digest text not null,
    exact_identifiers jsonb not null,
    project_candidates jsonb not null,
    ambiguities jsonb not null,
    subject_used text,
    anaphora_present boolean not null,
    grants_authority boolean not null default false,
    created_at timestamptz not null,
    constraint intake_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint intake_unique unique (realm_id, resolution_digest),
    constraint intake_class check (
        request_class in ('research', 'project-change', 'status', 'idea', 'ambiguous')
    ),
    constraint intake_json check (
        jsonb_typeof(exact_identifiers) = 'array'
        and jsonb_typeof(project_candidates) = 'array'
        and jsonb_typeof(ambiguities) = 'array'
    ),
    -- Belirsizlik varsa sinif ambiguous olmak zorundadir; sessiz tahmin yok.
    constraint intake_ambiguity_visible check (
        (jsonb_array_length(ambiguities) = 0) = (request_class <> 'ambiguous')
    ),
    constraint intake_no_authority check (grants_authority = false)
);

create table research.question (
    id uuid primary key,
    realm_id uuid not null,
    project_id uuid not null,
    work_item_id uuid not null,
    question text not null,
    intent_digest text not null,
    source_revision text not null,
    source_policy jsonb not null,
    budget jsonb not null,
    question_digest text not null,
    created_at timestamptz not null,
    constraint question_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint question_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint question_unique unique (realm_id, question_digest),
    constraint question_realm_scoped_key unique (realm_id, id),
    constraint question_json check (
        jsonb_typeof(source_policy) = 'object' and jsonb_typeof(budget) = 'object'
    ),
    constraint question_budget_positive check (
        (budget->>'max_tokens')::bigint > 0
        and (budget->>'max_cost_units')::bigint > 0
        and (budget->>'max_seconds')::bigint > 0
    ),
    -- Bounded deliberation: en fazla iki tur ve on dakika.
    constraint question_bounded check (
        (budget->>'max_rounds')::int between 1 and 2
        and (budget->>'max_seconds')::int <= 600
    )
);

create table research.source_snapshot (
    id uuid primary key,
    realm_id uuid not null,
    question_id uuid not null,
    kind text not null,
    locator text not null,
    host text,
    revision text,
    content_digest text not null,
    snapshot_digest text not null,
    captured_at timestamptz not null,
    constraint snapshot_question_same_realm
        foreign key (realm_id, question_id) references research.question (realm_id, id)
        on delete cascade,
    constraint snapshot_unique unique (realm_id, snapshot_digest),
    constraint snapshot_kind check (kind in ('file', 'repository', 'https', 'import')),
    constraint snapshot_locator_relative check (
        kind not in ('file', 'repository')
        or (locator !~ '^([a-zA-Z]:|/|\\)' and locator !~ '(^|/)\.\.(/|$)')
    ),
    constraint snapshot_https_host check (
        kind <> 'https' or (host is not null and locator like 'https://%' and locator not like '%?%')
    ),
    constraint snapshot_revision_required check (
        kind not in ('repository', 'import') or revision is not null
    )
);

create table research.role_result (
    id uuid primary key,
    realm_id uuid not null,
    question_id uuid not null,
    node_id text not null,
    role text not null,
    agent_ref text not null,
    outcome text not null,
    findings jsonb not null,
    objections jsonb not null,
    blocker text,
    result_digest text not null,
    grants_authority boolean not null default false,
    created_at timestamptz not null,
    constraint role_result_question_same_realm
        foreign key (realm_id, question_id) references research.question (realm_id, id)
        on delete cascade,
    constraint role_result_unique unique (realm_id, question_id, node_id),
    constraint role_result_digest_unique unique (realm_id, result_digest),
    -- Koordinator child sayilmaz; child sonucu da uretemez.
    constraint role_result_not_coordinator check (role <> 'coordinator'),
    constraint role_result_role check (
        role in ('researcher', 'domain-reviewer', 'critic', 'synthesizer', 'citation-verifier')
    ),
    constraint role_result_outcome check (
        outcome in ('success', 'partial', 'failed', 'blocked', 'abstained', 'recovery-required')
    ),
    constraint role_result_json check (
        jsonb_typeof(findings) = 'array' and jsonb_typeof(objections) = 'array'
    ),
    constraint role_result_success_has_finding check (
        outcome <> 'success' or jsonb_array_length(findings) > 0
    ),
    constraint role_result_blocked_has_reason check (
        outcome not in ('blocked', 'recovery-required') or blocker is not null
    ),
    constraint role_result_no_authority check (grants_authority = false)
);

create table research.report (
    id uuid primary key,
    realm_id uuid not null,
    question_id uuid not null,
    status text not null,
    findings jsonb not null,
    unresolved_conflicts jsonb not null,
    non_success_results jsonb not null,
    verifier_ref text not null,
    verification jsonb not null,
    report_digest text not null,
    question_digest text not null,
    grants_authority boolean not null default false,
    created_at timestamptz not null,
    constraint report_question_same_realm
        foreign key (realm_id, question_id) references research.question (realm_id, id)
        on delete cascade,
    constraint report_unique unique (realm_id, report_digest),
    constraint report_realm_scoped_key unique (realm_id, id),
    constraint report_status check (status in ('answered', 'partial', 'abstained')),
    constraint report_json check (
        jsonb_typeof(findings) = 'array'
        and jsonb_typeof(unresolved_conflicts) = 'array'
        and jsonb_typeof(non_success_results) = 'array'
        and jsonb_typeof(verification) = 'object'
    ),
    -- Answered rapor unresolved celiski veya non-success sonuc gizleyemez.
    constraint report_answered_clean check (
        status <> 'answered'
        or (
            jsonb_array_length(findings) > 0
            and jsonb_array_length(unresolved_conflicts) = 0
            and jsonb_array_length(non_success_results) = 0
        )
    ),
    constraint report_abstained_empty check (
        status <> 'abstained' or jsonb_array_length(findings) = 0
    ),
    constraint report_no_authority check (grants_authority = false)
);

create table research.plan_candidate (
    id uuid primary key,
    realm_id uuid not null,
    report_id uuid not null,
    work_item_id uuid not null,
    source_revision text not null,
    proposed_steps jsonb not null,
    writable_resources jsonb not null,
    acceptance jsonb not null,
    rollback text not null,
    risk text not null,
    open_questions jsonb not null,
    candidate_digest text not null,
    report_digest text not null,
    requires_authorization boolean not null default true,
    approval_inherited boolean not null default false,
    grants_authority boolean not null default false,
    created_at timestamptz not null,
    constraint candidate_report_same_realm
        foreign key (realm_id, report_id) references research.report (realm_id, id)
        on delete cascade,
    constraint candidate_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint candidate_unique unique (realm_id, candidate_digest),
    constraint candidate_risk check (risk in ('low', 'medium', 'high', 'critical')),
    constraint candidate_json check (
        jsonb_typeof(proposed_steps) = 'array'
        and jsonb_typeof(writable_resources) = 'array'
        and jsonb_typeof(acceptance) = 'array'
        and jsonb_typeof(open_questions) = 'array'
    ),
    constraint candidate_not_empty check (
        jsonb_array_length(proposed_steps) > 0 and jsonb_array_length(acceptance) > 0
    ),
    -- Arastirma authority uretmez; plan candidate daima exact authorization ister.
    constraint candidate_no_authority check (
        requires_authorization and not approval_inherited and not grants_authority
    )
);

-- Rapor yazilmadan once en az bir gercek subagent sonucu bulunmalidir.
create function research.require_subagent_result() returns trigger
language plpgsql security invoker set search_path = pg_catalog, research, core as $$
begin
    if not exists (
        select 1 from research.role_result r
        where r.realm_id = new.realm_id and r.question_id = new.question_id
    ) then
        raise exception 'arastirma raporu en az bir subagent sonucu ister'
            using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger report_requires_subagent
    before insert on research.report
    for each row execute function research.require_subagent_result();

-- Citation verifier arastirmaciyla ayni kimlik olamaz.
create function research.require_independent_verifier() returns trigger
language plpgsql security invoker set search_path = pg_catalog, research, core as $$
begin
    if exists (
        select 1 from research.role_result r
        where r.realm_id = new.realm_id
          and r.question_id = new.question_id
          and r.role <> 'citation-verifier'
          and r.agent_ref = new.verifier_ref
    ) then
        raise exception 'citation verifier arastirmaciyla ayni kimlik olamaz'
            using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger report_requires_independent_verifier
    before insert on research.report
    for each row execute function research.require_independent_verifier();

-- Plan candidate yalniz answered rapordan turetilir.
create function research.require_actionable_report() returns trigger
language plpgsql security invoker set search_path = pg_catalog, research, core as $$
declare
    report_status text;
begin
    select status into report_status
    from research.report
    where realm_id = new.realm_id and id = new.report_id;
    if report_status is distinct from 'answered' then
        raise exception 'plan candidate yalniz answered rapordan turetilir'
            using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger candidate_requires_actionable_report
    before insert on research.plan_candidate
    for each row execute function research.require_actionable_report();

do $$
declare target text;
begin
    foreach target in array array[
        'research.intake_resolution', 'research.question', 'research.source_snapshot',
        'research.role_result', 'research.report', 'research.plan_candidate'
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

create index intake_resolution_project_idx
    on research.intake_resolution (realm_id, project_id, created_at desc);
create index question_work_idx on research.question (realm_id, work_item_id, created_at desc);
create index role_result_question_idx on research.role_result (realm_id, question_id);
create index report_question_idx on research.report (realm_id, question_id, created_at desc);
create index plan_candidate_work_idx
    on research.plan_candidate (realm_id, work_item_id, created_at desc);

grant select, insert on research.intake_resolution, research.question,
    research.source_snapshot, research.role_result, research.report,
    research.plan_candidate to zekam_app;
grant execute on function research.require_subagent_result() to zekam_app;
grant execute on function research.require_independent_verifier() to zekam_app;
grant execute on function research.require_actionable_report() to zekam_app;
