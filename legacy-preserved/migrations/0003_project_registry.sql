-- Proje kayit defteri, alias, source binding, source revision ve capability profile.
--
-- Portability kurali: kanonik kayitlar makineye ozel absolute path tasimaz. Yerel
-- yol yalnizca `projects.source_binding_local` tablosunda, makine kimligiyle
-- birlikte tutulur ve export/backup kapsaminin disindadir.
--
-- Geri alma: 0003_project_registry.down.sql

-- 1. Proje ---------------------------------------------------------------------

create table projects.project (
    id            uuid        primary key,
    realm_id      uuid        not null references core.realm (id) on delete restrict,
    slug          text        not null,
    display_name  text        not null,
    status        text        not null default 'active',
    revision      integer     not null default 1,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    constraint project_slug_unique_per_realm unique (realm_id, slug),
    constraint project_realm_scoped_key unique (realm_id, id),
    constraint project_slug_format check (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    constraint project_slug_length check (char_length(slug) between 2 and 64),
    constraint project_display_name_not_blank check (btrim(display_name) <> ''),
    constraint project_status_allowed check (status in ('active', 'suspended', 'archived')),
    constraint project_revision_positive check (revision >= 1)
);

comment on table projects.project is
    'Kayitli proje. Portable kimlik; fiziksel konum bilgisi tasimaz.';

create index project_realm_status_idx on projects.project (realm_id, status);

create trigger project_touch_updated_at
    before update on projects.project
    for each row execute function core.touch_updated_at();

-- 2. Alias ---------------------------------------------------------------------

create table projects.project_alias (
    id          uuid        primary key,
    realm_id    uuid        not null,
    project_id  uuid        not null,
    alias       text        not null,
    normalized  text        not null,
    is_primary  boolean     not null default false,
    created_at  timestamptz not null default now(),
    constraint alias_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete cascade,
    constraint alias_normalized_unique_per_realm unique (realm_id, normalized),
    constraint alias_normalized_format check (normalized ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    constraint alias_not_blank check (btrim(alias) <> '')
);

comment on table projects.project_alias is
    'Dogal dil cozumlemesi icin proje takma adlari. Normalized deger realm icinde tekildir.';

create index alias_project_idx on projects.project_alias (realm_id, project_id);
create index alias_trigram_idx on projects.project_alias using gin (normalized gin_trgm_ops);
create index project_slug_trigram_idx on projects.project using gin (slug gin_trgm_ops);

-- Bir projenin en fazla bir birincil alias'i olabilir.
create unique index alias_single_primary_idx
    on projects.project_alias (realm_id, project_id)
    where is_primary;

-- 3. Source binding ------------------------------------------------------------

create table projects.source_binding (
    id              uuid        primary key,
    realm_id        uuid        not null,
    project_id      uuid        not null,
    kind            text        not null,
    root_label      text        not null,
    locator_digest  text        not null,
    status          text        not null default 'bound',
    access_mode     text        not null default 'read-only',
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    constraint binding_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete cascade,
    constraint binding_realm_scoped_key unique (realm_id, id),
    constraint binding_kind_allowed check (kind in ('git-repository', 'directory')),
    constraint binding_status_allowed check (status in ('bound', 'unbound', 'stale')),
    constraint binding_access_mode_allowed check (access_mode = 'read-only'),
    constraint binding_root_label_portable
        check (root_label !~ '^[A-Za-z]:' and root_label !~ '^/' and root_label !~ '\\'),
    constraint binding_locator_digest_format check (locator_digest ~ '^sha256:[0-9a-f]{64}$')
);

comment on table projects.source_binding is
    'Haricî kaynak agacina salt okunur baglanti. Absolute path burada tutulmaz.';
comment on constraint binding_root_label_portable on projects.source_binding is
    'Portable kayitta surucu harfi, mutlak posix yolu veya ters bolu bulunamaz.';

create index binding_project_idx on projects.source_binding (realm_id, project_id);

create trigger binding_touch_updated_at
    before update on projects.source_binding
    for each row execute function core.touch_updated_at();

-- 4. Makineye ozel yol ---------------------------------------------------------

create table projects.source_binding_local (
    binding_id     uuid        primary key,
    realm_id       uuid        not null,
    machine_label  text        not null,
    absolute_path  text        not null,
    updated_at     timestamptz not null default now(),
    constraint local_binding_same_realm
        foreign key (realm_id, binding_id) references projects.source_binding (realm_id, id)
        on delete cascade,
    constraint local_absolute_path_not_blank check (btrim(absolute_path) <> '')
);

comment on table projects.source_binding_local is
    'Makineye ozel kok dizin. Sahiplik sinifi local: export, backup manifesti ve '
    'portable kayitlara dahil edilmez.';

create trigger local_binding_touch_updated_at
    before update on projects.source_binding_local
    for each row execute function core.touch_updated_at();

-- 5. Source revision (append-only) ----------------------------------------------

create table projects.source_revision (
    id            uuid        primary key,
    realm_id      uuid        not null,
    binding_id    uuid        not null,
    revision_kind text        not null,
    revision      text        not null,
    tree_digest   text        not null,
    branch        text,
    is_dirty      boolean     not null default false,
    file_count    integer     not null default 0,
    observed_at   timestamptz not null default now(),
    constraint revision_binding_same_realm
        foreign key (realm_id, binding_id) references projects.source_binding (realm_id, id)
        on delete cascade,
    constraint source_revision_kind_allowed
        check (revision_kind in ('git-commit', 'tree-digest')),
    constraint source_revision_not_blank check (btrim(revision) <> ''),
    constraint source_tree_digest_format check (tree_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint source_file_count_non_negative check (file_count >= 0)
);

comment on table projects.source_revision is
    'Gozlemlenen kaynak surumu. Append-only; guncelleme yerine yeni gozlem eklenir.';

create index source_revision_binding_idx
    on projects.source_revision (realm_id, binding_id, observed_at desc);

create trigger source_revision_deny_update
    before update on projects.source_revision
    for each statement execute function core.deny_mutation();

create trigger source_revision_deny_delete
    before delete on projects.source_revision
    for each statement execute function core.deny_mutation();

-- 6. Capability profile (append-only) -------------------------------------------

create table projects.capability_profile (
    id                 uuid        primary key,
    realm_id           uuid        not null,
    project_id         uuid        not null,
    source_revision_id uuid        not null,
    profile            jsonb       not null,
    profile_digest     text        not null,
    generator_version  text        not null,
    generated_at       timestamptz not null default now(),
    constraint profile_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete cascade,
    constraint profile_revision_exists
        foreign key (source_revision_id) references projects.source_revision (id)
        on delete cascade,
    constraint profile_digest_format check (profile_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint profile_unique_per_revision unique (source_revision_id, generator_version)
);

comment on table projects.capability_profile is
    'Kaynak surumune bagli, deterministik capability profili. Ayni surum ve ayni '
    'uretici surumu ayni digest''i uretir.';

create index profile_project_idx
    on projects.capability_profile (realm_id, project_id, generated_at desc);

create trigger capability_profile_deny_update
    before update on projects.capability_profile
    for each statement execute function core.deny_mutation();

-- 7. Entegrasyon durumu ----------------------------------------------------------

create table projects.integration_state (
    project_id            uuid        primary key,
    realm_id              uuid        not null,
    stage                 text        not null default 'registered',
    observed_revision_id  uuid,
    detail                jsonb       not null default '{}'::jsonb,
    updated_at            timestamptz not null default now(),
    constraint integration_project_same_realm
        foreign key (realm_id, project_id) references projects.project (realm_id, id)
        on delete cascade,
    constraint integration_stage_allowed check (
        stage in ('registered', 'bound', 'discovered', 'profiled', 'current', 'stale', 'unbound')
    ),
    constraint integration_revision_exists
        foreign key (observed_revision_id) references projects.source_revision (id)
        on delete set null
);

comment on table projects.integration_state is
    'Entegrasyonun hangi asamada oldugunu tasir. Asama kaniti olmadan ilerletilemez.';

create trigger integration_state_touch_updated_at
    before update on projects.integration_state
    for each row execute function core.touch_updated_at();

-- 8. Row-level security ------------------------------------------------------------

do $$
declare
    target text;
begin
    foreach target in array array[
        'projects.project',
        'projects.project_alias',
        'projects.source_binding',
        'projects.source_binding_local',
        'projects.source_revision',
        'projects.capability_profile',
        'projects.integration_state'
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

-- Append-only olmayan tablolar guncelleme ve silme politikasi da alir.
create policy scope_update on projects.project
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_delete on projects.project
    for delete using (realm_id = core.current_realm_id());

create policy scope_update on projects.project_alias
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_delete on projects.project_alias
    for delete using (realm_id = core.current_realm_id());

create policy scope_update on projects.source_binding
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_delete on projects.source_binding
    for delete using (realm_id = core.current_realm_id());

create policy scope_update on projects.source_binding_local
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_delete on projects.source_binding_local
    for delete using (realm_id = core.current_realm_id());

create policy scope_update on projects.integration_state
    for update using (realm_id = core.current_realm_id())
    with check (realm_id = core.current_realm_id());
create policy scope_delete on projects.integration_state
    for delete using (realm_id = core.current_realm_id());

-- 9. Yetkiler ----------------------------------------------------------------------

grant select, insert, update, delete on
    projects.project,
    projects.project_alias,
    projects.source_binding,
    projects.source_binding_local,
    projects.integration_state
to zekam_app;

grant select, insert on projects.source_revision, projects.capability_profile to zekam_app;
