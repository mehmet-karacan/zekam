do $migration$
declare
  definition_ text;
  restored_ text;
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
  compatibility_parts_ text[]:=array[
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
  execute 'alter table continuity.lifecycle_hydration_admission no force row level security';
  execute 'alter table work.completion_admission no force row level security';
  if exists(select 1 from continuity.lifecycle_hydration_admission) then
    raise exception '0061 rollback refused: lifecycle hydration admission audit data exists'
      using errcode='55000';
  end if;
  if exists(select 1 from work.completion_admission where mode='projection-aware') then
    raise exception '0061 rollback refused: projection completion admission audit data exists'
      using errcode='55000';
  end if;
  execute 'alter table continuity.lifecycle_hydration_admission force row level security';
  execute 'alter table work.completion_admission force row level security';
  select pg_get_functiondef(
    'work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,'
    'uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text)'::regprocedure)
    into strict definition_;
  if cardinality(baseline_parts_)<>cardinality(compatibility_parts_) then
    raise exception '0061 rollback refused: projection compatibility map is incomplete'
      using errcode='55000';
  end if;
  for part_index_ in 1..cardinality(compatibility_parts_) loop
    if (length(definition_)
          -length(replace(definition_,compatibility_parts_[part_index_],'')))
          /length(compatibility_parts_[part_index_])<>1
        or position(baseline_parts_[part_index_] in definition_)>0 then
      raise exception '0061 rollback refused: compatibility function drift at part %',
        part_index_ using errcode='55000';
    end if;
  end loop;

  restored_:=definition_;
  for part_index_ in 1..cardinality(compatibility_parts_) loop
    restored_:=replace(
      restored_,compatibility_parts_[part_index_],baseline_parts_[part_index_]);
  end loop;
  if restored_=definition_ then
    raise exception '0061 rollback refused: projection baseline restore was empty'
      using errcode='55000';
  end if;
  for part_index_ in 1..cardinality(compatibility_parts_) loop
    if position(compatibility_parts_[part_index_] in restored_)>0
        or (length(restored_)-length(replace(restored_,baseline_parts_[part_index_],'')))
          /length(baseline_parts_[part_index_])<>1 then
      raise exception '0061 rollback refused: baseline restore incomplete at part %',
        part_index_ using errcode='55000';
    end if;
  end loop;
  execute restored_;
end $migration$;

do $codex_rollback$
declare
  definition_ text;
  restored_ text;
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
  if (length(definition_)-length(replace(definition_,payload_guard_,'')))
        /length(payload_guard_)<>1
      or position('job.payload->>''schema''=''zekam-codex-lifecycle-job/v1'''
        in definition_)=0
      or position('job.payload->>''authorization_id''='
        'admission.authorization_id::text' in definition_)=0
      or position('continuity.lifecycle_hydration_admission hydration_admission'
        in definition_)=0
      or position('hydration_admission.hydration_authorization_id::text'
        in definition_)=0
      or position('=job.payload->>''hydration_authorization_id''' in definition_)=0 then
    raise exception '0061 rollback refused: Codex lifecycle compatibility function drift'
      using errcode='55000';
  end if;

  restored_:=replace(definition_,payload_guard_,baseline_);
  if restored_=definition_
      or position('hydration_authorization_id' in restored_)>0
      or position('continuity.lifecycle_hydration_admission' in restored_)>0
      or position('job.payload->>''schema''=''zekam-codex-lifecycle-job/v1'''
        in restored_)=0
      or position('job.payload->>''authorization_id''='
        'admission.authorization_id::text' in restored_)=0
      or (length(restored_)-length(replace(restored_,baseline_,'')))
        /length(baseline_)<>1 then
    raise exception '0061 rollback refused: Codex lifecycle baseline restore incomplete'
      using errcode='55000';
  end if;
  execute restored_;
end $codex_rollback$;

comment on function work.admit_projection_completion(
  uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text
) is null;
