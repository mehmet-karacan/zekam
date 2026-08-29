-- Accept the canonical dirty-aware source revision in the hydration event
-- branch of projection-aware close. Migration 0074 normalized the plan, run,
-- envelope and terminal event checks, but left this one pre-close hydration
-- comparison bound to the plain Git head.

do $migration$
declare
  definition_ text;
  revised_ text;
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
  select pg_get_functiondef(
    'work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,'
    'uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text)'::regprocedure)
    into strict definition_;
  if (length(definition_)-length(replace(definition_,baseline_,'')))
        /length(baseline_)<>1
      or position(replacement_ in definition_)>0 then
    raise exception '0075 refused: projection hydration source baseline drift'
      using errcode='55000';
  end if;
  revised_:=replace(definition_,baseline_,replacement_);
  if revised_=definition_ or position(baseline_ in revised_)>0
      or (length(revised_)-length(replace(revised_,replacement_,'')))
        /length(replacement_)<>1 then
    raise exception '0075 refused: projection hydration source rewrite incomplete'
      using errcode='55000';
  end if;
  execute revised_;
end $migration$;

comment on function work.admit_projection_completion(
  uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text
) is '0075 dirty-aware pre-close hydration source binding';
