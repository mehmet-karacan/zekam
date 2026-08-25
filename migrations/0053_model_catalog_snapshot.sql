-- Provider catalog availability observation; benchmark/qualification authority degildir.
create function models.catalog_canonical_json(value_ jsonb) returns text
language plpgsql immutable strict set search_path=pg_catalog,models as $$
declare kind text:=jsonb_typeof(value_); result_ text;
begin
  if kind in ('null','boolean','number') then return value_::text; end if;
  if kind='string' then return to_jsonb(value_#>>'{}')::text; end if;
  if kind='array' then
    select '['||coalesce(string_agg(models.catalog_canonical_json(value),','
      order by ordinal),'')||']' into result_
      from jsonb_array_elements(value_) with ordinality item(value,ordinal);
    return result_;
  end if;
  select '{'||coalesce(string_agg(to_jsonb(key)::text||':'||
    models.catalog_canonical_json(value),',' order by key collate "C"),'')||'}'
    into result_ from jsonb_each(value_) item(key,value);
  return result_;
end $$;
create function models.catalog_jsonb_digest(value_ jsonb) returns text
language sql immutable strict set search_path=pg_catalog,models,public as $$
  select 'sha256:'||encode(public.digest(
    convert_to(models.catalog_canonical_json(value_),'UTF8'),'sha256'),'hex')
$$;

create table models.catalog_snapshot (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  provider_id text not null,
  catalog_digest text not null,
  snapshot_digest text not null,
  etag text,
  fetched_at timestamptz not null,
  expires_at timestamptz not null,
  client_version text not null,
  source text not null,
  entries jsonb not null,
  fetch_status text not null,
  error_category text,
  prior_snapshot_id uuid,
  fetch_provenance jsonb,
  manifest_body jsonb not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),unique(realm_id,snapshot_digest),
  foreign key(realm_id,prior_snapshot_id) references models.catalog_snapshot(realm_id,id),
  check(provider_id=btrim(provider_id) and provider_id<>'' and length(provider_id)<=256
    and provider_id not like '%://%' and provider_id not like E'%\\%'),
  check(catalog_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(expires_at>fetched_at),
  check(client_version=btrim(client_version) and client_version<>''
    and length(client_version)<=128),
  check(source in ('remote','static','package')),
  check(jsonb_typeof(entries)='array'),
  check(fetch_status in ('fetched','not-modified','failed')),
  check((fetch_status='failed')=(error_category is not null)),
  check((fetch_status='failed')=(jsonb_array_length(entries)=0)),
  check(fetch_status<>'not-modified' or (prior_snapshot_id is not null and source='remote')),
  check(source='remote' or etag is null),
  check((source='remote')=(fetch_provenance is not null)),
  check(fetch_provenance is null or jsonb_typeof(fetch_provenance)='object'),
  check(jsonb_typeof(manifest_body)='object'),
  check(not grants_authority)
);

create index catalog_snapshot_latest_idx
  on models.catalog_snapshot(realm_id,provider_id,fetched_at desc,id desc);

create function models.enforce_catalog_snapshot() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models,runtime as $$
declare expected_catalog jsonb; declare expected_manifest jsonb; declare expected_response jsonb;
declare expected_plan jsonb; declare prior_digest text;
declare sorted_entries jsonb; declare entry jsonb;
declare provenance jsonb; declare evidence record;
begin
  select coalesce(jsonb_agg(value order by value->>'model_id'),'[]'::jsonb)
    into sorted_entries from jsonb_array_elements(new.entries);
  if new.entries<>sorted_entries
    or (select count(*) from jsonb_array_elements(new.entries)) <>
       (select count(distinct value->>'model_id') from jsonb_array_elements(new.entries)) then
    raise exception 'catalog entries unique ve sirali olmali' using errcode='23514';
  end if;
  for entry in select value from jsonb_array_elements(new.entries) loop
    if jsonb_typeof(entry)<>'object'
      or (select array_agg(key order by key) from jsonb_object_keys(entry) key) <>
         array['authentication_required','capabilities','endpoint_class','model_id','visibility']
      or jsonb_typeof(entry->'authentication_required')<>'boolean'
      or jsonb_typeof(entry->'capabilities')<>'array'
      or entry->>'visibility' not in ('public','authenticated','restricted')
      or btrim(entry->>'model_id')='' or length(entry->>'model_id')>256
      or btrim(entry->>'endpoint_class')='' or length(entry->>'endpoint_class')>256
      or ((entry->>'visibility')<>'public'
        and (entry->>'authentication_required')::boolean=false)
      or (select count(*) from jsonb_array_elements(entry->'capabilities') value
          where jsonb_typeof(value)<>'string')>0
      or (select coalesce(jsonb_agg(value order by value),'[]'::jsonb)
          from jsonb_array_elements(entry->'capabilities'))<>entry->'capabilities'
      or (select count(*) from jsonb_array_elements(entry->'capabilities'))<>
         (select count(distinct value) from jsonb_array_elements(entry->'capabilities')) then
      raise exception 'catalog entry schema gecersiz' using errcode='23514';
    end if;
  end loop;
  expected_catalog:=jsonb_build_object(
    'schema','zekam-model-catalog/v1','provider_id',new.provider_id,
    'source',new.source,'entries',new.entries,'grants_authority',false);
  if new.catalog_digest<>models.catalog_jsonb_digest(expected_catalog) then
    raise exception 'catalog digest drift' using errcode='23514';
  end if;
  provenance:=new.fetch_provenance;
  if new.source='remote' then
    if (select array_agg(key order by key) from jsonb_object_keys(provenance) key) <>
       array['adapter_evidence_digest','authorization_digest','authorization_id',
         'claim_digest','claim_id','plan_digest','prior_snapshot_digest','receipt_id',
         'receipt_status','response_digest','response_etag','schema','status_code',
         'strategy','ttl_seconds']
      or provenance->>'schema'<>'zekam-model-catalog-fetch-provenance/v1'
      or (provenance->>'status_code')::integer not between 100 and 599
      or provenance->>'strategy' not in ('online','online-if-uncached','force-probe')
      or (provenance->>'ttl_seconds')::integer not between 60 and 604800 then
      raise exception 'remote catalog fetch provenance schema gecersiz' using errcode='42501';
    end if;
    if new.prior_snapshot_id is not null then
      select snapshot_digest into prior_digest from models.catalog_snapshot
        where realm_id=new.realm_id and id=new.prior_snapshot_id;
    end if;
    expected_plan:=jsonb_build_object(
      'schema','zekam-model-catalog-refresh-plan/v1','provider_id',new.provider_id,
      'strategy',provenance->>'strategy','client_version',new.client_version,
      'ttl_seconds',(provenance->>'ttl_seconds')::integer,
      'prior_snapshot_digest',prior_digest,'grants_authority',false);
    if provenance->>'plan_digest'<>models.catalog_jsonb_digest(expected_plan)
      or (provenance->>'ttl_seconds')::integer<>
        extract(epoch from (new.expires_at-new.fetched_at))::integer
      or provenance->>'prior_snapshot_digest' is distinct from prior_digest then
      raise exception 'remote catalog refresh plan provenance drift' using errcode='42501';
    end if;
    expected_response:=jsonb_build_object(
      'schema','zekam-model-catalog-fetch-response/v1',
      'status_code',(provenance->>'status_code')::integer,
      'entries',case when new.fetch_status='fetched' then new.entries else '[]'::jsonb end,
      'etag',provenance->'response_etag','error_category',new.error_category);
    if provenance->>'response_digest'<>models.catalog_jsonb_digest(expected_response)
      or (new.fetch_status='fetched' and (provenance->>'status_code')::integer<>200)
      or (new.fetch_status='not-modified' and (provenance->>'status_code')::integer<>304)
      or (new.fetch_status='failed' and (provenance->>'status_code')::integer in (200,304)) then
      raise exception 'remote catalog response digest drift' using errcode='42501';
    end if;
    select a.plan_digest a_plan_digest,a.effect_digest a_effect_digest,
      a.authorization_digest a_authorization_digest,a.state a_state,
      a.allowed_resources a_allowed_resources,a.allowed_effects a_allowed_effects,
      a.provider_refs a_provider_refs,c.operation c_operation,c.effect_digest c_effect_digest,
      c.authorization_digest c_authorization_digest,c.authorization_id c_authorization_id,
      c.claim_digest c_claim_digest,c.claimed_at c_claimed_at,
      r.status r_status,r.result_digest r_result_digest,r.completed_at r_completed_at,
      r.failure_digest r_failure_digest,r.adapter_evidence_digest r_adapter_evidence_digest
      into evidence from security.authorization a
      join runtime.effect_claim c on c.realm_id=a.realm_id
        and c.id=(provenance->>'claim_id')::uuid
      join runtime.effect_receipt r on r.realm_id=c.realm_id and r.claim_id=c.id
        and r.id=(provenance->>'receipt_id')::uuid
      where a.realm_id=new.realm_id and a.id=(provenance->>'authorization_id')::uuid;
    if evidence is null
      or evidence.a_plan_digest is distinct from provenance->>'plan_digest'
      or evidence.a_effect_digest is distinct from provenance->>'plan_digest'
      or evidence.a_authorization_digest is distinct from provenance->>'authorization_digest'
      or evidence.a_state<>'consumed'
      or not ('model-catalog-refresh'=any(evidence.a_allowed_effects))
      or not (('provider.catalog:'||new.provider_id)=any(evidence.a_allowed_resources))
      or not (new.provider_id=any(evidence.a_provider_refs))
      or evidence.c_operation<>'model-catalog-refresh'
      or evidence.c_effect_digest is distinct from provenance->>'plan_digest'
      or evidence.c_authorization_digest is distinct from provenance->>'authorization_digest'
      or evidence.c_authorization_id is distinct from (provenance->>'authorization_id')::uuid
      or evidence.c_claim_digest is distinct from provenance->>'claim_digest'
      or evidence.r_completed_at<evidence.c_claimed_at
      or evidence.r_status is distinct from provenance->>'receipt_status'
      or evidence.r_adapter_evidence_digest is distinct from provenance->>'adapter_evidence_digest'
      or (evidence.r_status='completed' and evidence.r_result_digest is distinct from
        provenance->>'response_digest')
      or (evidence.r_status='failed' and evidence.r_failure_digest is distinct from
        provenance->>'response_digest') then
      raise exception 'remote catalog authorization claim receipt provenance drift'
        using errcode='42501';
    end if;
  end if;
  expected_manifest:=jsonb_build_object(
    'schema','zekam-model-catalog-snapshot/v1','id',new.id::text,
    'realm_id',new.realm_id::text,'provider_id',new.provider_id,
    'catalog_digest',new.catalog_digest,'etag',new.etag,
    'fetched_at',runtime.environment_canonical_timestamp(new.fetched_at),
    'expires_at',runtime.environment_canonical_timestamp(new.expires_at),
    'client_version',new.client_version,'source',new.source,
    'fetch_status',new.fetch_status,'error_category',new.error_category,
    'prior_snapshot_id',new.prior_snapshot_id::text,
    'fetch_provenance',new.fetch_provenance,'grants_authority',false);
  if new.manifest_body<>expected_manifest
    or new.snapshot_digest<>models.catalog_jsonb_digest(expected_manifest) then
    raise exception 'catalog snapshot manifest drift' using errcode='23514',
      detail=format('expected=%s actual=%s expected_digest=%s actual_digest=%s',
        expected_manifest,new.manifest_body,
        models.catalog_jsonb_digest(expected_manifest),new.snapshot_digest);
  end if;
  return new;
end $$;
create trigger catalog_snapshot_guard before insert on models.catalog_snapshot
for each row execute function models.enforce_catalog_snapshot();

create function models.catalog_snapshot_immutable() returns trigger
language plpgsql security invoker set search_path=pg_catalog as $$
begin raise exception 'catalog snapshot append-only' using errcode='23514'; end $$;
create trigger catalog_snapshot_deny_update before update on models.catalog_snapshot
for each row execute function models.catalog_snapshot_immutable();
create trigger catalog_snapshot_deny_delete before delete on models.catalog_snapshot
for each row execute function models.catalog_snapshot_immutable();

alter table models.model_route_decision
  add column catalog_provider_id text,
  add column catalog_digest text,
  add column catalog_snapshot_digest text,
  add column catalog_snapshot_id uuid,
  add constraint route_catalog_same_realm foreign key(realm_id,catalog_snapshot_id)
    references models.catalog_snapshot(realm_id,id) on delete restrict,
  add constraint route_catalog_binding check(
    (catalog_provider_id is null and catalog_digest is null
      and catalog_snapshot_digest is null and catalog_snapshot_id is null)
    or (btrim(catalog_provider_id)<>''
      and catalog_digest ~ '^sha256:[0-9a-f]{64}$'
      and catalog_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'
      and catalog_snapshot_id is not null));

create or replace function models.enforce_route_decision() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models,projects,core as $$
declare policy_role text; policy_layer text; policy_digest_ text;
declare context_project uuid; target_digest text; catalog_record record;
begin
  select role,target_layer,policy_digest into policy_role,policy_layer,policy_digest_
    from models.routing_role_policy where realm_id=new.realm_id and id=new.role_policy_id;
  if policy_role is distinct from new.role or policy_layer is distinct from new.target_layer
    or policy_digest_ is distinct from new.routing_policy_digest then
    raise exception 'route decision role policy drift' using errcode='42501';
  end if;
  if new.project_context_id is not null then
    select project_id into context_project from projects.routing_context_snapshot
      where realm_id=new.realm_id and id=new.project_context_id;
    if context_project is distinct from new.project_id then
      raise exception 'route decision project context mismatch' using errcode='42501';
    end if;
  end if;
  select snapshot_digest into target_digest from models.execution_target_snapshot
    where realm_id=new.realm_id and id=new.execution_target_id;
  if target_digest is distinct from new.execution_target_digest then
    raise exception 'route decision execution target drift' using errcode='42501';
  end if;
  if new.catalog_snapshot_id is not null then
    select id,provider_id,catalog_digest,snapshot_digest,fetch_status,expires_at,entries
      into catalog_record from models.catalog_snapshot
      where realm_id=new.realm_id and id=new.catalog_snapshot_id;
    if catalog_record.provider_id is distinct from new.catalog_provider_id
      or catalog_record.catalog_digest is distinct from new.catalog_digest
      or catalog_record.snapshot_digest is distinct from new.catalog_snapshot_digest
      or catalog_record.id is distinct from (
        select current_snapshot.id from models.catalog_snapshot current_snapshot
        where current_snapshot.realm_id=new.realm_id
          and current_snapshot.provider_id=catalog_record.provider_id
        order by current_snapshot.fetched_at desc,current_snapshot.id desc limit 1)
      or catalog_record.fetch_status='failed' or catalog_record.expires_at<=new.decided_at
      or (new.primary_model_id is not null and not exists(
        select 1 from jsonb_array_elements(catalog_record.entries) entry
        where entry->>'model_id'=new.primary_model_id)) then
      raise exception 'route decision catalog current binding drift' using errcode='42501';
    end if;
  end if;
  return new;
end $$;

alter table models.request_manifest
  add column catalog_provider_id text,
  add column catalog_digest text,
  add column catalog_snapshot_digest text,
  add column catalog_snapshot_id uuid,
  add constraint request_catalog_same_realm foreign key(realm_id,catalog_snapshot_id)
    references models.catalog_snapshot(realm_id,id) on delete restrict,
  add constraint request_catalog_binding check(
    (catalog_provider_id is null and catalog_digest is null
      and catalog_snapshot_digest is null and catalog_snapshot_id is null)
    or (btrim(catalog_provider_id)<>''
      and catalog_digest ~ '^sha256:[0-9a-f]{64}$'
      and catalog_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'
      and catalog_snapshot_id is not null));

create function models.enforce_request_catalog() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models as $$
declare snapshot record; route record;
begin
  if new.catalog_snapshot_id is null then return new; end if;
  select id,provider_id,catalog_digest,snapshot_digest,fetch_status,expires_at,entries
    into snapshot from models.catalog_snapshot
    where realm_id=new.realm_id and id=new.catalog_snapshot_id;
  select catalog_provider_id,catalog_digest,catalog_snapshot_digest,catalog_snapshot_id
    into route from models.model_route_decision
    where realm_id=new.realm_id and evidence_digest=new.route_decision_digest;
  if row(snapshot.provider_id,snapshot.catalog_digest,snapshot.snapshot_digest,snapshot.id)
      is distinct from row(new.catalog_provider_id,new.catalog_digest,
        new.catalog_snapshot_digest,new.catalog_snapshot_id)
    or row(route.catalog_provider_id,route.catalog_digest,route.catalog_snapshot_digest,
      route.catalog_snapshot_id) is distinct from
      row(new.catalog_provider_id,new.catalog_digest,new.catalog_snapshot_digest,
        new.catalog_snapshot_id)
    or snapshot.id is distinct from (
      select current_snapshot.id from models.catalog_snapshot current_snapshot
      where current_snapshot.realm_id=new.realm_id
        and current_snapshot.provider_id=snapshot.provider_id
      order by current_snapshot.fetched_at desc,current_snapshot.id desc limit 1)
    or snapshot.fetch_status='failed' or snapshot.expires_at<=new.created_at
    or not exists(select 1 from jsonb_array_elements(snapshot.entries) entry
      where entry->>'model_id'=new.model_id) then
    raise exception 'model request catalog route/current/model binding drift'
      using errcode='42501';
  end if;
  return new;
end $$;
create trigger request_catalog_guard before insert on models.request_manifest
for each row execute function models.enforce_request_catalog();

create or replace function models.enforce_manifest_missing_bindings() returns trigger
language plpgsql as $$
declare expected text[];
begin
  select coalesce(array_agg(name order by name),'{}'::text[]) into expected from (values
    ('assignment_id',new.assignment_id is null),
    ('authorization_scope_digest',new.authorization_scope_digest is null),
    ('catalog_digest',new.catalog_digest is null),
    ('catalog_provider_id',new.catalog_provider_id is null),
    ('catalog_snapshot_digest',new.catalog_snapshot_digest is null),
    ('catalog_snapshot_id',new.catalog_snapshot_id is null),
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
    ('source_revision',new.source_revision is null),
    ('turn_execution_snapshot_digest',new.execution_envelope_id is not null
      and new.turn_execution_snapshot_digest is null),
    ('environment_digest',new.execution_envelope_id is not null and new.environment_digest is null),
    ('permission_profile_digest',new.execution_envelope_id is not null
      and new.permission_profile_digest is null),
    ('tool_set_digest',new.execution_envelope_id is not null and new.tool_set_digest is null),
    ('tool_visible_payload_digest',new.execution_envelope_id is not null
      and new.tool_visible_payload_digest is null),
    ('tool_visible_payload_mode',new.execution_envelope_id is not null
      and new.tool_visible_payload_mode is null),
    ('config_effective_digest',new.execution_envelope_id is not null
      and new.config_effective_digest is null),
    ('hook_set_digest',new.execution_envelope_id is not null and new.hook_set_digest is null)
  ) as fields(name,missing) where missing;
  if new.missing_bindings<>expected then
    raise exception 'missing bindings manifest alanlariyla exact eslesmeli' using errcode='23514';
  end if;
  return new;
end $$;

alter table models.catalog_snapshot enable row level security;
alter table models.catalog_snapshot force row level security;
create policy scope_select on models.catalog_snapshot for select
  using(realm_id=core.current_realm_id());
create policy scope_insert on models.catalog_snapshot for insert
  with check(realm_id=core.current_realm_id());
grant select,insert on models.catalog_snapshot to zekam_app;
revoke update,delete on models.catalog_snapshot from zekam_app;
grant execute on function models.catalog_canonical_json(jsonb) to zekam_app;
grant execute on function models.catalog_jsonb_digest(jsonb) to zekam_app;
