-- Restore only the 0062 resource-scope rewrite while preserving canonical 0061.

do $migration$
declare
  definition_ text;
  restored_ text;
  marker_ text;
  marker_index_ integer;
  marker_count_ integer;
  markers_ text[]:=array[
    'hydration.receipt_body->>''source_digest''=source_tree_digest_',
    'freshness_dimension->>''observed_digest''=source_tree_digest_',
    'freshness_dimension->>''expected_digest''=source_tree_digest_',
    'projection_ref->>''digest''=prior_projection.projection_digest',
    'hydration_event.event_type in (''session_start'',''hydration_required'')',
    'continuity.lifecycle_hydration_admission admission',
    'and ((hydration_event.event_type=''hydration_required''',
    '''run:''||run.id::text||'':genesis'''
  ];
  expected_marker_counts_ integer[]:=array[1,1,1,1,1,1,2,2];
  previous_scope_ text:='array[''continuity:session:''||event.session_id]';
  revised_scope_ text:=
    'array[''memory:''||event.project_id::text||'':session:''||event.session_id]';
begin
  execute 'alter table work.completion_admission no force row level security';
  if exists(select 1 from work.completion_admission where mode='projection-aware') then
    raise exception '0062 rollback refused: projection completion admission audit data exists'
      using errcode='55000';
  end if;
  execute 'alter table work.completion_admission force row level security';

  select pg_get_functiondef(
    'work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,'
    'uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text)'::regprocedure)
    into strict definition_;
  if cardinality(markers_)<>cardinality(expected_marker_counts_) then
    raise exception '0062 rollback refused: canonical 0061 marker map is incomplete'
      using errcode='55000';
  end if;
  for marker_index_ in 1..cardinality(markers_) loop
    marker_:=markers_[marker_index_];
    marker_count_:=(length(definition_)-length(replace(definition_,marker_,'')))
      /length(marker_);
    if marker_count_<>expected_marker_counts_[marker_index_] then
      raise exception '0062 rollback refused: canonical 0061 marker drift at part %',
        marker_index_ using errcode='55000';
    end if;
  end loop;
  if position(previous_scope_ in definition_)>0
      or (length(definition_)-length(replace(definition_,revised_scope_,'')))
        /length(revised_scope_)<>1 then
    raise exception '0062 rollback refused: lifecycle resource scope drift'
      using errcode='55000';
  end if;

  restored_:=replace(definition_,revised_scope_,previous_scope_);
  if restored_=definition_
      or position(revised_scope_ in restored_)>0
      or (length(restored_)-length(replace(restored_,previous_scope_,'')))
        /length(previous_scope_)<>1 then
    raise exception '0062 rollback refused: lifecycle resource scope restore incomplete'
      using errcode='55000';
  end if;
  for marker_index_ in 1..cardinality(markers_) loop
    marker_:=markers_[marker_index_];
    marker_count_:=(length(restored_)-length(replace(restored_,marker_,'')))
      /length(marker_);
    if marker_count_<>expected_marker_counts_[marker_index_] then
      raise exception '0062 rollback refused: canonical 0061 marker lost at part %',
        marker_index_ using errcode='55000';
    end if;
  end loop;
  execute restored_;
end $migration$;

comment on function work.admit_projection_completion(
  uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text
) is '0061 canonical inventory hydration compatibility';
