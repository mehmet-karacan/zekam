-- Typed session continuity, Memory Contract and candidate-only compiler ledger.

create schema if not exists continuity;
grant usage on schema continuity to zekam_app;

create function continuity.canonical_json(value_ jsonb) returns text
language plpgsql immutable strict set search_path=pg_catalog,continuity as $$
declare kind text:=jsonb_typeof(value_); result_ text;
begin
  if kind in ('null','boolean','number') then return value_::text; end if;
  if kind='string' then return to_jsonb(value_#>>'{}')::text; end if;
  if kind='array' then
    select '['||coalesce(string_agg(continuity.canonical_json(value),',' order by ordinal),'')||']'
      into result_ from jsonb_array_elements(value_) with ordinality item(value,ordinal);
    return result_;
  end if;
  select '{'||coalesce(string_agg(to_jsonb(key)::text||':'||continuity.canonical_json(value),','
      order by key collate "C"),'')||'}' into result_
    from jsonb_each(value_) item(key,value);
  return result_;
end $$;

create function continuity.jsonb_digest(value_ jsonb) returns text
language sql immutable strict set search_path=pg_catalog,continuity,public as $$
  select 'sha256:'||encode(public.digest(
    convert_to(continuity.canonical_json(value_),'UTF8'),'sha256'),'hex')
$$;

create function continuity.contains_forbidden_key(value_ jsonb) returns boolean
language sql immutable strict set search_path=pg_catalog,continuity as $$
with recursive nodes(value) as (
  select value_
  union all
  select child.value
    from nodes parent
    cross join lateral (
      select value from jsonb_each(case when jsonb_typeof(parent.value)='object'
        then parent.value else '{}'::jsonb end)
      union all
      select value from jsonb_array_elements(case when jsonb_typeof(parent.value)='array'
        then parent.value else '[]'::jsonb end)
    ) child
)
select exists(
  select 1 from nodes node, lateral jsonb_object_keys(
    case when jsonb_typeof(node.value)='object' then node.value else '{}'::jsonb end) key
  where key ~* '^(secret([-_]?value)?|credential([-_]?value)?|password|private[-_]?key|prompt([-_]?(body|text))?|response([-_]?(body|text))?|transcript([-_]?(body|text))?|raw[-_]?(content|prompt|response|transcript)|owner[-_]?token)$'
)
$$;

create function continuity.valid_classification(value_ text) returns boolean
language sql immutable strict set search_path=pg_catalog as $$
  select value_ in ('public','internal','confidential','restricted','local-only','pii',
    'corporate-confidential','secret','raw-transcript','diagnostic-payload')
$$;

create table continuity.session_lifecycle_event (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  project_id uuid not null,
  work_item_id uuid not null,
  run_id uuid not null,
  session_id text not null,
  client_id text not null,
  event_type text not null,
  sequence bigint not null,
  previous_digest text,
  origin text not null,
  causation_id text not null,
  correlation_id text not null,
  recursion_depth integer not null,
  classification text not null,
  idempotency_key text not null,
  event_body jsonb not null,
  event_digest text not null,
  occurred_at timestamptz not null,
  ingested_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id),
  unique(realm_id,idempotency_key),
  unique(realm_id,client_id,session_id,sequence),
  unique(realm_id,event_digest),
  foreign key(realm_id,project_id) references projects.project(realm_id,id) on delete restrict,
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id) on delete restrict,
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id) on delete restrict,
  check(event_type ~ '^[a-z][a-z0-9_.:-]{0,95}$'),
  check(btrim(session_id)<>'' and length(session_id)<=512),
  check(btrim(client_id)<>'' and length(client_id)<=512),
  check(btrim(origin)<>'' and length(origin)<=512),
  check(btrim(causation_id)<>'' and length(causation_id)<=512),
  check(btrim(correlation_id)<>'' and length(correlation_id)<=512),
  check(btrim(idempotency_key)<>'' and length(idempotency_key)<=512),
  check(sequence>0 and (sequence=1)=(previous_digest is null)),
  check(previous_digest is null or previous_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(event_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(recursion_depth between 0 and 16),
  check(continuity.valid_classification(classification)),
  check(ingested_at>=occurred_at),
  check(jsonb_typeof(event_body)='object'),
  check(not continuity.contains_forbidden_key(event_body)),
  check(event_body->'contains_prompt'='false'::jsonb
    and event_body->'contains_response'='false'::jsonb
    and event_body->'contains_transcript'='false'::jsonb
    and event_body->'grants_authority'='false'::jsonb),
  check(not grants_authority)
);

create table continuity.lifecycle_delivery_outbox (
  id uuid primary key,
  realm_id uuid not null,
  event_id uuid not null,
  plan_digest text not null,
  payload_digest text not null,
  state text not null default 'pending',
  terminal_receipt_digest text,
  created_at timestamptz not null,
  completed_at timestamptz,
  grants_authority boolean not null default false,
  unique(realm_id,id),
  unique(realm_id,event_id),
  foreign key(realm_id,event_id)
    references continuity.session_lifecycle_event(realm_id,id) on delete restrict,
  check(plan_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(payload_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(terminal_receipt_digest is null or terminal_receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(state in ('pending','processing','completed','failed','recovery-required')),
  check((state in ('pending','processing') and terminal_receipt_digest is null and completed_at is null)
    or (state in ('completed','failed','recovery-required')
      and terminal_receipt_digest is not null and completed_at is not null)),
  check(not grants_authority)
);

create table continuity.session_hydration_receipt (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  project_id uuid not null,
  work_item_id uuid not null,
  run_id uuid not null,
  session_id text not null,
  client_id text not null,
  idempotency_key text not null,
  receipt_body jsonb not null,
  receipt_digest text not null,
  fresh boolean not null,
  complete boolean not null,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,idempotency_key), unique(realm_id,receipt_digest),
  foreign key(realm_id,project_id) references projects.project(realm_id,id) on delete restrict,
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id) on delete restrict,
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id) on delete restrict,
  check(btrim(session_id)<>'' and length(session_id)<=512),
  check(btrim(client_id)<>'' and length(client_id)<=512),
  check(btrim(idempotency_key)<>'' and length(idempotency_key)<=512),
  check(receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(receipt_body)='object' and not continuity.contains_forbidden_key(receipt_body)),
  check(receipt_body->'grants_authority'='false'::jsonb),
  check(not grants_authority)
);

create table continuity.session_close_receipt (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  project_id uuid not null,
  work_item_id uuid not null,
  run_id uuid not null,
  session_id text not null,
  client_id text not null,
  job_id uuid not null,
  attempt_id uuid not null,
  close_status text not null,
  idempotency_key text not null,
  receipt_body jsonb not null,
  receipt_digest text not null,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,idempotency_key), unique(realm_id,receipt_digest),
  foreign key(realm_id,project_id) references projects.project(realm_id,id) on delete restrict,
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id) on delete restrict,
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id) on delete restrict,
  foreign key(realm_id,job_id) references runtime.job(realm_id,id) on delete restrict,
  foreign key(realm_id,attempt_id) references runtime.job_attempt(realm_id,id) on delete restrict,
  check(close_status in ('closed','degraded','recovery-required','failed')),
  check(btrim(session_id)<>'' and length(session_id)<=512),
  check(btrim(client_id)<>'' and length(client_id)<=512),
  check(btrim(idempotency_key)<>'' and length(idempotency_key)<=512),
  check(receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(receipt_body)='object' and not continuity.contains_forbidden_key(receipt_body)),
  check(receipt_body->'grants_authority'='false'::jsonb),
  check(not grants_authority)
);

create table continuity.compaction_receipt (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  project_id uuid not null,
  work_item_id uuid not null,
  run_id uuid not null,
  session_id text not null,
  client_id text not null,
  pre_compaction_event_digest text not null,
  checkpoint_digest text,
  hydration_receipt_id uuid,
  status text not null,
  idempotency_key text not null,
  receipt_body jsonb not null,
  receipt_digest text not null,
  created_at timestamptz not null,
  completed_at timestamptz,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,idempotency_key), unique(realm_id,receipt_digest),
  foreign key(realm_id,project_id) references projects.project(realm_id,id) on delete restrict,
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id) on delete restrict,
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id) on delete restrict,
  foreign key(realm_id,hydration_receipt_id)
    references continuity.session_hydration_receipt(realm_id,id) on delete restrict,
  check(status in ('prepared','completed','recovery-required','failed')),
  check(pre_compaction_event_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(checkpoint_digest is null or checkpoint_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(btrim(session_id)<>'' and length(session_id)<=512),
  check(btrim(client_id)<>'' and length(client_id)<=512),
  check(btrim(idempotency_key)<>'' and length(idempotency_key)<=512),
  check(receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(receipt_body)='object' and not continuity.contains_forbidden_key(receipt_body)),
  check(receipt_body->'grants_authority'='false'::jsonb),
  check((status='completed' and checkpoint_digest is not null
      and hydration_receipt_id is not null and completed_at is not null)
    or (status<>'completed' and completed_at is null)),
  check(not grants_authority)
);

create table continuity.memory_contract_evaluation (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  project_id uuid not null,
  work_item_id uuid not null,
  run_id uuid not null,
  source_revision text not null,
  policy_version text not null,
  evaluator_version text not null,
  passed boolean not null,
  idempotency_key text not null,
  evaluation_body jsonb not null,
  evaluation_digest text not null,
  evaluated_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,idempotency_key), unique(realm_id,evaluation_digest),
  foreign key(realm_id,project_id) references projects.project(realm_id,id) on delete restrict,
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id) on delete restrict,
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id) on delete restrict,
  check(btrim(source_revision)<>'' and length(source_revision)<=512),
  check(btrim(policy_version)<>'' and length(policy_version)<=512),
  check(btrim(evaluator_version)<>'' and length(evaluator_version)<=512),
  check(btrim(idempotency_key)<>'' and length(idempotency_key)<=512),
  check(evaluation_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(evaluation_body)='object'
    and jsonb_array_length(evaluation_body->'results')=20
    and not continuity.contains_forbidden_key(evaluation_body)),
  check(evaluation_body->'grants_authority'='false'::jsonb),
  check(not grants_authority)
);

create table memory.compiler_watermark_claim (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  project_id uuid not null,
  work_item_id uuid not null,
  run_id uuid not null,
  idempotency_key text not null,
  source_set_digest text not null,
  source_watermark text not null,
  state text not null default 'pending',
  compiler_run_id uuid,
  result_digest text,
  claimed_at timestamptz not null,
  completed_at timestamptz,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,idempotency_key),
  unique(realm_id,project_id,source_set_digest,source_watermark),
  foreign key(realm_id,project_id) references projects.project(realm_id,id) on delete restrict,
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id) on delete restrict,
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id) on delete restrict,
  check(source_set_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(result_digest is null or result_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(btrim(source_watermark)<>'' and length(source_watermark)<=512),
  check(btrim(idempotency_key)<>'' and length(idempotency_key)<=512),
  check(state in ('pending','processing','completed','failed','recovery-required')),
  check((state in ('pending','processing') and compiler_run_id is null
      and result_digest is null and completed_at is null)
    or (state in ('completed','failed','recovery-required')
      and result_digest is not null and completed_at is not null)),
  check(not grants_authority)
);

create table memory.compiler_run (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  project_id uuid not null,
  work_item_id uuid not null,
  run_id uuid not null,
  watermark_claim_id uuid not null,
  source_set jsonb not null,
  source_watermark text not null,
  output_body jsonb not null,
  output_digest text not null,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,output_digest), unique(realm_id,watermark_claim_id),
  foreign key(realm_id,project_id) references projects.project(realm_id,id) on delete restrict,
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id) on delete restrict,
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id) on delete restrict,
  foreign key(realm_id,watermark_claim_id)
    references memory.compiler_watermark_claim(realm_id,id) on delete restrict,
  check(jsonb_typeof(source_set)='array' and jsonb_array_length(source_set)>0
    and jsonb_array_length(source_set)<=128),
  check(btrim(source_watermark)<>'' and length(source_watermark)<=512),
  check(output_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(output_body)='object' and not continuity.contains_forbidden_key(output_body)),
  check(output_body->'direct_promotion'='false'::jsonb
    and output_body->'grants_authority'='false'::jsonb),
  check(not grants_authority)
);

alter table memory.compiler_watermark_claim
  add constraint compiler_watermark_run_same_realm foreign key(realm_id,compiler_run_id)
    references memory.compiler_run(realm_id,id) deferrable initially deferred;

create table memory.compiler_candidate (
  id uuid primary key,
  realm_id uuid not null,
  compiler_run_id uuid not null,
  logical_candidate_id text not null,
  candidate_type text not null,
  truth_class text not null,
  classification text not null,
  risk text not null,
  state text not null default 'candidate',
  is_current boolean not null default true,
  superseded_by uuid,
  candidate_body jsonb not null,
  candidate_digest text not null,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,compiler_run_id,logical_candidate_id),
  unique(realm_id,candidate_digest),
  foreign key(realm_id,compiler_run_id) references memory.compiler_run(realm_id,id) on delete restrict,
  foreign key(realm_id,superseded_by) references memory.compiler_candidate(realm_id,id)
    deferrable initially deferred,
  check(btrim(logical_candidate_id)<>'' and length(logical_candidate_id)<=512),
  check(candidate_type in ('durable_decision','project_fact','project_convention',
    'reusable_lesson','skill_candidate','failure_pattern','known_issue','unresolved_work',
    'obsolete_knowledge_candidate','conflict_candidate','projection_refresh_request')),
  check(truth_class in ('USER_DECISION','REPO_FACT','EXTERNAL_VERIFIED_FACT','MODEL_INFERENCE',
    'TEMPORARY_ASSUMPTION','SUPERSEDED','UNKNOWN')),
  check(continuity.valid_classification(classification)),
  check(risk in ('low','medium','high','critical')),
  check(state in ('candidate','reviewed','rejected','promoted','superseded','quarantined')),
  check((is_current and superseded_by is null and state<>'superseded')
    or (not is_current and superseded_by is not null and state='superseded')),
  check(candidate_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(jsonb_typeof(candidate_body)='object' and not continuity.contains_forbidden_key(candidate_body)),
  check(candidate_body->'state'=to_jsonb('candidate'::text)
    and candidate_body->'review_required'='true'::jsonb
    and candidate_body->'direct_promotion'='false'::jsonb
    and candidate_body->'grants_authority'='false'::jsonb),
  check(not grants_authority)
);
create unique index compiler_candidate_current_idx
  on memory.compiler_candidate(realm_id,logical_candidate_id) where is_current;

create table memory.compiler_candidate_source (
  id uuid primary key,
  realm_id uuid not null,
  candidate_id uuid not null,
  relation_kind text not null,
  ordinal integer not null,
  source_ref text not null,
  source_digest text not null,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,candidate_id,relation_kind,ordinal),
  unique(realm_id,candidate_id,relation_kind,source_ref),
  foreign key(realm_id,candidate_id) references memory.compiler_candidate(realm_id,id) on delete restrict,
  check(relation_kind in ('source','evidence')),
  check(ordinal between 1 and 128),
  check(btrim(source_ref)<>'' and length(source_ref)<=512),
  check(source_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(not grants_authority)
);

create table memory.compiler_candidate_review (
  id uuid primary key,
  realm_id uuid not null,
  candidate_id uuid not null,
  compiler_identity text not null,
  reviewer_identity text not null,
  decision text not null,
  review_ref text not null,
  review_digest text not null,
  reviewed_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,candidate_id), unique(realm_id,review_digest),
  foreign key(realm_id,candidate_id) references memory.compiler_candidate(realm_id,id)
    on delete restrict,
  check(btrim(compiler_identity)<>'' and length(compiler_identity)<=256),
  check(btrim(reviewer_identity)<>'' and length(reviewer_identity)<=256),
  check(compiler_identity<>reviewer_identity),
  check(decision in ('approved','rejected','quarantined')),
  check(btrim(review_ref)<>'' and length(review_ref)<=512),
  check(review_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(not grants_authority)
);

create table memory.compiler_candidate_promotion (
  id uuid primary key,
  realm_id uuid not null,
  candidate_id uuid not null,
  review_id uuid not null,
  authorization_id uuid not null,
  promotion_ref text not null,
  promotion_digest text not null,
  promoted_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,candidate_id), unique(realm_id,promotion_digest),
  foreign key(realm_id,candidate_id) references memory.compiler_candidate(realm_id,id)
    on delete restrict,
  foreign key(realm_id,review_id) references memory.compiler_candidate_review(realm_id,id)
    on delete restrict,
  foreign key(realm_id,authorization_id) references security.authorization(realm_id,id)
    on delete restrict,
  check(btrim(promotion_ref)<>'' and length(promotion_ref)<=512),
  check(promotion_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(not grants_authority)
);

create table continuity.projection_generation_receipt (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  project_id uuid not null,
  work_item_id uuid not null,
  idempotency_key text not null,
  source_ref text not null,
  source_digest text not null,
  projection_ref text not null,
  projection_digest text not null,
  generator_version text not null,
  classification text not null,
  public_filtered boolean not null,
  receipt_body jsonb not null,
  receipt_digest text not null,
  generated_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,idempotency_key),
  unique(realm_id,projection_ref,source_digest), unique(realm_id,receipt_digest),
  foreign key(realm_id,project_id) references projects.project(realm_id,id) on delete restrict,
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id) on delete restrict,
  check(btrim(source_ref)<>'' and length(source_ref)<=512),
  check(btrim(idempotency_key)<>'' and length(idempotency_key)<=512),
  check(btrim(projection_ref)<>'' and length(projection_ref)<=512),
  check(btrim(generator_version)<>'' and length(generator_version)<=512),
  check(source_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(projection_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(classification='public' and public_filtered),
  check(jsonb_typeof(receipt_body)='object'
    and not continuity.contains_forbidden_key(receipt_body)),
  check(receipt_body->'public_filtered'='true'::jsonb
    and receipt_body->>'classification'='public'
    and receipt_body->'grants_authority'='false'::jsonb),
  check(not grants_authority)
);

create table continuity.gap_recovery_reference (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  project_id uuid not null,
  work_item_id uuid not null,
  run_id uuid,
  gap_code text not null,
  gap_ref text not null,
  evidence_digest text not null,
  recovery_ref text not null,
  recovery_receipt_ref text,
  recovery_receipt_digest text,
  state text not null,
  created_at timestamptz not null,
  resolved_at timestamptz,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,gap_ref,evidence_digest),
  foreign key(realm_id,project_id) references projects.project(realm_id,id) on delete restrict,
  foreign key(realm_id,work_item_id) references work.work_item(realm_id,id) on delete restrict,
  foreign key(realm_id,run_id) references runtime.execution_run(realm_id,id) on delete restrict,
  check(gap_code ~ '^[a-z][a-z0-9_.:-]{0,95}$'),
  check(btrim(gap_ref)<>'' and length(gap_ref)<=512),
  check(btrim(recovery_ref)<>'' and length(recovery_ref)<=512),
  check(recovery_receipt_ref is null
    or (btrim(recovery_receipt_ref)<>'' and length(recovery_receipt_ref)<=512)),
  check(evidence_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(recovery_receipt_digest is null
    or recovery_receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(state in ('open','recovery-required','resolved')),
  check((state='resolved')=(resolved_at is not null
    and recovery_receipt_ref is not null and recovery_receipt_digest is not null)),
  check(not grants_authority)
);

create table continuity.feature_policy_state (
  id uuid primary key,
  realm_id uuid not null references core.realm(id) on delete restrict,
  component text not null,
  revision integer not null,
  state text not null,
  policy_body jsonb not null,
  policy_digest text not null,
  predecessor_id uuid,
  is_current boolean not null default true,
  verification_digest text,
  authorization_id uuid,
  created_at timestamptz not null,
  grants_authority boolean not null default false,
  unique(realm_id,id), unique(realm_id,component,revision), unique(realm_id,policy_digest),
  foreign key(realm_id,predecessor_id) references continuity.feature_policy_state(realm_id,id),
  foreign key(realm_id,authorization_id) references security.authorization(realm_id,id),
  check(component ~ '^[a-z][a-z0-9_.:-]{0,95}$'),
  check(revision>0),
  check(state in ('disabled','shadow','enforced')),
  check(jsonb_typeof(policy_body)='object' and not continuity.contains_forbidden_key(policy_body)),
  check(policy_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(verification_digest is null or verification_digest ~ '^sha256:[0-9a-f]{64}$'),
  check((state<>'enforced') or (verification_digest is not null and authorization_id is not null)),
  check((revision=1 and predecessor_id is null) or (revision>1 and predecessor_id is not null)),
  check(not grants_authority)
);
create unique index feature_policy_current_idx
  on continuity.feature_policy_state(realm_id,component) where is_current;

create function continuity.enforce_identity_and_digest() returns trigger
language plpgsql security invoker set search_path=pg_catalog,continuity,runtime as $$
declare run_ record; body_ jsonb; expected_ text; id_ uuid;
begin
  select project_id,work_item_id,session_id,client_id into strict run_
    from runtime.execution_run where realm_id=new.realm_id and id=new.run_id;
  if row(new.project_id,new.work_item_id) is distinct from row(run_.project_id,run_.work_item_id) then
    raise exception 'continuity run/project/work binding drift' using errcode='23514';
  end if;
  if tg_table_name<>'memory_contract_evaluation' then
    if (run_.session_id is not null and new.session_id<>run_.session_id)
      or new.client_id<>run_.client_id then
      raise exception 'continuity run/session/client binding drift' using errcode='23514';
    end if;
  end if;
  if tg_table_name='session_lifecycle_event' then
    body_=new.event_body; expected_=new.event_digest; id_=new.id;
  elsif tg_table_name in ('session_hydration_receipt','session_close_receipt','compaction_receipt') then
    body_=new.receipt_body; expected_=new.receipt_digest; id_=new.id;
  else
    body_=new.evaluation_body; expected_=new.evaluation_digest; id_=new.id;
  end if;
  if (body_->>case when tg_table_name='memory_contract_evaluation'
        then 'evaluation_id' when tg_table_name='session_lifecycle_event'
        then 'event_id' else 'receipt_id' end)::uuid<>id_
    or (body_->>'realm_id')::uuid<>new.realm_id
    or (body_->>'project_id')::uuid<>new.project_id
    or (body_->>'work_item_id')::uuid<>new.work_item_id
    or (body_->>'run_id')::uuid<>new.run_id
    or expected_<>continuity.jsonb_digest(body_) then
    raise exception 'continuity body identity/digest mismatch' using errcode='23514';
  end if;
  return new;
end $$;

create function continuity.enforce_lifecycle_chain() returns trigger
language plpgsql security invoker set search_path=pg_catalog,continuity as $$
declare head_ record;
begin
  perform pg_advisory_xact_lock(hashtextextended(
    new.realm_id::text||':'||new.client_id||':'||new.session_id,0));
  select sequence,event_digest into head_ from continuity.session_lifecycle_event
   where realm_id=new.realm_id and client_id=new.client_id and session_id=new.session_id
   order by sequence desc limit 1;
  if (head_ is null and (new.sequence<>1 or new.previous_digest is not null))
    or (head_ is not null and row(new.sequence,new.previous_digest)
      is distinct from row(head_.sequence+1,head_.event_digest)) then
    raise exception 'session lifecycle head/previous mismatch' using errcode='40001';
  end if;
  return new;
end $$;

create trigger lifecycle_event_identity before insert on continuity.session_lifecycle_event
  for each row execute function continuity.enforce_identity_and_digest();
create trigger lifecycle_event_chain before insert on continuity.session_lifecycle_event
  for each row execute function continuity.enforce_lifecycle_chain();
create trigger hydration_identity before insert on continuity.session_hydration_receipt
  for each row execute function continuity.enforce_identity_and_digest();
create trigger close_identity before insert on continuity.session_close_receipt
  for each row execute function continuity.enforce_identity_and_digest();
create trigger compaction_identity before insert on continuity.compaction_receipt
  for each row execute function continuity.enforce_identity_and_digest();
create trigger contract_identity before insert on continuity.memory_contract_evaluation
  for each row execute function continuity.enforce_identity_and_digest();

create function continuity.enforce_projection_receipt() returns trigger
language plpgsql security invoker set search_path=pg_catalog,continuity as $$
begin
  if (new.receipt_body->>'receipt_id')::uuid<>new.id
    or (new.receipt_body->>'realm_id')::uuid<>new.realm_id
    or (new.receipt_body->>'project_id')::uuid<>new.project_id
    or (new.receipt_body->>'work_item_id')::uuid<>new.work_item_id
    or new.receipt_body->>'source_ref'<>new.source_ref
    or new.receipt_body->>'source_digest'<>new.source_digest
    or new.receipt_body->>'projection_ref'<>new.projection_ref
    or new.receipt_body->>'projection_digest'<>new.projection_digest
    or new.receipt_body->>'generator_version'<>new.generator_version
    or (new.receipt_body->>'generated_at')::timestamptz<>new.generated_at
    or new.receipt_digest<>continuity.jsonb_digest(new.receipt_body) then
    raise exception 'projection receipt identity/body/digest mismatch' using errcode='23514';
  end if;
  return new;
end $$;
create trigger projection_receipt_guard before insert
  on continuity.projection_generation_receipt
  for each row execute function continuity.enforce_projection_receipt();

create function continuity.enforce_outbox_update() returns trigger
language plpgsql security invoker set search_path=pg_catalog,continuity as $$
begin
  if row(new.realm_id,new.event_id,new.plan_digest,new.payload_digest,new.created_at,
      new.grants_authority) is distinct from
     row(old.realm_id,old.event_id,old.plan_digest,old.payload_digest,old.created_at,
      old.grants_authority) then
    raise exception 'lifecycle outbox identity immutable' using errcode='23514';
  end if;
  if old.state in ('completed','failed','recovery-required') then
    if row(new.state,new.terminal_receipt_digest,new.completed_at) is distinct from
       row(old.state,old.terminal_receipt_digest,old.completed_at) then
      raise exception 'lifecycle outbox terminal state immutable' using errcode='23514';
    end if;
  elsif not ((old.state='pending' and new.state in ('processing','completed','failed','recovery-required'))
      or (old.state='processing' and new.state in ('completed','failed','recovery-required'))) then
    raise exception 'lifecycle outbox transition invalid' using errcode='23514';
  end if;
  return new;
end $$;
create trigger lifecycle_outbox_update before update on continuity.lifecycle_delivery_outbox
  for each row execute function continuity.enforce_outbox_update();

create function memory.enforce_compiler_watermark_update() returns trigger
language plpgsql security invoker set search_path=pg_catalog,memory as $$
begin
  if row(new.realm_id,new.project_id,new.work_item_id,new.run_id,new.idempotency_key,
      new.source_set_digest,new.source_watermark,new.claimed_at,new.grants_authority) is distinct from
     row(old.realm_id,old.project_id,old.work_item_id,old.run_id,old.idempotency_key,
      old.source_set_digest,old.source_watermark,old.claimed_at,old.grants_authority) then
    raise exception 'compiler watermark identity immutable' using errcode='23514';
  end if;
  if old.state in ('completed','failed','recovery-required') then
    if row(new.state,new.compiler_run_id,new.result_digest,new.completed_at) is distinct from
       row(old.state,old.compiler_run_id,old.result_digest,old.completed_at) then
      raise exception 'compiler watermark terminal immutable' using errcode='23514';
    end if;
  elsif not ((old.state='pending' and new.state in ('processing','completed','failed','recovery-required'))
      or (old.state='processing' and new.state in ('completed','failed','recovery-required'))) then
    raise exception 'compiler watermark transition invalid' using errcode='23514';
  end if;
  return new;
end $$;
create trigger compiler_watermark_update before update on memory.compiler_watermark_claim
  for each row execute function memory.enforce_compiler_watermark_update();

create function memory.enforce_compiler_candidate_update() returns trigger
language plpgsql security invoker set search_path=pg_catalog,memory as $$
declare review_ record; promotion_ record;
begin
  if row(new.id,new.realm_id,new.compiler_run_id,new.logical_candidate_id,new.candidate_type,
      new.truth_class,new.classification,new.risk,new.candidate_body,new.candidate_digest,
      new.created_at,new.grants_authority) is distinct from
     row(old.id,old.realm_id,old.compiler_run_id,old.logical_candidate_id,old.candidate_type,
      old.truth_class,old.classification,old.risk,old.candidate_body,old.candidate_digest,
      old.created_at,old.grants_authority) then
    raise exception 'compiler candidate identity/body immutable' using errcode='23514';
  end if;
  if not ((old.state='candidate'
      and new.state in ('reviewed','rejected','superseded','quarantined'))
    or (old.state='reviewed' and new.state in ('promoted','superseded'))) then
    raise exception 'compiler candidate transition invalid' using errcode='23514';
  end if;
  select decision into review_ from memory.compiler_candidate_review
    where realm_id=new.realm_id and candidate_id=new.id;
  if new.state='reviewed' and (review_ is null or review_.decision<>'approved')
    or new.state='rejected' and (review_ is null or review_.decision<>'rejected')
    or new.state='quarantined' and (review_ is null or review_.decision<>'quarantined') then
    raise exception 'compiler candidate transition independent review binding missing'
      using errcode='42501';
  end if;
  if new.state='promoted' then
    select id into promotion_ from memory.compiler_candidate_promotion
      where realm_id=new.realm_id and candidate_id=new.id;
    if promotion_ is null then
      raise exception 'compiler candidate promotion receipt binding missing' using errcode='42501';
    end if;
  end if;
  return new;
end $$;
create trigger compiler_candidate_update before update on memory.compiler_candidate
  for each row execute function memory.enforce_compiler_candidate_update();

create function memory.enforce_compiler_candidate_supersession() returns trigger
language plpgsql security invoker set search_path=pg_catalog,memory as $$
declare successor_ record;
begin
  if new.state='superseded' then
    select logical_candidate_id,is_current into successor_
      from memory.compiler_candidate
      where realm_id=new.realm_id and id=new.superseded_by;
    if successor_ is null or successor_.logical_candidate_id<>new.logical_candidate_id
      or not successor_.is_current then
      raise exception 'compiler candidate supersession binding invalid' using errcode='23514';
    end if;
  end if;
  return null;
end $$;
create constraint trigger compiler_candidate_supersession
  after insert or update on memory.compiler_candidate deferrable initially deferred
  for each row execute function memory.enforce_compiler_candidate_supersession();

create function memory.enforce_compiler_candidate_review_insert() returns trigger
language plpgsql security invoker set search_path=pg_catalog,memory as $$
declare candidate_ record;
begin
  select state into candidate_ from memory.compiler_candidate
    where realm_id=new.realm_id and id=new.candidate_id for update;
  if candidate_ is null or candidate_.state<>'candidate' then
    raise exception 'compiler candidate review requires candidate state' using errcode='23514';
  end if;
  return new;
end $$;
create trigger compiler_candidate_review_guard
  before insert on memory.compiler_candidate_review
  for each row execute function memory.enforce_compiler_candidate_review_insert();

create function memory.enforce_compiler_candidate_promotion_insert() returns trigger
language plpgsql security invoker
set search_path=pg_catalog,memory,security as $$
declare candidate_ record; review_ record; authorization_ record; required_resource text;
begin
  select candidate.state,candidate.logical_candidate_id,run.work_item_id into candidate_
    from memory.compiler_candidate candidate
    join memory.compiler_run run on run.realm_id=candidate.realm_id
      and run.id=candidate.compiler_run_id
    where candidate.realm_id=new.realm_id and candidate.id=new.candidate_id
    for update of candidate;
  select candidate_id,decision into review_ from memory.compiler_candidate_review
    where realm_id=new.realm_id and id=new.review_id;
  select state,work_item_id,allowed_resources,allowed_effects,consumed_by into authorization_
    from security.authorization where realm_id=new.realm_id and id=new.authorization_id;
  required_resource='memory:compiler-candidate:'||candidate_.logical_candidate_id;
  if candidate_ is null or candidate_.state<>'reviewed'
    or review_ is null or review_.candidate_id<>new.candidate_id or review_.decision<>'approved'
    or authorization_ is null or authorization_.state<>'consumed'
    or authorization_.work_item_id is distinct from candidate_.work_item_id
    or not ('database-write'=any(authorization_.allowed_effects))
    or not (required_resource=any(authorization_.allowed_resources))
    or authorization_.consumed_by<>'memory-compiler-candidate-promotion/v1' then
    raise exception 'compiler candidate promotion authorization/review binding invalid'
      using errcode='42501';
  end if;
  return new;
end $$;
create trigger compiler_candidate_promotion_guard
  before insert on memory.compiler_candidate_promotion
  for each row execute function memory.enforce_compiler_candidate_promotion_insert();

create function continuity.enforce_gap_resolution() returns trigger
language plpgsql security invoker set search_path=pg_catalog,continuity as $$
begin
  if row(new.id,new.realm_id,new.project_id,new.work_item_id,new.run_id,new.gap_code,
      new.gap_ref,new.evidence_digest,new.recovery_ref,new.created_at,new.grants_authority)
    is distinct from
     row(old.id,old.realm_id,old.project_id,old.work_item_id,old.run_id,old.gap_code,
      old.gap_ref,old.evidence_digest,old.recovery_ref,old.created_at,old.grants_authority)
    or old.state not in ('open','recovery-required') or new.state<>'resolved'
    or old.recovery_receipt_ref is not null or old.recovery_receipt_digest is not null
    or new.recovery_receipt_ref is null or new.recovery_receipt_digest is null
    or new.resolved_at is null or new.resolved_at<new.created_at then
    raise exception 'continuity gap resolution transition invalid' using errcode='23514';
  end if;
  return new;
end $$;
create trigger gap_resolution_guard before update on continuity.gap_recovery_reference
  for each row execute function continuity.enforce_gap_resolution();

create function continuity.enforce_gap_insert() returns trigger
language plpgsql security invoker set search_path=pg_catalog,continuity as $$
begin
  if new.state not in ('open','recovery-required') or new.resolved_at is not null
    or new.recovery_receipt_ref is not null or new.recovery_receipt_digest is not null then
    raise exception 'resolved continuity gap requires guarded update' using errcode='23514';
  end if;
  return new;
end $$;
create trigger gap_insert_guard before insert on continuity.gap_recovery_reference
  for each row execute function continuity.enforce_gap_insert();

create function continuity.enforce_feature_policy() returns trigger
language plpgsql security invoker set search_path=pg_catalog,continuity as $$
declare previous_ record;
begin
  perform pg_advisory_xact_lock(hashtextextended(new.realm_id::text||':'||new.component,0));
  select id,revision,policy_digest into previous_ from continuity.feature_policy_state
   where realm_id=new.realm_id and component=new.component and is_current for update;
  if (previous_ is null and (new.revision<>1 or new.predecessor_id is not null))
    or (previous_ is not null and row(new.revision,new.predecessor_id)
      is distinct from row(previous_.revision+1,previous_.id)) then
    raise exception 'continuity feature policy revision chain drift' using errcode='40001';
  end if;
  if new.policy_digest<>continuity.jsonb_digest(new.policy_body)
    or new.policy_body->'grants_authority'<>'false'::jsonb then
    raise exception 'continuity feature policy body/digest drift' using errcode='23514';
  end if;
  if previous_ is not null then
    update continuity.feature_policy_state set is_current=false
      where realm_id=new.realm_id and id=previous_.id;
  end if;
  return new;
end $$;
create trigger feature_policy_guard before insert on continuity.feature_policy_state
  for each row execute function continuity.enforce_feature_policy();

create function continuity.enforce_feature_policy_update() returns trigger
language plpgsql security invoker set search_path=pg_catalog,continuity as $$
begin
  if row(new.id,new.realm_id,new.component,new.revision,new.state,new.policy_body,
      new.policy_digest,new.predecessor_id,new.verification_digest,new.authorization_id,
      new.created_at,new.grants_authority) is distinct from
     row(old.id,old.realm_id,old.component,old.revision,old.state,old.policy_body,
      old.policy_digest,old.predecessor_id,old.verification_digest,old.authorization_id,
      old.created_at,old.grants_authority)
    or not(old.is_current and not new.is_current) then
    raise exception 'continuity feature policy revision immutable' using errcode='23514';
  end if;
  return new;
end $$;
create trigger feature_policy_update before update on continuity.feature_policy_state
  for each row execute function continuity.enforce_feature_policy_update();

create function continuity.seed_memory_continuity_policy() returns trigger
language plpgsql security definer
set search_path=pg_catalog,continuity,core,public as $$
declare body_ jsonb:=jsonb_build_object(
  'schema','zekam-memory-continuity-feature-policy/v1',
  'component','memory-continuity-plane',
  'state','shadow',
  'version',1,
  'grants_authority',false
);
begin
  insert into continuity.feature_policy_state(
    id,realm_id,component,revision,state,policy_body,policy_digest,predecessor_id,
    is_current,verification_digest,authorization_id,created_at,grants_authority)
  values(gen_random_uuid(),new.id,'memory-continuity-plane',1,'shadow',body_,
    continuity.jsonb_digest(body_),null,true,null,null,statement_timestamp(),false)
  on conflict(realm_id,component,revision) do nothing;
  return new;
end $$;
create trigger realm_memory_continuity_shadow_seed after insert on core.realm
  for each row execute function continuity.seed_memory_continuity_policy();

insert into continuity.feature_policy_state(
  id,realm_id,component,revision,state,policy_body,policy_digest,predecessor_id,
  is_current,verification_digest,authorization_id,created_at,grants_authority)
select gen_random_uuid(),realm.id,'memory-continuity-plane',1,'shadow',body.value,
  continuity.jsonb_digest(body.value),null,true,null,null,statement_timestamp(),false
from core.realm realm
cross join lateral (select jsonb_build_object(
  'schema','zekam-memory-continuity-feature-policy/v1',
  'component','memory-continuity-plane','state','shadow','version',1,
  'grants_authority',false) value) body
on conflict(realm_id,component,revision) do nothing;

do $$
declare target text;
begin
  foreach target in array array[
    'continuity.session_lifecycle_event','continuity.lifecycle_delivery_outbox',
    'continuity.session_hydration_receipt','continuity.session_close_receipt',
    'continuity.compaction_receipt','continuity.memory_contract_evaluation',
    'memory.compiler_watermark_claim','memory.compiler_run','memory.compiler_candidate',
    'memory.compiler_candidate_source','memory.compiler_candidate_review',
    'memory.compiler_candidate_promotion','continuity.projection_generation_receipt',
    'continuity.gap_recovery_reference','continuity.feature_policy_state'
  ] loop
    execute format('alter table %s enable row level security',target);
    execute format('alter table %s force row level security',target);
    execute format('create policy scope_select on %s for select using(realm_id=core.current_realm_id())',target);
    execute format('create policy scope_insert on %s for insert with check(realm_id=core.current_realm_id())',target);
  end loop;
end $$;

create policy scope_update on continuity.lifecycle_delivery_outbox for update
  using(realm_id=core.current_realm_id()) with check(realm_id=core.current_realm_id());
create policy scope_update on memory.compiler_watermark_claim for update
  using(realm_id=core.current_realm_id()) with check(realm_id=core.current_realm_id());
create policy scope_update on memory.compiler_candidate for update
  using(realm_id=core.current_realm_id()) with check(realm_id=core.current_realm_id());
create policy scope_update on continuity.gap_recovery_reference for update
  using(realm_id=core.current_realm_id()) with check(realm_id=core.current_realm_id());
create policy scope_update on continuity.feature_policy_state for update
  using(realm_id=core.current_realm_id()) with check(realm_id=core.current_realm_id());

do $$
declare target text;
begin
  foreach target in array array[
    'continuity.session_lifecycle_event','continuity.session_hydration_receipt',
    'continuity.session_close_receipt','continuity.compaction_receipt',
    'continuity.memory_contract_evaluation','memory.compiler_run',
    'memory.compiler_candidate_source',
    'memory.compiler_candidate_review','memory.compiler_candidate_promotion',
    'continuity.projection_generation_receipt'
  ] loop
    execute format('create trigger deny_update before update on %s for each statement execute function core.deny_mutation()',target);
    execute format('create trigger deny_delete before delete on %s for each statement execute function core.deny_mutation()',target);
  end loop;
end $$;
create trigger lifecycle_outbox_deny_delete before delete on continuity.lifecycle_delivery_outbox
  for each statement execute function core.deny_mutation();
create trigger compiler_watermark_deny_delete before delete on memory.compiler_watermark_claim
  for each statement execute function core.deny_mutation();
create trigger compiler_candidate_deny_delete before delete on memory.compiler_candidate
  for each statement execute function core.deny_mutation();
create trigger gap_recovery_deny_delete before delete on continuity.gap_recovery_reference
  for each statement execute function core.deny_mutation();
create trigger feature_policy_deny_delete before delete on continuity.feature_policy_state
  for each statement execute function core.deny_mutation();

alter table hooks.spec_revision drop constraint spec_revision_event_type_check;
alter table hooks.spec_revision add constraint spec_revision_event_type_check check(event_type in (
  'session.start','session.end','user.input.submitted','turn.start','turn.stop','pre.tool',
  'post.tool','permission.request','pre.compact','post.compact','checkpoint.created',
  'agent.spawned','agent.completed','recovery.required','session_start','hydration_required',
  'hydration_completed','pre_task','post_task','pre_compaction','post_compaction','pre_close',
  'post_close','on_failure','on_validation_failure','on_memory_write_failure',
  'on_memory_hydration_failure','on_skill_candidate','on_skill_update','on_state_drift',
  'unclean_exit'
));

create index lifecycle_event_session_idx on continuity.session_lifecycle_event
  (realm_id,client_id,session_id,sequence desc);
create index lifecycle_outbox_pending_idx on continuity.lifecycle_delivery_outbox
  (realm_id,created_at,id) where state='pending';
create index compiler_watermark_pending_idx on memory.compiler_watermark_claim
  (realm_id,claimed_at,id) where state in ('pending','processing');
create index gap_recovery_open_idx on continuity.gap_recovery_reference
  (realm_id,created_at,id) where state<>'resolved';

revoke all on function continuity.canonical_json(jsonb) from public;
revoke all on function continuity.jsonb_digest(jsonb) from public;
revoke all on function continuity.contains_forbidden_key(jsonb) from public;
revoke all on function continuity.enforce_identity_and_digest() from public;
revoke all on function continuity.enforce_projection_receipt() from public;
revoke all on function continuity.enforce_lifecycle_chain() from public;
revoke all on function continuity.enforce_outbox_update() from public;
revoke all on function continuity.enforce_feature_policy() from public;
revoke all on function continuity.enforce_feature_policy_update() from public;
revoke all on function continuity.seed_memory_continuity_policy() from public;
revoke all on function memory.enforce_compiler_watermark_update() from public;
revoke all on function memory.enforce_compiler_candidate_update() from public;
revoke all on function memory.enforce_compiler_candidate_supersession() from public;
revoke all on function memory.enforce_compiler_candidate_review_insert() from public;
revoke all on function memory.enforce_compiler_candidate_promotion_insert() from public;
revoke all on function continuity.enforce_gap_resolution() from public;
revoke all on function continuity.enforce_gap_insert() from public;

grant execute on function continuity.canonical_json(jsonb) to zekam_app;
grant execute on function continuity.jsonb_digest(jsonb) to zekam_app;
grant execute on function continuity.contains_forbidden_key(jsonb) to zekam_app;
grant select,insert on continuity.session_lifecycle_event,
  continuity.session_hydration_receipt,continuity.session_close_receipt,
  continuity.compaction_receipt,continuity.memory_contract_evaluation,
  memory.compiler_run,memory.compiler_candidate_source,
  memory.compiler_candidate_review,memory.compiler_candidate_promotion,
  continuity.projection_generation_receipt to zekam_app;
grant select,insert,update on memory.compiler_candidate to zekam_app;
grant select,insert,update on continuity.lifecycle_delivery_outbox,
  memory.compiler_watermark_claim,continuity.feature_policy_state to zekam_app;
grant select,insert,update on continuity.gap_recovery_reference to zekam_app;
