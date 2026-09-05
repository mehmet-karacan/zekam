-- Bind staged Codex pre-close to its immutable lifecycle admission while the
-- separate close job owns the terminal checkpoint.  Dirty-aware runtime source
-- revisions are compared to the canonical source HEAD without weakening either
-- digest chain.

do $migration$
declare
  definition_ text;
  revised_ text;
  replacements_ text[][] := array[
    array[
      'and plan.source_revision=source_head_',
      $replacement$and (case when plan.source_revision
        ~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'
        then substring(plan.source_revision from 5 for 40)
        else plan.source_revision end)=source_head_$replacement$
    ],
    array[
      'and run.source_revision=source_head_ and run.policy_digest=plan.policy_digest',
      $replacement$and (case when run.source_revision
        ~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'
        then substring(run.source_revision from 5 for 40)
        else run.source_revision end)=source_head_
    and run.policy_digest=plan.policy_digest$replacement$
    ],
    array[
      'and envelope.source_revision=source_head_ and envelope.policy_digest=plan.policy_digest',
      $replacement$and (case when envelope.source_revision
        ~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'
        then substring(envelope.source_revision from 5 for 40)
        else envelope.source_revision end)=source_head_
    and envelope.policy_digest=plan.policy_digest$replacement$
    ],
    array[
      $baseline$and event.event_body->>'checkpoint_ref'=close_receipt.receipt_body->'checkpoint_ref'->>'ref'$baseline$,
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
        and lifecycle_effect.status='completed')$replacement$
    ],
    array[
      $baseline$and event.event_body->>'source_revision'=source_head_$baseline$,
      $replacement$and (case when event.event_body->>'source_revision'
        ~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'
        then substring(event.event_body->>'source_revision' from 5 for 40)
        else event.event_body->>'source_revision' end)=source_head_$replacement$
    ],
    array[
      'and lifecycle_auth.scope->''data_classifications''=''[]''::jsonb',
      'and lifecycle_auth.scope->''data_classifications''=''["internal"]''::jsonb'
    ],
    array[
      'and hydration_event.sequence<event.sequence',
      $replacement$and ((hydration_event.event_type='pre_close'
        and hydration_event.id=event.id and hydration_event.sequence=event.sequence)
      or (hydration_event.event_type<>'pre_close'
        and hydration_event.sequence<event.sequence))$replacement$
    ]
  ];
  replacement_ text[];
begin
  select pg_get_functiondef(
    'work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,'
    'uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text)'::regprocedure)
    into strict definition_;
  revised_:=definition_;
  foreach replacement_ slice 1 in array replacements_ loop
    if (length(revised_)-length(replace(revised_,replacement_[1],'')))
          /length(replacement_[1])<>1
        or position(replacement_[2] in revised_)>0 then
      raise exception '0074 refused: projection close baseline drift'
        using errcode='55000';
    end if;
    revised_:=replace(revised_,replacement_[1],replacement_[2]);
  end loop;
  if revised_=definition_ then
    raise exception '0074 refused: projection close rewrite empty'
      using errcode='55000';
  end if;
  execute revised_;
end $migration$;

-- A pre-close re-bootstrap owns a new run, so it must carry a fresh hydration
-- receipt for that same run before the separate close effect can be released.
-- Reuse the immutable pre_close lifecycle event; do not introduce another
-- event, store or loop.
do $hydration_completion$
declare
  definition_ text;
  revised_ text;
  replacements_ text[][] := array[
    array[
      'hydration_event.event_type in (''session_start'',''hydration_required'')',
      'hydration_event.event_type in (''session_start'',''pre_close'',''hydration_required'')'
    ],
    array[
      $baseline$or (hydration_event.event_type='session_start'
            and hydration_event.event_body->'checkpoint_ref'='null'::jsonb$baseline$,
      $replacement$or (hydration_event.event_type in ('session_start','pre_close')
            and hydration_event.event_body->'checkpoint_ref'='null'::jsonb$replacement$
    ],
    array[
      $baseline$or (hydration_event.event_type='session_start'
          and exists(select 1 from continuity.lifecycle_hydration_admission admission
            where admission.realm_id=hydration_event.realm_id
              and admission.continuity_event_id=hydration_event.id
              and admission.delivery_outbox_id=hydration_outbox.id
              and admission.hydration_receipt_id=hydration.id
              and admission.hydration_applied_at=hydration_outbox.completed_at
              and admission.created_at>=hydration.created_at)))$baseline$,
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
              and admission.created_at<=hydration_outbox.completed_at)))$replacement$
    ]
  ];
  replacement_ text[];
begin
  select pg_get_functiondef(
    'work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,'
    'uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text)'::regprocedure)
    into strict definition_;
  revised_:=definition_;
  foreach replacement_ slice 1 in array replacements_ loop
    if (length(revised_)-length(replace(revised_,replacement_[1],'')))
          /length(replacement_[1])<>1
        or position(replacement_[2] in revised_)>0 then
      raise exception '0074 refused: projection hydration baseline drift'
        using errcode='55000';
    end if;
    revised_:=replace(revised_,replacement_[1],replacement_[2]);
  end loop;
  execute revised_;
end $hydration_completion$;

do $hydration_admission$
declare
  definition_ text;
  revised_ text;
  replacements_ text[][] := array[
    array[
      'event.realm_id=realm_ and event.id=event_id_ and event.event_type=''session_start''',
      'event.realm_id=realm_ and event.id=event_id_ and event.event_type in (''session_start'',''pre_close'')'
    ],
    array[
      'and event.id=event_id_ and event.event_type=''session_start''',
      'and event.id=event_id_ and event.event_type in (''session_start'',''pre_close'')'
    ],
    array[
      'and outbox.event_id=event.id and outbox.state=''completed''',
      $replacement$and outbox.event_id=event.id
    and ((event.event_type='session_start' and outbox.state='completed')
      or (event.event_type='pre_close' and outbox.state in ('pending','processing')
        and outbox.terminal_receipt_digest is null and outbox.completed_at is null))$replacement$
    ]
  ];
  replacement_ text[];
begin
  select pg_get_functiondef(
    'continuity.enforce_lifecycle_hydration_admission()'::regprocedure)
    into strict definition_;
  revised_:=definition_;
  foreach replacement_ slice 1 in array replacements_ loop
    if (length(revised_)-length(replace(revised_,replacement_[1],'')))
          /length(replacement_[1])<>1 then
      raise exception '0074 refused: lifecycle hydration admission baseline drift'
        using errcode='55000';
    end if;
    revised_:=replace(revised_,replacement_[1],replacement_[2]);
  end loop;
  execute revised_;
end $hydration_admission$;

do $codex_hydration_payload$
declare
  definition_ text;
  revised_ text;
begin
  select pg_get_functiondef('client.enforce_codex_lifecycle_admission()'::regprocedure)
    into strict definition_;
  if (length(definition_)-length(replace(
        definition_, 'continuity_event.event_type=''session_start''', '')))
      /length('continuity_event.event_type=''session_start''')<>1
      or (length(definition_)-length(replace(
        definition_, 'continuity_event.event_type<>''session_start''', '')))
      /length('continuity_event.event_type<>''session_start''')<>1 then
    raise exception '0074 refused: Codex hydration payload baseline drift'
      using errcode='55000';
  end if;
  revised_:=replace(
    definition_,
    'continuity_event.event_type=''session_start''',
    'continuity_event.event_type in (''session_start'',''pre_close'')');
  revised_:=replace(
    revised_,
    'continuity_event.event_type<>''session_start''',
    'continuity_event.event_type not in (''session_start'',''pre_close'')');
  execute revised_;
end $codex_hydration_payload$;

do $close_checkpoint_compat$
declare
  definition_ text;
  revised_ text;
begin
  select pg_get_functiondef('work.require_meaningful_job_checkpoint()'::regprocedure)
    into strict definition_;
  if (length(definition_)-length(replace(
        definition_, 'new.step_id=''apply-atomic-close''', '')))
      /length('new.step_id=''apply-atomic-close''')<>1
      or position(
        'new.step_id in (''apply-atomic-close'',''projection-aware-close'')'
        in definition_
      )>0 then
    raise exception '0074 refused: projection close checkpoint compatibility drift'
      using errcode='55000';
  end if;
  revised_:=replace(
    definition_,
    'new.step_id=''apply-atomic-close''',
    'new.step_id in (''apply-atomic-close'',''projection-aware-close'')');
  execute revised_;
end $close_checkpoint_compat$;

comment on function work.admit_projection_completion(
  uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text
) is '0074 dirty-aware staged lifecycle, same-run pre-close hydration and separate close checkpoint binding';
