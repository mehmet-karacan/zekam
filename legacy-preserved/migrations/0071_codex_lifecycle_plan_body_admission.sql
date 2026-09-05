-- Codex lifecycle child payload'ina eklenen immutable plan govdesini deferred
-- governed-admission kapisina baglar. Eski terminal kayitlar compatibility icin
-- govdesiz kalabilir; yeni kayit plan govdesi tasiyorsa exact admission govdesiyle
-- ayni olmak zorundadir.

do $migration$
declare
  definition_ text;
  revised_ text;
  session_count_old_ text :=
    '(select count(*) from jsonb_object_keys(job.payload))=3';
  session_count_new_ text :=
    '(select count(*) from jsonb_object_keys(job.payload)) in (3,4)';
  other_count_old_ text :=
    '(select count(*) from jsonb_object_keys(job.payload))=2';
  other_count_new_ text :=
    '(select count(*) from jsonb_object_keys(job.payload)) in (2,3)';
  authorization_guard_ text :=
    'job.payload->>''authorization_id''=admission.authorization_id::text';
  claim_key_old_ text :=
    'continuity_event.idempotency_key=claim.idempotency_key';
  claim_key_new_ text :=
    'claim.idempotency_key=continuity_event.idempotency_key||'':job:''||job.id::text';
  plan_body_guard_ text := $guard$
job.payload->>'authorization_id'=admission.authorization_id::text
     and (not (job.payload ? 'lifecycle_plan_body') or (
       jsonb_typeof(job.payload->'lifecycle_plan_body')='object'
       and job.payload->'lifecycle_plan_body'=admission.effect_plan_body))
     and not exists(select 1 from jsonb_object_keys(job.payload) payload_key
       where payload_key not in ('schema','authorization_id',
         'hydration_authorization_id','lifecycle_plan_body'))
$guard$;
begin
  select pg_get_functiondef(
    'client.enforce_codex_lifecycle_admission()'::regprocedure)
    into strict definition_;

  if (length(definition_)-length(replace(definition_,session_count_old_,'')))
        /length(session_count_old_)<>1
      or (length(definition_)-length(replace(definition_,other_count_old_,'')))
        /length(other_count_old_)<>1
      or (length(definition_)-length(replace(definition_,authorization_guard_,'')))
        /length(authorization_guard_)<>1
      or (length(definition_)-length(replace(definition_,claim_key_old_,'')))
        /length(claim_key_old_)<>1
      or position('lifecycle_plan_body' in definition_)>0 then
    raise exception '0071 refused: Codex lifecycle admission baseline drift'
      using errcode='55000';
  end if;

  revised_:=replace(definition_,session_count_old_,session_count_new_);
  revised_:=replace(revised_,other_count_old_,other_count_new_);
  revised_:=replace(revised_,authorization_guard_,plan_body_guard_);
  revised_:=replace(revised_,claim_key_old_,claim_key_new_);

  if revised_=definition_
      or position(session_count_new_ in revised_)=0
      or position(other_count_new_ in revised_)=0
      or position('jsonb_typeof(job.payload->''lifecycle_plan_body'')=''object'''
        in revised_)=0
      or position('job.payload->''lifecycle_plan_body''=admission.effect_plan_body'
        in revised_)=0
      or position($needle$payload_key not in ('schema','authorization_id',$needle$
        in revised_)=0
      or position(claim_key_new_ in revised_)=0 then
    raise exception '0071 refused: lifecycle plan body admission rewrite incomplete'
      using errcode='55000';
  end if;
  execute revised_;
end $migration$;

comment on function client.enforce_codex_lifecycle_admission() is
  '0071 Codex lifecycle immutable plan body compatibility and admission guard';
