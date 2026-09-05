-- Universal model request manifest, invocation ledger and audit/enforce policy.

alter table security.authorization
  add constraint authorization_realm_scoped_key unique (realm_id,id);
alter table security.outbound_request
  add constraint outbound_request_realm_scoped_key unique (realm_id,id);
alter table runtime.job_attempt
  add constraint job_attempt_realm_scoped_key unique (realm_id,id);
alter table runtime.effect_claim
  add constraint effect_claim_realm_scoped_key unique (realm_id,id);
alter table runtime.effect_receipt
  add constraint effect_receipt_realm_scoped_key unique (realm_id,id);

create table models.gateway_policy (
  realm_id uuid primary key references core.realm(id) on delete cascade,
  mode text not null default 'audit', policy_digest text not null,
  activated_at timestamptz not null default now(),
  check (mode in ('audit','enforce')),
  check (policy_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table models.request_manifest (
  id uuid primary key, realm_id uuid not null references core.realm(id) on delete cascade,
  project_id uuid not null, work_item_id uuid not null, plan_id uuid not null,
  step_id text not null, run_id uuid, job_id uuid not null,
  attempt_id uuid not null, assignment_id uuid,
  role text, risk text not null, route_decision_digest text,
  model_id text not null, provider_ref text not null,
  context_manifest_digest text, context_packet_digest text,
  checkpoint_digest text, source_revision text,
  policy_digest text, payload_digest text not null,
  authorization_scope_digest text, output_schema_digest text,
  idempotency_key text not null, max_input_tokens integer,
  max_output_tokens integer, max_cost_micros bigint,
  deadline timestamptz not null, route_expires_at timestamptz, source_label text not null,
  missing_bindings text[] not null default '{}', binding_status text not null,
  tool_contract_digest text, environment_digest text,
  permission_profile_digest text, tool_set_digest text,
  created_at timestamptz not null, manifest_digest text not null,
  unique(realm_id,id), unique(realm_id,manifest_digest), unique(realm_id,idempotency_key),
  foreign key(realm_id,project_id) references projects.project(realm_id,id),
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id),
  foreign key(realm_id,job_id) references runtime.job(realm_id,id),
  foreign key(realm_id,assignment_id) references agents.assignment(realm_id,id),
  foreign key(realm_id,attempt_id) references runtime.job_attempt(realm_id,id),
  check (btrim(step_id)<>'' and (role is null or btrim(role)<>'') and btrim(model_id)<>'' and btrim(provider_ref)<>''),
  check (risk in ('low','medium','high','critical')),
  check (source_label in ('opencode-embedding','provider-contract','model-campaign','model-capability','model-benchmark')),
  check (binding_status in ('bound','unbound')),
  check ((binding_status='bound')=(cardinality(missing_bindings)=0)),
  check ((max_input_tokens is null or max_input_tokens>0)
    and (max_output_tokens is null or max_output_tokens>0)
    and (max_cost_micros is null or max_cost_micros>0) and deadline>created_at),
  check (manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
  check (route_decision_digest is null or route_decision_digest ~ '^sha256:[0-9a-f]{64}$'),
  check (context_manifest_digest is null or context_manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
  check (context_packet_digest is null or context_packet_digest ~ '^sha256:[0-9a-f]{64}$'),
  check (checkpoint_digest is null or checkpoint_digest ~ '^sha256:[0-9a-f]{64}$'),
  check (policy_digest is null or policy_digest ~ '^sha256:[0-9a-f]{64}$'),
  check (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
  check (authorization_scope_digest is null or authorization_scope_digest ~ '^sha256:[0-9a-f]{64}$'),
  check (output_schema_digest is null or output_schema_digest ~ '^sha256:[0-9a-f]{64}$')
);

create function models.enforce_manifest_missing_bindings() returns trigger language plpgsql as $$
declare expected text[];
begin
  select coalesce(array_agg(name order by name),'{}'::text[]) into expected from (values
    ('assignment_id',new.assignment_id is null),
    ('authorization_scope_digest',new.authorization_scope_digest is null),
    ('checkpoint_digest',new.checkpoint_digest is null),
    ('context_manifest_digest',new.context_manifest_digest is null),
    ('context_packet_digest',new.context_packet_digest is null),
    ('max_cost_micros',new.max_cost_micros is null),
    ('max_input_tokens',new.max_input_tokens is null),
    ('max_output_tokens',new.max_output_tokens is null),
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
create trigger manifest_missing_check before insert on models.request_manifest
for each row execute function models.enforce_manifest_missing_bindings();

create table models.invocation_attempt (
  id uuid primary key, realm_id uuid not null, manifest_id uuid not null,
  ordinal integer not null, effect_claim_id uuid, outbound_request_id uuid,
  authorization_id uuid, state text not null, created_at timestamptz not null default now(),
  unique(realm_id,id), unique(realm_id,manifest_id,ordinal),
  foreign key(realm_id,manifest_id) references models.request_manifest(realm_id,id),
  foreign key(realm_id,effect_claim_id) references runtime.effect_claim(realm_id,id),
  foreign key(realm_id,outbound_request_id) references security.outbound_request(realm_id,id),
  foreign key(realm_id,authorization_id) references security.authorization(realm_id,id),
  check(ordinal>=1),
  check(state in ('prepared','authorized','claimed','sent','response-received','parsed','verified','rejected','reconciliation-required'))
);

create table models.invocation_audit (
  id uuid primary key, realm_id uuid not null references core.realm(id) on delete cascade,
  manifest_id uuid, source_label text not null, disposition text not null,
  missing_bindings text[] not null default '{}', call_digest text not null,
  payload_digest text not null, response_digest text, created_at timestamptz not null default now(),
  foreign key(realm_id,manifest_id) references models.request_manifest(realm_id,id),
  unique(realm_id,id),
  check(source_label in ('opencode-embedding','provider-contract','model-campaign','model-capability','model-benchmark')),
  check(disposition in ('bound','unbound','bypass','rejected')),
  check(call_digest ~ '^sha256:[0-9a-f]{64}$' and payload_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(response_digest is null or response_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table models.invocation_result (
  id uuid primary key, realm_id uuid not null, manifest_id uuid not null,
  attempt_id uuid not null, effect_receipt_id uuid,
  state text not null, response_digest text, envelope_digest text,
  failure_digest text, created_at timestamptz not null default now(),
  foreign key(realm_id,manifest_id) references models.request_manifest(realm_id,id),
  foreign key(realm_id,attempt_id) references models.invocation_attempt(realm_id,id),
  foreign key(realm_id,effect_receipt_id) references runtime.effect_receipt(realm_id,id),
  unique(realm_id,id), unique(realm_id,attempt_id),
  check(state in ('verified','rejected','reconciliation-required')),
  check(response_digest is null or response_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(envelope_digest is null or envelope_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(failure_digest is null or failure_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table models.invocation_usage (
  realm_id uuid not null, manifest_id uuid not null, attempt_id uuid not null,
  input_tokens integer not null, output_tokens integer not null,
  cost_micros bigint not null, latency_ms integer not null,
  foreign key(realm_id,manifest_id) references models.request_manifest(realm_id,id),
  foreign key(realm_id,attempt_id) references models.invocation_attempt(realm_id,id),
  primary key(realm_id,attempt_id),
  check(input_tokens>=0 and output_tokens>=0 and cost_micros>=0 and latency_ms>=0)
);

create function models.activate_gateway_enforce(p_policy_digest text) returns void
language plpgsql security invoker set search_path=pg_catalog,models,core as $$
declare rid uuid := core.current_realm_id();
begin
  if p_policy_digest !~ '^sha256:[0-9a-f]{64}$' then raise exception 'policy digest invalid' using errcode='23514'; end if;
  perform pg_advisory_xact_lock(hashtextextended(rid::text,0));
  if exists(select 1 from models.invocation_audit where realm_id=rid and disposition in ('unbound','bypass')) then
    raise exception 'gateway enforce requires zero unbound/bypass audit' using errcode='23514';
  end if;
  insert into models.gateway_policy(realm_id,mode,policy_digest) values(rid,'enforce',p_policy_digest)
  on conflict(realm_id) do update set mode='enforce',policy_digest=excluded.policy_digest,activated_at=now();
end $$;

create function models.enforce_gateway_attempt() returns trigger language plpgsql security invoker
set search_path=pg_catalog,models,core as $$
declare policy_mode text; manifest_status text;
begin
  select mode into policy_mode from models.gateway_policy where realm_id=new.realm_id;
  if coalesce(policy_mode,'audit')='enforce' and new.state in ('sent','response-received','parsed','verified') then
    select binding_status into manifest_status from models.request_manifest where realm_id=new.realm_id and id=new.manifest_id;
    if manifest_status is distinct from 'bound' or new.effect_claim_id is null or new.authorization_id is null then
      raise exception 'gateway enforce exact bound manifest/claim/authorization ister' using errcode='42501';
    end if;
  end if;
  return new;
end $$;
create trigger gateway_attempt_check before insert on models.invocation_attempt
for each row execute function models.enforce_gateway_attempt();

do $$ declare target text; begin foreach target in array array[
 'models.gateway_policy','models.request_manifest','models.invocation_attempt',
 'models.invocation_audit','models.invocation_result','models.invocation_usage'
] loop
 execute format('alter table %s enable row level security',target);
 execute format('alter table %s force row level security',target);
 execute format('create policy scope_select on %s for select using (realm_id=core.current_realm_id())',target);
 execute format('create policy scope_insert on %s for insert with check (realm_id=core.current_realm_id())',target);
end loop; end $$;
create policy scope_update on models.gateway_policy for update using(realm_id=core.current_realm_id()) with check(realm_id=core.current_realm_id());

create trigger manifest_no_mutation before update or delete on models.request_manifest for each statement execute function core.deny_mutation();
create trigger attempt_no_mutation before update or delete on models.invocation_attempt for each statement execute function core.deny_mutation();
create trigger audit_no_mutation before update or delete on models.invocation_audit for each statement execute function core.deny_mutation();
create trigger result_no_mutation before update or delete on models.invocation_result for each statement execute function core.deny_mutation();
create trigger usage_no_mutation before update or delete on models.invocation_usage for each statement execute function core.deny_mutation();

grant select,insert on models.request_manifest,models.invocation_attempt,models.invocation_audit,models.invocation_result,models.invocation_usage to zekam_app;
grant select,insert,update on models.gateway_policy to zekam_app;
grant execute on function models.activate_gateway_enforce(text) to zekam_app;
