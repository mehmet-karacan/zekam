-- Field-level config provenance snapshots and named permission profile revisions.

create table security.permission_profile_revision (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  name text not null,
  revision integer not null,
  allowed_capabilities text[] not null,
  denied_capabilities text[] not null,
  managed boolean not null,
  created_at timestamptz not null,
  profile_digest text not null,
  profile_body jsonb not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),unique(realm_id,name,revision),unique(realm_id,profile_digest),
  check(btrim(name)<>'' and revision>0),
  check(profile_digest ~ '^sha256:[0-9a-f]{64}$' and not grants_authority)
);

create function security.enforce_permission_profile_revision() returns trigger
language plpgsql security invoker set search_path=pg_catalog,security,runtime,models as $$
declare expected jsonb;
begin
  perform pg_advisory_xact_lock(hashtextextended(new.realm_id::text||':'||new.name,0));
  if not exists(select 1 from security.permission_profile_revision prior
      where prior.realm_id=new.realm_id and prior.profile_digest=new.profile_digest)
    and new.revision<>(select coalesce(max(prior.revision),0)+1
      from security.permission_profile_revision prior
      where prior.realm_id=new.realm_id and prior.name=new.name) then
    raise exception 'permission profile revision monotonic olmali' using errcode='23514';
  end if;
  if new.allowed_capabilities<>(select coalesce(array_agg(value order by value),'{}'::text[])
      from (select distinct unnest(new.allowed_capabilities) value) valueset)
    or new.denied_capabilities<>(select coalesce(array_agg(value order by value),'{}'::text[])
      from (select distinct unnest(new.denied_capabilities) value) valueset)
    or new.allowed_capabilities&&new.denied_capabilities
    or exists(select 1 from unnest(new.allowed_capabilities||new.denied_capabilities) value
      where btrim(value)='') then
    raise exception 'permission profile capability sets canonical/disjoint olmali'
      using errcode='23514';
  end if;
  expected:=jsonb_build_object(
    'schema','zekam-permission-profile-revision/v1','id',new.id::text,
    'realm_id',new.realm_id::text,'name',new.name,'revision',new.revision,
    'allowed_capabilities',to_jsonb(new.allowed_capabilities),
    'denied_capabilities',to_jsonb(new.denied_capabilities),'managed',new.managed,
    'created_at',runtime.environment_canonical_timestamp(new.created_at),
    'grants_authority',false);
  if new.profile_body<>expected
    or new.profile_digest<>models.capability_runtime_jsonb_digest(expected) then
    raise exception 'permission profile body/digest drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger permission_profile_revision_guard before insert
on security.permission_profile_revision for each row
execute function security.enforce_permission_profile_revision();

create table security.config_provenance_snapshot (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  layer_stack text[] not null,
  field_decisions jsonb not null,
  effective_document jsonb not null,
  effective_digest text not null,
  graph_digest text not null,
  graph_body jsonb not null,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),unique(realm_id,graph_digest),
  check(cardinality(layer_stack)>0),
  check(jsonb_typeof(field_decisions)='array' and jsonb_typeof(effective_document)='object'),
  check(effective_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(graph_digest ~ '^sha256:[0-9a-f]{64}$' and not grants_authority)
);

create function security.config_leaf_values(document jsonb, prefix text default '')
returns table(field_path text, field_value jsonb)
language plpgsql immutable security invoker set search_path=pg_catalog,security as $$
declare item record; declare next_path text;
begin
  for item in select key,value from jsonb_each(document) loop
    next_path:=case when prefix='' then item.key else prefix||'.'||item.key end;
    if jsonb_typeof(item.value)='object' then
      return query select * from security.config_leaf_values(item.value,next_path);
    else
      field_path:=next_path;
      field_value:=item.value;
      return next;
    end if;
  end loop;
end $$;

create function security.enforce_config_provenance_snapshot() returns trigger
language plpgsql security invoker set search_path=pg_catalog,security,models as $$
declare expected jsonb; declare field jsonb; declare candidate jsonb; declare prior_path text;
begin
  if cardinality(new.layer_stack)<>(select count(distinct value) from unnest(new.layer_stack) value)
    or exists(select 1 from unnest(new.layer_stack) value where btrim(value)='') then
    raise exception 'config provenance layer stack canonical olmali' using errcode='23514';
  end if;
  for field in select value from jsonb_array_elements(new.field_decisions) loop
    if (select array_agg(key order by key) from jsonb_object_keys(field) key)
        <>array['candidates','field_path','managed_requirement','origin','value','value_digest']
      or btrim(field->>'field_path')='' or btrim(field->>'origin')=''
      or not (field->>'origin'=any(new.layer_stack))
      or field->>'value_digest'<>models.capability_runtime_jsonb_digest(field->'value')
      or not exists(select 1 from security.config_leaf_values(new.effective_document) leaf
          where leaf.field_path=field->>'field_path' and leaf.field_value=field->'value')
      or jsonb_typeof(field->'candidates')<>'array'
      or jsonb_array_length(field->'candidates')=0
      or jsonb_array_length(field->'candidates')<>(select count(distinct item->>'layer')
          from jsonb_array_elements(field->'candidates') item)
      or (select count(*) from jsonb_array_elements(field->'candidates') item
          where (item->>'selected')::boolean)<>1
      or not exists(select 1 from jsonb_array_elements(field->'candidates') item
          where (item->>'selected')::boolean and item->>'layer'=field->>'origin'
            and item->>'value_digest'=field->>'value_digest') then
      raise exception 'config provenance field decision shape/digest drift' using errcode='23514';
    end if;
    if prior_path is not null and prior_path>=field->>'field_path' then
      raise exception 'config provenance fields canonical sirali olmali' using errcode='23514';
    end if;
    prior_path:=field->>'field_path';
    for candidate in select value from jsonb_array_elements(field->'candidates') loop
      if (select array_agg(key order by key) from jsonb_object_keys(candidate) key)
          <>array['disabled_reason','layer','selected','value_digest']
        or btrim(candidate->>'layer')=''
        or not (candidate->>'layer'=any(new.layer_stack))
        or candidate->>'value_digest' !~ '^sha256:[0-9a-f]{64}$'
        or ((candidate->>'selected')::boolean) is distinct from
          (candidate->'disabled_reason'='null'::jsonb) then
        raise exception 'config provenance candidate shape/reason drift' using errcode='23514';
      end if;
    end loop;
    if exists(select 1 from jsonb_array_elements(field->'candidates') item
        where array_position(new.layer_stack,item->>'layer')>
          array_position(new.layer_stack,field->>'origin')) then
      raise exception 'config provenance selected origin precedence drift' using errcode='23514';
    end if;
    if field->'managed_requirement'<>'null'::jsonb and (
      (select array_agg(key order by key)
         from jsonb_object_keys(field->'managed_requirement') key)
        <>array['field_path','mode','required_value_digest']
      or field->'managed_requirement'->>'field_path'<>field->>'field_path'
      or field->'managed_requirement'->>'mode' not in ('deny','exact')
      or (field->'managed_requirement'->>'mode'='deny'
          and (field->'managed_requirement'->'required_value_digest'<>'null'::jsonb
            or field->'value'<>'false'::jsonb))
      or (field->'managed_requirement'->>'mode'='exact'
          and (coalesce(field->'managed_requirement'->>'required_value_digest','')
              !~ '^sha256:[0-9a-f]{64}$'
            or field->'managed_requirement'->>'required_value_digest'<>
              field->>'value_digest'))
    ) then
      raise exception 'config provenance managed requirement drift' using errcode='23514';
    end if;
  end loop;
  if jsonb_array_length(new.field_decisions)<>
      (select count(*) from security.config_leaf_values(new.effective_document)) then
    raise exception 'config provenance effective field coverage drift' using errcode='23514';
  end if;
  expected:=jsonb_build_object(
    'schema','zekam-config-provenance-graph/v1','layer_stack',to_jsonb(new.layer_stack),
    'fields',new.field_decisions,'effective_digest',new.effective_digest,
    'grants_authority',false);
  if new.graph_body<>expected
    or new.effective_digest<>models.capability_runtime_jsonb_digest(new.effective_document)
    or new.graph_digest<>models.capability_runtime_jsonb_digest(expected) then
    raise exception 'config provenance body/digest drift' using errcode='23514';
  end if;
  return new;
end $$;
create trigger config_provenance_snapshot_guard before insert
on security.config_provenance_snapshot for each row
execute function security.enforce_config_provenance_snapshot();

do $$ declare target text; begin foreach target in array array[
  'security.permission_profile_revision','security.config_provenance_snapshot'
] loop
  execute format('alter table %s enable row level security',target);
  execute format('alter table %s force row level security',target);
  execute format('create policy scope_select on %s for select using(realm_id=core.current_realm_id())',target);
  execute format('create policy scope_insert on %s for insert with check(realm_id=core.current_realm_id())',target);
  execute format('grant select,insert on %s to zekam_app',target);
  execute format('create trigger no_mutation before update or delete on %s for each statement execute function core.deny_mutation()',target);
end loop; end $$;
