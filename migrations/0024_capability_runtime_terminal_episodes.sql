-- Capability runtime terminal episode semantics.
-- Model-contract failures are terminal calibration evidence; infrastructure,
-- transport and authority ambiguity remain recovery-only and never scoreable.

create table models.capability_runtime_episode_outcome (
    id uuid primary key,
    realm_id uuid not null references core.realm(id) on delete restrict,
    manifest_id uuid not null,
    model_id text not null,
    task_digest text not null,
    job_id uuid not null,
    status text not null,
    attempted_calls integer not null,
    successful_calls integer not null,
    failure_turn integer,
    reason_code text,
    evidence_digest text not null,
    completed_at timestamptz not null,
    constraint capability_runtime_episode_realm_id_unique unique(realm_id,id),
    constraint capability_runtime_episode_manifest_same_realm
        foreign key(realm_id,manifest_id)
        references models.capability_runtime_approval_manifest(realm_id,id) on delete restrict,
    constraint capability_runtime_episode_job_same_realm
        foreign key(realm_id,job_id) references runtime.job(realm_id,id) on delete restrict,
    constraint capability_runtime_episode_unique
        unique(realm_id,manifest_id,model_id,task_digest),
    constraint capability_runtime_episode_evidence_unique unique(realm_id,evidence_digest),
    constraint capability_runtime_episode_shape check (
        status in ('successful','model-contract-failed','recovery-required')
        and attempted_calls between 0 and 8
        and successful_calls between 0 and attempted_calls
        and evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and (
            (status='successful' and attempted_calls=8 and successful_calls=8
             and failure_turn is null and reason_code is null)
            or (status='model-contract-failed' and attempted_calls between 1 and 8
                and successful_calls=attempted_calls and failure_turn=attempted_calls
                and reason_code in ('malformed-model-response','model-contract-failure',
                                    'model-response-contract',
                                    'continuity-contract-violation'))
            or (status='recovery-required' and successful_calls<=attempted_calls
                and failure_turn is null
                and reason_code in ('infrastructure-failure','transport-failure',
                                    'authority-failure','ambiguous-effect'))
        )
    )
);

create table models.capability_runtime_skipped_slot (
    id uuid primary key,
    realm_id uuid not null references core.realm(id) on delete restrict,
    manifest_id uuid not null,
    episode_outcome_id uuid not null,
    slot_id uuid not null,
    reason_code text not null,
    evidence_digest text not null,
    sealed_at timestamptz not null,
    constraint capability_runtime_skipped_realm_id_unique unique(realm_id,id),
    constraint capability_runtime_skipped_manifest_same_realm
        foreign key(realm_id,manifest_id)
        references models.capability_runtime_approval_manifest(realm_id,id) on delete restrict,
    constraint capability_runtime_skipped_episode_same_realm
        foreign key(realm_id,episode_outcome_id)
        references models.capability_runtime_episode_outcome(realm_id,id) on delete restrict,
    constraint capability_runtime_skipped_slot_same_realm
        foreign key(realm_id,slot_id)
        references models.capability_runtime_approval_slot(realm_id,id) on delete restrict,
    constraint capability_runtime_skipped_slot_unique unique(slot_id),
    constraint capability_runtime_skipped_evidence_unique unique(realm_id,evidence_digest),
    constraint capability_runtime_skipped_shape check (
        reason_code in ('malformed-model-response','model-contract-failure',
                        'model-response-contract',
                        'continuity-contract-violation')
        and evidence_digest ~ '^sha256:[0-9a-f]{64}$'
    )
);

alter table models.capability_benchmark_episode drop constraint capability_episode_shape;
alter table models.capability_benchmark_episode add constraint capability_episode_shape check (
    role in ('implementer','reviewer','researcher','verifier')
    and status in ('passed','failed','timeout','unsafe','infrastructure-invalid','not-comparable')
    and duration_ms>=0 and start_skew_ms>=0 and model_turn_count>0
    and input_token_count>=0 and output_token_count>=0
    and correctness between 0 and 1 and completion between 0 and 1
    and sustained_progress between 0 and 1 and context_retention between 0 and 1
    and self_correction between 0 and 1 and tool_efficiency between 0 and 1
    and safety between 0 and 1 and hidden_acceptance_ratio between 0 and 1
    and sustained_progress_auc between 0 and 1 and noop_ratio between 0 and 1
    and least(longest_stagnation_ms,regression_count,checkpoint_count,
              self_correction_count,tool_call_count)>=0
    and (
        (checkpoint_count=0 and cardinality(checkpoint_receipt_digests)=0)
        or models.valid_routing_digest_array(checkpoint_receipt_digests)
    )
    and (
        (tool_call_count=0 and cardinality(tool_receipt_digests)=0)
        or (tool_call_count>0 and models.valid_routing_digest_array(tool_receipt_digests))
    )
    and cardinality(checkpoint_receipt_digests)=checkpoint_count
    and cardinality(tool_receipt_digests)=tool_call_count
    and task_digest ~ '^sha256:[0-9a-f]{64}$'
    and response_digest ~ '^sha256:[0-9a-f]{64}$'
    and verifier_provenance_digest ~ '^sha256:[0-9a-f]{64}$'
    and evidence_digest ~ '^sha256:[0-9a-f]{64}$'
    and acceptance_evidence_digest ~ '^sha256:[0-9a-f]{64}$'
    and model_id<>verifier_model_id
);

alter table models.capability_runtime_outcome drop column score_eligible;
alter table models.capability_runtime_outcome
    add column successful_episode_count integer not null default 21,
    add column contract_failed_episode_count integer not null default 0,
    add column skipped_slot_count integer not null default 0,
    add column score_eligible boolean generated always as
        (status='completed' and actual_retries=0) stored;
alter table models.capability_runtime_outcome
    drop constraint capability_runtime_outcome_shape;
alter table models.capability_runtime_outcome
    add constraint capability_runtime_outcome_shape check (
        status in ('completed','partial','recovery-required')
        and actual_provider_calls between 0 and 168 and actual_retries=0
        and successful_episode_count between 0 and 21
        and contract_failed_episode_count between 0 and 21
        and skipped_slot_count between 0 and 168
        and cardinality(call_evidence_digests)=actual_provider_calls
        and (
            (actual_provider_calls=0 and cardinality(call_evidence_digests)=0)
            or models.valid_routing_digest_array(call_evidence_digests)
        )
        and evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and (
            (status='completed'
             and successful_episode_count+contract_failed_episode_count=21
             and actual_provider_calls+skipped_slot_count=168)
            or (status='partial' and actual_provider_calls<168
                and successful_episode_count+contract_failed_episode_count<21)
            or status='recovery-required'
        )
    );

create function models.enforce_capability_runtime_skipped_slot() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models,security as $$
declare episode_record record; slot_record record; authorization_state text;
begin
    select manifest_id,model_id,task_digest,status,reason_code,attempted_calls
      into episode_record from models.capability_runtime_episode_outcome
     where realm_id=new.realm_id and id=new.episode_outcome_id;
    select manifest_id,model_id,task_digest,turn_number into slot_record
      from models.capability_runtime_approval_slot
     where realm_id=new.realm_id and id=new.slot_id;
    select a.state into authorization_state
      from models.capability_runtime_slot_authorization binding
      join security.authorization a on a.id=binding.authorization_id
     where binding.realm_id=new.realm_id and binding.slot_id=new.slot_id;
    if episode_record.manifest_id is distinct from new.manifest_id
       or slot_record.manifest_id is distinct from new.manifest_id
       or slot_record.model_id is distinct from episode_record.model_id
       or slot_record.task_digest is distinct from episode_record.task_digest
       or episode_record.status<>'model-contract-failed'
       or new.reason_code is distinct from episode_record.reason_code
       or slot_record.turn_number<=episode_record.attempted_calls
       or exists (select 1 from models.capability_runtime_call_outcome o
                   where o.realm_id=new.realm_id and o.slot_id=new.slot_id)
       or exists (select 1 from models.capability_runtime_continuity_state c
                   where c.realm_id=new.realm_id and c.slot_id=new.slot_id)
       or authorization_state is not null and authorization_state<>'revoked' then
        raise exception 'capability skipped slot is not sealed unused authority'
            using errcode='42501';
    end if;
    return new;
end
$$;

create function models.validate_capability_runtime_episode_outcome() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models,runtime as $$
declare episode record; observed_calls integer; observed_success integer;
        observed_failed integer; observed_recovery integer; observed_turns integer[];
        failed_turn integer; skipped_count integer; job_state text;
begin
    select * into episode from models.capability_runtime_episode_outcome
     where realm_id=new.realm_id and id=new.id;
    select count(*),count(*) filter(where o.status='completed'),
           count(*) filter(where o.status='failed'),
           count(*) filter(where o.status='recovery-required'),
           array_agg(s.turn_number order by s.turn_number),
           max(s.turn_number) filter(where o.status='failed')
      into observed_calls,observed_success,observed_failed,observed_recovery,
           observed_turns,failed_turn
      from models.capability_runtime_approval_slot s
      left join models.capability_runtime_call_outcome o
        on o.realm_id=s.realm_id and o.slot_id=s.id
     where s.realm_id=episode.realm_id and s.manifest_id=episode.manifest_id
       and s.model_id=episode.model_id and s.task_digest=episode.task_digest
       and o.id is not null;
    select count(*) into skipped_count
      from models.capability_runtime_skipped_slot skipped
     where skipped.realm_id=episode.realm_id
       and skipped.episode_outcome_id=episode.id;
    select state into job_state from runtime.job
     where realm_id=episode.realm_id and id=episode.job_id;
    if not exists (
        select 1 from models.capability_runtime_approval_slot s
         where s.realm_id=episode.realm_id and s.manifest_id=episode.manifest_id
           and s.model_id=episode.model_id and s.task_digest=episode.task_digest
           and s.job_id=episode.job_id
         group by s.job_id having count(*)=8
    ) or observed_calls<>episode.attempted_calls
       or observed_success<>episode.successful_calls
       or observed_turns is distinct from (
           select array_agg(value order by value)
             from generate_series(1,episode.attempted_calls) value
       ) or (episode.status='successful' and (
            observed_failed<>0 or observed_recovery<>0 or skipped_count<>0
            or job_state<>'completed'
       )) or (episode.status='model-contract-failed' and (
            observed_failed<>0 or observed_recovery<>0
            or observed_success<>episode.attempted_calls
            or skipped_count<>8-episode.attempted_calls
            or job_state<>'completed'
       )) or (episode.status='recovery-required' and (
            observed_recovery=0 and job_state<>'recovery-required'
       )) then
        raise exception 'capability episode terminal evidence mismatch' using errcode='42501';
    end if;
    return null;
end
$$;

create or replace function models.enforce_capability_runtime_call_outcome() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models,runtime,work as $$
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
            or receipt_record.status<>'completed'
            or receipt_record.result_digest is distinct from new.result_digest
            or checkpoint_record.result_digest is distinct from new.result_digest
            or job_state not in ('running','completed')
       )) or (new.status='failed' and (
            receipt_record.realm_id is distinct from new.realm_id
            or receipt_record.claim_id is distinct from new.claim_id
            or receipt_record.status<>'failed'
            or receipt_record.failure_category is distinct from new.failure_category
            or job_state not in ('failed','recovery-required')
       )) or (new.status='recovery-required' and job_state<>'recovery-required') then
        raise exception 'capability runtime claim/receipt/checkpoint/job binding mismatch'
            using errcode='42501';
    end if;
    return new;
end
$$;

create or replace function models.enforce_capability_runtime_outcome() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models,runtime,security as $$
declare observed_count integer; observed_digests text[]; recovery_count integer;
        recovery_job_count integer; episode_count integer; successful_episodes integer;
        contract_failed_episodes integer; episode_recovery integer; skipped_count integer;
        terminal_episode_jobs integer; coordinator_state text; claim_count integer;
        receipt_count integer; issued_authorizations integer; lease_count integer;
begin
    select count(*),array_agg(c.evidence_digest order by c.evidence_digest),
           count(*) filter(where c.status='recovery-required')
      into observed_count,observed_digests,recovery_count
      from models.capability_runtime_approval_slot s
      join models.capability_runtime_call_outcome c
        on c.realm_id=s.realm_id and c.slot_id=s.id
     where s.realm_id=new.realm_id and s.manifest_id=new.manifest_id;
    select count(*),count(*) filter(where status='successful'),
           count(*) filter(where status='model-contract-failed'),
           count(*) filter(where status='recovery-required')
      into episode_count,successful_episodes,contract_failed_episodes,episode_recovery
      from models.capability_runtime_episode_outcome
     where realm_id=new.realm_id and manifest_id=new.manifest_id;
    select count(*) into skipped_count from models.capability_runtime_skipped_slot
     where realm_id=new.realm_id and manifest_id=new.manifest_id;
    select count(*) filter(where j.state='recovery-required'),
           count(distinct j.id) filter(where j.state='completed')
      into recovery_job_count,terminal_episode_jobs
      from models.capability_runtime_approval_slot s
      join runtime.job j on j.realm_id=s.realm_id and j.id=s.job_id
     where s.realm_id=new.realm_id and s.manifest_id=new.manifest_id;
    select j.state into coordinator_state
      from models.capability_runtime_approval_manifest m
      join runtime.job j on j.realm_id=m.realm_id and j.id=m.coordinator_job_id
     where m.realm_id=new.realm_id and m.id=new.manifest_id;
    select count(distinct claim.id),count(distinct receipt.id)
      into claim_count,receipt_count
      from models.capability_runtime_approval_slot s
      join runtime.effect_claim claim on claim.realm_id=s.realm_id and claim.job_id=s.job_id
      left join runtime.effect_receipt receipt
        on receipt.realm_id=claim.realm_id and receipt.claim_id=claim.id
     where s.realm_id=new.realm_id and s.manifest_id=new.manifest_id
       and claim.operation like 'provider-contract:%';
    select count(*) into issued_authorizations
      from models.capability_runtime_approval_slot s
      join models.capability_runtime_slot_authorization binding
        on binding.realm_id=s.realm_id and binding.slot_id=s.id
      join security.authorization auth_record on auth_record.id=binding.authorization_id
     where s.realm_id=new.realm_id and s.manifest_id=new.manifest_id
       and auth_record.state='issued';
    select count(*) into lease_count
      from runtime.lease lease
     where lease.realm_id=new.realm_id and lease.job_id in (
        select s.job_id from models.capability_runtime_approval_slot s
         where s.realm_id=new.realm_id and s.manifest_id=new.manifest_id
        union
        select m.coordinator_job_id from models.capability_runtime_approval_manifest m
         where m.realm_id=new.realm_id and m.id=new.manifest_id
     );
    if observed_count<>new.actual_provider_calls
       or observed_digests is distinct from (
           select array_agg(value order by value) from unnest(new.call_evidence_digests) value
       ) or (new.status='completed' and (
            episode_count<>21 or successful_episodes<>new.successful_episode_count
            or contract_failed_episodes<>new.contract_failed_episode_count
            or successful_episodes+contract_failed_episodes<>21 or episode_recovery<>0
            or skipped_count<>new.skipped_slot_count
            or observed_count+skipped_count<>168
            or recovery_count<>0 or recovery_job_count<>0
            or terminal_episode_jobs<>21 or coordinator_state<>'completed'
            or claim_count<>observed_count or receipt_count<>observed_count
            or issued_authorizations<>0 or lease_count<>0
       )) or (new.status='partial' and (
            observed_count>=168 or recovery_count<>0 or recovery_job_count<>0
       )) or (new.status='recovery-required' and (
            recovery_count=0 and recovery_job_count=0 and episode_recovery=0
       )) then
        raise exception 'capability runtime aggregate count/status/evidence mismatch'
            using errcode='42501';
    end if;
    return new;
end
$$;

create or replace function models.enforce_capability_runtime_scorecard_gate() returns trigger
language plpgsql security invoker set search_path=pg_catalog,models as $$
declare has_runtime_manifest boolean; runtime_episode_count integer;
        benchmark_episode_count integer;
begin
    select exists (
        select 1 from models.capability_runtime_approval_manifest m
         where m.realm_id=new.realm_id and m.cohort_id=new.cohort_id
    ) into has_runtime_manifest;
    if has_runtime_manifest and not exists (
        select 1 from models.capability_runtime_approval_manifest m
        join models.capability_runtime_outcome o
          on o.realm_id=m.realm_id and o.manifest_id=m.id
         where m.realm_id=new.realm_id and m.cohort_id=new.cohort_id
           and o.score_eligible
    ) then
        raise exception 'partial/recovery capability runtime cannot produce score or routing evidence'
            using errcode='42501';
    end if;
    select count(*) into runtime_episode_count
      from models.capability_runtime_approval_manifest m
      join models.capability_runtime_episode_outcome terminal
        on terminal.realm_id=m.realm_id and terminal.manifest_id=m.id
     where m.realm_id=new.realm_id and m.cohort_id=new.cohort_id
       and terminal.model_id=new.model_id;
    select count(*) into benchmark_episode_count
      from models.capability_benchmark_episode episode
     where episode.realm_id=new.realm_id and episode.cohort_id=new.cohort_id
       and episode.model_id=new.model_id;
    if has_runtime_manifest and (
        runtime_episode_count<>3 or benchmark_episode_count<>3
        or exists (
            select 1 from models.capability_runtime_approval_manifest m
            join models.capability_runtime_episode_outcome terminal
              on terminal.realm_id=m.realm_id and terminal.manifest_id=m.id
            left join models.capability_benchmark_episode episode
              on episode.realm_id=m.realm_id and episode.cohort_id=m.cohort_id
             and episode.model_id=terminal.model_id
             and episode.task_digest=terminal.task_digest
           where m.realm_id=new.realm_id and m.cohort_id=new.cohort_id
             and terminal.model_id=new.model_id and episode.id is null
        ) or exists (
            select 1 from models.capability_benchmark_episode episode
            left join models.capability_runtime_approval_manifest m
              on m.realm_id=episode.realm_id and m.cohort_id=episode.cohort_id
            left join models.capability_runtime_episode_outcome terminal
              on terminal.realm_id=m.realm_id and terminal.manifest_id=m.id
             and terminal.model_id=episode.model_id
             and terminal.task_digest=episode.task_digest
           where episode.realm_id=new.realm_id and episode.cohort_id=new.cohort_id
             and episode.model_id=new.model_id and terminal.id is null
        )
    ) then
        raise exception 'capability scorecard requires exact terminal episode correspondence'
            using errcode='42501';
    end if;
    if exists (
        select 1 from models.capability_runtime_approval_manifest m
        join models.capability_runtime_episode_outcome terminal
          on terminal.realm_id=m.realm_id and terminal.manifest_id=m.id
        join models.capability_benchmark_episode episode
          on episode.realm_id=m.realm_id and episode.cohort_id=m.cohort_id
         and episode.model_id=terminal.model_id and episode.task_digest=terminal.task_digest
       where m.realm_id=new.realm_id and m.cohort_id=new.cohort_id
         and terminal.model_id=new.model_id and (
            (terminal.status='model-contract-failed' and (
                episode.status<>'not-comparable' or greatest(
                    episode.correctness,episode.completion,episode.sustained_progress,
                    episode.context_retention,episode.self_correction,
                    episode.tool_efficiency,episode.safety,
                    episode.hidden_acceptance_ratio,episode.sustained_progress_auc
                )<>0 or episode.noop_ratio<>1
            )) or (terminal.status<>'model-contract-failed'
                    and episode.status='not-comparable')
         )
    ) then
        raise exception 'model-contract failed task metrics must be zero' using errcode='42501';
    end if;
    return new;
end
$$;

create trigger capability_runtime_skipped_slot_binding before insert
    on models.capability_runtime_skipped_slot for each row
    execute function models.enforce_capability_runtime_skipped_slot();
create constraint trigger capability_runtime_episode_terminal_evidence
    after insert on models.capability_runtime_episode_outcome deferrable initially deferred
    for each row execute function models.validate_capability_runtime_episode_outcome();

do $$ declare target text; begin
    foreach target in array array[
        'models.capability_runtime_episode_outcome',
        'models.capability_runtime_skipped_slot'
    ] loop
        execute format('alter table %s enable row level security',target);
        execute format('alter table %s force row level security',target);
        execute format('create policy scope_select on %s for select using (realm_id=core.current_realm_id())',target);
        execute format('create policy scope_insert on %s for insert with check (realm_id=core.current_realm_id())',target);
        execute format('create trigger deny_update before update on %s for each statement execute function core.deny_mutation()',target);
        execute format('create trigger deny_delete before delete on %s for each statement execute function core.deny_mutation()',target);
    end loop;
end $$;

grant select,insert on models.capability_runtime_episode_outcome,
    models.capability_runtime_skipped_slot to zekam_app;
grant execute on function models.enforce_capability_runtime_skipped_slot() to zekam_app;
grant execute on function models.validate_capability_runtime_episode_outcome() to zekam_app;
