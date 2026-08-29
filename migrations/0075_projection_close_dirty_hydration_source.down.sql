do $migration$
declare
  definition_ text;
  restored_ text;
  baseline_ text:=$baseline$and hydration_event.event_body->>'source_revision'=source_head_
        and hydration_outbox.created_at=hydration_event.ingested_at$baseline$;
  replacement_ text:=$replacement$and (case
          when hydration_event.event_body->>'source_revision'
            ~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'
          then substring(hydration_event.event_body->>'source_revision' from 5 for 40)
          else hydration_event.event_body->>'source_revision'
        end)=source_head_
        and hydration_outbox.created_at=hydration_event.ingested_at$replacement$;
begin
  if exists(
      select 1 from continuity.session_lifecycle_event event
      where event.event_type='pre_close'
        and event.event_body->>'source_revision'
          ~ '^git:[0-9a-f]{40};state:sha256:[0-9a-f]{64}$'
  ) then
    raise exception '0075 rollback refused: dirty-aware pre-close evidence exists'
      using errcode='55000';
  end if;
  select pg_get_functiondef(
    'work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,'
    'uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text)'::regprocedure)
    into strict definition_;
  if (length(definition_)-length(replace(definition_,replacement_,'')))
        /length(replacement_)<>1
      or position(baseline_ in definition_)>0 then
    raise exception '0075 rollback refused: projection hydration source drift'
      using errcode='55000';
  end if;
  restored_:=replace(definition_,replacement_,baseline_);
  execute restored_;
end $migration$;

comment on function work.admit_projection_completion(
  uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text
) is '0074 dirty-aware staged lifecycle, same-run pre-close hydration and separate close checkpoint binding';
