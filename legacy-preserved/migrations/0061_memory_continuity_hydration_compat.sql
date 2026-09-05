-- Align projection-aware close and Codex lifecycle admission with canonical hydration.
-- 0057/0058 remain immutable history; this migration revises only their live functions.

do $migration$
declare
  definition_ text;
  revised_ text;
  part_index_ integer;
  baseline_parts_ text[]:=array[
    'hydration.receipt_body->>''source_digest''=current_projection_source_digest_',
    'freshness_dimension->>''observed_digest''=current_projection_source_digest_',
    'freshness_dimension->>''expected_digest''=current_projection_source_digest_',
    'projection_ref->>''digest''=prior_projection.receipt_digest',
    $event$and hydration_event.event_type='hydration_required'
        and hydration_event.event_digest=hydration.receipt_body->>'hydration_event_digest'$event$,
    $checkpoint$and hydration_event.event_body->>'checkpoint_ref'
          =hydration.receipt_body->>'checkpoint_ref'$checkpoint$,
    $completion$and hydration_event.ingested_at<=hydration.created_at
        and hydration_outbox.completed_at<=hydration.created_at$completion$,
    $receipt_checkpoint$and hydration.receipt_body->>'checkpoint_ref' in
      (checkpoint.checkpoint_key,'db:work.checkpoint/'||checkpoint.id::text)$receipt_checkpoint$
  ];
  revised_parts_ text[]:=array[
    'hydration.receipt_body->>''source_digest''=source_tree_digest_',
    'freshness_dimension->>''observed_digest''=source_tree_digest_',
    'freshness_dimension->>''expected_digest''=source_tree_digest_',
    'projection_ref->>''digest''=prior_projection.projection_digest',
    $event$and hydration_event.event_type in ('session_start','hydration_required')
        and hydration_event.event_digest=hydration.receipt_body->>'hydration_event_digest'$event$,
    $checkpoint$and ((hydration_event.event_type='hydration_required'
          and hydration_event.event_body->>'checkpoint_ref'
            =hydration.receipt_body->>'checkpoint_ref'
          and hydration.receipt_body->>'checkpoint_ref' in
            (checkpoint.checkpoint_key,'db:work.checkpoint/'||checkpoint.id::text))
          or (hydration_event.event_type='session_start'
            and hydration_event.event_body->'checkpoint_ref'='null'::jsonb
            and hydration.receipt_body->>'checkpoint_ref'=
              'run:'||run.id::text||':genesis'))$checkpoint$,
    $completion$and ((hydration_event.event_type='hydration_required'
          and hydration_event.ingested_at<=hydration.created_at
          and hydration_outbox.completed_at<=hydration.created_at)
        or (hydration_event.event_type='session_start'
          and exists(select 1 from continuity.lifecycle_hydration_admission admission
            where admission.realm_id=hydration_event.realm_id
              and admission.continuity_event_id=hydration_event.id
              and admission.delivery_outbox_id=hydration_outbox.id
              and admission.hydration_receipt_id=hydration.id
              and admission.hydration_applied_at=hydration_outbox.completed_at
              and admission.created_at>=hydration.created_at)))$completion$,
    $receipt_checkpoint$and hydration.receipt_body->>'checkpoint_ref' in
      (checkpoint.checkpoint_key,'db:work.checkpoint/'||checkpoint.id::text,
       'run:'||run.id::text||':genesis')$receipt_checkpoint$
  ];
begin
  if to_regclass('continuity.lifecycle_hydration_admission') is null then
    raise exception '0061 requires lifecycle hydration admission migration 0060'
      using errcode='55000';
  end if;
  select pg_get_functiondef(
    'work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,'
    'uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text)'::regprocedure)
    into strict definition_;
  if cardinality(baseline_parts_)<>cardinality(revised_parts_) then
    raise exception '0061 refused: projection compatibility map is incomplete'
      using errcode='55000';
  end if;
  for part_index_ in 1..cardinality(baseline_parts_) loop
    if (length(definition_)-length(replace(definition_,baseline_parts_[part_index_],'')))
          /length(baseline_parts_[part_index_])<>1
        or position(revised_parts_[part_index_] in definition_)>0 then
      raise exception '0061 refused: 0057 projection completion baseline drift at part %',
        part_index_ using errcode='55000';
    end if;
  end loop;

  revised_:=definition_;
  for part_index_ in 1..cardinality(baseline_parts_) loop
    revised_:=replace(
      revised_,baseline_parts_[part_index_],revised_parts_[part_index_]);
  end loop;
  if revised_=definition_ then
    raise exception '0061 refused: projection completion compatibility rewrite was empty'
      using errcode='55000';
  end if;
  for part_index_ in 1..cardinality(baseline_parts_) loop
    if position(baseline_parts_[part_index_] in revised_)>0
        or (length(revised_)-length(replace(revised_,revised_parts_[part_index_],'')))
          /length(revised_parts_[part_index_])<>1 then
      raise exception '0061 refused: projection compatibility rewrite incomplete at part %',
        part_index_ using errcode='55000';
    end if;
  end loop;
  execute revised_;
end $migration$;

do $codex_migration$
declare
  definition_ text;
  revised_ text;
  baseline_ text:='(select count(*) from jsonb_object_keys(job.payload))=2';
  payload_guard_ text:=$payload_guard$(
        (continuity_event.event_type='session_start'
          and (select count(*) from jsonb_object_keys(job.payload))=3
          and job.payload ? 'hydration_authorization_id'
          and exists(select 1
            from continuity.lifecycle_hydration_admission hydration_admission
            where hydration_admission.realm_id=admission.realm_id
              and hydration_admission.codex_admission_id=admission.id
              and hydration_admission.continuity_event_id=continuity_event.id
              and hydration_admission.delivery_outbox_id=outbox.id
              and hydration_admission.hydration_authorization_id::text
                =job.payload->>'hydration_authorization_id'))
        or (continuity_event.event_type<>'session_start'
          and (select count(*) from jsonb_object_keys(job.payload))=2
          and not (job.payload ? 'hydration_authorization_id')))
      $payload_guard$;
begin
  select pg_get_functiondef(
    'client.enforce_codex_lifecycle_admission()'::regprocedure)
    into strict definition_;
  if position('job.payload->>''schema''=''zekam-codex-lifecycle-job/v1'''
        in definition_)=0
      or position('job.payload->>''authorization_id''='
        'admission.authorization_id::text' in definition_)=0
      or (length(definition_)-length(replace(definition_,baseline_,'')))
        /length(baseline_)<>1
      or position('hydration_authorization_id' in definition_)>0
      or position('continuity.lifecycle_hydration_admission' in definition_)>0 then
    raise exception '0061 refused: 0058 Codex lifecycle admission baseline drift'
      using errcode='55000';
  end if;

  revised_:=replace(definition_,baseline_,payload_guard_);
  if revised_=definition_
      or position('continuity_event.event_type=''session_start''' in revised_)=0
      or position('continuity_event.event_type<>''session_start''' in revised_)=0
      or position('(select count(*) from jsonb_object_keys(job.payload))=3'
        in revised_)=0
      or position('job.payload ? ''hydration_authorization_id''' in revised_)=0
      or position('continuity.lifecycle_hydration_admission hydration_admission'
        in revised_)=0
      or position('hydration_admission.hydration_authorization_id::text'
        in revised_)=0
      or position('=job.payload->>''hydration_authorization_id''' in revised_)=0
      or position('not (job.payload ? ''hydration_authorization_id'')'
        in revised_)=0 then
    raise exception '0061 refused: Codex lifecycle payload compatibility rewrite incomplete'
      using errcode='55000';
  end if;
  execute revised_;
end $codex_migration$;

comment on function work.admit_projection_completion(
  uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text
) is '0061 canonical inventory hydration compatibility';
