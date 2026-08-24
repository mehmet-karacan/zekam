-- Typed context fragments and exact model-visible provider payload binding.

create table work.context_fragment_set (
    id uuid primary key,
    realm_id uuid not null,
    project_id uuid not null,
    work_item_id uuid not null,
    context_manifest_id uuid not null,
    fragment_count integer not null,
    fragment_set_digest text not null,
    created_at timestamptz not null,
    foreign key (realm_id, context_manifest_id)
        references work.context_manifest (realm_id, id) on delete cascade,
    foreign key (realm_id, project_id)
        references projects.project (realm_id, id) on delete restrict,
    foreign key (realm_id, work_item_id)
        references work.work_item (realm_id, id) on delete cascade,
    unique (realm_id, id),
    unique (realm_id, context_manifest_id),
    unique (realm_id, fragment_set_digest),
    check (fragment_count > 0),
    check (fragment_set_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table work.context_fragment (
    id uuid primary key,
    realm_id uuid not null,
    project_id uuid not null,
    work_item_id uuid not null,
    fragment_set_id uuid not null,
    context_manifest_id uuid not null,
    fragment_id text not null,
    candidate_id text not null,
    content_kind text not null,
    role text not null,
    fragment_order integer not null,
    visibility text not null,
    authority smallint not null,
    source_ref text not null,
    source_revision text not null,
    content_digest text not null,
    token_count integer not null,
    required boolean not null,
    grants_authority boolean not null default false,
    fragment_digest text not null,
    created_at timestamptz not null,
    foreign key (realm_id, context_manifest_id)
        references work.context_manifest (realm_id, id) on delete cascade,
    foreign key (realm_id, fragment_set_id)
        references work.context_fragment_set (realm_id, id) on delete cascade,
    foreign key (realm_id, project_id)
        references projects.project (realm_id, id) on delete restrict,
    foreign key (realm_id, work_item_id)
        references work.work_item (realm_id, id) on delete cascade,
    unique (realm_id, context_manifest_id, fragment_id),
    unique (realm_id, context_manifest_id, candidate_id),
    unique (realm_id, context_manifest_id, fragment_order),
    check (
        btrim(fragment_id) <> '' and btrim(candidate_id) <> ''
        and fragment_id !~ '(^/|^[A-Za-z]:|\\\\|(^|/)\.\.(/|$))'
        and candidate_id !~ '(^/|^[A-Za-z]:|\\\\|(^|/)\.\.(/|$))'
    ),
    check (content_kind in (
        'system-instruction', 'user-message', 'assistant-message', 'tool-result',
        'work-context', 'knowledge', 'memory', 'checkpoint'
    )),
    check (role in ('system', 'user', 'assistant', 'tool')),
    check (visibility in (
        'model-visible', 'client-visible', 'runtime-only', 'diagnostic-only'
    )),
    check (fragment_order >= 0 and authority between 0 and 3 and token_count > 0),
    check (
        btrim(source_ref) <> '' and btrim(source_revision) <> ''
        and source_ref !~ '(^/|^[A-Za-z]:|\\\\|(^|/)\.\.(/|$))'
        and source_revision !~ '(^/|^[A-Za-z]:|\\\\|(^|/)\.\.(/|$))'
    ),
    check (content_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (fragment_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (grants_authority = false)
);

create function work.enforce_context_fragment_set_scope() returns trigger
language plpgsql security invoker set search_path = pg_catalog, work, core as $$
declare manifest_project uuid; manifest_work uuid;
begin
    select project_id, work_item_id into manifest_project, manifest_work
    from work.context_manifest
    where realm_id = new.realm_id and id = new.context_manifest_id;
    if manifest_project is null
       or manifest_project <> new.project_id
       or manifest_work <> new.work_item_id then
        raise exception 'context fragment set exact manifest scope mismatch' using errcode = '23514';
    end if;
    return new;
end
$$;
create trigger context_fragment_set_scope_guard before insert on work.context_fragment_set
    for each row execute function work.enforce_context_fragment_set_scope();

create function work.enforce_context_fragment_scope() returns trigger
language plpgsql security invoker set search_path = pg_catalog, work, core as $$
declare parent work.context_fragment_set%rowtype;
        expected_fragment_digest text;
begin
    select * into parent from work.context_fragment_set
    where realm_id = new.realm_id and id = new.fragment_set_id;
    if parent.id is null
       or parent.project_id <> new.project_id
       or parent.work_item_id <> new.work_item_id
       or parent.context_manifest_id <> new.context_manifest_id then
        raise exception 'context fragment exact set/manifest scope mismatch' using errcode = '23514';
    end if;
    expected_fragment_digest := models.capability_runtime_jsonb_digest(jsonb_build_object(
        'fragment_id', new.fragment_id,
        'candidate_id', new.candidate_id,
        'content_kind', new.content_kind,
        'role', new.role,
        'order', new.fragment_order,
        'visibility', new.visibility,
        'authority', new.authority,
        'source_ref', new.source_ref,
        'source_revision', new.source_revision,
        'content_digest', new.content_digest,
        'token_count', new.token_count,
        'required', new.required,
        'grants_authority', false
    ));
    if new.fragment_digest is distinct from expected_fragment_digest then
        raise exception 'context fragment canonical digest mismatch' using errcode = '23514';
    end if;
    return new;
end
$$;
create trigger context_fragment_scope_guard before insert on work.context_fragment
    for each row execute function work.enforce_context_fragment_scope();

create function work.enforce_context_fragment_set_complete() returns trigger
language plpgsql security invoker set search_path = pg_catalog, work, core as $$
declare actual_count integer; minimum_order integer; maximum_order integer;
        manifest_digest_ text; expected_set_digest text; fragment_bodies jsonb;
        selected_ jsonb; selected_count integer; selected_distinct_count integer;
begin
    select count(*), min(fragment_order), max(fragment_order),
           jsonb_agg(jsonb_build_object(
               'fragment_id', fragment_id,
               'candidate_id', candidate_id,
               'content_kind', content_kind,
               'role', role,
               'order', fragment_order,
               'visibility', visibility,
               'authority', authority,
               'source_ref', source_ref,
               'source_revision', source_revision,
               'content_digest', content_digest,
               'token_count', token_count,
               'required', required,
               'grants_authority', false
           ) order by fragment_order)
      into actual_count, minimum_order, maximum_order, fragment_bodies
    from work.context_fragment
    where realm_id = new.realm_id and fragment_set_id = new.id;
    if actual_count <> new.fragment_count
       or minimum_order <> 0
       or maximum_order <> new.fragment_count - 1 then
        raise exception 'context fragment set exact count/order mismatch' using errcode = '23514';
    end if;
    select manifest_digest, selected into manifest_digest_, selected_
    from work.context_manifest
    where realm_id = new.realm_id and id = new.context_manifest_id;
    selected_count := jsonb_array_length(selected_);
    select count(distinct value->>'candidate_id') into selected_distinct_count
    from jsonb_array_elements(selected_) item(value);
    if selected_count <> new.fragment_count
       or selected_distinct_count <> selected_count
       or exists (
           select 1
           from jsonb_array_elements(selected_) selected(value)
           full join (
               select id, candidate_id, content_digest, token_count
               from work.context_fragment
               where realm_id = new.realm_id and fragment_set_id = new.id
           ) fragment
             on fragment.candidate_id = selected.value->>'candidate_id'
           where selected.value is null
              or fragment.id is null
              or fragment.content_digest is distinct from selected.value->>'content_digest'
              or fragment.token_count is distinct from (selected.value->>'token_count')::integer
       ) then
        raise exception 'context fragment set selected candidate exact partition mismatch'
            using errcode = '23514';
    end if;
    expected_set_digest := models.capability_runtime_jsonb_digest(jsonb_build_object(
        'schema', 'zekam-context-fragment-set/v2',
        'context_manifest_digest', manifest_digest_,
        'fragments', fragment_bodies
    ));
    if new.fragment_set_digest is distinct from expected_set_digest then
        raise exception 'context fragment set canonical digest mismatch' using errcode = '23514';
    end if;
    return null;
end
$$;
create constraint trigger context_fragment_set_complete_guard
    after insert on work.context_fragment_set deferrable initially deferred
    for each row execute function work.enforce_context_fragment_set_complete();

alter table models.request_manifest
    add column context_fragment_set_digest text,
    add column model_visible_payload_digest text,
    add constraint request_manifest_fragment_set_digest_format check (
        context_fragment_set_digest is null
        or context_fragment_set_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    add constraint request_manifest_visible_payload_digest_format check (
        model_visible_payload_digest is null
        or model_visible_payload_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    add constraint request_manifest_visible_payload_exact check (
        model_visible_payload_digest is null
        or model_visible_payload_digest = payload_digest
    );

create or replace function models.enforce_manifest_missing_bindings() returns trigger
language plpgsql as $$
declare expected text[];
begin
  select coalesce(array_agg(name order by name),'{}'::text[]) into expected from (values
    ('assignment_id',new.assignment_id is null),
    ('authorization_scope_digest',new.authorization_scope_digest is null),
    ('checkpoint_digest',new.checkpoint_digest is null),
    ('context_fragment_set_digest',new.context_fragment_set_digest is null),
    ('context_manifest_digest',new.context_manifest_digest is null),
    ('context_packet_digest',new.context_packet_digest is null),
    ('execution_envelope_digest',new.execution_envelope_digest is null),
    ('execution_envelope_id',new.execution_envelope_id is null),
    ('max_cost_micros',new.max_cost_micros is null),
    ('max_input_tokens',new.max_input_tokens is null),
    ('max_output_tokens',new.max_output_tokens is null),
    ('model_visible_payload_digest',new.model_visible_payload_digest is null),
    ('output_schema_digest',new.output_schema_digest is null),
    ('policy_digest',new.policy_digest is null),
    ('role',new.role is null),
    ('route_decision_digest',new.route_decision_digest is null),
    ('route_expires_at',new.route_expires_at is null),
    ('run_id',new.run_id is null),
    ('source_revision',new.source_revision is null)
  ) as fields(name,missing) where missing;
  if new.missing_bindings<>expected then
    raise exception 'missing bindings manifest alanlariyla exact eslesmeli' using errcode='23514';
  end if;
  return new;
end $$;

create function models.enforce_request_context_fragment_binding() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, work, core as $$
begin
    if new.context_fragment_set_digest is not null and not exists (
        select 1
        from work.context_fragment_set s
        join work.context_manifest m
          on m.realm_id = s.realm_id and m.id = s.context_manifest_id
        where s.realm_id = new.realm_id
          and s.project_id = new.project_id
          and s.work_item_id = new.work_item_id
          and s.fragment_set_digest = new.context_fragment_set_digest
          and m.manifest_digest = new.context_manifest_digest
    ) then
        raise exception 'model request canonical context fragment set binding ister'
            using errcode = '23514';
    end if;
    return new;
end
$$;
create trigger request_context_fragment_binding_guard
    before insert on models.request_manifest
    for each row execute function models.enforce_request_context_fragment_binding();

alter table work.context_fragment_set enable row level security;
alter table work.context_fragment_set force row level security;
alter table work.context_fragment enable row level security;
alter table work.context_fragment force row level security;
create policy scope_select on work.context_fragment_set for select
    using (realm_id = core.current_realm_id());
create policy scope_insert on work.context_fragment_set for insert
    with check (realm_id = core.current_realm_id());
create policy scope_select on work.context_fragment for select
    using (realm_id = core.current_realm_id());
create policy scope_insert on work.context_fragment for insert
    with check (realm_id = core.current_realm_id());
create trigger deny_update before update on work.context_fragment_set
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on work.context_fragment_set
    for each statement execute function core.deny_mutation();
create trigger deny_update before update on work.context_fragment
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on work.context_fragment
    for each statement execute function core.deny_mutation();

grant select, insert on work.context_fragment_set, work.context_fragment to zekam_app;
grant execute on function work.enforce_context_fragment_scope() to zekam_app;
grant execute on function work.enforce_context_fragment_set_scope() to zekam_app;
grant execute on function work.enforce_context_fragment_set_complete() to zekam_app;
grant execute on function models.enforce_request_context_fragment_binding() to zekam_app;
