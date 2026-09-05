do $migration$
declare
  definition_ text;
  restored_ text;
  replacements_ text[][] := array[
    array[
      $replacement$and (case when plan.source_revision
        ~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'
        then substring(plan.source_revision from 5 for 40)
        else plan.source_revision end)=source_head_$replacement$,
      'and plan.source_revision=source_head_'
    ],
    array[
      $replacement$and (case when run.source_revision
        ~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'
        then substring(run.source_revision from 5 for 40)
        else run.source_revision end)=source_head_
    and run.policy_digest=plan.policy_digest$replacement$,
      'and run.source_revision=source_head_ and run.policy_digest=plan.policy_digest'
    ],
    array[
      $replacement$and (case when envelope.source_revision
        ~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'
        then substring(envelope.source_revision from 5 for 40)
        else envelope.source_revision end)=source_head_
    and envelope.policy_digest=plan.policy_digest$replacement$,
      'and envelope.source_revision=source_head_ and envelope.policy_digest=plan.policy_digest'
    ],
    array[
      $replacement$and exists(select 1 from client.codex_lifecycle_admission lifecycle_admission
      join runtime.execution_envelope lifecycle_envelope
        on lifecycle_envelope.realm_id=lifecycle_admission.realm_id
        and lifecycle_envelope.id=lifecycle_admission.envelope_id
      join security.authorization lifecycle_authorization
        on lifecycle_authorization.realm_id=lifecycle_admission.realm_id
        and lifecycle_authorization.id=lifecycle_admission.authorization_id
      join runtime.effect_claim lifecycle_claim
        on lifecycle_claim.realm_id=lifecycle_admission.realm_id
        and lifecycle_claim.id=lifecycle_admission.claim_id
      join runtime.effect_receipt lifecycle_effect
        on lifecycle_effect.realm_id=lifecycle_admission.realm_id
        and lifecycle_effect.id=lifecycle_admission.effect_receipt_id
      where lifecycle_admission.realm_id=realm_id_
        and lifecycle_admission.continuity_event_id=event.id
        and lifecycle_admission.delivery_outbox_id=outbox.id
        and lifecycle_admission.job_id<>job.id
        and lifecycle_admission.work_plan_digest=plan.plan_digest
        and lifecycle_admission.effect_plan_digest=outbox.plan_digest
        and lifecycle_admission.policy_digest=plan.policy_digest
        and lifecycle_admission.envelope_digest=lifecycle_envelope.envelope_digest
        and ((lifecycle_envelope.checkpoint_disposition='not-applicable-genesis'
          and event.event_body->'checkpoint_ref'='null'::jsonb)
          or (lifecycle_envelope.checkpoint_disposition='bound'
            and event.event_body->>'checkpoint_ref'
              ='checkpoint:'||lifecycle_envelope.checkpoint_id::text
            and exists(select 1 from work.checkpoint lifecycle_checkpoint
              where lifecycle_checkpoint.realm_id=lifecycle_envelope.realm_id
                and lifecycle_checkpoint.id=lifecycle_envelope.checkpoint_id
                and lifecycle_checkpoint.checkpoint_digest
                  =lifecycle_envelope.checkpoint_digest))
          or (lifecycle_envelope.checkpoint_disposition='bound-v2'
            and event.event_body->>'checkpoint_ref'
              ='checkpoint-v2:'||lifecycle_envelope.checkpoint_v2_id::text
            and exists(select 1 from work.checkpoint_v2 lifecycle_checkpoint_v2
              where lifecycle_checkpoint_v2.realm_id=lifecycle_envelope.realm_id
                and lifecycle_checkpoint_v2.id=lifecycle_envelope.checkpoint_v2_id
                and lifecycle_checkpoint_v2.checkpoint_digest
                  =lifecycle_envelope.checkpoint_v2_digest)))
        and lifecycle_authorization.state='consumed'
        and lifecycle_authorization.consumed_by='client-lifecycle-bridge/v1'
        and lifecycle_authorization.plan_digest=lifecycle_admission.effect_plan_digest
        and lifecycle_claim.authorization_id=lifecycle_authorization.id
        and lifecycle_claim.job_id=lifecycle_admission.job_id
        and lifecycle_effect.claim_id=lifecycle_claim.id
        and lifecycle_effect.status='completed')$replacement$,
      $baseline$and event.event_body->>'checkpoint_ref'=close_receipt.receipt_body->'checkpoint_ref'->>'ref'$baseline$
    ],
    array[
      $replacement$and (case when event.event_body->>'source_revision'
        ~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'
        then substring(event.event_body->>'source_revision' from 5 for 40)
        else event.event_body->>'source_revision' end)=source_head_$replacement$,
      $baseline$and event.event_body->>'source_revision'=source_head_$baseline$
    ],
    array[
      'and lifecycle_auth.scope->''data_classifications''=''["internal"]''::jsonb',
      'and lifecycle_auth.scope->''data_classifications''=''[]''::jsonb'
    ],
    array[
      $replacement$and ((hydration_event.event_type='pre_close'
        and hydration_event.id=event.id and hydration_event.sequence=event.sequence)
      or (hydration_event.event_type<>'pre_close'
        and hydration_event.sequence<event.sequence))$replacement$,
      'and hydration_event.sequence<event.sequence'
    ]
  ];
  replacement_ text[];
begin
  if exists(
      select 1 from runtime.job job
      where job.step_id='projection-aware-close'
        and job.state in ('ready','running','blocked','recovery-required')
    ) or exists(
      select 1 from runtime.lease lease
      join runtime.job job on job.realm_id=lease.realm_id and job.id=lease.job_id
      where job.step_id='projection-aware-close'
    ) or exists(
      select 1 from runtime.effect_claim claim
      join runtime.job job on job.realm_id=claim.realm_id and job.id=claim.job_id
      left join runtime.effect_receipt receipt
        on receipt.realm_id=claim.realm_id and receipt.claim_id=claim.id
      where job.step_id='projection-aware-close' and receipt.id is null
    ) or exists(
      select 1 from continuity.session_lifecycle_event event
      join continuity.lifecycle_delivery_outbox outbox
        on outbox.realm_id=event.realm_id and outbox.event_id=event.id
      where event.event_type='pre_close'
        and outbox.state in ('pending','processing')
    ) then
    raise exception '0074 rollback refused: projection close runtime is still open'
      using errcode='55000';
  end if;

  execute 'alter table work.completion_admission no force row level security';
  if exists(select 1 from work.completion_admission where mode='projection-aware') then
    raise exception '0074 rollback refused: projection completion audit data exists'
      using errcode='55000';
  end if;
  execute 'alter table work.completion_admission force row level security';

  execute 'alter table continuity.lifecycle_hydration_admission no force row level security';
  if exists(select 1 from continuity.lifecycle_hydration_admission admission
      join continuity.session_lifecycle_event event
        on event.realm_id=admission.realm_id and event.id=admission.continuity_event_id
      where event.event_type='pre_close') then
    raise exception '0074 rollback refused: pre-close hydration audit data exists'
      using errcode='55000';
  end if;
  execute 'alter table continuity.lifecycle_hydration_admission force row level security';
  select pg_get_functiondef(
    'work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,'
    'uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text)'::regprocedure)
    into strict definition_;
  restored_:=definition_;
  foreach replacement_ slice 1 in array replacements_ loop
    if (length(restored_)-length(replace(restored_,replacement_[1],'')))
          /length(replacement_[1])<>1 then
      raise exception '0074 rollback refused: projection close runtime drift'
        using errcode='55000';
    end if;
    restored_:=replace(restored_,replacement_[1],replacement_[2]);
  end loop;
  execute restored_;
end $migration$;

do $projection_hydration_rollback$
declare
  definition_ text;
  restored_ text;
  replacements_ text[][] := array[
    array[
      'hydration_event.event_type in (''session_start'',''pre_close'',''hydration_required'')',
      'hydration_event.event_type in (''session_start'',''hydration_required'')'
    ],
    array[
      $replacement$or (hydration_event.event_type in ('session_start','pre_close')
            and hydration_event.event_body->'checkpoint_ref'='null'::jsonb$replacement$,
      $baseline$or (hydration_event.event_type='session_start'
            and hydration_event.event_body->'checkpoint_ref'='null'::jsonb$baseline$
    ],
    array[
      $replacement$or (hydration_event.event_type='session_start'
          and exists(select 1 from continuity.lifecycle_hydration_admission admission
            where admission.realm_id=hydration_event.realm_id
              and admission.continuity_event_id=hydration_event.id
              and admission.delivery_outbox_id=hydration_outbox.id
              and admission.hydration_receipt_id=hydration.id
              and admission.hydration_applied_at=hydration_outbox.completed_at
              and admission.created_at>=hydration.created_at))
        or (hydration_event.event_type='pre_close'
          and hydration_event.id=event.id
          and exists(select 1 from continuity.lifecycle_hydration_admission admission
            where admission.realm_id=hydration_event.realm_id
              and admission.continuity_event_id=hydration_event.id
              and admission.delivery_outbox_id=hydration_outbox.id
              and admission.hydration_receipt_id=hydration.id
              and hydration.created_at<=admission.created_at
              and admission.hydration_applied_at=admission.created_at
              and admission.created_at<=hydration_outbox.completed_at)))$replacement$,
      $baseline$or (hydration_event.event_type='session_start'
          and exists(select 1 from continuity.lifecycle_hydration_admission admission
            where admission.realm_id=hydration_event.realm_id
              and admission.continuity_event_id=hydration_event.id
              and admission.delivery_outbox_id=hydration_outbox.id
              and admission.hydration_receipt_id=hydration.id
              and admission.hydration_applied_at=hydration_outbox.completed_at
              and admission.created_at>=hydration.created_at)))$baseline$
    ]
  ];
  replacement_ text[];
begin
  select pg_get_functiondef(
    'work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,'
    'uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text)'::regprocedure)
    into strict definition_;
  restored_:=definition_;
  foreach replacement_ slice 1 in array replacements_ loop
    if (length(restored_)-length(replace(restored_,replacement_[1],'')))
          /length(replacement_[1])<>1 then
      raise exception '0074 rollback refused: projection hydration runtime drift'
        using errcode='55000';
    end if;
    restored_:=replace(restored_,replacement_[1],replacement_[2]);
  end loop;
  execute restored_;
end $projection_hydration_rollback$;

do $hydration_rollback$
declare
  definition_ text;
  restored_ text;
begin
  select pg_get_functiondef('continuity.enforce_lifecycle_hydration_admission()'::regprocedure)
    into strict definition_;
  restored_:=replace(
    definition_,
    'event.realm_id=realm_ and event.id=event_id_ and event.event_type in (''session_start'',''pre_close'')',
    'event.realm_id=realm_ and event.id=event_id_ and event.event_type=''session_start''');
  restored_:=replace(
    restored_,
    'and event.id=event_id_ and event.event_type in (''session_start'',''pre_close'')',
    'and event.id=event_id_ and event.event_type=''session_start''');
  restored_:=replace(
    restored_,
    $replacement$and outbox.event_id=event.id
    and ((event.event_type='session_start' and outbox.state='completed')
      or (event.event_type='pre_close' and outbox.state in ('pending','processing')
        and outbox.terminal_receipt_digest is null and outbox.completed_at is null))$replacement$,
    'and outbox.event_id=event.id and outbox.state=''completed''');
  if restored_=definition_ then
    raise exception '0074 rollback refused: lifecycle hydration runtime drift'
      using errcode='55000';
  end if;
  execute restored_;

  select pg_get_functiondef('client.enforce_codex_lifecycle_admission()'::regprocedure)
    into strict definition_;
  restored_:=replace(
    definition_,
    'continuity_event.event_type in (''session_start'',''pre_close'')',
    'continuity_event.event_type=''session_start''');
  restored_:=replace(
    restored_,
    'continuity_event.event_type not in (''session_start'',''pre_close'')',
    'continuity_event.event_type<>''session_start''');
  if restored_=definition_ then
    raise exception '0074 rollback refused: Codex hydration payload drift'
      using errcode='55000';
  end if;
  execute restored_;
end $hydration_rollback$;

do $close_checkpoint_rollback$
declare
  definition_ text;
  restored_ text;
begin
  select pg_get_functiondef('work.require_meaningful_job_checkpoint()'::regprocedure)
    into strict definition_;
  if (length(definition_)-length(replace(
        definition_,
        'new.step_id in (''apply-atomic-close'',''projection-aware-close'')',
        '')))
      /length('new.step_id in (''apply-atomic-close'',''projection-aware-close'')')<>1 then
    raise exception '0074 rollback refused: projection close checkpoint runtime drift'
      using errcode='55000';
  end if;
  restored_:=replace(
    definition_,
    'new.step_id in (''apply-atomic-close'',''projection-aware-close'')',
    'new.step_id=''apply-atomic-close''');
  execute restored_;
end $close_checkpoint_rollback$;

comment on function work.admit_projection_completion(
  uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text
) is '0063 executable projection close and pre-effect checkpoint binding';
