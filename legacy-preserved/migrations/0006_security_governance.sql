-- Governance: policy, capability, SecretRef, exact authorization, outbound
-- disclosure ve audit ledger.
--
-- Temel kural: bu semada hicbir tablo secret **degeri** tutmaz. Yalnizca
-- referans, kapsam, amac, surum ve digest saklanir. Deger runtime'da broker
-- tarafindan cozulur ve hicbir yere yazilmaz.
--
-- Geri alma: 0006_security_governance.down.sql

-- 1. Policy ---------------------------------------------------------------------------

create table security.policy (
    id             uuid        primary key,
    realm_id       uuid        not null references core.realm (id) on delete restrict,
    name           text        not null,
    revision       integer     not null,
    document       jsonb       not null,
    policy_digest  text        not null,
    effective_from timestamptz not null default now(),
    created_at     timestamptz not null default now(),
    constraint policy_revision_unique unique (realm_id, name, revision),
    constraint policy_name_format check (name ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    constraint policy_revision_positive check (revision >= 1),
    constraint policy_document_is_object check (jsonb_typeof(document) = 'object'),
    constraint policy_digest_format check (policy_digest ~ '^sha256:[0-9a-f]{64}$')
);

comment on table security.policy is
    'Surumlu policy belgeleri. Append-only; degisiklik yeni revision uretir.';

create index policy_current_idx on security.policy (realm_id, name, revision desc);

create trigger policy_deny_update
    before update on security.policy
    for each statement execute function core.deny_mutation();
create trigger policy_deny_delete
    before delete on security.policy
    for each statement execute function core.deny_mutation();

-- 2. Capability -------------------------------------------------------------------------

create table security.capability (
    id                 uuid        primary key,
    realm_id           uuid        not null references core.realm (id) on delete restrict,
    name               text        not null,
    revision           integer     not null,
    kind               text        not null,
    description        text        not null default '',
    definition         jsonb       not null default '{}'::jsonb,
    capability_digest  text        not null,
    created_at         timestamptz not null default now(),
    constraint capability_revision_unique unique (realm_id, name, revision),
    constraint capability_name_format check (name ~ '^[a-z0-9]+([.-][a-z0-9]+)*$'),
    constraint capability_revision_positive check (revision >= 1),
    constraint capability_kind_allowed check (
        kind in ('read', 'filesystem', 'database', 'network', 'provider', 'process', 'git')
    ),
    constraint capability_digest_format check (capability_digest ~ '^sha256:[0-9a-f]{64}$')
);

comment on table security.capability is
    'Adapter/worker yeteneklerinin surumlu tanimi. Yetenek beyani yetki degildir.';

create index capability_current_idx on security.capability (realm_id, name, revision desc);

create trigger capability_deny_update
    before update on security.capability
    for each statement execute function core.deny_mutation();

-- 3. SecretRef ---------------------------------------------------------------------------

create table security.secret_ref (
    id                  uuid        primary key,
    realm_id            uuid        not null references core.realm (id) on delete restrict,
    project_id          uuid,
    name                text        not null,
    provider            text        not null,
    purpose             text        not null,
    allowed_operations  text[]      not null,
    store_backend       text        not null,
    store_locator       text        not null,
    version             integer     not null default 1,
    status              text        not null default 'active',
    expires_at          timestamptz,
    metadata_digest     text        not null,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint secret_ref_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete cascade,
    constraint secret_ref_name_unique unique (realm_id, name, version),
    constraint secret_ref_name_format check (name ~ '^[a-z0-9]+([._-][a-z0-9]+)*$'),
    constraint secret_ref_version_positive check (version >= 1),
    constraint secret_ref_status_allowed check (status in ('active', 'rotating', 'revoked')),
    constraint secret_ref_operations_not_empty check (array_length(allowed_operations, 1) >= 1),
    constraint secret_ref_purpose_not_blank check (btrim(purpose) <> ''),
    constraint secret_ref_backend_allowed
        check (store_backend in ('environment', 'os-keychain', 'vault', 'kms', 'local-encrypted')),
    constraint secret_ref_digest_format check (metadata_digest ~ '^sha256:[0-9a-f]{64}$')
);

comment on table security.secret_ref is
    'Secret metadata. Deger burada TUTULMAZ; store_locator yalnizca nerede arayacagini soyler.';
comment on column security.secret_ref.store_locator is
    'Backend icindeki mantiksal ad (ornegin ortam degiskeni adi). Degerin kendisi degildir.';

create index secret_ref_status_idx on security.secret_ref (realm_id, status);

create trigger secret_ref_touch_updated_at
    before update on security.secret_ref
    for each row execute function core.touch_updated_at();

-- 4. Exact authorization ------------------------------------------------------------------

create table security.authorization (
    id                    uuid        primary key,
    realm_id              uuid        not null references core.realm (id) on delete restrict,
    actor_id              uuid        not null,
    work_item_id          uuid,
    plan_id               uuid,
    plan_digest           text        not null,
    effect_digest         text        not null,
    scope                 jsonb       not null,
    allowed_resources     text[]      not null default '{}',
    allowed_effects       text[]      not null default '{}',
    provider_refs         text[]      not null default '{}',
    secret_ref_ids        uuid[]      not null default '{}',
    risk                  text        not null,
    state                 text        not null default 'issued',
    issued_at             timestamptz not null default now(),
    expires_at            timestamptz not null,
    consumed_at           timestamptz,
    consumed_by           text,
    revoked_at            timestamptz,
    revocation_reason     text,
    authorization_digest  text        not null,
    constraint authorization_actor_same_realm
        foreign key (realm_id, actor_id) references core.actor (realm_id, id)
        on delete restrict,
    constraint authorization_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint authorization_state_allowed
        check (state in ('issued', 'consumed', 'revoked', 'expired')),
    constraint authorization_risk_allowed
        check (risk in ('none', 'low', 'medium', 'high', 'critical')),
    constraint authorization_plan_digest_format check (plan_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint authorization_effect_digest_format check (effect_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint authorization_digest_format
        check (authorization_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint authorization_expiry_after_issue check (expires_at > issued_at),
    constraint authorization_consumed_has_time
        check ((state = 'consumed') = (consumed_at is not null)),
    constraint authorization_revoked_has_time
        check ((state = 'revoked') = (revoked_at is not null)),
    constraint authorization_effects_not_empty check (array_length(allowed_effects, 1) >= 1)
);

comment on table security.authorization is
    'Exact one-shot authorization. Generic "her seyi yap" yetkisi yoktur; her kayit tek bir '
    'plan/effect digest ciftine baglidir.';
comment on constraint authorization_expiry_after_issue on security.authorization is
    'Suresiz yetki verilemez.';

create index authorization_state_idx on security.authorization (realm_id, state, expires_at);
create index authorization_effect_idx on security.authorization (realm_id, effect_digest);
create index authorization_work_idx on security.authorization (realm_id, work_item_id);

-- Silme yasak: yetki gecmisi denetlenebilir kalmalidir.
create trigger authorization_deny_delete
    before delete on security.authorization
    for each statement execute function core.deny_mutation();

-- Terminal duruma gecen bir yetki tekrar `issued` yapilamaz.
create or replace function security.enforce_authorization_transition()
returns trigger
language plpgsql
as $$
begin
    if old.state <> 'issued' and new.state <> old.state then
        raise exception 'terminal yetki durumu degistirilemez: % -> %', old.state, new.state
            using errcode = '42501';
    end if;
    if old.plan_digest <> new.plan_digest or old.effect_digest <> new.effect_digest then
        raise exception 'yetkinin bagli oldugu digest degistirilemez' using errcode = '42501';
    end if;
    if old.allowed_resources <> new.allowed_resources
       or old.allowed_effects <> new.allowed_effects then
        raise exception 'yetki kapsami genisletilemez' using errcode = '42501';
    end if;
    return new;
end;
$$;

comment on function security.enforce_authorization_transition() is
    'Yetki kapsaminin sonradan genisletilmesini ve terminal durumun geri alinmasini engeller.';

create trigger authorization_transition_check
    before update on security.authorization
    for each row execute function security.enforce_authorization_transition();

-- 5. Outbound disclosure --------------------------------------------------------------------

create table security.outbound_request (
    id                    uuid        primary key,
    realm_id              uuid        not null references core.realm (id) on delete restrict,
    authorization_id      uuid,
    provider_ref          text        not null,
    endpoint_ref          text        not null,
    operation             text        not null,
    data_categories       text[]      not null default '{}',
    payload_digest        text        not null,
    retention_assumption  text        not null,
    region                text        not null default 'unknown',
    request_identity      text        not null,
    state                 text        not null default 'prepared',
    denial_reason         text,
    created_at            timestamptz not null default now(),
    executed_at           timestamptz,
    constraint outbound_authorization_exists
        foreign key (authorization_id) references security.authorization (id) on delete restrict,
    constraint outbound_state_allowed
        check (state in ('prepared', 'approved', 'executed', 'denied')),
    constraint outbound_payload_digest_format check (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint outbound_executed_requires_authorization
        check (state <> 'executed' or authorization_id is not null),
    constraint outbound_denied_has_reason
        check ((state = 'denied') = (denial_reason is not null))
);

comment on table security.outbound_request is
    'Disari acilan her istegin kaydi. Payload icerigi degil, digest ve veri kategorileri tutulur.';
comment on constraint outbound_executed_requires_authorization on security.outbound_request is
    'Authorization olmadan hicbir istek executed olamaz.';

create index outbound_state_idx on security.outbound_request (realm_id, state, created_at desc);

-- 6. Audit ledger -----------------------------------------------------------------------------

create table security.audit_event (
    id                uuid        primary key,
    realm_id          uuid        not null references core.realm (id) on delete restrict,
    sequence          bigint      generated always as identity,
    actor_id          uuid,
    correlation_id    uuid,
    action            text        not null,
    subject_type      text        not null,
    subject_id        text        not null,
    authorization_id  uuid,
    decision          text        not null,
    reason            text        not null,
    evidence_digest   text        not null,
    occurred_at       timestamptz not null default now(),
    constraint audit_actor_same_realm
        foreign key (realm_id, actor_id) references core.actor (realm_id, id)
        on delete restrict,
    constraint audit_authorization_exists
        foreign key (authorization_id) references security.authorization (id) on delete restrict,
    constraint audit_decision_allowed check (decision in ('allow', 'deny', 'record')),
    constraint audit_action_format check (action ~ '^[a-z][a-z0-9_.-]*$'),
    constraint audit_digest_format check (evidence_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint audit_reason_not_blank check (btrim(reason) <> '')
);

comment on table security.audit_event is
    'Append-only denetim kaydi. Her effect kararini actor, authorization ve kanit ile baglar.';

create unique index audit_sequence_idx on security.audit_event (sequence);
create index audit_subject_idx on security.audit_event (realm_id, subject_type, subject_id);
create index audit_authorization_idx on security.audit_event (realm_id, authorization_id)
    where authorization_id is not null;
create index audit_correlation_idx on security.audit_event (realm_id, correlation_id)
    where correlation_id is not null;

create trigger audit_deny_update
    before update on security.audit_event
    for each statement execute function core.deny_mutation();
create trigger audit_deny_delete
    before delete on security.audit_event
    for each statement execute function core.deny_mutation();

-- 7. Row-level security --------------------------------------------------------------------------

do $$
declare
    target text;
begin
    foreach target in array array[
        'security.policy',
        'security.capability',
        'security.secret_ref',
        'security.authorization',
        'security.outbound_request',
        'security.audit_event'
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

create policy scope_update on security.secret_ref
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_update on security.authorization
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_update on security.outbound_request
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());

-- 8. Yetkiler ------------------------------------------------------------------------------------

grant select, insert on security.policy, security.capability, security.audit_event to zekam_app;
grant select, insert, update on
    security.secret_ref, security.authorization, security.outbound_request
to zekam_app;
