-- Model envanteri, health probe kayitlari, sozlesme kontrolleri ve raporlar.
--
-- Temel kural: aktif envanter kaydi ham endpoint adresi veya credential degeri
-- tasimaz. Veritabani constraint'i URL, IP, bearer ve anahtar benzeri desenleri
-- reddeder.
--
-- Geri alma: 0008_model_inventory.down.sql

-- 1. Envanter ---------------------------------------------------------------------------

create table models.model_inventory (
    id                          uuid        primary key,
    realm_id                    uuid        not null references core.realm (id) on delete restrict,
    model_id                    text        not null,
    inventory_index             integer     not null,
    access_name                 text        not null,
    backend_model               text        not null,
    provider_protocol           text        not null,
    declared_mode               text,
    declared_category           text        not null,
    modality                    text        not null,
    endpoint_ref                text        not null,
    credential_ref              text        not null,
    endpoint_scope              text,
    declared_parameter_profile  text,
    reasoning_effort            text,
    enabled                     boolean     not null default true,
    status                      text        not null default 'candidate',
    health_state                text        not null default 'untested',
    benchmark_state             text        not null default 'not-run',
    quarantine_until            timestamptz,
    capabilities_declared       text[]      not null default '{}',
    capabilities_verified       text[]      not null default '{}',
    cost_evidence               jsonb       not null default '{}'::jsonb,
    provenance                  jsonb       not null default '{}'::jsonb,
    technical_profile_available boolean     not null default false,
    inventory_digest            text        not null,
    last_health_at              timestamptz,
    last_health_policy_digest   text,
    last_health_inventory_digest text,
    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now(),
    constraint model_unique_per_realm unique (realm_id, model_id),
    constraint model_index_positive check (inventory_index >= 1),
    constraint model_digest_format check (inventory_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint model_health_state_allowed check (
        health_state in ('untested', 'health-passed', 'contract-passed', 'benchmark-eligible',
                         'project-qualified', 'active-candidate', 'quarantined', 'cooldown')
    ),
    constraint model_benchmark_state_allowed
        check (benchmark_state in ('not-run', 'running', 'passed', 'failed', 'stale')),
    -- Referanslar mantiksaldir; ham deger tasiyamaz.
    constraint model_endpoint_ref_format
        check (endpoint_ref ~ '^model-endpoint:[A-Za-z0-9._-]{1,128}$'),
    constraint model_credential_ref_format
        check (credential_ref ~ '^model-credential:[A-Za-z0-9._-]{1,128}$'),
    constraint model_refs_have_no_url
        check (endpoint_ref !~ '://' and credential_ref !~ '://'),
    constraint model_refs_have_no_ip
        check (endpoint_ref !~ '[0-9]{1,3}(\.[0-9]{1,3}){3}'
               and credential_ref !~ '[0-9]{1,3}(\.[0-9]{1,3}){3}')
);

comment on table models.model_inventory is
    'Kanonik model envanteri. Endpoint ve credential yalnizca mantiksal referanstir.';
comment on constraint model_refs_have_no_url on models.model_inventory is
    'Ham endpoint adresi envantere yazilamaz.';
comment on column models.model_inventory.technical_profile_available is
    'Teknik profil farki gizlenmez; gorunur provenance olarak korunur.';

create index model_health_idx on models.model_inventory (realm_id, health_state);
create index model_modality_idx on models.model_inventory (realm_id, modality);
create index model_backend_idx on models.model_inventory (realm_id, backend_model);

create trigger model_inventory_touch_updated_at
    before update on models.model_inventory
    for each row execute function core.touch_updated_at();

-- 2. Health probe (append-only) -----------------------------------------------------------

create table models.health_probe (
    id               uuid        primary key,
    realm_id         uuid        not null references core.realm (id) on delete restrict,
    model_id         text        not null,
    modality         text        not null,
    fixture_name     text        not null,
    fixture_digest   text        not null,
    status           text        not null,
    failure_category text,
    detail           text        not null default '',
    latency_ms       integer     not null default 0,
    response_digest  text,
    policy_digest    text        not null,
    inventory_digest text        not null,
    observed_at      timestamptz not null default now(),
    constraint probe_model_exists
        foreign key (realm_id, model_id) references models.model_inventory (realm_id, model_id)
        on delete cascade,
    constraint probe_status_allowed check (status in ('passed', 'failed', 'skipped')),
    constraint probe_failed_has_category
        check ((status = 'failed') = (failure_category is not null)),
    constraint probe_latency_non_negative check (latency_ms >= 0),
    constraint probe_fixture_digest_format check (fixture_digest ~ '^sha256:[0-9a-f]{64}$')
);

comment on table models.health_probe is
    'Sentetik probe sonuclari. Prompt ve yanit icerigi saklanmaz; yalnizca durum ve digest.';

create index probe_model_idx on models.health_probe (realm_id, model_id, observed_at desc);

create trigger probe_deny_update
    before update on models.health_probe
    for each statement execute function core.deny_mutation();
create trigger probe_deny_delete
    before delete on models.health_probe
    for each statement execute function core.deny_mutation();

-- 3. Sozlesme kontrolu (append-only) --------------------------------------------------------

create table models.capability_check (
    id               uuid        primary key,
    realm_id         uuid        not null references core.realm (id) on delete restrict,
    model_id         text        not null,
    capability       text        not null,
    verified         boolean     not null,
    evidence         text        not null,
    failure_category text,
    evidence_digest  text        not null,
    checked_at       timestamptz not null default now(),
    constraint capability_model_exists
        foreign key (realm_id, model_id) references models.model_inventory (realm_id, model_id)
        on delete cascade,
    constraint capability_evidence_not_blank check (btrim(evidence) <> ''),
    constraint capability_digest_format check (evidence_digest ~ '^sha256:[0-9a-f]{64}$')
);

comment on table models.capability_check is
    'Sozlesme dogrulamalari. Ilan edilen fakat dogrulanmayan yetenek verified=false kalir.';

create index capability_model_idx
    on models.capability_check (realm_id, model_id, capability, checked_at desc);

create trigger capability_deny_update
    before update on models.capability_check
    for each statement execute function core.deny_mutation();

-- 4. Karantina olaylari (append-only) ----------------------------------------------------------

create table models.quarantine_event (
    id                    uuid        primary key,
    realm_id              uuid        not null references core.realm (id) on delete restrict,
    model_id              text        not null,
    action                text        not null,
    reason                text        not null,
    consecutive_failures  integer     not null default 0,
    cooldown_until        timestamptz,
    policy_digest         text        not null,
    occurred_at           timestamptz not null default now(),
    constraint quarantine_model_exists
        foreign key (realm_id, model_id) references models.model_inventory (realm_id, model_id)
        on delete cascade,
    constraint quarantine_action_allowed check (action in ('quarantined', 'released', 'cooldown')),
    constraint quarantine_reason_not_blank check (btrim(reason) <> '')
);

create index quarantine_model_idx
    on models.quarantine_event (realm_id, model_id, occurred_at desc);

create trigger quarantine_deny_update
    before update on models.quarantine_event
    for each statement execute function core.deny_mutation();

-- 5. Gunluk saglik raporu (append-only) ------------------------------------------------------------

create table models.health_report (
    id               uuid        primary key,
    realm_id         uuid        not null references core.realm (id) on delete restrict,
    report_date      date        not null,
    summary          jsonb       not null,
    evidence_digest  text        not null,
    markdown_digest  text        not null,
    json_digest      text        not null,
    generated_at     timestamptz not null default now(),
    constraint report_unique_per_day unique (realm_id, report_date),
    constraint report_evidence_digest_format check (evidence_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint report_markdown_digest_format check (markdown_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint report_json_digest_format check (json_digest ~ '^sha256:[0-9a-f]{64}$')
);

comment on table models.health_report is
    'Gunluk model saglik raporu. Turkce Markdown ve JSON ayni evidence digest''ine baglanir.';

create trigger report_deny_update
    before update on models.health_report
    for each statement execute function core.deny_mutation();

-- 6. Row-level security --------------------------------------------------------------------------------

do $$
declare
    target text;
begin
    foreach target in array array[
        'models.model_inventory',
        'models.health_probe',
        'models.capability_check',
        'models.quarantine_event',
        'models.health_report'
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

create policy scope_update on models.model_inventory
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_delete on models.model_inventory
    for delete using (realm_id = core.current_realm_id());

-- 7. Yetkiler -----------------------------------------------------------------------------------------------

grant select, insert, update, delete on models.model_inventory to zekam_app;
grant select, insert on
    models.health_probe, models.capability_check, models.quarantine_event, models.health_report
to zekam_app;
