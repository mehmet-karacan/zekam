-- Append-only revision ve event altyapisi.
--
-- Kanonik durum degisiklikleri iki kayitla temsil edilir:
--
--   core.revision  : bir varligin belirli bir andaki tam durumu (immutable)
--   core.event     : olan biteni anlatan, sirali ve immutable olay kaydi
--
-- Iki tablo da append-only'dir: UPDATE ve DELETE trigger ile reddedilir.
-- Revision'lar varlik basina hash zinciri olusturur; araya kayit sikistirilamaz.
--
-- Geri alma: 0002_append_only_revision_event.down.sql

-- 1. Mutasyon reddi ---------------------------------------------------------------

create or replace function core.deny_mutation()
returns trigger
language plpgsql
as $$
begin
    raise exception 'append-only tablo: % islemi reddedildi (%.%)',
        tg_op, tg_table_schema, tg_table_name
        using errcode = '42501';
end;
$$;

comment on function core.deny_mutation() is
    'Append-only tablolarda UPDATE/DELETE islemlerini fail-closed reddeder.';

-- 2. Revision --------------------------------------------------------------------

create table core.revision (
    id               uuid        primary key,
    realm_id         uuid        not null references core.realm (id) on delete restrict,
    entity_type      text        not null,
    entity_id        uuid        not null,
    revision         integer     not null,
    payload          jsonb       not null,
    payload_digest   text        not null,
    previous_digest  text,
    reason           text        not null,
    actor_id         uuid,
    recorded_at      timestamptz not null default now(),
    constraint revision_unique_per_entity unique (realm_id, entity_type, entity_id, revision),
    constraint revision_positive check (revision >= 1),
    constraint revision_entity_type_format check (entity_type ~ '^[a-z][a-z0-9_.]*$'),
    constraint revision_reason_not_blank check (btrim(reason) <> ''),
    constraint revision_digest_format check (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint revision_previous_digest_format
        check (previous_digest is null or previous_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint revision_first_has_no_previous
        check ((revision = 1) = (previous_digest is null)),
    constraint revision_actor_same_realm
        foreign key (realm_id, actor_id) references core.actor (realm_id, id)
        on delete restrict
);

comment on table core.revision is
    'Varlik durumlarinin immutable zinciri. Guncelleme yeni revision uretir.';
comment on constraint revision_actor_same_realm on core.revision is
    'Bilesik FK cross-realm actor referansini veritabani seviyesinde reddeder.';

create index revision_entity_idx
    on core.revision (realm_id, entity_type, entity_id, revision desc);
create index revision_recorded_at_idx on core.revision (realm_id, recorded_at desc);
create index revision_payload_idx on core.revision using gin (payload jsonb_path_ops);

-- 3. Zincir butunlugu --------------------------------------------------------------

create or replace function core.enforce_revision_chain()
returns trigger
language plpgsql
as $$
declare
    previous core.revision%rowtype;
begin
    select * into previous
    from core.revision
    where realm_id = new.realm_id
      and entity_type = new.entity_type
      and entity_id = new.entity_id
    order by revision desc
    limit 1;

    if previous.id is null then
        if new.revision <> 1 then
            raise exception 'ilk revision 1 olmali, % verildi', new.revision
                using errcode = '23514';
        end if;
        return new;
    end if;

    if new.revision <> previous.revision + 1 then
        raise exception 'revision bosluksuz artmali: beklenen %, verilen %',
            previous.revision + 1, new.revision
            using errcode = '23514';
    end if;

    if new.previous_digest is distinct from previous.payload_digest then
        raise exception 'revision zinciri kopuk: previous_digest onceki payload_digest ile eslesmiyor'
            using errcode = '23514';
    end if;

    return new;
end;
$$;

create trigger revision_chain_check
    before insert on core.revision
    for each row execute function core.enforce_revision_chain();

create trigger revision_deny_update
    before update on core.revision
    for each statement execute function core.deny_mutation();

create trigger revision_deny_delete
    before delete on core.revision
    for each statement execute function core.deny_mutation();

-- 4. Event -------------------------------------------------------------------------

create table core.event (
    id              uuid        primary key,
    realm_id        uuid        not null references core.realm (id) on delete restrict,
    sequence        bigint      generated always as identity,
    event_type      text        not null,
    entity_type     text        not null,
    entity_id       uuid        not null,
    revision_id     uuid        references core.revision (id) on delete restrict,
    payload         jsonb       not null default '{}'::jsonb,
    payload_digest  text        not null,
    correlation_id  uuid,
    causation_id    uuid,
    actor_id        uuid,
    occurred_at     timestamptz not null,
    recorded_at     timestamptz not null default now(),
    constraint event_type_format check (event_type ~ '^[a-z][a-z0-9_.]*$'),
    constraint event_entity_type_format check (entity_type ~ '^[a-z][a-z0-9_.]*$'),
    constraint event_digest_format check (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint event_actor_same_realm
        foreign key (realm_id, actor_id) references core.actor (realm_id, id)
        on delete restrict
);

comment on table core.event is
    'Immutable olay kaydi. Projection ve rapor bu kayittan turetilir; authority Work Graph''tir.';

create unique index event_sequence_idx on core.event (sequence);
create index event_entity_idx on core.event (realm_id, entity_type, entity_id, sequence);
create index event_type_idx on core.event (realm_id, event_type, sequence desc);
create index event_correlation_idx on core.event (realm_id, correlation_id)
    where correlation_id is not null;

create trigger event_deny_update
    before update on core.event
    for each statement execute function core.deny_mutation();

create trigger event_deny_delete
    before delete on core.event
    for each statement execute function core.deny_mutation();

-- 5. Row-level security --------------------------------------------------------------

alter table core.revision enable row level security;
alter table core.revision force row level security;

create policy revision_scope_select on core.revision
    for select using (realm_id = core.current_realm_id());
create policy revision_scope_insert on core.revision
    for insert with check (realm_id = core.current_realm_id());

alter table core.event enable row level security;
alter table core.event force row level security;

create policy event_scope_select on core.event
    for select using (realm_id = core.current_realm_id());
create policy event_scope_insert on core.event
    for insert with check (realm_id = core.current_realm_id());

-- 6. Yetkiler -------------------------------------------------------------------------

grant select, insert on core.revision, core.event to zekam_app;
