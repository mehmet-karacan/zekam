-- Memory usage and verified outcome telemetry derived from canonical runtime evidence.

alter table work.context_fragment
    add constraint context_fragment_realm_scoped_key unique (realm_id, id);

create table memory.usage_event (
    id uuid primary key,
    realm_id uuid not null,
    record_id uuid not null,
    context_fragment_id uuid not null,
    fragment_set_id uuid not null,
    context_manifest_id uuid not null,
    request_manifest_id uuid not null,
    invocation_attempt_id uuid not null,
    invocation_result_id uuid not null,
    task_plan_id uuid not null,
    run_id uuid not null,
    job_id uuid not null,
    runtime_attempt_id uuid not null,
    assignment_id uuid not null,
    step_id text not null,
    project_id uuid not null,
    work_item_id uuid not null,
    source_authorization_id uuid not null,
    record_digest text not null,
    fragment_digest text not null,
    model_visible_payload_digest text not null,
    context_manifest_digest text not null,
    used_at timestamptz not null,
    event_digest text not null,
    grants_authority boolean not null default false,
    unique (realm_id, id),
    unique (realm_id, invocation_result_id, context_fragment_id, record_id),
    unique (realm_id, event_digest),
    foreign key (realm_id, record_id) references memory.record (realm_id, id),
    foreign key (realm_id, context_fragment_id)
        references work.context_fragment (realm_id, id),
    foreign key (realm_id, fragment_set_id)
        references work.context_fragment_set (realm_id, id),
    foreign key (realm_id, context_manifest_id)
        references work.context_manifest (realm_id, id),
    foreign key (realm_id, request_manifest_id)
        references models.request_manifest (realm_id, id),
    foreign key (realm_id, invocation_attempt_id)
        references models.invocation_attempt (realm_id, id),
    foreign key (realm_id, invocation_result_id)
        references models.invocation_result (realm_id, id),
    foreign key (realm_id, task_plan_id) references work.task_plan (realm_id, id),
    foreign key (realm_id, run_id) references runtime.execution_run (realm_id, id),
    foreign key (realm_id, job_id) references runtime.job (realm_id, id),
    foreign key (realm_id, runtime_attempt_id)
        references runtime.job_attempt (realm_id, id),
    foreign key (realm_id, assignment_id) references agents.assignment (realm_id, id),
    foreign key (realm_id, project_id) references projects.project (realm_id, id),
    foreign key (realm_id, work_item_id) references work.work_item (realm_id, id),
    foreign key (realm_id, source_authorization_id)
        references security.authorization (realm_id, id),
    check (record_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (fragment_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (model_visible_payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (context_manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (event_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (btrim(step_id) <> ''),
    check (not grants_authority)
);

create table memory.usage_outcome (
    id uuid primary key,
    realm_id uuid not null,
    usage_event_id uuid not null,
    checkpoint_id uuid not null,
    step_id text not null,
    verifier_assignment_id uuid not null,
    verifier_invocation_id uuid not null,
    verifier_envelope_digest text not null,
    checkpoint_digest text not null,
    result_digest text not null,
    outcome_status text not null,
    correlated_at timestamptz not null,
    outcome_digest text not null,
    grants_authority boolean not null default false,
    unique (realm_id, id),
    unique (realm_id, usage_event_id, checkpoint_id, step_id, verifier_invocation_id),
    unique (realm_id, outcome_digest),
    foreign key (realm_id, usage_event_id) references memory.usage_event (realm_id, id),
    foreign key (realm_id, checkpoint_id) references work.checkpoint_v2 (realm_id, id),
    foreign key (realm_id, checkpoint_id, step_id, verifier_invocation_id)
        references work.checkpoint_v2_step_verification
            (realm_id, checkpoint_id, step_id, verifier_invocation_id),
    foreign key (realm_id, verifier_assignment_id)
        references agents.assignment (realm_id, id),
    foreign key (realm_id, verifier_invocation_id)
        references agents.invocation (realm_id, id),
    check (outcome_status = 'verified-success'),
    check (verifier_envelope_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (checkpoint_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (result_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (outcome_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (not grants_authority)
);

create function memory.enforce_usage_event() returns trigger
language plpgsql security definer
set search_path = pg_catalog, memory, work, models, security as $$
declare record_ record; fragment_ record; request_ record; attempt_ record; result_ record;
        receipt_ record; claim_ record; authorization_ record;
        expected_digest text;
begin
    select logical_memory_id, scope, project_id, work_item_id, content, state,
           valid_from, valid_until, record_digest
      into record_ from memory.record
     where realm_id = new.realm_id and id = new.record_id;
    select fragment_set_id, context_manifest_id, project_id, work_item_id, content_kind,
           visibility, source_ref, source_revision, content_digest, fragment_digest, created_at
      into fragment_ from work.context_fragment
     where realm_id = new.realm_id and id = new.context_fragment_id;
    select project_id, work_item_id, plan_id, run_id, job_id, attempt_id,
           assignment_id, step_id, context_manifest_digest, context_fragment_set_digest,
           model_visible_payload_digest
      into request_ from models.request_manifest
     where realm_id = new.realm_id and id = new.request_manifest_id;
    select manifest_id, authorization_id, state into attempt_
      from models.invocation_attempt
     where realm_id = new.realm_id and id = new.invocation_attempt_id;
    select manifest_id, attempt_id, state, effect_receipt_id, created_at into result_
      from models.invocation_result
     where realm_id = new.realm_id and id = new.invocation_result_id;
    select status, claim_id into receipt_ from runtime.effect_receipt
     where realm_id = new.realm_id and id = result_.effect_receipt_id;
    select job_id, attempt_id, authorization_id, authorization_digest, effect_digest
      into claim_ from runtime.effect_claim
     where realm_id = new.realm_id and id = receipt_.claim_id;
    select work_item_id, plan_id, plan_digest, state, effect_digest, authorization_digest,
           allowed_effects, provider_refs into authorization_ from security.authorization
     where realm_id = new.realm_id and id = attempt_.authorization_id;

    if record_.logical_memory_id is null or fragment_.fragment_set_id is null
       or request_.project_id is null or attempt_.manifest_id is null
       or result_.manifest_id is null then
        raise exception 'memory usage canonical binding missing' using errcode = '23514';
    end if;
    if fragment_.content_kind <> 'memory' or fragment_.visibility <> 'model-visible'
       or fragment_.source_ref <> 'memory-record/' || new.record_id::text
       or fragment_.source_revision <> record_.record_digest
       or fragment_.content_digest <> models.capability_runtime_jsonb_digest(to_jsonb(record_.content))
       or row(new.fragment_set_id, new.context_manifest_id, new.project_id, new.work_item_id,
              new.record_digest, new.fragment_digest, new.context_manifest_digest)
          is distinct from
          row(fragment_.fragment_set_id, fragment_.context_manifest_id, fragment_.project_id,
              fragment_.work_item_id, record_.record_digest, fragment_.fragment_digest,
              request_.context_manifest_digest) then
        raise exception 'memory usage record/fragment exact binding drift' using errcode = '23514';
    end if;
    if row(request_.project_id, request_.work_item_id, request_.context_fragment_set_digest,
           request_.model_visible_payload_digest)
       is distinct from
       row(fragment_.project_id, fragment_.work_item_id,
           (select fragment_set_digest from work.context_fragment_set
             where realm_id = new.realm_id and id = fragment_.fragment_set_id),
           new.model_visible_payload_digest)
       or request_.context_manifest_digest is distinct from
          (select manifest_digest from work.context_manifest
            where realm_id = new.realm_id and id = fragment_.context_manifest_id)
       or row(attempt_.manifest_id, attempt_.authorization_id)
          is distinct from row(new.request_manifest_id, new.source_authorization_id)
       or row(result_.manifest_id, result_.attempt_id, result_.state)
          is distinct from row(new.request_manifest_id, new.invocation_attempt_id, 'verified')
       or result_.effect_receipt_id is null
       or receipt_.status is distinct from 'completed'
       or receipt_.claim_id is distinct from
          (select effect_claim_id from models.invocation_attempt
            where realm_id = new.realm_id and id = new.invocation_attempt_id)
       or request_.model_visible_payload_digest is null
       or row(new.task_plan_id, new.run_id, new.job_id, new.runtime_attempt_id,
              new.assignment_id, new.step_id)
          is distinct from row(request_.plan_id, request_.run_id, request_.job_id,
              request_.attempt_id, request_.assignment_id, request_.step_id)
       or row(claim_.job_id, claim_.attempt_id, claim_.authorization_id,
              claim_.authorization_digest, claim_.effect_digest)
          is distinct from row(request_.job_id, request_.attempt_id,
              attempt_.authorization_id, authorization_.authorization_digest,
              authorization_.effect_digest)
       or row(authorization_.work_item_id, authorization_.plan_id, authorization_.state)
          is distinct from row(request_.work_item_id, request_.plan_id, 'consumed')
       or authorization_.plan_digest is distinct from
          (select plan_digest from work.task_plan
            where realm_id = new.realm_id and id = request_.plan_id)
       or authorization_.allowed_effects is distinct from array['provider-call']::text[]
       or authorization_.provider_refs is distinct from array[
          (select provider_ref from models.request_manifest
            where realm_id = new.realm_id and id = new.request_manifest_id)
       ]::text[]
       or result_.state = 'verified' and
          (select result_digest from runtime.effect_receipt
            where realm_id = new.realm_id and id = result_.effect_receipt_id)
          is distinct from
          (select response_digest from models.invocation_result
            where realm_id = new.realm_id and id = new.invocation_result_id)
       or new.used_at is distinct from result_.created_at
       or attempt_.state not in ('sent', 'response-received', 'parsed', 'verified') then
        raise exception 'memory usage verified invocation binding drift' using errcode = '23514';
    end if;
    if (record_.scope = 'project' and record_.project_id is distinct from new.project_id)
       or (record_.scope = 'work-item' and record_.work_item_id is distinct from new.work_item_id)
       or record_.scope in ('run', 'agent')
       or fragment_.created_at < coalesce(record_.valid_from, '-infinity'::timestamptz)
       or fragment_.created_at >= coalesce(record_.valid_until, 'infinity'::timestamptz) then
        raise exception 'memory usage scope/temporal binding drift' using errcode = '23514';
    end if;
    expected_digest := models.capability_runtime_jsonb_digest(jsonb_build_object(
        'schema', 'zekam-memory-usage/v1', 'realm_id', new.realm_id::text,
        'record_id', new.record_id::text, 'record_digest', new.record_digest,
        'context_fragment_id', new.context_fragment_id::text,
        'fragment_digest', new.fragment_digest,
        'request_manifest_id', new.request_manifest_id::text,
        'invocation_attempt_id', new.invocation_attempt_id::text,
        'invocation_result_id', new.invocation_result_id::text,
        'task_plan_id', new.task_plan_id::text, 'run_id', new.run_id::text,
        'job_id', new.job_id::text, 'runtime_attempt_id', new.runtime_attempt_id::text,
        'assignment_id', new.assignment_id::text, 'step_id', new.step_id,
        'model_visible_payload_digest', new.model_visible_payload_digest,
        'context_manifest_digest', new.context_manifest_digest,
        'project_id', new.project_id::text, 'work_item_id', new.work_item_id::text,
        'used_at', new.used_at, 'grants_authority', false
    ));
    if new.event_digest is distinct from expected_digest then
        raise exception 'memory usage canonical digest mismatch' using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger usage_event_integrity before insert on memory.usage_event
    for each row execute function memory.enforce_usage_event();

create function memory.capture_verified_invocation_usage() returns trigger
language plpgsql security definer
set search_path = pg_catalog, memory, work, models, security as $$
declare item record; event_body jsonb; expected_count integer; valid_count integer;
begin
    if new.state <> 'verified' or new.effect_receipt_id is null then return new; end if;
    select count(*) into expected_count
      from models.request_manifest m
      join work.context_fragment_set s on s.realm_id = m.realm_id
       and s.fragment_set_digest = m.context_fragment_set_digest
      join work.context_fragment f on f.realm_id = s.realm_id and f.fragment_set_id = s.id
       and f.content_kind = 'memory' and f.visibility = 'model-visible'
     where m.realm_id = new.realm_id and m.id = new.manifest_id;
    if expected_count > 0 then
        if not exists (
            select 1 from models.invocation_attempt a
            join models.request_manifest m on m.realm_id = a.realm_id and m.id = a.manifest_id
            join runtime.effect_claim claim on claim.realm_id = a.realm_id
             and claim.id = a.effect_claim_id and claim.job_id = m.job_id
             and claim.attempt_id = m.attempt_id and claim.authorization_id = a.authorization_id
            join security.authorization auth on auth.realm_id = a.realm_id
             and auth.id = a.authorization_id and auth.state = 'consumed'
             and auth.work_item_id = m.work_item_id
             and auth.plan_id = m.plan_id
             and auth.plan_digest = (
                 select plan_digest from work.task_plan p
                  where p.realm_id = m.realm_id and p.id = m.plan_id
             )
             and auth.effect_digest = claim.effect_digest
             and auth.authorization_digest = claim.authorization_digest
             and auth.allowed_effects = array['provider-call']::text[]
             and auth.provider_refs = array[m.provider_ref]::text[]
             and claim.operation = 'provider.invoke'
            join runtime.effect_receipt receipt on receipt.realm_id = a.realm_id
             and receipt.id = new.effect_receipt_id and receipt.claim_id = a.effect_claim_id
             and receipt.status = 'completed' and receipt.result_digest = new.response_digest
            where a.realm_id = new.realm_id and a.id = new.attempt_id
              and a.manifest_id = new.manifest_id
              and m.model_visible_payload_digest is not null
        ) then
            raise exception 'memory usage terminal invocation receipt binding drift'
                using errcode = '23514';
        end if;
        select count(*) into valid_count
          from models.request_manifest m
          join work.context_fragment_set s on s.realm_id = m.realm_id
           and s.fragment_set_digest = m.context_fragment_set_digest
          join work.context_fragment f on f.realm_id = s.realm_id and f.fragment_set_id = s.id
           and f.content_kind = 'memory' and f.visibility = 'model-visible'
          join memory.record r on r.realm_id = f.realm_id
           and f.source_ref = 'memory-record/' || r.id::text
           and f.source_revision = r.record_digest
           and f.content_digest = models.capability_runtime_jsonb_digest(to_jsonb(r.content))
           and r.scope not in ('run', 'agent')
           and (r.scope <> 'project' or r.project_id = f.project_id)
           and (r.scope <> 'work-item' or r.work_item_id = f.work_item_id)
           and f.created_at >= coalesce(r.valid_from, '-infinity'::timestamptz)
           and f.created_at < coalesce(r.valid_until, 'infinity'::timestamptz)
         where m.realm_id = new.realm_id and m.id = new.manifest_id;
        if valid_count <> expected_count then
            raise exception 'memory usage selected fragment/record partition drift'
                using errcode = '23514';
        end if;
    end if;
    for item in
        select r.id record_id, r.record_digest, f.id fragment_id, f.fragment_digest,
               f.fragment_set_id, f.context_manifest_id, f.project_id, f.work_item_id,
               m.model_visible_payload_digest, m.context_manifest_digest,
               m.plan_id, m.run_id, m.job_id, m.attempt_id runtime_attempt_id,
               m.assignment_id, m.step_id, a.authorization_id
          from models.invocation_attempt a
          join models.request_manifest m on m.realm_id = a.realm_id and m.id = a.manifest_id
          join work.context_fragment_set s on s.realm_id = m.realm_id
           and s.fragment_set_digest = m.context_fragment_set_digest
          join work.context_fragment f on f.realm_id = s.realm_id and f.fragment_set_id = s.id
           and f.content_kind = 'memory' and f.visibility = 'model-visible'
          join memory.record r on r.realm_id = f.realm_id
           and f.source_ref = 'memory-record/' || r.id::text
           and f.source_revision = r.record_digest
           and f.content_digest = models.capability_runtime_jsonb_digest(to_jsonb(r.content))
         where a.realm_id = new.realm_id and a.id = new.attempt_id
           and a.manifest_id = new.manifest_id
           and m.model_visible_payload_digest is not null
           and exists (
               select 1 from runtime.effect_receipt receipt
                where receipt.realm_id = new.realm_id
                  and receipt.id = new.effect_receipt_id
                  and receipt.claim_id = a.effect_claim_id
                  and receipt.status = 'completed'
           )
    loop
        event_body := jsonb_build_object(
            'schema', 'zekam-memory-usage/v1', 'realm_id', new.realm_id::text,
            'record_id', item.record_id::text, 'record_digest', item.record_digest,
            'context_fragment_id', item.fragment_id::text,
            'fragment_digest', item.fragment_digest,
            'request_manifest_id', new.manifest_id::text,
            'invocation_attempt_id', new.attempt_id::text,
            'invocation_result_id', new.id::text,
            'task_plan_id', item.plan_id::text, 'run_id', item.run_id::text,
            'job_id', item.job_id::text,
            'runtime_attempt_id', item.runtime_attempt_id::text,
            'assignment_id', item.assignment_id::text, 'step_id', item.step_id,
            'model_visible_payload_digest', item.model_visible_payload_digest,
            'context_manifest_digest', item.context_manifest_digest,
            'project_id', item.project_id::text, 'work_item_id', item.work_item_id::text,
            'used_at', new.created_at, 'grants_authority', false
        );
        insert into memory.usage_event (
            id, realm_id, record_id, context_fragment_id, fragment_set_id,
            context_manifest_id, request_manifest_id, invocation_attempt_id,
            invocation_result_id, task_plan_id, run_id, job_id, runtime_attempt_id,
            assignment_id, step_id, project_id, work_item_id, source_authorization_id,
            record_digest, fragment_digest, model_visible_payload_digest,
            context_manifest_digest, used_at,
            event_digest, grants_authority
        ) values (
            gen_random_uuid(), new.realm_id, item.record_id, item.fragment_id,
            item.fragment_set_id, item.context_manifest_id, new.manifest_id, new.attempt_id,
            new.id, item.plan_id, item.run_id, item.job_id, item.runtime_attempt_id,
            item.assignment_id, item.step_id, item.project_id, item.work_item_id,
            item.authorization_id,
            item.record_digest, item.fragment_digest, item.model_visible_payload_digest,
            item.context_manifest_digest, new.created_at,
            models.capability_runtime_jsonb_digest(event_body), false
        ) on conflict (realm_id, invocation_result_id, context_fragment_id, record_id) do nothing;
    end loop;
    return new;
end
$$;

create trigger invocation_memory_usage after insert on models.invocation_result
    for each row execute function memory.capture_verified_invocation_usage();

create function memory.project_last_used_at() returns trigger
language plpgsql security definer set search_path = pg_catalog, memory as $$
begin
    update memory.record set last_used_at = greatest(coalesce(last_used_at, new.used_at), new.used_at)
     where realm_id = new.realm_id and id = new.record_id;
    return new;
end
$$;
create trigger usage_last_used_projection after insert on memory.usage_event
    for each row execute function memory.project_last_used_at();

create function memory.enforce_usage_outcome() returns trigger
language plpgsql security definer
set search_path = pg_catalog, memory, work, agents as $$
declare usage_ record; checkpoint_ record; verification_ record; result_ record;
        expected_digest text;
begin
    select project_id, work_item_id, task_plan_id, run_id, job_id,
           runtime_attempt_id, assignment_id, step_id, context_manifest_digest, used_at
      into usage_ from memory.usage_event
     where realm_id = new.realm_id and id = new.usage_event_id;
    select project_id, work_item_id, task_plan_id, run_id, context_manifest_digest,
           checkpoint_digest, created_at into checkpoint_
      from work.checkpoint_v2 where realm_id = new.realm_id and id = new.checkpoint_id;
    select verifier_assignment_id, verifier_invocation_id, envelope_digest into verification_
      from work.checkpoint_v2_step_verification where realm_id = new.realm_id
       and checkpoint_id = new.checkpoint_id and step_id = new.step_id
       and verifier_invocation_id = new.verifier_invocation_id;
    select result_digest, job_id, attempt_id, assignment_id into result_
      from work.checkpoint_v2_step_result
     where realm_id = new.realm_id and checkpoint_id = new.checkpoint_id
       and step_id = new.step_id;
    if usage_.project_id is null or checkpoint_.project_id is null
       or not work.validate_checkpoint_v2(new.realm_id, new.checkpoint_id)
       or row(usage_.project_id, usage_.work_item_id)
          is distinct from row(checkpoint_.project_id, checkpoint_.work_item_id)
       or row(usage_.task_plan_id, usage_.run_id, usage_.job_id,
              usage_.runtime_attempt_id, usage_.assignment_id, usage_.step_id)
          is distinct from row(checkpoint_.task_plan_id, checkpoint_.run_id,
              result_.job_id, result_.attempt_id, result_.assignment_id, new.step_id)
       or checkpoint_.created_at < usage_.used_at
       or row(new.verifier_assignment_id, new.verifier_invocation_id,
              new.verifier_envelope_digest, new.checkpoint_digest, new.result_digest)
          is distinct from row(verification_.verifier_assignment_id,
              verification_.verifier_invocation_id, verification_.envelope_digest,
              checkpoint_.checkpoint_digest, result_.result_digest)
       or new.outcome_status <> 'verified-success' then
        raise exception 'memory outcome canonical verifier/checkpoint binding drift'
            using errcode = '23514';
    end if;
    expected_digest := models.capability_runtime_jsonb_digest(jsonb_build_object(
        'schema', 'zekam-memory-usage-outcome/v1', 'realm_id', new.realm_id::text,
        'usage_event_id', new.usage_event_id::text,
        'checkpoint_id', new.checkpoint_id::text, 'step_id', new.step_id,
        'verifier_assignment_id', new.verifier_assignment_id::text,
        'verifier_invocation_id', new.verifier_invocation_id::text,
        'verifier_envelope_digest', new.verifier_envelope_digest,
        'checkpoint_digest', new.checkpoint_digest, 'result_digest', new.result_digest,
        'outcome_status', new.outcome_status, 'correlated_at', new.correlated_at,
        'grants_authority', false
    ));
    if new.outcome_digest is distinct from expected_digest then
        raise exception 'memory outcome canonical digest mismatch' using errcode = '23514';
    end if;
    return new;
end
$$;
create trigger usage_outcome_integrity before insert on memory.usage_outcome
    for each row execute function memory.enforce_usage_outcome();

create function memory.capture_checkpoint_usage_outcomes() returns trigger
language plpgsql security definer
set search_path = pg_catalog, memory, work, models as $$
declare item record; checkpoint_ record; result_digest_ text; outcome_body jsonb;
begin
    if not work.validate_checkpoint_v2(new.realm_id, new.checkpoint_id) then
        raise exception 'memory outcome requires canonical complete checkpoint'
            using errcode = '23514';
    end if;
    select project_id, work_item_id, checkpoint_digest, created_at into checkpoint_
      from work.checkpoint_v2 where realm_id = new.realm_id and id = new.checkpoint_id;
    select result_digest into result_digest_ from work.checkpoint_v2_step_result
     where realm_id = new.realm_id and checkpoint_id = new.checkpoint_id
       and step_id = new.step_id;
    for item in
        select u.id, u.used_at from memory.usage_event u
        join work.checkpoint_v2_step_result sr on sr.realm_id = u.realm_id
         and sr.checkpoint_id = new.checkpoint_id and sr.step_id = new.step_id
         and sr.job_id = u.job_id and sr.attempt_id = u.runtime_attempt_id
         and sr.assignment_id = u.assignment_id
        where u.realm_id = new.realm_id and u.project_id = checkpoint_.project_id
          and u.work_item_id = checkpoint_.work_item_id and u.used_at <= checkpoint_.created_at
          and u.task_plan_id = (
              select task_plan_id from work.checkpoint_v2
               where realm_id = new.realm_id and id = new.checkpoint_id
          )
          and u.run_id = (
              select run_id from work.checkpoint_v2
               where realm_id = new.realm_id and id = new.checkpoint_id
          )
    loop
        outcome_body := jsonb_build_object(
            'schema', 'zekam-memory-usage-outcome/v1', 'realm_id', new.realm_id::text,
            'usage_event_id', item.id::text, 'checkpoint_id', new.checkpoint_id::text,
            'step_id', new.step_id, 'verifier_assignment_id', new.verifier_assignment_id::text,
            'verifier_invocation_id', new.verifier_invocation_id::text,
            'verifier_envelope_digest', new.envelope_digest,
            'checkpoint_digest', checkpoint_.checkpoint_digest,
            'result_digest', result_digest_, 'outcome_status', 'verified-success',
            'correlated_at', checkpoint_.created_at, 'grants_authority', false
        );
        insert into memory.usage_outcome (
            id, realm_id, usage_event_id, checkpoint_id, step_id,
            verifier_assignment_id, verifier_invocation_id, verifier_envelope_digest,
            checkpoint_digest, result_digest, outcome_status, correlated_at,
            outcome_digest, grants_authority
        ) values (
            gen_random_uuid(), new.realm_id, item.id, new.checkpoint_id, new.step_id,
            new.verifier_assignment_id, new.verifier_invocation_id, new.envelope_digest,
            checkpoint_.checkpoint_digest, result_digest_, 'verified-success',
            checkpoint_.created_at, models.capability_runtime_jsonb_digest(outcome_body), false
        ) on conflict (realm_id, usage_event_id, checkpoint_id, step_id,
                       verifier_invocation_id) do nothing;
    end loop;
    return null;
end
$$;
create constraint trigger checkpoint_memory_outcome
    after insert on work.checkpoint_v2_step_verification deferrable initially deferred
    for each row execute function memory.capture_checkpoint_usage_outcomes();

alter table memory.usage_event enable row level security;
alter table memory.usage_event force row level security;
alter table memory.usage_outcome enable row level security;
alter table memory.usage_outcome force row level security;
create policy scope_select on memory.usage_event for select
    using (realm_id = core.current_realm_id());
create policy scope_select on memory.usage_outcome for select
    using (realm_id = core.current_realm_id());
create trigger deny_update before update on memory.usage_event
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on memory.usage_event
    for each statement execute function core.deny_mutation();
create trigger deny_update before update on memory.usage_outcome
    for each statement execute function core.deny_mutation();
create trigger deny_delete before delete on memory.usage_outcome
    for each statement execute function core.deny_mutation();

create index usage_event_record_time_idx on memory.usage_event (realm_id, record_id, used_at desc);
create index usage_event_work_time_idx on memory.usage_event (realm_id, work_item_id, used_at desc);
create index usage_outcome_usage_idx on memory.usage_outcome (realm_id, usage_event_id);

create view memory.usage_effectiveness as
select u.realm_id, u.record_id, u.record_digest,
       count(*) as usage_count,
       count(o.id) as verified_outcome_count,
       count(o.id) filter (where o.outcome_status = 'verified-success') as verified_success_count,
       max(u.used_at) as last_used_at,
       max(o.correlated_at) as last_verified_outcome_at
from memory.usage_event u
left join memory.usage_outcome o
  on o.realm_id = u.realm_id and o.usage_event_id = u.id
group by u.realm_id, u.record_id, u.record_digest;

create function memory.rebuild_last_used_projection(p_record_id uuid) returns timestamptz
language plpgsql security definer set search_path = pg_catalog, memory as $$
declare projected timestamptz;
begin
    select max(used_at) into projected from memory.usage_event
     where realm_id = core.current_realm_id() and record_id = p_record_id;
    update memory.record set last_used_at = projected
     where realm_id = core.current_realm_id() and id = p_record_id;
    if not found then
        raise exception 'memory projection record missing' using errcode = 'P0002';
    end if;
    return projected;
end
$$;

revoke update (last_used_at) on memory.record from zekam_app;
grant select on memory.usage_event, memory.usage_outcome, memory.usage_effectiveness to zekam_app;
grant execute on function memory.enforce_usage_event() to zekam_app;
grant execute on function memory.enforce_usage_outcome() to zekam_app;
grant execute on function memory.rebuild_last_used_projection(uuid) to zekam_app;
