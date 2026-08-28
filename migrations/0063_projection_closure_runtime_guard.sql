-- Make the live projection closure admission executable and bind its immutable
-- checkpoint to the pre-effect state. Historical migrations remain unchanged.

do $migration$
declare
  definition_ text;
  revised_ text;
  alias_before_ text:=$before$from jsonb_array_elements(
      hydration.receipt_body->'projection_refs') projection_ref
      where projection_ref->>'digest'=prior_projection.projection_digest$before$;
  alias_after_ text:=$after$from jsonb_array_elements(
      hydration.receipt_body->'projection_refs') projection_ref(value)
      where projection_ref.value->>'digest'=prior_projection.projection_digest$after$;
  checkpoint_before_ text:=$before$and checkpoint.completed_steps=checkpoint.plan_steps
    and cardinality(checkpoint.pending_steps)=0
    and (select array_agg(result.key order by result.key)
      from jsonb_each_text(checkpoint.step_results) result)
      =(select array_agg(step order by step) from unnest(checkpoint.plan_steps) step)
    and not exists(select 1 from jsonb_each_text(checkpoint.step_results) result
      where result.value !~ '^sha256:[0-9a-f]{64}$')
    and checkpoint.step_results->>job.step_id=effect.result_digest$before$;
  checkpoint_after_ text:=$after$and checkpoint.completed_steps=array_remove(checkpoint.plan_steps,job.step_id)
    and checkpoint.pending_steps=array[job.step_id]
    and (select array_agg(result.key order by result.key)
      from jsonb_each_text(checkpoint.step_results) result)
      =(select array_agg(step order by step)
          from unnest(array_remove(checkpoint.plan_steps,job.step_id)) step)
    and not exists(select 1 from jsonb_each_text(checkpoint.step_results) result
      where result.value !~ '^sha256:[0-9a-f]{64}$')$after$;
begin
  select pg_get_functiondef(
    'work.admit_projection_completion(uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,'
    'uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text)'::regprocedure)
    into strict definition_;
  if (length(definition_)-length(replace(definition_,alias_before_,'')))
        /length(alias_before_)<>1
      or position(alias_after_ in definition_)>0
      or (length(definition_)-length(replace(definition_,checkpoint_before_,'')))
        /length(checkpoint_before_)<>1
      or position(checkpoint_after_ in definition_)>0 then
    raise exception '0063 refused: projection closure runtime baseline drift'
      using errcode='55000';
  end if;
  revised_:=replace(replace(definition_,alias_before_,alias_after_),
    checkpoint_before_,checkpoint_after_);
  if revised_=definition_ or position(alias_before_ in revised_)>0
      or position(checkpoint_before_ in revised_)>0
      or (length(revised_)-length(replace(revised_,alias_after_,'')))
        /length(alias_after_)<>1
      or (length(revised_)-length(replace(revised_,checkpoint_after_,'')))
        /length(checkpoint_after_)<>1 then
    raise exception '0063 refused: projection closure runtime rewrite incomplete'
      using errcode='55000';
  end if;
  execute revised_;
end $migration$;

comment on function work.admit_projection_completion(
  uuid,uuid,uuid,integer,text,uuid,text,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,uuid,text
) is '0063 executable projection close and pre-effect checkpoint binding';
