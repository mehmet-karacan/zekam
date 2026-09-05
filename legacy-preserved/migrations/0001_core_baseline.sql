-- Zekam kanonik persistence baseline'i.
--
-- Bu migration bounded context schema'larini, uygulama rolunu ve realm/actor
-- kimlik tablolarini olusturur. Realm yalitimi hem foreign key hem row-level
-- security ile uygulanir.
--
-- Geri alma: 0001_core_baseline.down.sql (yalnizca exact authorization ile).

-- 1. Gerekli eklentiler -----------------------------------------------------------
--
-- Migration'lar temiz kurulumda kendi kendine yeterlidir; container initdb betigine
-- bagimli degildir. `vector` eklentisi trusted olmadigi icin migration'i uygulayan
-- rolun superuser olmasi gerekir; bu yalnizca kurulum anina ozgudur.

create extension if not exists vector;
create extension if not exists pg_trgm;
create extension if not exists btree_gin;
create extension if not exists pgcrypto;

-- 2. Bounded context schema'lari -------------------------------------------------

create schema if not exists core;
create schema if not exists projects;
create schema if not exists work;
create schema if not exists runtime;
create schema if not exists models;
create schema if not exists research;
create schema if not exists knowledge;
create schema if not exists memory;
create schema if not exists skills;
create schema if not exists security;
create schema if not exists ops;

comment on schema core is 'Realm, actor, kanonik kimlik ve policy revision';
comment on schema projects is 'Proje, alias, source binding, capability profile';
comment on schema work is 'Work Item, revision, event, relation, Intent, Decision, Plan';
comment on schema runtime is 'Job, attempt, lease, lock, checkpoint, claim, receipt, outbox';
comment on schema models is 'Envanter, health, benchmark, quota, assignment, observation';
comment on schema research is 'Soru, source snapshot, claim, contradiction, rapor';
comment on schema knowledge is 'Kaynak, artifact, normalized unit, chunk, embedding, citation';
comment on schema memory is 'Bellek revision, relation, kullanim, hijyen, promotion';
comment on schema skills is 'Skill candidate, evaluation, lifecycle, registry referansi';
comment on schema security is 'SecretRef metadata, authorization, disclosure, audit';
comment on schema ops is 'Scheduler, rapor, backup, incident, derived projection durumu';

-- 3. Uygulama rolu ---------------------------------------------------------------
--
-- Uygulama superuser olarak calismaz. Superuser row-level security'yi atlar; bu rol
-- sayesinde RLS gercekten uygulanir ve testlerde dogrulanabilir.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'zekam_app') then
        create role zekam_app nologin noinherit;
    end if;
end
$$;

grant usage on schema core, projects, work, runtime, models, research,
                      knowledge, memory, skills, security, ops to zekam_app;

-- 4. Ortak yardimcilar -----------------------------------------------------------

create or replace function core.current_realm_id()
returns uuid
language sql
stable
as $$
    select nullif(current_setting('zekam.realm_id', true), '')::uuid;
$$;

comment on function core.current_realm_id() is
    'Oturumun realm kimligi. Ayarlanmamissa NULL doner ve RLS politikalari fail-closed calisir.';

create or replace function core.assert_realm_selected()
returns uuid
language plpgsql
stable
as $$
declare
    selected uuid;
begin
    selected := core.current_realm_id();
    if selected is null then
        raise exception 'zekam.realm_id ayarlanmadan realm kapsamli islem yapilamaz'
            using errcode = '42501';
    end if;
    return selected;
end;
$$;

create or replace function core.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

-- 5. Realm -----------------------------------------------------------------------

create table core.realm (
    id            uuid        primary key,
    slug          text        not null,
    display_name  text        not null,
    status        text        not null default 'active',
    revision      integer     not null default 1,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    constraint realm_slug_unique unique (slug),
    constraint realm_slug_format check (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    constraint realm_slug_length check (char_length(slug) between 2 and 64),
    constraint realm_display_name_not_blank check (btrim(display_name) <> ''),
    constraint realm_status_allowed check (status in ('active', 'suspended', 'archived')),
    constraint realm_revision_positive check (revision >= 1)
);

comment on table core.realm is 'En dis yalitim siniri. Realm arasi iliski yasaktir.';

create trigger realm_touch_updated_at
    before update on core.realm
    for each row execute function core.touch_updated_at();

-- 6. Actor -----------------------------------------------------------------------

create table core.actor (
    id            uuid        primary key,
    realm_id      uuid        not null references core.realm (id) on delete restrict,
    kind          text        not null,
    slug          text        not null,
    display_name  text        not null,
    status        text        not null default 'active',
    revision      integer     not null default 1,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    constraint actor_slug_unique_per_realm unique (realm_id, slug),
    constraint actor_realm_scoped_key unique (realm_id, id),
    constraint actor_kind_allowed check (kind in ('human', 'agent', 'service', 'system')),
    constraint actor_slug_format check (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    constraint actor_slug_length check (char_length(slug) between 2 and 64),
    constraint actor_display_name_not_blank check (btrim(display_name) <> ''),
    constraint actor_status_allowed check (status in ('active', 'suspended', 'archived')),
    constraint actor_revision_positive check (revision >= 1)
);

comment on table core.actor is
    'Realm icinde eylem baslatabilen taraf. Actor kimlik tasir, yetki tasimaz.';
comment on constraint actor_realm_scoped_key on core.actor is
    'Alt tablolarin (realm_id, actor_id) bilesik FK ile ayni realm''i zorlamasini saglar.';

create index actor_realm_status_idx on core.actor (realm_id, status);

create trigger actor_touch_updated_at
    before update on core.actor
    for each row execute function core.touch_updated_at();

-- 7. Row-level security ----------------------------------------------------------
--
-- FORCE ile tablo sahibi de politikaya tabidir. `zekam.realm_id` ayarlanmadan
-- hicbir satir gorunmez veya yazilamaz.

alter table core.realm enable row level security;
alter table core.realm force row level security;

create policy realm_scope_select on core.realm
    for select using (id = core.current_realm_id());
create policy realm_scope_insert on core.realm
    for insert with check (id = core.current_realm_id());
create policy realm_scope_update on core.realm
    for update using (id = core.current_realm_id())
    with check (id = core.current_realm_id());
create policy realm_scope_delete on core.realm
    for delete using (id = core.current_realm_id());

alter table core.actor enable row level security;
alter table core.actor force row level security;

create policy actor_scope_select on core.actor
    for select using (realm_id = core.current_realm_id());
create policy actor_scope_insert on core.actor
    for insert with check (realm_id = core.current_realm_id());
create policy actor_scope_update on core.actor
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy actor_scope_delete on core.actor
    for delete using (realm_id = core.current_realm_id());

-- 8. Yetkiler --------------------------------------------------------------------

grant select, insert, update, delete on core.realm, core.actor to zekam_app;
grant execute on function core.current_realm_id() to zekam_app;
grant execute on function core.assert_realm_selected() to zekam_app;
grant select on core.schema_migrations to zekam_app;
