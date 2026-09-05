-- Execution Plane: durable queue, attempt, lease, logical lock, outbox,
-- effect claim ve receipt.
--
-- Tasarim:
--   * `runtime.job` mutable lifecycle head'dir; attempt/claim/receipt/event
--     append-only kanittir.
--   * Claim secimi yalnizca kuyruk sorgusunda `for update skip locked` kullanir.
--   * Fencing token her claim'de artar; eski sahip sonuc yayimlayamaz.
--   * Logical lock catismalari veritabani trigger'i ile uygulanir.
--   * Claim varsa ve terminal receipt yoksa is `recovery-required` olur.
--
-- Geri alma: 0007_runtime_queue.down.sql

-- 1. Job ------------------------------------------------------------------------------

create table runtime.job (
    id                     uuid        primary key,
    realm_id               uuid        not null,
    project_id             uuid        not null,
    work_item_id           uuid,
    plan_id                uuid,
    step_id                text,
    kind                   text        not null,
    state                  text        not null default 'ready',
    priority               integer     not null default 100,
    attempt_count          integer     not null default 0,
    max_attempts           integer     not null default 3,
    fencing_token          bigint      not null default 0,
    idempotency_key        text        not null,
    required_capabilities  text[]      not null default '{}',
    read_resources         text[]      not null default '{}',
    write_resources        text[]      not null default '{}',
    payload                jsonb       not null default '{}'::jsonb,
    available_at           timestamptz not null default now(),
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    constraint job_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete restrict,
    constraint job_work_same_realm
        foreign key (realm_id, work_item_id) references work.work_item (realm_id, id)
        on delete cascade,
    constraint job_realm_scoped_key unique (realm_id, id),
    constraint job_idempotency_unique unique (realm_id, idempotency_key),
    constraint job_kind_allowed
        check (kind in ('read-only', 'mutation', 'provider-call', 'verification')),
    constraint job_state_allowed check (
        state in ('ready', 'running', 'completed', 'failed', 'blocked',
                  'recovery-required', 'cancelled')
    ),
    constraint job_attempts_positive check (max_attempts >= 1 and attempt_count >= 0),
    constraint job_fencing_non_negative check (fencing_token >= 0),
    constraint job_idempotency_not_blank check (btrim(idempotency_key) <> '')
);

comment on table runtime.job is
    'Durable kuyruk basligi. Ayni idempotency key ile ikinci job olusturulamaz.';
comment on constraint job_idempotency_unique on runtime.job is
    'Yinelenen enqueue tek job uretir.';

create index job_claimable_idx on runtime.job (realm_id, state, priority, available_at, id)
    where state = 'ready';
create index job_state_idx on runtime.job (realm_id, state);
create index job_work_idx on runtime.job (realm_id, work_item_id);

create trigger job_touch_updated_at
    before update on runtime.job
    for each row execute function core.touch_updated_at();

-- 2. Attempt (append-only) ---------------------------------------------------------------

create table runtime.job_attempt (
    id                uuid        primary key,
    realm_id          uuid        not null,
    job_id            uuid        not null,
    attempt_number    integer     not null,
    fencing_token     bigint      not null,
    worker_label      text        not null,
    outcome           text,
    failure_category  text,
    result_digest     text,
    started_at        timestamptz not null default now(),
    finished_at       timestamptz,
    constraint attempt_job_same_realm
        foreign key (realm_id, job_id) references runtime.job (realm_id, id) on delete cascade,
    constraint attempt_unique unique (realm_id, job_id, attempt_number),
    constraint attempt_number_positive check (attempt_number >= 1),
    constraint attempt_outcome_allowed
        check (outcome is null or outcome in
               ('succeeded', 'failed', 'abandoned', 'recovery-required')),
    constraint attempt_result_digest_format
        check (result_digest is null or result_digest ~ '^sha256:[0-9a-f]{64}$')
);

comment on table runtime.job_attempt is
    'Her yurutme denemesi. Guncelleme yalnizca sonuc alanlarina izin verir.';

create index attempt_job_idx on runtime.job_attempt (realm_id, job_id, attempt_number desc);

create trigger attempt_deny_delete
    before delete on runtime.job_attempt
    for each statement execute function core.deny_mutation();

-- Bir denemenin baslangic kimligi degistirilemez.
create or replace function runtime.enforce_attempt_immutability()
returns trigger
language plpgsql
as $$
begin
    if old.job_id <> new.job_id
       or old.attempt_number <> new.attempt_number
       or old.fencing_token <> new.fencing_token
       or old.worker_label <> new.worker_label
       or old.started_at <> new.started_at then
        raise exception 'attempt kimligi degistirilemez' using errcode = '42501';
    end if;
    if old.outcome is not null and new.outcome is distinct from old.outcome then
        raise exception 'terminal attempt sonucu degistirilemez' using errcode = '42501';
    end if;
    return new;
end;
$$;

create trigger attempt_immutability_check
    before update on runtime.job_attempt
    for each row execute function runtime.enforce_attempt_immutability();

-- 3. Lease --------------------------------------------------------------------------------

create table runtime.lease (
    id             uuid        primary key,
    realm_id       uuid        not null,
    job_id         uuid        not null,
    attempt_id     uuid        not null,
    owner_digest   text        not null,
    fencing_token  bigint      not null,
    worker_label   text        not null,
    expires_at     timestamptz not null,
    heartbeat_at   timestamptz not null default now(),
    created_at     timestamptz not null default now(),
    constraint lease_job_same_realm
        foreign key (realm_id, job_id) references runtime.job (realm_id, id) on delete cascade,
    constraint lease_attempt_exists
        foreign key (attempt_id) references runtime.job_attempt (id) on delete cascade,
    constraint lease_job_unique unique (job_id),
    constraint lease_realm_scoped_key unique (realm_id, id),
    constraint lease_owner_digest_format check (owner_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint lease_fencing_positive check (fencing_token >= 1)
);

comment on table runtime.lease is
    'Gecici sahiplik. Lease yetki degildir; yalnizca kimin yurutugunu soyler.';
comment on column runtime.lease.owner_digest is
    'Owner token''in digest''i. Token''in kendisi hicbir zaman saklanmaz.';

create index lease_expiry_idx on runtime.lease (realm_id, expires_at);

-- 4. Logical lock ---------------------------------------------------------------------------

create table runtime.resource_lock (
    id           uuid        primary key,
    realm_id     uuid        not null,
    resource     text        not null,
    mode         text        not null,
    job_id       uuid        not null,
    lease_id     uuid,
    acquired_at  timestamptz not null default now(),
    constraint lock_job_same_realm
        foreign key (realm_id, job_id) references runtime.job (realm_id, id) on delete cascade,
    constraint lock_lease_exists
        foreign key (lease_id) references runtime.lease (id) on delete cascade,
    constraint lock_mode_allowed check (mode in ('read', 'write')),
    constraint lock_resource_no_backslash check (resource !~ '\\'),
    constraint lock_resource_no_traversal check (resource !~ '(^|/)\.\.(/|$)'),
    constraint lock_unique_per_job unique (realm_id, resource, mode, job_id)
);

comment on table runtime.resource_lock is
    'Aktif logical kilitler. Catismalar trigger ile veritabani seviyesinde reddedilir.';

create index lock_resource_idx on runtime.resource_lock (realm_id, resource);
create index lock_job_idx on runtime.resource_lock (realm_id, job_id);

-- Resource metnini parcalara ayirir.
create or replace function runtime.resource_kind(p_resource text)
returns text
language sql
immutable
as $$
    select split_part(p_resource, ':', 1);
$$;

create or replace function runtime.resource_scope(p_resource text)
returns text
language sql
immutable
as $$
    select split_part(p_resource, ':', 2);
$$;

create or replace function runtime.resource_rest(p_resource text)
returns text
language sql
immutable
as $$
    select coalesce(
        nullif(substring(p_resource from position(':' in p_resource) + 1), ''),
        ''
    );
$$;

create or replace function runtime.resource_path(p_resource text)
returns text
language sql
immutable
as $$
    select case
        when split_part(p_resource, ':', 1) = 'path'
        then substring(
            p_resource
            from length(split_part(p_resource, ':', 1))
                 + length(split_part(p_resource, ':', 2)) + 3
        )
        else null
    end;
$$;

comment on function runtime.resource_path(text) is
    'path:<proje>:<yol> bicimindeki resource icin yol kismini dondurur.';

-- Iki resource catisiyor mu?
create or replace function runtime.locks_conflict(
    p_left text, p_left_mode text, p_right text, p_right_mode text
)
returns boolean
language plpgsql
immutable
as $$
declare
    left_kind text := runtime.resource_kind(p_left);
    right_kind text := runtime.resource_kind(p_right);
    left_scope text := runtime.resource_scope(p_left);
    right_scope text := runtime.resource_scope(p_right);
    left_path text;
    right_path text;
begin
    -- Iki okuma hicbir zaman catismaz.
    if p_left_mode = 'read' and p_right_mode = 'read' then
        return false;
    end if;

    -- Global resource'lar yalnizca birebir eslesmede catisir.
    if left_kind in ('provider', 'model-benchmark', 'skill-registry')
       or right_kind in ('provider', 'model-benchmark', 'skill-registry') then
        return p_left = p_right;
    end if;

    -- Farkli proje varsayilan olarak catismaz.
    if left_scope <> right_scope then
        return false;
    end if;

    -- Proje kilidi ayni projedeki her seyle catisir.
    if left_kind = 'project' or right_kind = 'project' then
        return true;
    end if;

    if p_left = p_right then
        return true;
    end if;

    if left_kind = 'path' and right_kind = 'path' then
        left_path := runtime.resource_path(p_left);
        right_path := runtime.resource_path(p_right);
        return left_path like right_path || '/%' or right_path like left_path || '/%';
    end if;

    return false;
end;
$$;

create or replace function runtime.enforce_lock_conflict()
returns trigger
language plpgsql
as $$
declare
    blocking record;
begin
    select l.resource, l.mode, l.job_id into blocking
    from runtime.resource_lock l
    where l.realm_id = new.realm_id
      and l.job_id <> new.job_id
      and runtime.locks_conflict(new.resource, new.mode, l.resource, l.mode)
    limit 1;

    if found then
        raise exception 'kilit catismasi: % (%) ile % (%) job %',
            new.resource, new.mode, blocking.resource, blocking.mode, blocking.job_id
            using errcode = '55P03';
    end if;
    return new;
end;
$$;

comment on function runtime.enforce_lock_conflict() is
    'Ayni yazilabilir kaynakta iki job, project kilidi ve path parent/child catismasini reddeder.';

create trigger resource_lock_conflict_check
    before insert on runtime.resource_lock
    for each row execute function runtime.enforce_lock_conflict();

-- 5. Outbox ------------------------------------------------------------------------------------

create table runtime.outbox_event (
    id            uuid        primary key,
    realm_id      uuid        not null,
    job_id        uuid,
    event_type    text        not null,
    payload       jsonb       not null default '{}'::jsonb,
    created_at    timestamptz not null default now(),
    published_at  timestamptz,
    constraint outbox_job_same_realm
        foreign key (realm_id, job_id) references runtime.job (realm_id, id) on delete cascade,
    constraint outbox_event_type_format check (event_type ~ '^[a-z][a-z0-9_.-]*$')
);

comment on table runtime.outbox_event is
    'Enqueue ile ayni transaction''da yazilan olaylar. Yayimlanma ayri adimdir.';

create index outbox_unpublished_idx on runtime.outbox_event (realm_id, created_at)
    where published_at is null;

-- 6. Effect claim (append-only) --------------------------------------------------------------------

create table runtime.effect_claim (
    id                    uuid        primary key,
    realm_id              uuid        not null,
    job_id                uuid        not null,
    attempt_id            uuid        not null,
    operation             text        not null,
    effect_digest         text        not null,
    authorization_digest  text        not null,
    authorization_id      uuid,
    idempotency_key       text        not null,
    resources             jsonb       not null default '[]'::jsonb,
    execution_identity    text        not null,
    fencing_token         bigint      not null,
    adapter_digest        text        not null,
    claim_digest          text        not null,
    claimed_at            timestamptz not null default now(),
    constraint claim_job_same_realm
        foreign key (realm_id, job_id) references runtime.job (realm_id, id) on delete cascade,
    constraint claim_attempt_exists
        foreign key (attempt_id) references runtime.job_attempt (id) on delete cascade,
    constraint claim_authorization_exists
        foreign key (authorization_id) references security.authorization (id) on delete restrict,
    constraint claim_idempotency_unique unique (realm_id, idempotency_key),
    constraint claim_effect_digest_format check (effect_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint claim_authorization_digest_format
        check (authorization_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint claim_adapter_digest_format check (adapter_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint claim_digest_format check (claim_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint claim_operation_not_blank check (btrim(operation) <> '')
);

comment on table runtime.effect_claim is
    'Dis effect baslatma niyeti. Effect''in gerceklestigini kanitlamaz.';
comment on constraint claim_idempotency_unique on runtime.effect_claim is
    'Ayni exact effect icin ikinci claim olusturulamaz.';

create index claim_job_idx on runtime.effect_claim (realm_id, job_id, claimed_at desc);
create index claim_effect_idx on runtime.effect_claim (realm_id, effect_digest);

create trigger claim_deny_update
    before update on runtime.effect_claim
    for each statement execute function core.deny_mutation();
create trigger claim_deny_delete
    before delete on runtime.effect_claim
    for each statement execute function core.deny_mutation();

-- 7. Effect receipt (append-only) ----------------------------------------------------------------------

create table runtime.effect_receipt (
    id                       uuid        primary key,
    realm_id                 uuid        not null,
    claim_id                 uuid        not null,
    status                   text        not null,
    result_digest            text,
    failure_category         text,
    failure_digest           text,
    adapter_evidence_digest  text,
    token_count              integer     not null default 0,
    cost_micros              bigint      not null default 0,
    latency_ms               integer     not null default 0,
    completed_at             timestamptz not null default now(),
    constraint receipt_claim_exists
        foreign key (claim_id) references runtime.effect_claim (id) on delete restrict,
    constraint receipt_claim_unique unique (claim_id),
    constraint receipt_status_allowed check (status in ('completed', 'failed')),
    constraint receipt_completed_has_result
        check ((status = 'completed') = (result_digest is not null)),
    constraint receipt_failed_has_category
        check ((status = 'failed') = (failure_category is not null)),
    constraint receipt_result_digest_format
        check (result_digest is null or result_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint receipt_measurements_non_negative
        check (token_count >= 0 and cost_micros >= 0 and latency_ms >= 0)
);

comment on table runtime.effect_receipt is
    'Effect''in terminal sonucu. Bir claim icin en fazla bir receipt olabilir.';
comment on constraint receipt_claim_unique on runtime.effect_receipt is
    'Ayni claim icin ikinci receipt yazilamaz; replay kanonik receipt''i dondurur.';

create index receipt_status_idx on runtime.effect_receipt (realm_id, status, completed_at desc);

create trigger receipt_deny_update
    before update on runtime.effect_receipt
    for each statement execute function core.deny_mutation();
create trigger receipt_deny_delete
    before delete on runtime.effect_receipt
    for each statement execute function core.deny_mutation();

-- 8. Execution event (append-only) --------------------------------------------------------------------------

create table runtime.execution_event (
    id           uuid        primary key,
    realm_id     uuid        not null,
    job_id       uuid,
    attempt_id   uuid,
    event_type   text        not null,
    payload      jsonb       not null default '{}'::jsonb,
    occurred_at  timestamptz not null default now(),
    constraint execution_job_same_realm
        foreign key (realm_id, job_id) references runtime.job (realm_id, id) on delete cascade,
    constraint execution_event_type_format check (event_type ~ '^[a-z][a-z0-9_.-]*$')
);

create index execution_event_job_idx on runtime.execution_event (realm_id, job_id, occurred_at);

create trigger execution_event_deny_update
    before update on runtime.execution_event
    for each statement execute function core.deny_mutation();

-- 9. Recovery gorunumu -------------------------------------------------------------------------------------------

-- `security_invoker` olmadan view, sahibinin (superuser) haklariyla calisir ve
-- row-level security'yi atlar. Bu, realm yalitimini sessizce delerdi.
create or replace view runtime.claim_without_receipt
with (security_invoker = true) as
select
    c.id            as claim_id,
    c.realm_id,
    c.job_id,
    c.operation,
    c.effect_digest,
    c.claimed_at,
    j.state         as job_state
from runtime.effect_claim c
join runtime.job j on j.id = c.job_id
left join runtime.effect_receipt r on r.claim_id = c.id
where r.id is null;

comment on view runtime.claim_without_receipt is
    'Terminal receipt''i olmayan claim''ler. Bu kayitlar recovery-required demektir.';

-- 10. Row-level security ---------------------------------------------------------------------------------------------

do $$
declare
    target text;
begin
    foreach target in array array[
        'runtime.job',
        'runtime.job_attempt',
        'runtime.lease',
        'runtime.resource_lock',
        'runtime.outbox_event',
        'runtime.effect_claim',
        'runtime.effect_receipt',
        'runtime.execution_event'
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

create policy scope_update on runtime.job
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_delete on runtime.job
    for delete using (realm_id = core.current_realm_id());

create policy scope_update on runtime.job_attempt
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());

create policy scope_update on runtime.lease
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_delete on runtime.lease
    for delete using (realm_id = core.current_realm_id());

create policy scope_delete on runtime.resource_lock
    for delete using (realm_id = core.current_realm_id());

create policy scope_update on runtime.outbox_event
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());

-- 11. Yetkiler ----------------------------------------------------------------------------------------------------------

grant select, insert, update, delete on runtime.job, runtime.lease, runtime.resource_lock
to zekam_app;
grant select, insert, update on runtime.job_attempt, runtime.outbox_event to zekam_app;
grant select, insert on
    runtime.effect_claim, runtime.effect_receipt, runtime.execution_event
to zekam_app;
grant select on runtime.claim_without_receipt to zekam_app;
