-- Exact approval and runtime ledger for the reviewed 21 episode x 8 turn run.
-- Public templates are pre-approved; each continuity-derived payload is independently
-- reconstructed and bound to a one-shot authorization before claim/effect.

create function models.capability_runtime_canonical_json(value_ jsonb) returns text
language plpgsql immutable strict set search_path=pg_catalog,models as $$
declare kind text:=jsonb_typeof(value_); result_ text;
begin
    if kind in ('null','boolean','number') then return value_::text; end if;
    if kind='string' then return to_jsonb(value_#>>'{}')::text; end if;
    if kind='array' then
        select '['||coalesce(string_agg(models.capability_runtime_canonical_json(value),','
                                      order by ordinal),'')||']'
          into result_ from jsonb_array_elements(value_) with ordinality item(value,ordinal);
        return result_;
    end if;
    select '{'||coalesce(string_agg(to_jsonb(key)::text||':'||
                    models.capability_runtime_canonical_json(value),',' order by key),'')||'}'
      into result_ from jsonb_each(value_) item(key,value);
    return result_;
end
$$;

create function models.capability_runtime_jsonb_digest(value_ jsonb) returns text
language sql immutable strict set search_path=pg_catalog,models as $$
    select 'sha256:' || encode(public.digest(
        convert_to(models.capability_runtime_canonical_json(value_),'UTF8'),'sha256'),'hex')
$$;

create function models.derive_capability_request_body(template_ jsonb,state_ jsonb) returns jsonb
language sql immutable strict set search_path=pg_catalog,models as $$
    select jsonb_build_object(
        'model',template_->>'model',
        'messages',jsonb_build_array(
            jsonb_build_object('role','system','content',template_->>'system'),
            jsonb_build_object(
                'role','user','content',(template_->>'prompt_prefix') ||
                'prior_state_digest tam olarak ' || models.capability_runtime_jsonb_digest(state_) ||
                ' olmali. Onceki continuity_state:' || chr(10) ||
                models.capability_runtime_canonical_json(state_)
            )
        ),
        'temperature',0,
        'max_tokens',(template_->>'max_tokens')::integer
    )
$$;

create function models.capability_runtime_continuity_valid(value_ jsonb) returns boolean
language sql immutable strict set search_path=pg_catalog as $$
    select jsonb_typeof(value_)='object'
       and (select array_agg(key order by key) from jsonb_object_keys(value_) key)
           = array['facts','next_action','open_questions','risks']::text[]
       and jsonb_typeof(value_->'facts')='array'
       and jsonb_typeof(value_->'open_questions')='array'
       and jsonb_typeof(value_->'risks')='array'
       and jsonb_typeof(value_->'next_action')='string'
$$;

create table models.capability_runtime_approval_manifest (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    cohort_id uuid not null,
    work_item_id uuid not null,
    task_plan_id uuid not null,
    coordinator_job_id uuid not null,
    source_revision text not null,
    model_ids text[] not null,
    task_digests text[] not null,
    episode_count integer not null,
    slots_per_episode integer not null,
    max_provider_calls integer not null,
    max_retries integer not null,
    approval_evidence_digest text not null,
    manifest_digest text not null,
    approved_at timestamptz not null default now(),
    constraint capability_runtime_manifest_realm_id_unique unique (realm_id, id),
    constraint capability_runtime_manifest_cohort_same_realm
        foreign key (realm_id, cohort_id)
        references models.capability_benchmark_cohort (realm_id, id) on delete restrict,
    constraint capability_runtime_manifest_work_same_realm
        foreign key (realm_id, work_item_id)
        references work.work_item (realm_id, id) on delete restrict,
    constraint capability_runtime_manifest_plan_same_realm
        foreign key (realm_id, task_plan_id)
        references work.task_plan (realm_id, id) on delete restrict,
    constraint capability_runtime_manifest_coordinator_same_realm
        foreign key (realm_id, coordinator_job_id)
        references runtime.job (realm_id, id) on delete restrict,
    constraint capability_runtime_manifest_coordinator_unique unique (realm_id, coordinator_job_id),
    constraint capability_runtime_manifest_cohort_unique unique (realm_id, cohort_id),
    constraint capability_runtime_manifest_digest_unique unique (realm_id, manifest_digest),
    constraint capability_runtime_manifest_reviewed_shape check (
        episode_count = 21 and slots_per_episode = 8
        and max_provider_calls = 168 and max_retries = 0
        and cardinality(model_ids) = 7 and cardinality(task_digests) = 3
        and models.valid_routing_text_array(model_ids)
        and models.valid_routing_digest_array(task_digests)
        and length(btrim(source_revision)) between 1 and 2048
        and position('://' in source_revision) = 0
        and approval_evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and manifest_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create table models.capability_runtime_approval_slot (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    manifest_id uuid not null,
    cohort_id uuid not null,
    model_id text not null,
    task_digest text not null,
    turn_number integer not null,
    ordinal integer not null,
    job_id uuid not null,
    provider_ref text not null,
    backend_model text not null,
    endpoint_resource text not null,
    endpoint_identity_digest text not null,
    operation text not null,
    call_resource text not null,
    call_id text not null,
    fixture_digest text not null,
    fixture_identity_digest text not null,
    max_output_tokens integer not null,
    request_template jsonb not null,
    request_template_digest text not null,
    derivation_rule_digest text not null,
    chain_seed_digest text not null,
    slot_digest text not null,
    created_at timestamptz not null default now(),
    constraint capability_runtime_slot_realm_id_unique unique (realm_id, id),
    constraint capability_runtime_slot_manifest_same_realm
        foreign key (realm_id, manifest_id)
        references models.capability_runtime_approval_manifest (realm_id, id) on delete restrict,
    constraint capability_runtime_slot_cohort_same_realm
        foreign key (realm_id, cohort_id)
        references models.capability_benchmark_cohort (realm_id, id) on delete restrict,
    constraint capability_runtime_slot_job_same_realm
        foreign key (realm_id, job_id) references runtime.job (realm_id, id) on delete restrict,
    constraint capability_runtime_slot_exact_unique
        unique (realm_id, cohort_id, model_id, task_digest, turn_number),
    constraint capability_runtime_slot_ordinal_unique unique (realm_id, manifest_id, ordinal),
    constraint capability_runtime_slot_job_turn_unique unique (realm_id, job_id, turn_number),
    constraint capability_runtime_slot_digest_unique unique (realm_id, slot_digest),
    constraint capability_runtime_slot_shape check (
        turn_number between 1 and 8 and ordinal between 1 and 168
        and task_digest ~ '^sha256:[0-9a-f]{64}$'
        and length(btrim(provider_ref)) > 0 and length(btrim(backend_model)) > 0
        and length(btrim(endpoint_resource)) > 0
        and length(btrim(call_resource)) > 0 and length(btrim(operation)) > 0
        and length(btrim(call_id)) > 0
        and operation in ('chat-completions','code-completions')
        and max_output_tokens between 1 and 16384
        and jsonb_typeof(request_template)='object'
        and request_template->>'schema'='zekam-capability-request-template/v1'
        and request_template->>'model'=backend_model
        and (request_template->>'max_tokens')::integer=max_output_tokens
        and length(request_template::text)<=32768
        and request_template::text !~* 'raw_response|provider_response|secret|api[_-]?key|authorization'
        and endpoint_identity_digest ~ '^sha256:[0-9a-f]{64}$'
        and fixture_digest ~ '^sha256:[0-9a-f]{64}$'
        and fixture_identity_digest ~ '^sha256:[0-9a-f]{64}$'
        and request_template_digest ~ '^sha256:[0-9a-f]{64}$'
        and derivation_rule_digest ~ '^sha256:[0-9a-f]{64}$'
        and chain_seed_digest ~ '^sha256:[0-9a-f]{64}$'
        and slot_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create table models.capability_runtime_continuity_state (
    id uuid primary key,
    realm_id uuid not null references core.realm(id) on delete restrict,
    manifest_id uuid not null,
    slot_id uuid not null,
    continuity_state jsonb not null,
    continuity_state_digest text not null,
    prior_result_digest text not null,
    derivation_attestation_digest text not null,
    checkpoint_id uuid,
    event_digest text not null,
    created_at timestamptz not null default now(),
    constraint capability_runtime_continuity_realm_id_unique unique(realm_id,id),
    constraint capability_runtime_continuity_manifest_same_realm foreign key(realm_id,manifest_id)
        references models.capability_runtime_approval_manifest(realm_id,id) on delete restrict,
    constraint capability_runtime_continuity_slot_same_realm foreign key(realm_id,slot_id)
        references models.capability_runtime_approval_slot(realm_id,id) on delete restrict,
    constraint capability_runtime_continuity_slot_unique unique(slot_id),
    constraint capability_runtime_continuity_shape check (
        models.capability_runtime_continuity_valid(continuity_state)
        and length(continuity_state::text)<=8192
        and continuity_state::text !~* 'raw_response|provider_response|secret|api[_-]?key|authorization'
        and continuity_state_digest ~ '^sha256:[0-9a-f]{64}$'
        and prior_result_digest ~ '^sha256:[0-9a-f]{64}$'
        and derivation_attestation_digest ~ '^sha256:[0-9a-f]{64}$'
        and event_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create table models.capability_runtime_turn_checkpoint (
    id uuid primary key,
    realm_id uuid not null references core.realm(id) on delete restrict,
    manifest_id uuid not null,
    slot_id uuid not null,
    continuity_state_id uuid not null,
    job_id uuid not null,
    completed_turns integer[] not null,
    pending_turns integer[] not null,
    result_digest text not null,
    checkpoint_digest text not null,
    created_at timestamptz not null default now(),
    constraint capability_runtime_turn_checkpoint_realm_id_unique unique(realm_id,id),
    constraint capability_runtime_turn_checkpoint_manifest_same_realm foreign key(realm_id,manifest_id)
        references models.capability_runtime_approval_manifest(realm_id,id) on delete restrict,
    constraint capability_runtime_turn_checkpoint_slot_same_realm foreign key(realm_id,slot_id)
        references models.capability_runtime_approval_slot(realm_id,id) on delete restrict,
    constraint capability_runtime_turn_checkpoint_state_same_realm
        foreign key(realm_id,continuity_state_id)
        references models.capability_runtime_continuity_state(realm_id,id) on delete restrict,
    constraint capability_runtime_turn_checkpoint_job_same_realm foreign key(realm_id,job_id)
        references runtime.job(realm_id,id) on delete restrict,
    constraint capability_runtime_turn_checkpoint_slot_unique unique(slot_id),
    constraint capability_runtime_turn_checkpoint_digest_unique unique(realm_id,checkpoint_digest),
    constraint capability_runtime_turn_checkpoint_shape check (
        not completed_turns&&pending_turns
        and result_digest ~ '^sha256:[0-9a-f]{64}$'
        and checkpoint_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

alter table models.capability_runtime_continuity_state
    add constraint capability_runtime_continuity_checkpoint_exists
    foreign key(checkpoint_id) references models.capability_runtime_turn_checkpoint(id)
    on delete restrict;

create table models.capability_runtime_slot_authorization (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    manifest_id uuid not null,
    slot_id uuid not null,
    authorization_id uuid not null references security.authorization (id) on delete restrict,
    authorization_plan_digest text not null,
    authorization_digest text not null,
    request_body_digest text not null,
    effect_digest text not null,
    prior_response_chain_digest text not null,
    binding_digest text not null,
    issued_at timestamptz not null default now(),
    constraint capability_runtime_slot_auth_realm_id_unique unique (realm_id,id),
    constraint capability_runtime_slot_auth_manifest_same_realm foreign key (realm_id,manifest_id)
        references models.capability_runtime_approval_manifest (realm_id,id) on delete restrict,
    constraint capability_runtime_slot_auth_slot_same_realm foreign key (realm_id,slot_id)
        references models.capability_runtime_approval_slot (realm_id,id) on delete restrict,
    constraint capability_runtime_slot_auth_slot_unique unique (slot_id),
    constraint capability_runtime_slot_auth_authorization_unique unique (authorization_id),
    constraint capability_runtime_slot_auth_binding_unique unique (realm_id,binding_digest),
    constraint capability_runtime_slot_auth_shape check (
        authorization_plan_digest ~ '^sha256:[0-9a-f]{64}$'
        and authorization_digest ~ '^sha256:[0-9a-f]{64}$'
        and request_body_digest ~ '^sha256:[0-9a-f]{64}$'
        and effect_digest ~ '^sha256:[0-9a-f]{64}$'
        and prior_response_chain_digest ~ '^sha256:[0-9a-f]{64}$'
        and binding_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

create function models.capability_runtime_derived_digests(realm_id_ uuid,slot_id_ uuid)
returns table(request_body jsonb,request_body_digest text,authorization_plan_digest text,
              effect_digest text,effect_action text,claim_operation text)
language sql stable security invoker set search_path=pg_catalog,models as $$
    with material as (
        select s.*,m.manifest_digest,c.continuity_state,
               models.derive_capability_request_body(s.request_template,c.continuity_state) body
          from models.capability_runtime_approval_slot s
          join models.capability_runtime_approval_manifest m
            on m.realm_id=s.realm_id and m.id=s.manifest_id
          join models.capability_runtime_continuity_state c
            on c.realm_id=s.realm_id and c.slot_id=s.id
         where s.id=slot_id_ and s.realm_id=realm_id_
    ), request as (
        select material.*,models.capability_runtime_jsonb_digest(body) body_digest from material
    ), derived as (
        select request.*,
               models.capability_runtime_jsonb_digest(jsonb_build_object(
               'call_id',call_id,'model_id',model_id,'payload_digest',body_digest,
               'fixture_digest',fixture_digest,
               'fixture_identity_digest',fixture_identity_digest,
               'endpoint_binding_digest',endpoint_identity_digest,'target',endpoint_resource
               )) plan_digest
          from request
    )
    select body,body_digest,plan_digest,
           models.capability_runtime_jsonb_digest(jsonb_build_array(jsonb_build_object(
               'effect','provider-call','resources',(
                   select jsonb_agg(resource order by resource)
                     from unnest(array[call_resource,endpoint_resource]) resource
               )
           ))),
           'provider-contract-call-' || replace(
               models.capability_runtime_jsonb_digest(jsonb_build_object(
                   'request_identity',call_id,
                   'payload_digest',body_digest,
                   'plan_digest',plan_digest
               )),
               'sha256:',
               ''
           ),
           'provider-contract:' || call_id
      from derived
$$;

create table models.capability_runtime_call_outcome (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    slot_id uuid not null,
    claim_id uuid not null references runtime.effect_claim (id) on delete restrict,
    receipt_id uuid references runtime.effect_receipt (id) on delete restrict,
    checkpoint_id uuid not null references models.capability_runtime_turn_checkpoint(id)
        on delete restrict,
    status text not null,
    result_digest text,
    failure_category text,
    evidence_digest text not null,
    completed_at timestamptz not null,
    constraint capability_runtime_call_realm_id_unique unique (realm_id, id),
    constraint capability_runtime_call_slot_same_realm
        foreign key (realm_id, slot_id)
        references models.capability_runtime_approval_slot (realm_id, id) on delete restrict,
    constraint capability_runtime_call_slot_unique unique (slot_id),
    constraint capability_runtime_call_claim_unique unique (claim_id),
    constraint capability_runtime_call_receipt_unique unique (receipt_id),
    constraint capability_runtime_call_evidence_unique unique (realm_id, evidence_digest),
    constraint capability_runtime_call_shape check (
        status in ('completed', 'failed', 'recovery-required')
        and evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and (result_digest is null or result_digest ~ '^sha256:[0-9a-f]{64}$')
        and (
            (status = 'completed' and receipt_id is not null
             and result_digest is not null and failure_category is null)
            or (status = 'failed' and receipt_id is not null
                and result_digest is null and length(btrim(failure_category)) > 0)
            or (status = 'recovery-required' and receipt_id is null
                and result_digest is null and length(btrim(failure_category)) > 0)
        )
    )
);

create table models.capability_runtime_outcome (
    id uuid primary key,
    realm_id uuid not null references core.realm (id) on delete restrict,
    manifest_id uuid not null,
    status text not null,
    actual_provider_calls integer not null,
    actual_retries integer not null,
    call_evidence_digests text[] not null,
    score_eligible boolean generated always as
        (status = 'completed' and actual_provider_calls = 168 and actual_retries = 0) stored,
    routing_eligible boolean generated always as (false) stored,
    evidence_digest text not null,
    completed_at timestamptz not null,
    constraint capability_runtime_outcome_manifest_same_realm
        foreign key (realm_id, manifest_id)
        references models.capability_runtime_approval_manifest (realm_id, id) on delete restrict,
    constraint capability_runtime_outcome_manifest_unique unique (realm_id, manifest_id),
    constraint capability_runtime_outcome_evidence_unique unique (realm_id, evidence_digest),
    constraint capability_runtime_outcome_shape check (
        status in ('completed', 'partial', 'recovery-required')
        and actual_provider_calls between 0 and 168 and actual_retries = 0
        and cardinality(call_evidence_digests) = actual_provider_calls
        and (
            (actual_provider_calls = 0 and cardinality(call_evidence_digests) = 0)
            or models.valid_routing_digest_array(call_evidence_digests)
        )
        and evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and (
            (status = 'completed' and actual_provider_calls = 168)
            or (status = 'partial' and actual_provider_calls < 168)
            or (status = 'recovery-required' and actual_provider_calls <= 168)
        )
    )
);

create function models.enforce_capability_runtime_manifest() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, work as $$
declare cohort_record record; plan_record record; coordinator_record record;
begin
    select c.source_revision,c.model_ids,s.task_digests,s.task_count,s.max_model_turns,
           c.provider_call_budget
      into cohort_record
      from models.capability_benchmark_cohort c
      join models.capability_benchmark_suite s
        on s.realm_id=c.realm_id and s.id=c.suite_id
     where c.realm_id=new.realm_id and c.id=new.cohort_id;
    select work_item_id,source_revision into plan_record
      from work.task_plan where realm_id=new.realm_id and id=new.task_plan_id;
    select work_item_id,plan_id,kind,max_attempts into coordinator_record
      from runtime.job where realm_id=new.realm_id and id=new.coordinator_job_id;
    if not found or cohort_record.source_revision is distinct from new.source_revision
       or cohort_record.model_ids is distinct from new.model_ids
       or cohort_record.task_digests is distinct from new.task_digests
       or cohort_record.task_count <> 3 or cardinality(cohort_record.model_ids) <> 7
       or cohort_record.max_model_turns <> 8 or cohort_record.provider_call_budget <> 168
       or plan_record.work_item_id is distinct from new.work_item_id
       or plan_record.source_revision is distinct from new.source_revision
       or coordinator_record.work_item_id is distinct from new.work_item_id
       or coordinator_record.plan_id is distinct from new.task_plan_id
       or coordinator_record.kind <> 'verification'
       or coordinator_record.max_attempts <> 1 then
        raise exception 'capability runtime manifest cohort/plan reviewed scope mismatch'
            using errcode='42501';
    end if;
    return new;
end
$$;

create function models.enforce_capability_runtime_slot() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, runtime as $$
declare manifest_record record; job_record record;
begin
    select cohort_id,work_item_id,task_plan_id,model_ids,task_digests
      into manifest_record from models.capability_runtime_approval_manifest
     where realm_id=new.realm_id and id=new.manifest_id;
    select work_item_id,plan_id,kind,max_attempts,state into job_record
      from runtime.job where realm_id=new.realm_id and id=new.job_id;
    if not found or new.cohort_id is distinct from manifest_record.cohort_id
       or not new.model_id=any(manifest_record.model_ids)
       or not new.task_digest=any(manifest_record.task_digests)
       or new.request_template_digest is distinct from
          models.capability_runtime_jsonb_digest(new.request_template)
       or new.call_resource is distinct from
          'provider:' || new.model_id || ':' || new.operation || ':' || new.call_id
       or left(new.endpoint_resource,length(new.provider_ref)+1)
          is distinct from new.provider_ref || ':'
       or right(new.endpoint_resource,length(new.operation)+1)
          is distinct from ':' || new.operation
       or length(new.endpoint_resource)
          <= length(new.provider_ref)+length(new.operation)+2
       or job_record.work_item_id is distinct from manifest_record.work_item_id
       or job_record.plan_id is distinct from manifest_record.task_plan_id
       or job_record.kind <> 'provider-call' or job_record.max_attempts <> 1
       or job_record.state <> 'ready' then
        raise exception 'capability runtime slot template/job binding mismatch'
            using errcode='42501';
    end if;
    return new;
end
$$;

create function models.enforce_capability_runtime_slot_authorization() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, runtime, security as $$
declare slot_record record; manifest_record record; auth_record record; derived_record record;
        episode_state text; coordinator_state text; expected_prior_digest text;
begin
    select manifest_id,model_id,task_digest,turn_number,job_id,provider_ref,
           endpoint_resource,call_resource,operation
      into slot_record from models.capability_runtime_approval_slot
     where realm_id=new.realm_id and id=new.slot_id;
    select work_item_id,task_plan_id,coordinator_job_id into manifest_record
      from models.capability_runtime_approval_manifest
     where realm_id=new.realm_id and id=new.manifest_id;
    select realm_id,work_item_id,plan_id,plan_digest,effect_digest,authorization_digest,
           scope,allowed_resources,allowed_effects,provider_refs,secret_ref_ids,state,
           expires_at,risk
      into auth_record from security.authorization where id=new.authorization_id;
    select state into episode_state from runtime.job
     where realm_id=new.realm_id and id=slot_record.job_id;
    select state into coordinator_state from runtime.job
     where realm_id=new.realm_id and id=manifest_record.coordinator_job_id;
    select * into derived_record
      from models.capability_runtime_derived_digests(new.realm_id,new.slot_id);
    if slot_record.turn_number = 1 then
        select chain_seed_digest into expected_prior_digest
          from models.capability_runtime_approval_slot
         where realm_id=new.realm_id and id=new.slot_id;
    else
        select outcome.result_digest into expected_prior_digest
          from models.capability_runtime_approval_slot previous_slot
          join models.capability_runtime_call_outcome outcome
            on outcome.realm_id=previous_slot.realm_id and outcome.slot_id=previous_slot.id
         where previous_slot.realm_id=new.realm_id
           and previous_slot.manifest_id=new.manifest_id
           and previous_slot.model_id=slot_record.model_id
           and previous_slot.task_digest=slot_record.task_digest
           and previous_slot.turn_number=slot_record.turn_number-1
           and outcome.status='completed';
    end if;
    if slot_record.manifest_id is distinct from new.manifest_id
       or expected_prior_digest is null
       or expected_prior_digest is distinct from new.prior_response_chain_digest
       or derived_record.request_body_digest is distinct from new.request_body_digest
       or derived_record.authorization_plan_digest is distinct from new.authorization_plan_digest
       or derived_record.effect_digest is distinct from new.effect_digest
       or auth_record.realm_id is distinct from new.realm_id
       or auth_record.work_item_id is distinct from manifest_record.work_item_id
       or auth_record.plan_id is distinct from manifest_record.task_plan_id
       or auth_record.plan_digest is distinct from new.authorization_plan_digest
       or auth_record.effect_digest is distinct from new.effect_digest
       or auth_record.authorization_digest is distinct from new.authorization_digest
       or auth_record.state <> 'issued' or auth_record.expires_at <= now()
       or auth_record.risk <> 'critical'
       or auth_record.allowed_effects <> array['provider-call']::text[]
       or cardinality(auth_record.allowed_resources)<>2
       or not auth_record.allowed_resources @>
          array[slot_record.call_resource,slot_record.endpoint_resource]::text[]
       or not auth_record.allowed_resources <@
          array[slot_record.call_resource,slot_record.endpoint_resource]::text[]
       or exists (select 1 from unnest(auth_record.allowed_resources) value where value like '%*')
       or auth_record.provider_refs <> array[slot_record.provider_ref]::text[]
       or cardinality(auth_record.secret_ref_ids) <> 1
       or auth_record.scope -> 'data_classifications' <> '["public"]'::jsonb
       or episode_state <> 'running' or coordinator_state not in ('ready','running') then
        raise exception 'capability runtime derived authorization tamper/scope/chain mismatch'
            using errcode='42501';
    end if;
    return new;
end
$$;

create function models.enforce_capability_runtime_continuity() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models as $$
declare slot_record record; expected_prior text; expected_checkpoint uuid; expected_attestation text;
        request_digest text;
begin
    select manifest_id,model_id,task_digest,turn_number,slot_digest,chain_seed_digest,
           request_template,request_template_digest
      into slot_record from models.capability_runtime_approval_slot
     where realm_id=new.realm_id and id=new.slot_id;
    if slot_record.turn_number=1 then
        expected_prior:=slot_record.chain_seed_digest;
        expected_checkpoint:=null;
    else
        select o.result_digest,o.checkpoint_id into expected_prior,expected_checkpoint
          from models.capability_runtime_approval_slot s
          join models.capability_runtime_call_outcome o
            on o.realm_id=s.realm_id and o.slot_id=s.id and o.status='completed'
         where s.realm_id=new.realm_id and s.manifest_id=new.manifest_id
           and s.model_id=slot_record.model_id and s.task_digest=slot_record.task_digest
           and s.turn_number=slot_record.turn_number-1;
    end if;
    request_digest:=models.capability_runtime_jsonb_digest(
        models.derive_capability_request_body(slot_record.request_template,new.continuity_state));
    expected_attestation:=models.capability_runtime_jsonb_digest(jsonb_build_object(
        'schema','zekam-capability-request-derivation/v1',
        'algorithm','zekam-capability-continuity-derive/v3',
        'template_digest',slot_record.request_template_digest,
        'continuity_state_digest',new.continuity_state_digest,
        'request_body_digest',request_digest
    ));
    if slot_record.manifest_id is distinct from new.manifest_id
       or new.continuity_state_digest is distinct from
          models.capability_runtime_jsonb_digest(new.continuity_state)
       or expected_prior is null or new.prior_result_digest is distinct from expected_prior
       or new.checkpoint_id is distinct from expected_checkpoint
       or new.derivation_attestation_digest is distinct from expected_attestation then
        raise exception 'capability runtime continuity digest/prior/checkpoint attestation mismatch'
            using errcode='42501';
    end if;
    return new;
end
$$;

create function models.validate_capability_runtime_slot_set() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models as $$
declare target_manifest uuid; target_realm uuid; expected_count integer; actual_count integer;
        plan_steps jsonb; coordinator_job uuid;
begin
    if tg_table_name = 'capability_runtime_approval_manifest' then
        target_manifest := coalesce(new.id, old.id);
    else
        target_manifest := coalesce(new.manifest_id, old.manifest_id);
    end if;
    target_realm := coalesce(new.realm_id, old.realm_id);
    select max_provider_calls into expected_count
      from models.capability_runtime_approval_manifest
     where realm_id=target_realm and id=target_manifest;
    if expected_count is null then return null; end if;
    select p.steps,m.coordinator_job_id into plan_steps,coordinator_job
      from models.capability_runtime_approval_manifest m
      join work.task_plan p on p.realm_id=m.realm_id and p.id=m.task_plan_id
     where m.realm_id=target_realm and m.id=target_manifest;
    select count(*) into actual_count from models.capability_runtime_approval_slot
     where realm_id=target_realm and manifest_id=target_manifest;
    if actual_count <> expected_count
       or (select count(distinct job_id) from models.capability_runtime_approval_slot
            where realm_id=target_realm and manifest_id=target_manifest) <> 21
       or jsonb_array_length(plan_steps)<>190
       or exists (
        select 1
          from models.capability_runtime_approval_manifest m
          cross join unnest(m.model_ids) model_id
          cross join unnest(m.task_digests) task_digest
          cross join generate_series(1,m.slots_per_episode) turn_number
         where m.realm_id=target_realm and m.id=target_manifest
           and not exists (
               select 1 from models.capability_runtime_approval_slot s
                where s.realm_id=m.realm_id and s.manifest_id=m.id
                  and s.model_id=model_id and s.task_digest=task_digest
                  and s.turn_number=turn_number
           )
    ) or exists (
        select 1 from models.capability_runtime_approval_slot s
         where s.realm_id=target_realm and s.manifest_id=target_manifest
           and not exists (
               select 1 from jsonb_array_elements(plan_steps) with ordinality step(value,ordinal)
                where ordinal=s.ordinal and value->>'step_id'=s.call_id
                  and value->>'effect'='provider-call'
                  and value->'logical_resources'=jsonb_build_array(s.call_resource)
                  and value->'depends_on'=case when s.turn_number=1 then '[]'::jsonb else (
                      select jsonb_build_array(previous.call_id)
                        from models.capability_runtime_approval_slot previous
                       where previous.realm_id=s.realm_id and previous.manifest_id=s.manifest_id
                         and previous.model_id=s.model_id and previous.task_digest=s.task_digest
                         and previous.turn_number=s.turn_number-1
                  ) end
           )
    ) or exists (
        select 1 from (
            select distinct s.job_id from models.capability_runtime_approval_slot s
             where s.realm_id=target_realm and s.manifest_id=target_manifest
        ) episode
        join runtime.job j on j.realm_id=target_realm and j.id=episode.job_id
        where not exists (
            select 1 from jsonb_array_elements(plan_steps) with ordinality step(value,ordinal)
             where ordinal between 169 and 189 and value->>'step_id'=j.step_id
               and value->>'effect'='database-write'
               and value->'logical_resources'=to_jsonb(j.write_resources)
               and value->'depends_on'=(
                   select to_jsonb(array_agg(s.call_id order by s.call_id))
                     from models.capability_runtime_approval_slot s
                    where s.realm_id=target_realm and s.manifest_id=target_manifest
                      and s.job_id=j.id
               )
        )
    ) or not exists (
        select 1 from runtime.job j,
             jsonb_array_elements(plan_steps) with ordinality step(value,ordinal)
         where j.realm_id=target_realm and j.id=coordinator_job and ordinal=190
           and value->>'step_id'=j.step_id and value->>'effect'='database-write'
           and value->'logical_resources'=to_jsonb(j.write_resources)
           and value->'depends_on'=(
               select to_jsonb(array_agg(distinct episode_job.step_id order by episode_job.step_id))
                 from models.capability_runtime_approval_slot s
                 join runtime.job episode_job on episode_job.realm_id=s.realm_id
                  and episode_job.id=s.job_id
                where s.realm_id=target_realm and s.manifest_id=target_manifest
           )
    ) then
        raise exception 'capability runtime approval requires exact 168-slot cartesian set'
            using errcode='23514';
    end if;
    return null;
end
$$;

create function models.enforce_capability_runtime_call_outcome() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, runtime, work as $$
declare slot_record record; claim_record record; receipt_record record;
        checkpoint_record record; job_state text;
begin
    select s.job_id,a.authorization_id,a.authorization_digest,a.effect_digest,
           derived.claim_operation
      into slot_record from models.capability_runtime_approval_slot s
      join models.capability_runtime_slot_authorization a
        on a.realm_id=s.realm_id and a.slot_id=s.id
      cross join lateral models.capability_runtime_derived_digests(s.realm_id,s.id) derived
     where s.realm_id=new.realm_id and s.id=new.slot_id;
    select realm_id,job_id,authorization_id,authorization_digest,effect_digest,operation
      into claim_record from runtime.effect_claim where id=new.claim_id;
    select realm_id,claim_id,status,result_digest,failure_category
      into receipt_record from runtime.effect_receipt where id=new.receipt_id;
    select realm_id,job_id,slot_id,result_digest into checkpoint_record
      from models.capability_runtime_turn_checkpoint where id=new.checkpoint_id;
    select state into job_state from runtime.job
     where realm_id=new.realm_id and id=slot_record.job_id;
    if slot_record.job_id is null
       or claim_record.realm_id is distinct from new.realm_id
       or claim_record.job_id is distinct from slot_record.job_id
       or claim_record.authorization_id is distinct from slot_record.authorization_id
       or claim_record.authorization_digest is distinct from slot_record.authorization_digest
       or claim_record.effect_digest is distinct from slot_record.effect_digest
       or claim_record.operation is distinct from slot_record.claim_operation
       or checkpoint_record.realm_id is distinct from new.realm_id
       or checkpoint_record.job_id is distinct from slot_record.job_id
       or checkpoint_record.slot_id is distinct from new.slot_id
       or (new.status='completed' and (
            receipt_record.realm_id is distinct from new.realm_id
            or receipt_record.claim_id is distinct from new.claim_id
            or receipt_record.status <> 'completed'
            or receipt_record.result_digest is distinct from new.result_digest
            or checkpoint_record.result_digest is distinct from new.result_digest
            or job_state not in ('running','completed')
       )) or (new.status='failed' and (
            receipt_record.realm_id is distinct from new.realm_id
            or receipt_record.claim_id is distinct from new.claim_id
            or receipt_record.status <> 'failed'
            or receipt_record.failure_category is distinct from new.failure_category
            or job_state not in ('failed','recovery-required')
       )) or (new.status='recovery-required' and job_state <> 'recovery-required') then
        raise exception 'capability runtime claim/receipt/checkpoint/job binding mismatch'
            using errcode='42501';
    end if;
    return new;
end
$$;

create function models.enforce_capability_runtime_turn_checkpoint() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models as $$
declare slot_record record;
begin
    select manifest_id,job_id,turn_number into slot_record
      from models.capability_runtime_approval_slot
     where realm_id=new.realm_id and id=new.slot_id;
    if slot_record.manifest_id is distinct from new.manifest_id
       or slot_record.job_id is distinct from new.job_id
       or new.completed_turns is distinct from (
           select array_agg(value order by value) from generate_series(1,slot_record.turn_number) value
       ) or new.pending_turns is distinct from coalesce((
           select array_agg(value order by value)
             from generate_series(slot_record.turn_number+1,8) value
       ),'{}'::integer[])
       or not exists (
           select 1 from models.capability_runtime_continuity_state c
            where c.realm_id=new.realm_id and c.id=new.continuity_state_id
              and c.slot_id=new.slot_id and c.manifest_id=new.manifest_id
       ) then
        raise exception 'capability runtime turn checkpoint exact partition mismatch'
            using errcode='42501';
    end if;
    return new;
end
$$;

create function models.enforce_capability_runtime_outcome() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models, runtime as $$
declare observed_count integer; observed_digests text[]; recovery_count integer;
        recovery_job_count integer; successful_count integer; completed_episode_jobs integer;
        coordinator_state text;
begin
    select count(*),array_agg(c.evidence_digest order by c.evidence_digest),
           count(*) filter (where c.status='recovery-required'),
           count(*) filter (
               where c.status='completed' and receipt.status='completed'
                 and receipt.result_digest=c.result_digest
           )
      into observed_count,observed_digests,recovery_count,successful_count
      from models.capability_runtime_approval_slot s
      join models.capability_runtime_call_outcome c
        on c.realm_id=s.realm_id and c.slot_id=s.id
      left join runtime.effect_receipt receipt
        on receipt.realm_id=c.realm_id and receipt.id=c.receipt_id and receipt.claim_id=c.claim_id
     where s.realm_id=new.realm_id and s.manifest_id=new.manifest_id;
    select count(*) into recovery_job_count
      from runtime.job j
      join models.capability_runtime_approval_manifest m
        on m.realm_id=j.realm_id and m.task_plan_id=j.plan_id
     where m.realm_id=new.realm_id and m.id=new.manifest_id
       and j.state='recovery-required';
    select count(distinct j.id) into completed_episode_jobs
      from models.capability_runtime_approval_slot s
      join runtime.job j on j.realm_id=s.realm_id and j.id=s.job_id and j.state='completed'
     where s.realm_id=new.realm_id and s.manifest_id=new.manifest_id;
    select j.state into coordinator_state
      from models.capability_runtime_approval_manifest m
      join runtime.job j on j.realm_id=m.realm_id and j.id=m.coordinator_job_id
     where m.realm_id=new.realm_id and m.id=new.manifest_id;
    if observed_count <> new.actual_provider_calls
       or observed_digests is distinct from (
           select array_agg(value order by value) from unnest(new.call_evidence_digests) value
       ) or (new.status='completed' and (
            observed_count<>168 or successful_count<>168 or recovery_count<>0
            or completed_episode_jobs<>21 or coordinator_state<>'completed'
            or recovery_job_count<>0
       ))
       or (new.status='partial' and (observed_count>=168 or recovery_count<>0
                                     or recovery_job_count<>0))
       or (new.status='recovery-required'
           and recovery_count=0 and recovery_job_count=0) then
        raise exception 'capability runtime aggregate count/status/evidence mismatch'
            using errcode='42501';
    end if;
    return new;
end
$$;

create function models.enforce_capability_runtime_scorecard_gate() returns trigger
language plpgsql security invoker set search_path = pg_catalog, models as $$
begin
    if exists (
        select 1 from models.capability_runtime_approval_manifest m
         where m.realm_id=new.realm_id and m.cohort_id=new.cohort_id
    ) and not exists (
        select 1 from models.capability_runtime_approval_manifest m
        join models.capability_runtime_outcome o
          on o.realm_id=m.realm_id and o.manifest_id=m.id
         where m.realm_id=new.realm_id and m.cohort_id=new.cohort_id
           and o.score_eligible
    ) then
        raise exception 'partial/recovery capability runtime cannot produce score or routing evidence'
            using errcode='42501';
    end if;
    return new;
end
$$;

create trigger capability_runtime_manifest_binding before insert
    on models.capability_runtime_approval_manifest for each row
    execute function models.enforce_capability_runtime_manifest();
create trigger capability_runtime_slot_binding before insert
    on models.capability_runtime_approval_slot for each row
    execute function models.enforce_capability_runtime_slot();
create trigger capability_runtime_slot_authorization_binding before insert
    on models.capability_runtime_slot_authorization for each row
    execute function models.enforce_capability_runtime_slot_authorization();
create trigger capability_runtime_continuity_binding before insert
    on models.capability_runtime_continuity_state for each row
    execute function models.enforce_capability_runtime_continuity();
create trigger capability_runtime_turn_checkpoint_binding before insert
    on models.capability_runtime_turn_checkpoint for each row
    execute function models.enforce_capability_runtime_turn_checkpoint();
create constraint trigger capability_runtime_manifest_exact_slots
    after insert on models.capability_runtime_approval_manifest deferrable initially deferred
    for each row execute function models.validate_capability_runtime_slot_set();
create constraint trigger capability_runtime_slot_exact_set
    after insert on models.capability_runtime_approval_slot deferrable initially deferred
    for each row execute function models.validate_capability_runtime_slot_set();
create trigger capability_runtime_call_binding before insert
    on models.capability_runtime_call_outcome for each row
    execute function models.enforce_capability_runtime_call_outcome();
create trigger capability_runtime_outcome_binding before insert
    on models.capability_runtime_outcome for each row
    execute function models.enforce_capability_runtime_outcome();
create trigger capability_runtime_scorecard_gate before insert
    on models.capability_benchmark_scorecard for each row
    execute function models.enforce_capability_runtime_scorecard_gate();

do $$ declare target text; begin
    foreach target in array array[
        'models.capability_runtime_approval_manifest',
        'models.capability_runtime_approval_slot',
        'models.capability_runtime_continuity_state',
        'models.capability_runtime_turn_checkpoint',
        'models.capability_runtime_slot_authorization',
        'models.capability_runtime_call_outcome',
        'models.capability_runtime_outcome'
    ] loop
        execute format('alter table %s enable row level security', target);
        execute format('alter table %s force row level security', target);
        execute format('create policy scope_select on %s for select using (realm_id=core.current_realm_id())', target);
        execute format('create policy scope_insert on %s for insert with check (realm_id=core.current_realm_id())', target);
        execute format('create trigger deny_update before update on %s for each statement execute function core.deny_mutation()', target);
        execute format('create trigger deny_delete before delete on %s for each statement execute function core.deny_mutation()', target);
    end loop;
end $$;

create index capability_runtime_slot_manifest_idx
    on models.capability_runtime_approval_slot (realm_id, manifest_id, ordinal);
create index capability_runtime_call_status_idx
    on models.capability_runtime_call_outcome (realm_id, status, completed_at);

grant select,insert on models.capability_runtime_approval_manifest,
    models.capability_runtime_approval_slot,models.capability_runtime_continuity_state,
    models.capability_runtime_turn_checkpoint,
    models.capability_runtime_slot_authorization,
    models.capability_runtime_call_outcome,
    models.capability_runtime_outcome to zekam_app;
grant execute on function models.enforce_capability_runtime_manifest() to zekam_app;
grant execute on function models.enforce_capability_runtime_slot() to zekam_app;
grant execute on function models.enforce_capability_runtime_slot_authorization() to zekam_app;
grant execute on function models.enforce_capability_runtime_continuity() to zekam_app;
grant execute on function models.enforce_capability_runtime_turn_checkpoint() to zekam_app;
grant execute on function models.capability_runtime_canonical_json(jsonb) to zekam_app;
grant execute on function models.capability_runtime_jsonb_digest(jsonb) to zekam_app;
grant execute on function models.derive_capability_request_body(jsonb,jsonb) to zekam_app;
grant execute on function models.capability_runtime_derived_digests(uuid,uuid) to zekam_app;
grant execute on function models.validate_capability_runtime_slot_set() to zekam_app;
grant execute on function models.enforce_capability_runtime_call_outcome() to zekam_app;
grant execute on function models.enforce_capability_runtime_outcome() to zekam_app;
grant execute on function models.enforce_capability_runtime_scorecard_gate() to zekam_app;
