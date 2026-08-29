-- Bind measured-loop novelty to its canonical semantic components in PostgreSQL.

alter table runtime.loop_attempt_novelty
  add column novelty_body jsonb,
  add column novelty_body_digest text;

create function runtime.assert_loop_novelty_body(
  p_novelty_body jsonb,p_novelty_digest text,p_objective_digest text
) returns void language plpgsql immutable security definer
set search_path=pg_catalog,runtime,continuity as $$
begin
  if p_novelty_body is null or jsonb_typeof(p_novelty_body)<>'object'
    or (select array_agg(key order by key) from jsonb_object_keys(p_novelty_body) key)
      <>array['action_semantics_digest','artifact_digest','failure_signature',
        'hypothesis_digest','objective_digest','patch_digest']::text[]
    or exists(select 1 from jsonb_each_text(p_novelty_body) item
      where item.value is null or item.value!~'^sha256:[0-9a-f]{64}$')
    or (p_novelty_body->>'objective_digest') is distinct from p_objective_digest
    or p_novelty_digest is null
    or p_novelty_digest!~'^sha256:[0-9a-f]{64}$'
    or continuity.jsonb_digest(p_novelty_body) is distinct from p_novelty_digest then
    raise exception 'measured loop novelty supplied digest canonical body ile uyusmuyor'
      using errcode='42501';
  end if;
end $$;

alter table runtime.loop_attempt_novelty
  add constraint loop_attempt_novelty_body_pair check (
    (novelty_body is null and novelty_body_digest is null)
    or (novelty_body is not null and novelty_body_digest is not null
      and jsonb_typeof(novelty_body)='object'
      and novelty_body ?& array['action_semantics_digest','artifact_digest',
        'failure_signature','hypothesis_digest','objective_digest','patch_digest']
      and novelty_body-array['action_semantics_digest','artifact_digest',
        'failure_signature','hypothesis_digest','objective_digest','patch_digest']='{}'::jsonb
      and jsonb_typeof(novelty_body->'action_semantics_digest')='string'
      and jsonb_typeof(novelty_body->'artifact_digest')='string'
      and jsonb_typeof(novelty_body->'failure_signature')='string'
      and jsonb_typeof(novelty_body->'hypothesis_digest')='string'
      and jsonb_typeof(novelty_body->'objective_digest')='string'
      and jsonb_typeof(novelty_body->'patch_digest')='string'
      and novelty_body->>'action_semantics_digest' ~ '^sha256:[0-9a-f]{64}$'
      and novelty_body->>'artifact_digest' ~ '^sha256:[0-9a-f]{64}$'
      and novelty_body->>'failure_signature' ~ '^sha256:[0-9a-f]{64}$'
      and novelty_body->>'hypothesis_digest' ~ '^sha256:[0-9a-f]{64}$'
      and novelty_body->>'objective_digest' ~ '^sha256:[0-9a-f]{64}$'
      and novelty_body->>'patch_digest' ~ '^sha256:[0-9a-f]{64}$'
      and novelty_body_digest ~ '^sha256:[0-9a-f]{64}$'
      and novelty_body_digest=continuity.jsonb_digest(novelty_body)
      and novelty_digest=novelty_body_digest
      and (novelty_body->>'objective_digest') is not distinct from objective_digest)
  );

create unique index loop_attempt_novelty_hypothesis_once
  on runtime.loop_attempt_novelty(realm_id,loop_id,(novelty_body->>'hypothesis_digest'))
  where novelty_body is not null;
create unique index loop_attempt_novelty_patch_once
  on runtime.loop_attempt_novelty(realm_id,loop_id,(novelty_body->>'patch_digest'))
  where novelty_body is not null;
create unique index loop_attempt_novelty_failure_once
  on runtime.loop_attempt_novelty(realm_id,loop_id,(novelty_body->>'failure_signature'))
  where novelty_body is not null;

create function runtime.assert_optimization_objective_body(
  p_body jsonb,p_id uuid,p_realm_id uuid,p_project_id uuid,p_work_item_id uuid,p_plan_id uuid
) returns void language plpgsql stable security definer
set search_path=pg_catalog,runtime,continuity as $$
declare metric_row record; keys text[]:=array['artifact_baseline_digest','artifact_ref','created_at',
  'deadline','grants_authority','max_attempts','max_cost_micros','max_tokens','measurement_plan_digest',
  'metric_specs','objective_id','plan_id','project_id','realm_id','reversibility_class','step_id',
  'validator_asset_manifest_digest','work_item_id'];
begin
  if p_body is null or jsonb_typeof(p_body)<>'object' or not (p_body ?& keys)
    or p_body-keys<>'{}'::jsonb
    or (p_body->>'objective_id') is distinct from p_id::text
    or (p_body->>'realm_id') is distinct from p_realm_id::text
    or (p_body->>'project_id') is distinct from p_project_id::text
    or (p_body->>'work_item_id') is distinct from p_work_item_id::text
    or (p_body->>'plan_id') is distinct from p_plan_id::text
    or jsonb_typeof(p_body->'grants_authority') is distinct from 'boolean'
    or p_body->'grants_authority'<>'false'::jsonb
    or coalesce(btrim(p_body->>'step_id'),'')=''
    or coalesce(btrim(p_body->>'artifact_ref'),'')=''
    or coalesce(btrim(p_body->>'reversibility_class'),'')=''
    or p_body->>'artifact_baseline_digest' is null
    or p_body->>'artifact_baseline_digest'!~'^sha256:[0-9a-f]{64}$'
    or p_body->>'measurement_plan_digest' is null
    or p_body->>'measurement_plan_digest'!~'^sha256:[0-9a-f]{64}$'
    or p_body->>'validator_asset_manifest_digest' is null
    or p_body->>'validator_asset_manifest_digest'!~'^sha256:[0-9a-f]{64}$'
    or jsonb_typeof(p_body->'max_attempts') is distinct from 'number'
    or jsonb_typeof(p_body->'max_tokens') is distinct from 'number'
    or jsonb_typeof(p_body->'max_cost_micros') is distinct from 'number'
    or jsonb_typeof(p_body->'deadline') is distinct from 'string'
    or jsonb_typeof(p_body->'created_at') is distinct from 'string'
    or p_body->>'deadline'!~'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$'
    or p_body->>'created_at'!~'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$'
    or jsonb_typeof(p_body->'metric_specs') is distinct from 'array' then
    raise exception 'optimization objective canonical exact body ister' using errcode='42501';
  end if;
  if p_body->>'max_attempts'!~'^-?(0|[1-9][0-9]*)$'
    or p_body->>'max_tokens'!~'^-?(0|[1-9][0-9]*)$'
    or p_body->>'max_cost_micros'!~'^-?(0|[1-9][0-9]*)$' then
    raise exception 'optimization objective budget/time/metric shape gecersiz' using errcode='42501';
  end if;
  if (p_body->>'max_attempts')::integer not between 1 and 100
    or (p_body->>'max_tokens')::bigint<1 or (p_body->>'max_cost_micros')::bigint<1
    or not isfinite((p_body->>'deadline')::timestamptz)
    or not isfinite((p_body->>'created_at')::timestamptz)
    or (p_body->>'deadline')::timestamptz<=(p_body->>'created_at')::timestamptz
    or jsonb_array_length(p_body->'metric_specs')=0 then
    raise exception 'optimization objective budget/time/metric shape gecersiz' using errcode='42501';
  end if;
  for metric_row in select value from jsonb_array_elements(p_body->'metric_specs') loop
    if jsonb_typeof(metric_row.value)<>'object'
      or not (metric_row.value ?& array['aggregation','direction','max_value','metric_id',
        'min_value','minimum_meaningful_delta','name','regression_tolerance','role','source_kind',
        'target_value','unit'])
      or metric_row.value-array['aggregation','direction','max_value','metric_id','min_value',
        'minimum_meaningful_delta','name','regression_tolerance','role','source_kind',
        'target_value','unit']<>'{}'::jsonb
      or coalesce(btrim(metric_row.value->>'metric_id'),'')=''
      or coalesce(btrim(metric_row.value->>'name'),'')=''
      or coalesce(btrim(metric_row.value->>'unit'),'')=''
      or coalesce(btrim(metric_row.value->>'source_kind'),'')=''
      or metric_row.value->>'direction' is null
      or metric_row.value->>'direction' not in ('maximize','minimize','target','range')
      or metric_row.value->>'role' is null
      or metric_row.value->>'role' not in ('primary','hard-guard','secondary','cost')
      or metric_row.value->>'aggregation' is null
      or metric_row.value->>'aggregation' not in ('latest','mean','median','p95','sum')
      or jsonb_typeof(metric_row.value->'target_value') not in ('number','null')
      or jsonb_typeof(metric_row.value->'min_value') not in ('number','null')
      or jsonb_typeof(metric_row.value->'max_value') not in ('number','null')
      or jsonb_typeof(metric_row.value->'minimum_meaningful_delta') is distinct from 'number'
      or jsonb_typeof(metric_row.value->'regression_tolerance') is distinct from 'number'
      or (metric_row.value->>'minimum_meaningful_delta')::double precision<0
      or (metric_row.value->>'regression_tolerance')::double precision<0
      or not (abs((metric_row.value->>'minimum_meaningful_delta')::double precision)
        <='1.7976931348623157e308'::double precision)
      or not (abs((metric_row.value->>'regression_tolerance')::double precision)
        <='1.7976931348623157e308'::double precision)
      or (jsonb_typeof(metric_row.value->'target_value')='number'
        and not (abs((metric_row.value->>'target_value')::double precision)
          <='1.7976931348623157e308'::double precision))
      or (jsonb_typeof(metric_row.value->'min_value')='number'
        and not (abs((metric_row.value->>'min_value')::double precision)
          <='1.7976931348623157e308'::double precision))
      or (jsonb_typeof(metric_row.value->'max_value')='number'
        and not (abs((metric_row.value->>'max_value')::double precision)
          <='1.7976931348623157e308'::double precision))
      or (metric_row.value->>'direction' in ('maximize','minimize','target')
        and (jsonb_typeof(metric_row.value->'target_value')<>'number'
          or jsonb_typeof(metric_row.value->'min_value')<>'null'
          or jsonb_typeof(metric_row.value->'max_value')<>'null'))
      or (metric_row.value->>'direction'='range'
        and (jsonb_typeof(metric_row.value->'target_value')<>'null'
          or jsonb_typeof(metric_row.value->'min_value')<>'number'
          or jsonb_typeof(metric_row.value->'max_value')<>'number'
          or (metric_row.value->>'min_value')::double precision
            >(metric_row.value->>'max_value')::double precision)) then
      raise exception 'optimization objective canonical metric spec ister' using errcode='42501';
    end if;
  end loop;
  if not exists(select 1 from jsonb_array_elements(p_body->'metric_specs') spec
      where spec->>'role'='primary')
    or exists(select 1 from jsonb_array_elements(p_body->'metric_specs') spec
      group by spec->>'metric_id' having count(*)>1)
    or exists(select 1 from (
      select ordinality,row_number() over(order by spec->>'metric_id') expected
      from jsonb_array_elements(p_body->'metric_specs') with ordinality entry(spec,ordinality)
    ) ordered where ordinality<>expected) then
    raise exception 'optimization objective metric listesi tekil ve kanonik olmali'
      using errcode='42501';
  end if;
end $$;

create function runtime.assert_validator_asset_manifest_body(
  p_body jsonb,p_id uuid,p_objective_id uuid,p_builder_id uuid,p_verifier_id uuid
) returns void language plpgsql immutable security definer
set search_path=pg_catalog,runtime as $$
declare asset_row record; keys text[]:=array['assets','builder_assignment_id','created_at',
  'grants_authority','manifest_id','objective_id','schema','source_revision',
  'validator_spec_digest','verifier_assignment_id'];
begin
  if p_body is null or jsonb_typeof(p_body)<>'object' or not (p_body ?& keys)
    or p_body-keys<>'{}'::jsonb
    or (p_body->>'schema') is distinct from 'zekam-validator-asset-manifest/v1'
    or (p_body->>'manifest_id') is distinct from p_id::text
    or (p_body->>'objective_id') is distinct from p_objective_id::text
    or (p_body->>'builder_assignment_id') is distinct from p_builder_id::text
    or (p_body->>'verifier_assignment_id') is distinct from p_verifier_id::text
    or p_builder_id=p_verifier_id
    or jsonb_typeof(p_body->'grants_authority') is distinct from 'boolean'
    or p_body->'grants_authority'<>'false'::jsonb
    or p_body->>'validator_spec_digest' is null
    or p_body->>'validator_spec_digest'!~'^sha256:[0-9a-f]{64}$'
    or coalesce(btrim(p_body->>'source_revision'),'')=''
    or jsonb_typeof(p_body->'created_at') is distinct from 'string'
    or p_body->>'created_at'!~'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$'
    or jsonb_typeof(p_body->'assets') is distinct from 'array'
    or jsonb_array_length(p_body->'assets')=0 then
    raise exception 'validator asset manifest canonical exact body ister' using errcode='42501';
  end if;
  for asset_row in select value from jsonb_array_elements(p_body->'assets') loop
    if jsonb_typeof(asset_row.value)<>'object'
      or not (asset_row.value ?& array['asset_id','logical_ref','content_digest','role'])
      or asset_row.value-array['asset_id','logical_ref','content_digest','role']<>'{}'::jsonb
      or coalesce(btrim(asset_row.value->>'asset_id'),'')=''
      or coalesce(btrim(asset_row.value->>'logical_ref'),'')=''
      or asset_row.value->>'content_digest' is null
      or asset_row.value->>'content_digest'!~'^sha256:[0-9a-f]{64}$'
      or asset_row.value->>'role' is null
      or asset_row.value->>'role' not in ('test','fixture','metric','threshold') then
      raise exception 'validator asset manifest canonical asset ister' using errcode='42501';
    end if;
  end loop;
  if exists(select 1 from jsonb_array_elements(p_body->'assets') entry_asset
      group by entry_asset->>'asset_id' having count(*)>1)
    or exists(select 1 from jsonb_array_elements(p_body->'assets') entry_asset
      group by entry_asset->>'logical_ref' having count(*)>1)
    or exists(select 1 from (
      select ordinality,
        row_number() over(order by entry_asset->>'asset_id',entry_asset->>'logical_ref') expected
      from jsonb_array_elements(p_body->'assets')
        with ordinality entry(entry_asset,ordinality)
    ) ordered where ordinality<>expected) then
    raise exception 'validator asset manifest tekil ve kanonik sirali asset ister'
      using errcode='42501';
  end if;
end $$;

create function runtime.assert_loop_policy_v2_body(
  p_body jsonb,p_realm_id uuid,p_loop_id uuid,p_objective_id uuid,p_manifest_id uuid,
  p_stable_digest text,p_stall_limit integer,p_diagnostic_patience integer,
  p_progress_token_budget integer,p_minimum_value_per_cost double precision
) returns void language plpgsql stable security definer
set search_path=pg_catalog,runtime as $$
declare base runtime.loop_policy%rowtype; manifest runtime.validator_asset_manifest%rowtype;
  objective runtime.optimization_objective%rowtype; measured jsonb; keys text[]:=array[
  'assignment_id','canonical_effect_kind','context_manifest_digest','context_manifest_id',
  'created_at','deadline','forbidden_effects','grants_authority','id','max_attempts',
  'max_cost_micros','max_tokens','measured_v2','plan_digest','plan_id','policy_revision_digest',
  'project_id','realm_id','required_delta','source_revision','step_id','terminal_states',
  'validator_assignment_id','validator_spec_digest','work_item_id'];
begin
  select * into base from runtime.loop_policy where realm_id=p_realm_id and id=p_loop_id;
  select * into manifest from runtime.validator_asset_manifest
    where realm_id=p_realm_id and id=p_manifest_id;
  select * into objective from runtime.optimization_objective
    where realm_id=p_realm_id and id=p_objective_id;
  measured:=p_body->'measured_v2';
  if p_body is null or jsonb_typeof(p_body)<>'object'
    or not (p_body ?& keys) or p_body-keys<>'{}'::jsonb
    or jsonb_typeof(p_body->'max_attempts') is distinct from 'number'
    or jsonb_typeof(p_body->'max_tokens') is distinct from 'number'
    or jsonb_typeof(p_body->'max_cost_micros') is distinct from 'number'
    or p_body->>'max_attempts'!~'^-?(0|[1-9][0-9]*)$'
    or p_body->>'max_tokens'!~'^-?(0|[1-9][0-9]*)$'
    or p_body->>'max_cost_micros'!~'^-?(0|[1-9][0-9]*)$'
    or jsonb_typeof(p_body->'grants_authority') is distinct from 'boolean'
    or p_body->'grants_authority'<>'false'::jsonb
    or jsonb_typeof(p_body->'deadline') is distinct from 'string'
    or jsonb_typeof(p_body->'created_at') is distinct from 'string'
    or p_body->>'deadline'!~'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$'
    or p_body->>'created_at'!~'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$'
    or jsonb_typeof(measured)<>'object'
    or not (measured ?& array['diagnostic_patience','measurement_plan_digest',
      'metric_specs_digest','minimum_value_per_cost','objective_id','progress_token_budget',
      'stable_objective_digest','stall_limit','validator_asset_manifest_digest',
      'validator_manifest_id'])
    or measured-array['diagnostic_patience','measurement_plan_digest','metric_specs_digest',
      'minimum_value_per_cost','objective_id','progress_token_budget','stable_objective_digest',
      'stall_limit','validator_asset_manifest_digest','validator_manifest_id']<>'{}'::jsonb
    or jsonb_typeof(measured->'stall_limit') is distinct from 'number'
    or jsonb_typeof(measured->'diagnostic_patience') is distinct from 'number'
    or jsonb_typeof(measured->'progress_token_budget') is distinct from 'number'
    or measured->>'stall_limit'!~'^-?(0|[1-9][0-9]*)$'
    or measured->>'diagnostic_patience'!~'^-?(0|[1-9][0-9]*)$'
    or measured->>'progress_token_budget'!~'^-?(0|[1-9][0-9]*)$'
    or jsonb_typeof(measured->'minimum_value_per_cost') is distinct from 'number'
    or not (abs((measured->>'minimum_value_per_cost')::double precision)
      <='1.7976931348623157e308'::double precision) then
    raise exception 'measured loop policy canonical exact body ister' using errcode='42501';
  end if;
  if base.id is null or manifest.id is null or objective.id is null
    or (p_body->>'id') is distinct from p_loop_id::text
    or (p_body->>'realm_id') is distinct from p_realm_id::text
    or (p_body->>'project_id') is distinct from base.project_id::text
    or (p_body->>'work_item_id') is distinct from base.work_item_id::text
    or (p_body->>'plan_id') is distinct from base.plan_id::text
    or (p_body->>'step_id') is distinct from base.step_id
    or (p_body->>'assignment_id') is distinct from base.assignment_id::text
    or (p_body->>'context_manifest_id') is distinct from base.context_manifest_id::text
    or (p_body->>'validator_assignment_id') is distinct from base.validator_assignment_id::text
    or jsonb_typeof(p_body->'max_attempts') is distinct from 'number'
    or jsonb_typeof(p_body->'max_tokens') is distinct from 'number'
    or jsonb_typeof(p_body->'max_cost_micros') is distinct from 'number'
    or (p_body->>'max_attempts')::numeric<>trunc((p_body->>'max_attempts')::numeric)
    or (p_body->>'max_tokens')::numeric<>trunc((p_body->>'max_tokens')::numeric)
    or (p_body->>'max_cost_micros')::numeric<>trunc((p_body->>'max_cost_micros')::numeric)
    or (p_body->>'max_attempts')::integer is distinct from base.max_attempts
    or (p_body->>'max_tokens')::bigint is distinct from base.max_tokens
    or (p_body->>'max_cost_micros')::bigint is distinct from base.max_cost_micros
    or (p_body->>'deadline')::timestamptz is distinct from base.deadline
    or (p_body->>'validator_spec_digest') is distinct from base.validator_spec_digest
    or p_body->'required_delta' is distinct from to_jsonb(base.required_delta)
    or p_body->'forbidden_effects' is distinct from to_jsonb(base.forbidden_effects)
    or p_body->'terminal_states' is distinct from to_jsonb(base.terminal_states)
    or (p_body->>'plan_digest') is distinct from base.plan_digest
    or (p_body->>'source_revision') is distinct from base.source_revision
    or (p_body->>'context_manifest_digest') is distinct from base.context_manifest_digest
    or (p_body->>'policy_revision_digest') is distinct from base.policy_revision_digest
    or (p_body->>'canonical_effect_kind') is distinct from base.canonical_effect_kind
    or (p_body->>'created_at')::timestamptz is distinct from base.created_at
    or jsonb_typeof(p_body->'grants_authority') is distinct from 'boolean'
    or p_body->'grants_authority'<>'false'::jsonb
    or jsonb_typeof(measured)<>'object'
    or not (measured ?& array['diagnostic_patience','measurement_plan_digest',
      'metric_specs_digest','minimum_value_per_cost','objective_id','progress_token_budget',
      'stable_objective_digest','stall_limit','validator_asset_manifest_digest',
      'validator_manifest_id'])
    or measured-array['diagnostic_patience','measurement_plan_digest','metric_specs_digest',
      'minimum_value_per_cost','objective_id','progress_token_budget','stable_objective_digest',
      'stall_limit','validator_asset_manifest_digest','validator_manifest_id']<>'{}'::jsonb
    or jsonb_typeof(measured->'stall_limit') is distinct from 'number'
    or jsonb_typeof(measured->'diagnostic_patience') is distinct from 'number'
    or jsonb_typeof(measured->'progress_token_budget') is distinct from 'number'
    or (measured->>'stall_limit')::numeric<>trunc((measured->>'stall_limit')::numeric)
    or (measured->>'diagnostic_patience')::numeric
      <>trunc((measured->>'diagnostic_patience')::numeric)
    or (measured->>'progress_token_budget')::numeric
      <>trunc((measured->>'progress_token_budget')::numeric)
    or (measured->>'objective_id') is distinct from p_objective_id::text
    or (measured->>'validator_manifest_id') is distinct from p_manifest_id::text
    or (measured->>'stable_objective_digest') is distinct from p_stable_digest
    or p_stable_digest is distinct from objective.objective_digest
    or manifest.objective_id is distinct from objective.id
    or (objective.objective_body->>'step_id') is distinct from base.step_id
    or (objective.objective_body->>'max_attempts')::integer is distinct from base.max_attempts
    or (objective.objective_body->>'max_tokens')::bigint is distinct from base.max_tokens
    or (objective.objective_body->>'max_cost_micros')::bigint is distinct from base.max_cost_micros
    or (objective.objective_body->>'deadline')::timestamptz is distinct from base.deadline
    or measured->>'measurement_plan_digest' is null
    or measured->>'measurement_plan_digest'!~'^sha256:[0-9a-f]{64}$'
    or measured->>'metric_specs_digest' is null
    or measured->>'metric_specs_digest'!~'^sha256:[0-9a-f]{64}$'
    or (measured->>'metric_specs_digest')
      is distinct from continuity.jsonb_digest(objective.objective_body->'metric_specs')
    or measured->>'validator_asset_manifest_digest' is null
    or measured->>'validator_asset_manifest_digest'!~'^sha256:[0-9a-f]{64}$'
    or (measured->>'measurement_plan_digest')
      is distinct from (objective.objective_body->>'measurement_plan_digest')
    or (measured->>'validator_asset_manifest_digest') is distinct from manifest.manifest_digest
    or (manifest.manifest_body->>'validator_spec_digest')
      is distinct from base.validator_spec_digest
    or (measured->>'stall_limit')::integer is distinct from p_stall_limit
    or (measured->>'diagnostic_patience')::integer is distinct from p_diagnostic_patience
    or (measured->>'progress_token_budget')::integer is distinct from p_progress_token_budget
    or (measured->>'minimum_value_per_cost')::double precision
      is distinct from p_minimum_value_per_cost then
    raise exception 'measured loop policy canonical exact body ister' using errcode='42501';
  end if;
end $$;

create function runtime.enforce_optimization_objective_canonical() returns trigger
language plpgsql security definer set search_path=pg_catalog,runtime,continuity as $$
begin
  perform runtime.assert_optimization_objective_body(new.objective_body,new.id,new.realm_id,
    new.project_id,new.work_item_id,new.plan_id);
  if continuity.jsonb_digest(new.objective_body) is distinct from new.objective_digest then
    raise exception 'optimization objective digest canonical body ile uyusmuyor'
      using errcode='42501';
  end if;
  return new;
end $$;
create trigger optimization_objective_canonical before insert on runtime.optimization_objective
for each row execute function runtime.enforce_optimization_objective_canonical();

create function runtime.enforce_validator_asset_manifest_canonical() returns trigger
language plpgsql security definer set search_path=pg_catalog,runtime,continuity,agents as $$
declare item record;
begin
  perform pg_advisory_xact_lock(
    hashtextextended(new.realm_id::text||':'||new.builder_assignment_id::text,0));
  perform runtime.assert_validator_asset_manifest_body(new.manifest_body,new.id,
    new.objective_id,new.builder_assignment_id,new.verifier_assignment_id);
  if continuity.jsonb_digest(new.manifest_body) is distinct from new.manifest_digest
    or (new.manifest_body->>'manifest_id') is distinct from new.id::text
    or (new.manifest_body->>'objective_id') is distinct from new.objective_id::text
    or (new.manifest_body->>'builder_assignment_id')
      is distinct from new.builder_assignment_id::text
    or (new.manifest_body->>'verifier_assignment_id')
      is distinct from new.verifier_assignment_id::text
    or (new.manifest_body->>'schema') is distinct from 'zekam-validator-asset-manifest/v1'
    or jsonb_typeof(new.manifest_body->'grants_authority') is distinct from 'boolean'
    or new.manifest_body->'grants_authority'<>'false'::jsonb then
    raise exception 'validator asset manifest canonical body ile uyusmuyor'
      using errcode='42501';
  end if;
  if jsonb_typeof(new.manifest_body->'assets') is distinct from 'array'
    or jsonb_array_length(new.manifest_body->'assets')=0 then
    raise exception 'validator asset manifest dolu asset listesi ister' using errcode='42501';
  end if;
  for item in select value from jsonb_array_elements(new.manifest_body->'assets') loop
    if jsonb_typeof(item.value)<>'object'
      or not (item.value ?& array['asset_id','logical_ref','content_digest','role'])
      or item.value-array['asset_id','logical_ref','content_digest','role']<>'{}'::jsonb
      or coalesce(btrim(item.value->>'asset_id'),'')=''
      or coalesce(btrim(item.value->>'logical_ref'),'')=''
      or item.value->>'content_digest' is null
      or item.value->>'content_digest'!~'^sha256:[0-9a-f]{64}$'
      or item.value->>'role' is null
      or item.value->>'role' not in ('test','fixture','metric','threshold') then
      raise exception 'validator asset manifest canonical asset ister' using errcode='42501';
    end if;
    if exists(select 1 from agents.assignment_resource ar
      where ar.realm_id=new.realm_id and ar.assignment_id=new.builder_assignment_id
        and ar.mode='write'
        and runtime.locks_conflict(ar.resource,'write',item.value->>'logical_ref','write')) then
      raise exception 'builder validator asset write scope disinda olmali' using errcode='42501';
    end if;
  end loop;
  if exists(select 1 from jsonb_array_elements(new.manifest_body->'assets') asset
      group by asset->>'asset_id' having count(*)>1)
    or exists(select 1 from jsonb_array_elements(new.manifest_body->'assets') asset
      group by asset->>'logical_ref' having count(*)>1)
    or exists(select 1 from (
      select ordinality,row_number() over(order by asset->>'asset_id',asset->>'logical_ref') expected
      from jsonb_array_elements(new.manifest_body->'assets') with ordinality entry(asset,ordinality)
    ) ordered where ordinality<>expected) then
    raise exception 'validator asset manifest tekil ve kanonik sirali asset ister'
      using errcode='42501';
  end if;
  return new;
end $$;
create trigger validator_asset_manifest_canonical
before insert on runtime.validator_asset_manifest
for each row execute function runtime.enforce_validator_asset_manifest_canonical();

create function runtime.enforce_loop_policy_v2_canonical() returns trigger
language plpgsql security definer set search_path=pg_catalog,runtime,continuity as $$
begin
  perform runtime.assert_loop_policy_v2_body(new.policy_body,new.realm_id,new.loop_id,
    new.objective_id,new.validator_manifest_id,new.stable_objective_digest,new.stall_limit,
    new.diagnostic_patience,new.progress_token_budget,new.minimum_value_per_cost);
  if continuity.jsonb_digest(new.policy_body) is distinct from new.policy_digest then
    raise exception 'measured loop policy digest canonical body ile uyusmuyor'
      using errcode='42501';
  end if;
  return new;
end $$;
create trigger loop_policy_v2_canonical before insert on runtime.loop_policy_v2
for each row execute function runtime.enforce_loop_policy_v2_canonical();

create function runtime.protect_validator_asset_from_builder_write() returns trigger
language plpgsql security definer set search_path=pg_catalog,runtime,agents as $$
begin
  if new.mode<>'write' then return new; end if;
  perform pg_advisory_xact_lock(
    hashtextextended(new.realm_id::text||':'||new.assignment_id::text,0));
  if exists(select 1 from runtime.validator_asset_manifest manifest
    cross join lateral jsonb_array_elements(manifest.manifest_body->'assets') asset
    where manifest.realm_id=new.realm_id
      and manifest.builder_assignment_id=new.assignment_id
      and runtime.locks_conflict(new.resource,'write',asset->>'logical_ref','write')) then
    raise exception 'builder validator asset write scope disinda olmali' using errcode='42501';
  end if;
  return new;
end $$;
create trigger assignment_resource_validator_asset_guard
before insert on agents.assignment_resource
for each row execute function runtime.protect_validator_asset_from_builder_write();

create function runtime.enforce_effect_claim_authorization_once() returns trigger
language plpgsql security definer set search_path=pg_catalog,runtime as $$
begin
  if new.authorization_id is null then return new; end if;
  perform pg_advisory_xact_lock(hashtextextended(new.authorization_id::text,0));
  if exists(select 1 from runtime.effect_claim claim
      where claim.authorization_id=new.authorization_id) then
    raise exception 'effect claim authorization exact one-shot olmali'
      using errcode='23505';
  end if;
  return new;
end $$;
revoke all on function runtime.enforce_effect_claim_authorization_once() from public;
create trigger effect_claim_authorization_once
before insert on runtime.effect_claim
for each row execute function runtime.enforce_effect_claim_authorization_once();

do $$ declare item record;
begin
  for item in select * from runtime.optimization_objective loop
    perform runtime.assert_optimization_objective_body(item.objective_body,item.id,item.realm_id,
      item.project_id,item.work_item_id,item.plan_id);
  end loop;
  for item in select * from runtime.validator_asset_manifest loop
    perform runtime.assert_validator_asset_manifest_body(item.manifest_body,item.id,
      item.objective_id,item.builder_assignment_id,item.verifier_assignment_id);
  end loop;
  for item in select * from runtime.loop_policy_v2 loop
    perform runtime.assert_loop_policy_v2_body(item.policy_body,item.realm_id,item.loop_id,
      item.objective_id,item.validator_manifest_id,item.stable_objective_digest,item.stall_limit,
      item.diagnostic_patience,item.progress_token_budget,item.minimum_value_per_cost);
  end loop;
  if exists(select 1 from runtime.optimization_objective
      where continuity.jsonb_digest(objective_body) is distinct from objective_digest)
    or exists(select 1 from runtime.validator_asset_manifest
      where continuity.jsonb_digest(manifest_body) is distinct from manifest_digest)
    or exists(select 1 from runtime.loop_policy_v2
      where continuity.jsonb_digest(policy_body) is distinct from policy_digest) then
    raise exception '0077 upgrade refused: legacy measured contract canonical digest drift';
  end if;
  if exists(select 1 from runtime.validator_asset_manifest manifest
      join runtime.optimization_objective objective
        on objective.realm_id=manifest.realm_id and objective.id=manifest.objective_id
      where (manifest.manifest_body->>'schema')
          is distinct from 'zekam-validator-asset-manifest/v1'
        or (manifest.manifest_body->>'manifest_id') is distinct from manifest.id::text
        or (manifest.manifest_body->>'objective_id') is distinct from manifest.objective_id::text
        or (manifest.manifest_body->>'builder_assignment_id')
          is distinct from manifest.builder_assignment_id::text
        or (manifest.manifest_body->>'verifier_assignment_id')
          is distinct from manifest.verifier_assignment_id::text
        or jsonb_typeof(manifest.manifest_body->'grants_authority') is distinct from 'boolean'
        or manifest.manifest_body->'grants_authority'<>'false'::jsonb
        or jsonb_typeof(manifest.manifest_body->'assets') is distinct from 'array'
        or jsonb_array_length(manifest.manifest_body->'assets')=0
        or (objective.objective_body->>'objective_id') is distinct from objective.id::text
        or (objective.objective_body->>'validator_asset_manifest_digest')
          is distinct from manifest.manifest_digest)
    or exists(select 1 from runtime.validator_asset_manifest manifest
      cross join lateral jsonb_array_elements(manifest.manifest_body->'assets') asset
      where jsonb_typeof(asset)<>'object'
        or not (asset ?& array['asset_id','logical_ref','content_digest','role'])
        or asset-array['asset_id','logical_ref','content_digest','role']<>'{}'::jsonb
        or coalesce(btrim(asset->>'asset_id'),'')=''
        or coalesce(btrim(asset->>'logical_ref'),'')=''
        or asset->>'content_digest' is null
        or asset->>'content_digest'!~'^sha256:[0-9a-f]{64}$'
        or asset->>'role' is null
        or asset->>'role' not in ('test','fixture','metric','threshold'))
    or exists(select 1 from runtime.validator_asset_manifest manifest
      cross join lateral jsonb_array_elements(manifest.manifest_body->'assets') asset
      group by manifest.realm_id,manifest.id,asset->>'asset_id' having count(*)>1)
    or exists(select 1 from runtime.validator_asset_manifest manifest
      cross join lateral jsonb_array_elements(manifest.manifest_body->'assets') asset
      group by manifest.realm_id,manifest.id,asset->>'logical_ref' having count(*)>1)
    or exists(select 1 from runtime.validator_asset_manifest manifest
      cross join lateral (
        select ordinality,
          row_number() over(order by asset->>'asset_id',asset->>'logical_ref') expected
        from jsonb_array_elements(manifest.manifest_body->'assets')
          with ordinality entry(asset,ordinality)
      ) ordered where ordered.ordinality<>ordered.expected)
    or exists(select 1 from runtime.validator_asset_manifest manifest
      cross join lateral jsonb_array_elements(manifest.manifest_body->'assets') asset
      join agents.assignment_resource resource
        on resource.realm_id=manifest.realm_id
        and resource.assignment_id=manifest.builder_assignment_id and resource.mode='write'
        and runtime.locks_conflict(resource.resource,'write',asset->>'logical_ref','write'))
    or exists(select 1 from runtime.loop_policy_v2 policy
      join runtime.optimization_objective objective
        on objective.realm_id=policy.realm_id and objective.id=policy.objective_id
      join runtime.validator_asset_manifest manifest
        on manifest.realm_id=policy.realm_id and manifest.id=policy.validator_manifest_id
      where (policy.policy_body#>>'{measured_v2,objective_id}')
          is distinct from policy.objective_id::text
        or (policy.policy_body#>>'{measured_v2,stable_objective_digest}')
          is distinct from policy.stable_objective_digest
        or (policy.policy_body#>>'{measured_v2,validator_manifest_id}')
          is distinct from policy.validator_manifest_id::text
        or (policy.policy_body#>>'{measured_v2,validator_asset_manifest_digest}')
          is distinct from manifest.manifest_digest
        or (objective.objective_body->>'measurement_plan_digest')
          is distinct from (policy.policy_body#>>'{measured_v2,measurement_plan_digest}')) then
    raise exception '0077 upgrade refused: legacy measured contract shape drift';
  end if;
  if exists(select 1 from runtime.loop_attempt_job binding
      join runtime.job job on job.realm_id=binding.realm_id and job.id=binding.job_id
      join runtime.loop_policy_v2 policy
        on policy.realm_id=binding.realm_id and policy.loop_id=binding.loop_id
      where job.state in ('ready','running')
        and (job.payload#>>'{admission,novelty_digest}') is not null
        and (job.payload#>'{admission,novelty_body}') is null) then
    raise exception '0077 upgrade refused: queued legacy novelty job reconciliation required';
  end if;
end $$;

create or replace function runtime.record_loop_control_event(
  p_id uuid,p_loop_id uuid,p_state text,p_authorization_id uuid,
  p_authorization_digest text,p_reason_digest text
) returns uuid language plpgsql security definer
set search_path=pg_catalog,runtime,security,work,continuity,core as $$
declare rid uuid:=core.current_realm_id(); lp runtime.loop_policy%rowtype;
  auth security.authorization%rowtype; source_state text; expected_effect_digest text;
begin
  perform pg_advisory_xact_lock(hashtextextended(rid::text||':'||p_loop_id::text,0));
  select * into lp from runtime.loop_policy where realm_id=rid and id=p_loop_id;
  if not found or exists(select 1 from runtime.loop_terminal
      where realm_id=rid and loop_id=p_loop_id)
    or not exists(select 1 from work.work_item item
      where item.realm_id=rid and item.id=lp.work_item_id
        and item.state in ('proposed','ready','active','blocked','verification'))
    or lp.plan_id is distinct from (select candidate.id from work.task_plan candidate
      where candidate.realm_id=rid and candidate.work_item_id=lp.work_item_id
      order by candidate.revision desc,candidate.id desc limit 1) then
    raise exception 'loop control current open Work ve TaskPlan ister' using errcode='42501';
  end if;
  select coalesce((select event.state from runtime.loop_control_event event
    where event.realm_id=rid and event.loop_id=p_loop_id
    order by event.created_at desc,event.id desc limit 1),'active') into source_state;
  if not ((source_state='active' and p_state in ('paused','draining','cancelled'))
    or (source_state='paused' and p_state in ('active','draining','cancelled'))
    or (source_state='draining' and p_state in ('active','cancelled'))) then
    raise exception 'loop control state transition gecersiz' using errcode='42501';
  end if;
  expected_effect_digest:=continuity.jsonb_digest(jsonb_build_object(
    'effect','database-write','resource','loop:'||p_loop_id::text,
    'loop_id',p_loop_id::text,'plan_digest',lp.plan_digest,
    'source_state',source_state,'target_state',p_state,'reason_digest',p_reason_digest));
  select * into auth from security.authorization where realm_id=rid and id=p_authorization_id
    for update;
  if not found or auth.state<>'issued' or auth.expires_at<=clock_timestamp()
    or auth.authorization_digest is distinct from p_authorization_digest
    or auth.work_item_id is distinct from lp.work_item_id
    or auth.plan_id is distinct from lp.plan_id or auth.plan_digest is distinct from lp.plan_digest
    or auth.effect_digest is distinct from expected_effect_digest
    or auth.allowed_effects is distinct from array['database-write']::text[]
    or auth.allowed_resources is distinct from array['loop:'||p_loop_id::text]::text[]
    or cardinality(auth.provider_refs)<>0 or cardinality(auth.secret_ref_ids)<>0 then
    raise exception 'loop control exact one-shot authorization ister' using errcode='42501';
  end if;
  insert into runtime.loop_control_event(id,realm_id,loop_id,state,plan_digest,
    authorization_id,authorization_digest,reason_digest)
  values(p_id,rid,p_loop_id,p_state,lp.plan_digest,p_authorization_id,
    p_authorization_digest,p_reason_digest);
  update security.authorization set state='consumed',consumed_at=clock_timestamp(),
    consumed_by='runtime.loop-control/v2' where id=p_authorization_id and state='issued';
  if not found then raise exception 'loop control authorization consume drift' using errcode='42501';
  end if;
  return p_id;
end $$;

create function runtime.admit_loop_attempt_current_v3(
  p_attempt_id uuid,p_loop_id uuid,p_predecessor_attempt_id uuid,
  p_semantic_request_digest text,p_prompt_digest text,p_context_digest text,
  p_action_digest text,p_binding_digest text,p_source_revision text,p_plan_digest text,
  p_policy_revision_digest text,p_validator_spec_digest text,p_reserved_input_tokens bigint,
  p_reserved_output_tokens bigint,p_reserved_cost_micros bigint,p_evidence_ids uuid[],
  p_delta_digest text,p_attempt_ordinal integer,p_objective_digest text,
  p_validator_asset_manifest_digest text,p_progress_packet_digest text,
  p_metric_vector_digest text,p_novelty_digest text,p_novelty_body jsonb
) returns table(admitted boolean,attempt_id uuid,ordinal integer,terminal_state text,reason text)
language plpgsql security definer set search_path=pg_catalog,runtime,continuity,core as $$
declare rid uuid:=core.current_realm_id(); v2 runtime.loop_policy_v2%rowtype;
  expected_ordinal integer; response record; existing runtime.loop_attempt%rowtype;
  duplicate_component text;
begin
  select * into v2 from runtime.loop_policy_v2 where realm_id=rid and loop_id=p_loop_id;
  if not found then
    return query select * from runtime.admit_loop_attempt_current(
      p_attempt_id,p_loop_id,p_predecessor_attempt_id,p_semantic_request_digest,
      p_prompt_digest,p_context_digest,p_action_digest,p_binding_digest,p_source_revision,
      p_plan_digest,p_policy_revision_digest,p_validator_spec_digest,p_reserved_input_tokens,
      p_reserved_output_tokens,p_reserved_cost_micros,p_evidence_ids,p_delta_digest,
      p_attempt_ordinal,p_objective_digest,p_validator_asset_manifest_digest,
      p_progress_packet_digest,p_metric_vector_digest,p_novelty_digest);
    return;
  end if;
  perform runtime.assert_loop_novelty_body(
    p_novelty_body,p_novelty_digest,p_objective_digest);
  perform pg_advisory_xact_lock(hashtextextended(rid::text||':'||p_loop_id::text,0));
  if coalesce((select event.state from runtime.loop_control_event event
      where event.realm_id=rid and event.loop_id=p_loop_id
      order by event.created_at desc,event.id desc limit 1),'active')<>'active' then
    raise exception 'paused/draining/cancelled loop attempt admission reddedildi'
      using errcode='42501';
  end if;
  if exists(select 1 from runtime.loop_attempt_novelty legacy
      where legacy.realm_id=rid and legacy.loop_id=p_loop_id
        and legacy.novelty_body is null) then
    raise exception 'measured loop legacy novelty recovery reconciliation ister'
      using errcode='55000';
  end if;
  select count(*)+1 into expected_ordinal from runtime.loop_attempt
    where realm_id=rid and loop_id=p_loop_id;
  select attempt.* into existing from runtime.loop_attempt_novelty novelty
    join runtime.loop_attempt attempt on attempt.realm_id=novelty.realm_id
      and attempt.id=novelty.attempt_id
    where novelty.realm_id=rid and novelty.loop_id=p_loop_id
      and novelty.novelty_digest=p_novelty_digest
      and novelty.novelty_body=p_novelty_body
      and novelty.novelty_body_digest=p_novelty_digest;
  if found then
    if existing.id<>p_attempt_id or existing.ordinal<>p_attempt_ordinal
      or existing.predecessor_attempt_id is distinct from p_predecessor_attempt_id
      or existing.source_revision<>p_source_revision or existing.plan_digest<>p_plan_digest
      or existing.policy_revision_digest<>p_policy_revision_digest then
      raise exception 'measured loop novelty duplicate baska attempt ile tekrarlandi'
        using errcode='23505';
    end if;
    return query select true,existing.id,existing.ordinal,null::text,'idempotent replay'::text;
    return;
  end if;
  select component into duplicate_component from (values
    ('hypothesis',p_novelty_body->>'hypothesis_digest'),
    ('patch',p_novelty_body->>'patch_digest'),
    ('failure',p_novelty_body->>'failure_signature')) candidate(component,value)
    where exists(select 1 from runtime.loop_attempt_novelty prior
      where prior.realm_id=rid and prior.loop_id=p_loop_id and prior.novelty_body is not null
        and ((candidate.component='hypothesis'
              and prior.novelty_body->>'hypothesis_digest'=candidate.value)
          or (candidate.component='patch'
              and prior.novelty_body->>'patch_digest'=candidate.value)
          or (candidate.component='failure'
              and prior.novelty_body->>'failure_signature'=candidate.value)))
    order by component limit 1;
  if duplicate_component is not null then
    raise exception 'measured loop novelty component duplicate: %',duplicate_component
      using errcode='23505';
  end if;
  if p_attempt_ordinal<>expected_ordinal or p_objective_digest<>v2.stable_objective_digest
    or p_validator_asset_manifest_digest<>(v2.policy_body#>>'{measured_v2,validator_asset_manifest_digest}')
    or not exists(select 1 from runtime.loop_attempt_job j
      join runtime.job job on job.realm_id=j.realm_id and job.id=j.job_id
      where j.realm_id=rid and j.loop_id=p_loop_id and j.ordinal=expected_ordinal
        and j.predecessor_attempt_id is not distinct from p_predecessor_attempt_id
        and j.progress_packet_digest is not distinct from p_progress_packet_digest
        and job.max_attempts=1 and job.state in ('ready','running')) then
    raise exception 'measured loop admission exact objective/job/ordinal binding ister'
      using errcode='42501';
  end if;
  if expected_ordinal=1 then
    if p_predecessor_attempt_id is not null or p_progress_packet_digest is not null
      or p_metric_vector_digest is not null then
      raise exception 'ilk measured loop attempt progress packet tasiyamaz' using errcode='42501';
    end if;
  elsif not exists(select 1 from runtime.loop_progress_packet packet
      where packet.realm_id=rid and packet.loop_id=p_loop_id
        and packet.attempt_id=p_predecessor_attempt_id
        and packet.packet_digest=p_progress_packet_digest
        and packet.metric_vector_digest=p_metric_vector_digest
        and coalesce((packet.progress_decision_body->>'allow_next_attempt')::boolean,false)) then
    raise exception 'measured loop attempt 2+ exact fresh progress packet ister'
      using errcode='42501';
  end if;
  select * into response from runtime.admit_loop_attempt(
    p_attempt_id,p_loop_id,p_predecessor_attempt_id,p_semantic_request_digest,
    p_prompt_digest,p_context_digest,p_action_digest,p_binding_digest,p_source_revision,
    p_plan_digest,p_policy_revision_digest,p_validator_spec_digest,p_reserved_input_tokens,
    p_reserved_output_tokens,p_reserved_cost_micros,p_evidence_ids,p_delta_digest);
  if response.admitted then
    insert into runtime.loop_attempt_novelty(realm_id,loop_id,attempt_id,ordinal,
      objective_digest,validator_asset_manifest_digest,novelty_digest,
      progress_packet_digest,metric_vector_digest,novelty_body,novelty_body_digest)
    values(rid,p_loop_id,response.attempt_id,response.ordinal,p_objective_digest,
      p_validator_asset_manifest_digest,p_novelty_digest,p_progress_packet_digest,
      p_metric_vector_digest,p_novelty_body,p_novelty_digest);
  end if;
  return query select response.admitted,response.attempt_id,response.ordinal,
    response.terminal_state,response.reason;
end $$;

create or replace function runtime.admit_loop_attempt_current(
  p_attempt_id uuid,p_loop_id uuid,p_predecessor_attempt_id uuid,
  p_semantic_request_digest text,p_prompt_digest text,p_context_digest text,
  p_action_digest text,p_binding_digest text,p_source_revision text,p_plan_digest text,
  p_policy_revision_digest text,p_validator_spec_digest text,p_reserved_input_tokens bigint,
  p_reserved_output_tokens bigint,p_reserved_cost_micros bigint,p_evidence_ids uuid[],
  p_delta_digest text,p_attempt_ordinal integer,p_objective_digest text,
  p_validator_asset_manifest_digest text,p_progress_packet_digest text,
  p_metric_vector_digest text,p_novelty_digest text
) returns table(admitted boolean,attempt_id uuid,ordinal integer,terminal_state text,reason text)
language plpgsql security definer set search_path=pg_catalog,runtime,core as $$
declare rid uuid:=core.current_realm_id();
begin
  perform pg_advisory_xact_lock(hashtextextended(rid::text||':'||p_loop_id::text,0));
  if coalesce((select event.state from runtime.loop_control_event event
      where event.realm_id=rid and event.loop_id=p_loop_id
      order by event.created_at desc,event.id desc limit 1),'active')<>'active' then
    raise exception 'paused/draining/cancelled loop attempt admission reddedildi'
      using errcode='42501';
  end if;
  if exists(select 1 from runtime.loop_policy_v2 where realm_id=rid and loop_id=p_loop_id) then
    raise exception 'measured loop admission current v3 canonical novelty body ister'
      using errcode='42501';
  end if;
  return query select * from runtime.admit_loop_attempt(
    p_attempt_id,p_loop_id,p_predecessor_attempt_id,p_semantic_request_digest,
    p_prompt_digest,p_context_digest,p_action_digest,p_binding_digest,p_source_revision,
    p_plan_digest,p_policy_revision_digest,p_validator_spec_digest,p_reserved_input_tokens,
    p_reserved_output_tokens,p_reserved_cost_micros,p_evidence_ids,p_delta_digest);
end $$;

revoke all on function runtime.admit_loop_attempt_current_v3(uuid,uuid,uuid,text,text,text,text,
  text,text,text,text,text,bigint,bigint,bigint,uuid[],text,integer,text,text,text,text,text,jsonb)
  from public;
grant execute on function runtime.admit_loop_attempt_current_v3(uuid,uuid,uuid,text,text,text,text,
  text,text,text,text,text,bigint,bigint,bigint,uuid[],text,integer,text,text,text,text,text,jsonb)
  to zekam_app;
revoke all on function runtime.assert_loop_novelty_body(jsonb,text,text) from public;
grant execute on function runtime.assert_loop_novelty_body(jsonb,text,text) to zekam_app;
revoke all on function runtime.assert_optimization_objective_body(
  jsonb,uuid,uuid,uuid,uuid,uuid) from public;
revoke all on function runtime.assert_validator_asset_manifest_body(
  jsonb,uuid,uuid,uuid,uuid) from public;
revoke all on function runtime.assert_loop_policy_v2_body(
  jsonb,uuid,uuid,uuid,uuid,text,integer,integer,integer,double precision) from public;
