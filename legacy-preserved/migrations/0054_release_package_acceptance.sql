-- Shipped wheel/sdist/container acceptance evidence. Authority uretmez.
create schema if not exists release;

create function release.canonical_json(value_ jsonb) returns text
language plpgsql immutable strict set search_path=pg_catalog,release as $$
declare kind text:=jsonb_typeof(value_); result_ text;
begin
  if kind in ('null','boolean','number') then return value_::text; end if;
  if kind='string' then return to_jsonb(value_#>>'{}')::text; end if;
  if kind='array' then
    select '['||coalesce(string_agg(release.canonical_json(value),',' order by ordinal),'')||']'
      into result_ from jsonb_array_elements(value_) with ordinality item(value,ordinal);
    return result_;
  end if;
  select '{'||coalesce(string_agg(to_jsonb(key)::text||':'||release.canonical_json(value),','
      order by key collate "C"),'')||'}' into result_
    from jsonb_each(value_) item(key,value);
  return result_;
end $$;

create function release.jsonb_digest(value_ jsonb) returns text
language sql immutable strict set search_path=pg_catalog,release,public as $$
  select 'sha256:'||encode(public.digest(convert_to(release.canonical_json(value_),'UTF8'),'sha256'),'hex')
$$;

create table release.package_manifest (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  artifact_kind text not null,
  artifact_digest text not null,
  source_revision text not null,
  manifest_body jsonb not null,
  manifest_digest text not null,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),
  unique(realm_id,artifact_kind,artifact_digest),
  check(artifact_kind in ('wheel','sdist','container')),
  check(artifact_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(source_revision=btrim(source_revision) and source_revision<>'' and length(source_revision)<=256),
  check(jsonb_typeof(manifest_body)='object'),
  check(not grants_authority)
);

create table release.acceptance_run (
  id uuid primary key,
  realm_id uuid not null,
  package_manifest_id uuid not null,
  run_body jsonb not null,
  run_digest text not null,
  status text not null,
  platform text not null,
  python_version text not null,
  builder_identity text not null,
  verifier_identity text not null,
  builder_assignment_id uuid not null,
  builder_invocation_id uuid not null,
  builder_execution_identity text not null,
  builder_envelope_digest text not null,
  verifier_assignment_id uuid not null,
  verifier_invocation_id uuid not null,
  verifier_execution_identity text not null,
  verifier_envelope_digest text not null,
  verifier_source_digest text not null,
  verifier_provenance_digest text not null,
  authorization_id uuid not null,
  claim_id uuid not null,
  receipt_id uuid not null,
  started_at timestamptz not null,
  completed_at timestamptz not null,
  isolated_environment boolean not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),
  unique(realm_id,run_digest),
  foreign key(realm_id,package_manifest_id)
    references release.package_manifest(realm_id,id) on delete restrict,
  foreign key(realm_id,builder_assignment_id)
    references agents.assignment(realm_id,id) on delete restrict,
  foreign key(realm_id,builder_invocation_id)
    references agents.invocation(realm_id,id) on delete restrict,
  foreign key(realm_id,verifier_assignment_id)
    references agents.assignment(realm_id,id) on delete restrict,
  foreign key(realm_id,verifier_invocation_id)
    references agents.invocation(realm_id,id) on delete restrict,
  check(run_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(status in ('passed','failed','skipped')),
  check(platform=btrim(platform) and platform<>'' and length(platform)<=256),
  check(python_version=btrim(python_version) and python_version<>'' and length(python_version)<=256),
  check(builder_identity=btrim(builder_identity) and builder_identity<>'' and length(builder_identity)<=256),
  check(verifier_identity=btrim(verifier_identity) and verifier_identity<>'' and length(verifier_identity)<=256),
  check(builder_identity<>verifier_identity),
  check(builder_assignment_id<>verifier_assignment_id),
  check(builder_invocation_id<>verifier_invocation_id),
  check(builder_execution_identity<>verifier_execution_identity),
  check(builder_execution_identity=btrim(builder_execution_identity)
    and builder_execution_identity<>'' and length(builder_execution_identity)<=256),
  check(verifier_execution_identity=btrim(verifier_execution_identity)
    and verifier_execution_identity<>'' and length(verifier_execution_identity)<=256),
  check(builder_envelope_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(verifier_envelope_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(verifier_source_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(verifier_provenance_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(completed_at>=started_at),
  check(isolated_environment),
  check(not grants_authority)
);

create table release.acceptance_result (
  id uuid primary key,
  realm_id uuid not null,
  acceptance_run_id uuid not null,
  check_id text not null,
  status text not null,
  result_body jsonb not null,
  result_digest text not null,
  command_digest text not null,
  stdout_digest text not null,
  stderr_digest text not null,
  duration_ms bigint not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),
  unique(realm_id,acceptance_run_id,check_id),
  foreign key(realm_id,acceptance_run_id)
    references release.acceptance_run(realm_id,id) on delete restrict,
  check(check_id ~ '^[a-z][a-z0-9_.-]{0,95}$'),
  check(status in ('passed','failed','skipped')),
  check(result_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(command_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(stdout_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(stderr_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(duration_ms>=0),
  check(not grants_authority)
);

create function release.enforce_package_manifest() returns trigger
language plpgsql security invoker set search_path=pg_catalog,release as $$
declare keys text[];
begin
  select array_agg(key order by key) into keys from jsonb_object_keys(new.manifest_body) key;
  if keys<>array['agent_template_digest','build_provenance_digest','config_bundle_digest',
      'entrypoints','included_migrations_digest','protocol_schema_digest','python','schema','version']
    or new.manifest_body->>'schema'<>'zekam-package-manifest/v2'
    or jsonb_typeof(new.manifest_body->'entrypoints')<>'array'
    or jsonb_array_length(new.manifest_body->'entrypoints')=0
    or new.manifest_body->>'version'!~'^\d+\.\d+\.\d+[A-Za-z0-9.+-]*$'
    or new.manifest_body->>'python'!~'^>='
    or exists(select 1 from jsonb_array_elements_text(new.manifest_body->'entrypoints') value
      where btrim(value)='' or length(value)>128)
    or (select count(*) from jsonb_array_elements_text(new.manifest_body->'entrypoints'))<>
       (select count(distinct value) from jsonb_array_elements_text(new.manifest_body->'entrypoints') value)
    or (select coalesce(jsonb_agg(value order by value),'[]'::jsonb)
        from jsonb_array_elements(new.manifest_body->'entrypoints'))<>new.manifest_body->'entrypoints'
    or exists(select 1 from (values
      (new.manifest_body->>'included_migrations_digest'),
      (new.manifest_body->>'config_bundle_digest'),
      (new.manifest_body->>'protocol_schema_digest'),
      (new.manifest_body->>'agent_template_digest'),
      (new.manifest_body->>'build_provenance_digest')) item(value)
      where value!~'^sha256:[0-9a-f]{64}$')
    or new.manifest_digest<>release.jsonb_digest(new.manifest_body) then
    raise exception 'release package manifest schema/digest drift' using errcode='23514';
  end if;
  return new;
end $$;

create trigger package_manifest_guard before insert on release.package_manifest
for each row execute function release.enforce_package_manifest();

create function release.enforce_acceptance_run() returns trigger
language plpgsql security invoker set search_path=pg_catalog,release,security,runtime as $$
declare manifest record; evidence record; agent_evidence record; keys text[]; provenance_keys text[];
begin
  select artifact_kind,artifact_digest,source_revision,manifest_digest into manifest
    from release.package_manifest where realm_id=new.realm_id and id=new.package_manifest_id;
  select array_agg(key order by key) into keys from jsonb_object_keys(new.run_body) key;
  if keys<>array['artifact_digest','artifact_kind','builder_identity','completed_at',
      'grants_authority','id','isolated_environment','manifest_digest','platform',
      'python_version','results','schema','source_revision','started_at','status',
      'suite_digest','verifier_identity','verifier_provenance','verifier_provenance_digest']
    or new.run_body->>'schema'<>'zekam-package-acceptance-run/v1'
    or (new.run_body->>'id')::uuid<>new.id
    or new.run_body->>'manifest_digest'<>manifest.manifest_digest
    or new.run_body->>'artifact_digest'<>manifest.artifact_digest
    or new.run_body->>'artifact_kind'<>manifest.artifact_kind
    or new.run_body->>'source_revision'<>manifest.source_revision
    or new.run_body->>'status'<>new.status
    or new.run_body->>'platform'<>new.platform
    or new.run_body->>'python_version'<>new.python_version
    or new.run_body->>'builder_identity'<>new.builder_identity
    or new.run_body->>'verifier_identity'<>new.verifier_identity
    or new.run_body->'verifier_provenance' is null
    or new.run_body->>'verifier_provenance_digest'<>new.verifier_provenance_digest
    or (new.run_body->>'started_at')::timestamptz<>new.started_at
    or (new.run_body->>'completed_at')::timestamptz<>new.completed_at
    or new.run_body->>'suite_digest'!~'^sha256:[0-9a-f]{64}$'
    or new.run_body->'isolated_environment'<>'true'::jsonb
    or new.run_body->'grants_authority'<>'false'::jsonb
    or jsonb_typeof(new.run_body->'results')<>'array'
    or jsonb_array_length(new.run_body->'results')=0
    or new.run_digest<>release.jsonb_digest(new.run_body) then
    raise exception 'release acceptance run schema/digest drift' using errcode='23514';
  end if;
  select array_agg(key order by key) into provenance_keys
    from jsonb_object_keys(new.run_body->'verifier_provenance') key;
  if provenance_keys<>array['builder_assignment_id','builder_envelope_digest',
      'builder_execution_identity','builder_invocation_id','schema','verifier_assignment_id',
      'verifier_envelope_digest','verifier_execution_identity','verifier_invocation_id',
      'verifier_source_digest']
    or new.run_body->'verifier_provenance'->>'schema'<>'zekam-package-verifier-provenance/v1'
    or (new.run_body->'verifier_provenance'->>'builder_assignment_id')::uuid<>new.builder_assignment_id
    or (new.run_body->'verifier_provenance'->>'builder_invocation_id')::uuid<>new.builder_invocation_id
    or new.run_body->'verifier_provenance'->>'builder_execution_identity'<>new.builder_execution_identity
    or new.run_body->'verifier_provenance'->>'builder_envelope_digest'<>new.builder_envelope_digest
    or (new.run_body->'verifier_provenance'->>'verifier_assignment_id')::uuid<>new.verifier_assignment_id
    or (new.run_body->'verifier_provenance'->>'verifier_invocation_id')::uuid<>new.verifier_invocation_id
    or new.run_body->'verifier_provenance'->>'verifier_execution_identity'<>new.verifier_execution_identity
    or new.run_body->'verifier_provenance'->>'verifier_envelope_digest'<>new.verifier_envelope_digest
    or new.run_body->'verifier_provenance'->>'verifier_source_digest'<>new.verifier_source_digest
    or new.verifier_provenance_digest<>release.jsonb_digest(new.run_body->'verifier_provenance') then
    raise exception 'release acceptance verifier provenance schema/digest drift' using errcode='23514';
  end if;
  select ba.role builder_role,ba.agent_ref builder_agent,bi.execution_identity builder_execution,
      br.envelope_digest builder_envelope,va.role verifier_role,va.agent_ref verifier_agent,
      vi.execution_identity verifier_execution,vr.envelope_digest verifier_envelope
    into agent_evidence
    from agents.assignment ba
    join agents.invocation bi on bi.realm_id=ba.realm_id and bi.assignment_id=ba.id
      and bi.id=new.builder_invocation_id
    join agents.result_receipt br on br.realm_id=bi.realm_id and br.assignment_id=ba.id
      and br.invocation_id=bi.id
    join agents.assignment va on va.realm_id=ba.realm_id and va.id=new.verifier_assignment_id
    join agents.invocation vi on vi.realm_id=va.realm_id and vi.assignment_id=va.id
      and vi.id=new.verifier_invocation_id
    join agents.result_receipt vr on vr.realm_id=vi.realm_id and vr.assignment_id=va.id
      and vr.invocation_id=vi.id
    where ba.realm_id=new.realm_id and ba.id=new.builder_assignment_id;
  if agent_evidence is null or agent_evidence.builder_role<>'builder'
    or agent_evidence.verifier_role<>'verifier'
    or agent_evidence.builder_agent<>new.builder_identity
    or agent_evidence.verifier_agent<>new.verifier_identity
    or agent_evidence.builder_agent=agent_evidence.verifier_agent
    or agent_evidence.builder_execution<>new.builder_execution_identity
    or agent_evidence.verifier_execution<>new.verifier_execution_identity
    or agent_evidence.builder_execution=agent_evidence.verifier_execution
    or agent_evidence.builder_envelope<>new.builder_envelope_digest
    or agent_evidence.verifier_envelope<>new.verifier_envelope_digest then
    raise exception 'release acceptance canonical verifier receipt drift' using errcode='42501';
  end if;
  select a.state,a.allowed_resources,a.allowed_effects,c.operation,c.authorization_id,
    r.status receipt_status,r.result_digest into evidence
    from security.authorization a
    join runtime.effect_claim c on c.realm_id=a.realm_id and c.id=new.claim_id
    join runtime.effect_receipt r on r.realm_id=c.realm_id and r.claim_id=c.id and r.id=new.receipt_id
    where a.realm_id=new.realm_id and a.id=new.authorization_id;
  if evidence is null or evidence.state<>'consumed'
    or evidence.operation<>'package-acceptance'
    or evidence.authorization_id<>new.authorization_id
    or not ('process-run'=any(evidence.allowed_effects))
    or not (('artifact:release:'||manifest.artifact_digest)=any(evidence.allowed_resources))
    or evidence.receipt_status<>'completed'
    or evidence.result_digest<>new.run_digest then
    raise exception 'release acceptance authorization claim receipt drift' using errcode='42501';
  end if;
  return new;
end $$;

create trigger acceptance_run_guard before insert on release.acceptance_run
for each row execute function release.enforce_acceptance_run();

create function release.enforce_acceptance_result() returns trigger
language plpgsql security invoker set search_path=pg_catalog,release as $$
declare keys text[];
begin
  select array_agg(key order by key) into keys from jsonb_object_keys(new.result_body) key;
  if keys<>array['check_id','command_digest','detail','duration_ms','status','stderr_digest','stdout_digest']
    or new.result_body->>'check_id'<>new.check_id
    or new.result_body->>'status'<>new.status
    or new.result_body->>'command_digest'<>new.command_digest
    or new.result_body->>'stdout_digest'<>new.stdout_digest
    or new.result_body->>'stderr_digest'<>new.stderr_digest
    or (new.result_body->>'duration_ms')::bigint<>new.duration_ms
    or new.result_digest<>release.jsonb_digest(new.result_body) then
    raise exception 'release acceptance result schema/digest drift' using errcode='23514';
  end if;
  return new;
end $$;

create trigger acceptance_result_guard before insert on release.acceptance_result
for each row execute function release.enforce_acceptance_result();

create function release.verify_acceptance_result_set() returns trigger
language plpgsql security invoker set search_path=pg_catalog,release as $$
declare expected jsonb; actual jsonb; run_status text; run_id_ uuid;
begin
  if tg_table_name='acceptance_run' then run_id_:=new.id;
  else run_id_:=new.acceptance_run_id; end if;
  select run_body->'results',status into expected,run_status from release.acceptance_run
    where realm_id=new.realm_id and id=run_id_;
  select coalesce(jsonb_agg(result_body||jsonb_build_object('result_digest',result_digest)
      order by check_id),'[]'::jsonb) into actual from release.acceptance_result
    where realm_id=new.realm_id and acceptance_run_id=run_id_;
  if expected is distinct from actual
    or (run_status='passed' and exists(select 1 from release.acceptance_result
      where realm_id=new.realm_id and acceptance_run_id=run_id_
        and status<>'passed')) then
    raise exception 'release acceptance result set drift' using errcode='23514';
  end if;
  return null;
end $$;

create constraint trigger acceptance_run_result_set
after insert on release.acceptance_run deferrable initially deferred
for each row execute function release.verify_acceptance_result_set();
create constraint trigger acceptance_result_set
after insert on release.acceptance_result deferrable initially deferred
for each row execute function release.verify_acceptance_result_set();

create function release.deny_mutation() returns trigger
language plpgsql security invoker set search_path=pg_catalog as $$
begin raise exception 'release acceptance records append-only' using errcode='23514'; end $$;

do $$ declare target regclass; begin
  foreach target in array array[
    'release.package_manifest'::regclass,
    'release.acceptance_run'::regclass,
    'release.acceptance_result'::regclass
  ] loop
    execute format('alter table %s enable row level security',target);
    execute format('alter table %s force row level security',target);
    execute format('create policy scope_select on %s for select using(realm_id=core.current_realm_id())',target);
    execute format('create policy scope_insert on %s for insert with check(realm_id=core.current_realm_id())',target);
    execute format('grant select,insert on %s to zekam_app',target);
    execute format('create trigger deny_update before update on %s for each statement execute function release.deny_mutation()',target);
    execute format('create trigger deny_delete before delete on %s for each statement execute function release.deny_mutation()',target);
  end loop;
end $$;

grant usage on schema release to zekam_app;
revoke all on function release.canonical_json(jsonb) from public;
revoke all on function release.jsonb_digest(jsonb) from public;
grant execute on function release.canonical_json(jsonb) to zekam_app;
grant execute on function release.jsonb_digest(jsonb) to zekam_app;
