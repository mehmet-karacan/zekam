-- Durable scheduler tanimi, calisma kaydi, gelen belge ve gunluk rapor.

create table ops.job_definition (
    id uuid primary key,
    realm_id uuid not null,
    job_name text not null,
    interval_spec text not null,
    timezone text not null default 'UTC',
    state text not null default 'active',
    misfire text not null default 'run-once',
    overlap text not null default 'skip',
    payload_digest text,
    last_run_at timestamptz,
    created_at timestamptz not null,
    constraint job_definition_unique unique (realm_id, job_name),
    constraint job_definition_realm_scoped_key unique (realm_id, id),
    constraint job_definition_state check (state in ('active', 'paused', 'cancelled')),
    constraint job_definition_misfire check (misfire in ('run-once', 'skip-visible')),
    constraint job_definition_overlap check (overlap in ('skip', 'queue')),
    constraint job_definition_interval check (interval_spec ~ '^[1-9][0-9]*[mhd]$')
);

create table ops.job_run (
    id uuid primary key,
    realm_id uuid not null,
    definition_id uuid not null,
    idempotency_key text not null,
    scheduled_for timestamptz not null,
    started_at timestamptz,
    finished_at timestamptz,
    state text not null default 'pending',
    missed_count integer not null default 0,
    detail text,
    constraint job_run_definition_same_realm
        foreign key (realm_id, definition_id) references ops.job_definition (realm_id, id)
        on delete cascade,
    -- Ayni tetikleme iki kez is uretmez.
    constraint job_run_idempotent unique (realm_id, idempotency_key),
    constraint job_run_realm_scoped_key unique (realm_id, id),
    constraint job_run_state check (
        state in ('pending', 'running', 'succeeded', 'failed', 'skipped', 'recovery-required')
    ),
    constraint job_run_missed check (missed_count >= 0),
    constraint job_run_finish_pairing check (
        state in ('pending', 'running') or finished_at is not null
    )
);

-- Bir tanimin ayni anda yalniz bir calismasi surebilir (overlap = skip).
create unique index job_run_single_active_idx
    on ops.job_run (realm_id, definition_id)
    where state in ('pending', 'running');

create table ops.incoming_document (
    id uuid primary key,
    realm_id uuid not null,
    relative_path text not null,
    content_digest text not null,
    byte_size bigint not null,
    decision text not null,
    target text,
    detail text not null,
    observed_at timestamptz not null,
    -- Ayni icerik ikinci kez islenmez.
    constraint incoming_digest_unique unique (realm_id, content_digest),
    constraint incoming_size check (byte_size > 0),
    constraint incoming_decision check (
        decision in ('accepted', 'duplicate', 'unstable', 'choice-required', 'rejected')
    ),
    constraint incoming_accepted_needs_target check (decision <> 'accepted' or target is not null),
    constraint incoming_path_portable check (
        relative_path !~ '^([a-zA-Z]:|/|\\)' and relative_path !~ '(^|/)\.\.(/|$)'
    )
);

create table ops.daily_report (
    id uuid primary key,
    realm_id uuid not null,
    scope text not null,
    report_date date not null,
    sections jsonb not null,
    report_digest text not null,
    grants_authority boolean not null default false,
    generated_at timestamptz not null,
    constraint report_unique unique (realm_id, scope, report_date),
    constraint report_digest_unique unique (realm_id, report_digest),
    constraint report_sections_object check (jsonb_typeof(sections) = 'object'),
    constraint report_no_authority check (grants_authority = false),
    -- Zorunlu bolumler eksikse rapor kaydedilmez.
    constraint report_required_sections check (
        sections ?& array[
            'tamamlanan-isler', 'aktif-lease-ve-recovery', 'subagent-model-dagilimi',
            'okunan-kaynaklar', 'token-cost-latency-quota', 'model-health-benchmark',
            'memory-skill-adaylari', 'retrieval-index-sorunlari',
            'security-policy-olaylari', 'onerilen-next-actions'
        ]
    )
);

create table ops.scheduler_incident (
    id uuid primary key,
    realm_id uuid not null,
    job_name text not null,
    kind text not null,
    detail text not null,
    next_safe_action text not null,
    created_at timestamptz not null,
    constraint incident_kind check (kind in ('misfire', 'overlap', 'failure', 'recovery-required')),
    constraint incident_next_action check (length(btrim(next_safe_action)) > 0)
);

do $$
declare target text;
begin
    foreach target in array array[
        'ops.job_definition', 'ops.job_run', 'ops.incoming_document',
        'ops.daily_report', 'ops.scheduler_incident'
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

-- Tanim ve calisma durumu ilerler; belge, rapor ve olay degismez.
create policy scope_update on ops.job_definition for update
    using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_update on ops.job_run for update
    using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());

create trigger deny_update before update on ops.incoming_document
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on ops.incoming_document
    for each statement execute function core.deny_mutation();
create trigger deny_update before update on ops.daily_report
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on ops.daily_report
    for each statement execute function core.deny_mutation();
create trigger deny_update before update on ops.scheduler_incident
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on ops.scheduler_incident
    for each statement execute function core.deny_mutation();

create index job_run_definition_idx on ops.job_run (realm_id, definition_id, scheduled_for desc);
create index incoming_decision_idx on ops.incoming_document (realm_id, decision, observed_at desc);
create index report_scope_idx on ops.daily_report (realm_id, scope, report_date desc);

grant select, insert on ops.job_definition, ops.job_run, ops.incoming_document,
    ops.daily_report, ops.scheduler_incident to zekam_app;
grant update (state, last_run_at) on ops.job_definition to zekam_app;
grant update (state, started_at, finished_at, detail) on ops.job_run to zekam_app;
