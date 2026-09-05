do $migration$
declare
  definition_ text;
  revised_ text;
  session_count_old_ text :=
    '(select count(*) from jsonb_object_keys(job.payload)) in (3,4)';
  session_count_new_ text :=
    '(select count(*) from jsonb_object_keys(job.payload))=3';
  other_count_old_ text :=
    '(select count(*) from jsonb_object_keys(job.payload)) in (2,3)';
  other_count_new_ text :=
    '(select count(*) from jsonb_object_keys(job.payload))=2';
  plan_body_guard_ text := $guard$
job.payload->>'authorization_id'=admission.authorization_id::text
     and (not (job.payload ? 'lifecycle_plan_body') or (
       jsonb_typeof(job.payload->'lifecycle_plan_body')='object'
       and job.payload->'lifecycle_plan_body'=admission.effect_plan_body))
     and not exists(select 1 from jsonb_object_keys(job.payload) payload_key
       where payload_key not in ('schema','authorization_id',
         'hydration_authorization_id','lifecycle_plan_body'))
$guard$;
  authorization_guard_ text :=
    'job.payload->>''authorization_id''=admission.authorization_id::text';
  claim_key_old_ text :=
    'claim.idempotency_key=continuity_event.idempotency_key||'':job:''||job.id::text';
  claim_key_new_ text :=
    'continuity_event.idempotency_key=claim.idempotency_key';
begin
  select pg_get_functiondef(
    'client.enforce_codex_lifecycle_admission()'::regprocedure)
    into strict definition_;

  if (length(definition_)-length(replace(definition_,session_count_old_,'')))
        /length(session_count_old_)<>1
      or (length(definition_)-length(replace(definition_,other_count_old_,'')))
        /length(other_count_old_)<>1
      or (length(definition_)-length(replace(definition_,plan_body_guard_,'')))
        /length(plan_body_guard_)<>1
      or (length(definition_)-length(replace(definition_,claim_key_old_,'')))
        /length(claim_key_old_)<>1 then
    raise exception '0071 down refused: lifecycle plan body baseline drift'
      using errcode='55000';
  end if;

  revised_:=replace(definition_,session_count_old_,session_count_new_);
  revised_:=replace(revised_,other_count_old_,other_count_new_);
  revised_:=replace(revised_,plan_body_guard_,authorization_guard_);
  revised_:=replace(revised_,claim_key_old_,claim_key_new_);
  if revised_=definition_ or position('lifecycle_plan_body' in revised_)>0 then
    raise exception '0071 down refused: lifecycle plan body rewrite incomplete'
      using errcode='55000';
  end if;
  execute revised_;
end $migration$;

comment on function client.enforce_codex_lifecycle_admission() is
  '0061 canonical inventory hydration compatibility';
