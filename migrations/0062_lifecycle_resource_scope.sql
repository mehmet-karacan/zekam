-- Bind projection-aware close to the exact project/session lifecycle resource.
-- 0057/0061 remain immutable history; only the live 0061 function is revised.

do $migration$
declare
  definition_ text;
  revised_ text;
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
  if to_regclass('continuity.lifecycle_hydration_admission') is null then
    raise exception '0062 requires canonical hydration compatibility migration 0061'
      using errcode='55000';
  end if;
  select pg_get_functiondef(
    'work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,'
    'uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text)'::regprocedure)
    into strict definition_;
  if cardinality(markers_)<>cardinality(expected_marker_counts_) then
    raise exception '0062 refused: canonical 0061 marker map is incomplete'
      using errcode='55000';
  end if;
  for marker_index_ in 1..cardinality(markers_) loop
    marker_:=markers_[marker_index_];
    marker_count_:=(length(definition_)-length(replace(definition_,marker_,'')))
      /length(marker_);
    if marker_count_<>expected_marker_counts_[marker_index_] then
      raise exception '0062 refused: canonical 0061 marker drift at part %',
        marker_index_ using errcode='55000';
    end if;
  end loop;
  if (length(definition_)-length(replace(definition_,previous_scope_,'')))
        /length(previous_scope_)<>1
      or position(revised_scope_ in definition_)>0 then
    raise exception '0062 refused: lifecycle resource scope precondition failed'
      using errcode='55000';
  end if;

  revised_:=replace(definition_,previous_scope_,revised_scope_);
  if revised_=definition_
      or position(previous_scope_ in revised_)>0
      or (length(revised_)-length(replace(revised_,revised_scope_,'')))
        /length(revised_scope_)<>1 then
    raise exception '0062 refused: lifecycle resource scope rewrite incomplete'
      using errcode='55000';
  end if;
  for marker_index_ in 1..cardinality(markers_) loop
    marker_:=markers_[marker_index_];
    marker_count_:=(length(revised_)-length(replace(revised_,marker_,'')))
      /length(marker_);
    if marker_count_<>expected_marker_counts_[marker_index_] then
      raise exception '0062 refused: canonical 0061 marker lost at part %',
        marker_index_ using errcode='55000';
    end if;
  end loop;
  execute revised_;
end $migration$;

comment on function work.admit_projection_completion(
  uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text
) is '0062 project-scoped lifecycle resource authorization';
