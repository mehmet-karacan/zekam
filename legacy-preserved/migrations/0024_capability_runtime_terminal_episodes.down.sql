drop trigger if exists capability_runtime_episode_terminal_evidence
    on models.capability_runtime_episode_outcome;
drop trigger if exists capability_runtime_skipped_slot_binding
    on models.capability_runtime_skipped_slot;
drop function if exists models.validate_capability_runtime_episode_outcome();
drop function if exists models.enforce_capability_runtime_skipped_slot();
drop table if exists models.capability_runtime_skipped_slot;
drop table if exists models.capability_runtime_episode_outcome;

-- 0023 application/domain contracts already allow a zero-checkpoint episode. Keep the
-- exact zero/empty compatibility case during rollback so immutable NOT_COMPARABLE
-- evidence is never rewritten into a fabricated checkpoint receipt.
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

alter table models.capability_runtime_outcome drop constraint capability_runtime_outcome_shape;
alter table models.capability_runtime_outcome drop column score_eligible;
alter table models.capability_runtime_outcome
    drop column successful_episode_count,
    drop column contract_failed_episode_count,
    drop column skipped_slot_count,
    add column score_eligible boolean generated always as
        (status='completed' and actual_provider_calls=168 and actual_retries=0) stored;
alter table models.capability_runtime_outcome
    add constraint capability_runtime_outcome_shape check (
        status in ('completed','partial','recovery-required')
        and actual_provider_calls between 0 and 168 and actual_retries=0
        and cardinality(call_evidence_digests)=actual_provider_calls
        and (
            (actual_provider_calls=0 and cardinality(call_evidence_digests)=0)
            or models.valid_routing_digest_array(call_evidence_digests)
        )
        and evidence_digest ~ '^sha256:[0-9a-f]{64}$'
        and (
            -- Existing 0024 terminal calibration rows may have fewer than 168 attempts.
            -- The restored 0023 trigger still rejects any new completed row below 168;
            -- this CHECK compatibility residue only preserves immutable prior evidence.
            (status='completed' and actual_provider_calls<=168)
            or (status='partial' and actual_provider_calls<168)
            or (status='recovery-required' and actual_provider_calls<=168)
        )
    );

create or replace function models.enforce_capability_runtime_call_outcome() returns trigger
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

create or replace function models.enforce_capability_runtime_outcome() returns trigger
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

create or replace function models.enforce_capability_runtime_scorecard_gate() returns trigger
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
