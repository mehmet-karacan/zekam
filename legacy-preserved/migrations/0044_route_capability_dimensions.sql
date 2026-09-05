-- Route decisions consume only verifier-bound capability benchmark evidence.

create function models.valid_task_route_dimensions(p_value jsonb, p_tasks text[])
returns boolean language sql immutable parallel safe as $$
  select jsonb_typeof(p_value)='object'
    and not exists (
      select 1 from jsonb_each(p_value) item
      where not (item.key=any(p_tasks)) or jsonb_typeof(item.value)<>'array'
        or exists (
          select 1 from jsonb_array_elements_text(item.value) dimension
          where dimension not in ('context','tool','structured-output','long-session')
        )
        or (select count(*) from jsonb_array_elements_text(item.value))
           is distinct from
           (select count(distinct dimension.value)
              from jsonb_array_elements_text(item.value) dimension(value))
    )
$$;

alter table models.capability_benchmark_suite
  add column task_route_dimensions jsonb not null default '{}'::jsonb,
  add constraint capability_suite_route_dimensions check (
    models.valid_task_route_dimensions(task_route_dimensions,task_digests)
  );

alter table models.model_route_decision
  add column minimum_context_tokens integer not null default 0,
  add column minimum_tool_score double precision not null default 0,
  add column minimum_structured_output_score double precision not null default 0,
  add column minimum_long_session_seconds integer not null default 0,
  add column minimum_long_session_score double precision not null default 0,
  add column capability_evidence_role text,
  add column capability_source_revision text,
  add column capability_suite_digest text,
  add column capability_registry_digest text,
  add column capability_execution_profile_digest text,
  add column capability_evaluator_provenance_digest text,
  add constraint model_route_decision_capability_requirements check (
    minimum_context_tokens>=0 and minimum_long_session_seconds>=0
    and minimum_tool_score between 0 and 1
    and minimum_structured_output_score between 0 and 1
    and minimum_long_session_score between 0 and 1
    and (minimum_long_session_seconds=0)=(minimum_long_session_score=0)
    and ((minimum_context_tokens>0 or minimum_tool_score>0
          or minimum_structured_output_score>0 or minimum_long_session_seconds>0)
      = (capability_evidence_role is not null))
    and ((capability_evidence_role is null
      and capability_source_revision is null and capability_suite_digest is null
      and capability_registry_digest is null and capability_execution_profile_digest is null
      and capability_evaluator_provenance_digest is null) or (
      capability_evidence_role in ('implementer','reviewer','researcher','verifier')
      and length(btrim(capability_source_revision)) between 1 and 128
      and position('://' in capability_source_revision)=0
      and capability_suite_digest ~ '^sha256:[0-9a-f]{64}$'
      and capability_registry_digest ~ '^sha256:[0-9a-f]{64}$'
      and capability_execution_profile_digest ~ '^sha256:[0-9a-f]{64}$'
      and capability_evaluator_provenance_digest ~ '^sha256:[0-9a-f]{64}$'
    ))
  );

create view models.route_capability_evidence
with (security_invoker=true) as
select e.realm_id,e.model_id,e.role,dimension.value dimension,
       case dimension.value
         when 'context' then avg(e.context_retention)
         when 'tool' then avg(case when e.tool_call_count>0 then e.tool_efficiency else 0 end)
         when 'structured-output' then avg(least(e.correctness,e.hidden_acceptance_ratio))
         when 'long-session' then avg(least(e.sustained_progress_auc,e.context_retention))
       end score,
       case dimension.value
         when 'context' then max(e.input_token_count)
         when 'tool' then sum(e.tool_call_count)
         when 'structured-output' then count(*)
         when 'long-session' then max(e.duration_ms)/1000
       end::integer observed_quantity,
       case dimension.value
         when 'tool' then sum(cardinality(e.tool_receipt_digests))
         when 'long-session' then sum(cardinality(e.checkpoint_receipt_digests))
         else 0
       end::integer receipt_count,
       c.inventory_digest,c.policy_digest,c.source_revision,s.suite_digest,
       s.registry_digest,s.execution_profile_digest,s.evaluator_provenance_digest,
       sc.evidence_digest source_scorecard_digest,
       array_agg(e.evidence_digest order by e.task_digest) episode_evidence_digests,
       sc.created_at observed_at,sc.created_at+interval '30 days' expires_at
from models.capability_benchmark_scorecard sc
join models.capability_benchmark_cohort c
  on c.realm_id=sc.realm_id and c.id=sc.cohort_id
join models.capability_benchmark_suite s
  on s.realm_id=c.realm_id and s.id=c.suite_id
join models.capability_benchmark_episode e
  on e.realm_id=sc.realm_id and e.cohort_id=sc.cohort_id and e.model_id=sc.model_id
 and e.evidence_digest=any(sc.episode_evidence_digests)
join lateral jsonb_array_elements_text(
  coalesce(s.task_route_dimensions->e.task_digest,'[]'::jsonb)
) dimension(value) on true
where e.status='passed'
group by e.realm_id,e.model_id,e.role,dimension.value,c.inventory_digest,c.policy_digest,
         c.source_revision,s.suite_digest,s.registry_digest,s.execution_profile_digest,
         s.evaluator_provenance_digest,sc.evidence_digest,sc.created_at;

comment on view models.route_capability_evidence is
  'Kanonik capability episode ve scorecardlarindan turetilen authoritysiz route kaniti.';

grant select on models.route_capability_evidence to zekam_app;
grant execute on function models.valid_task_route_dimensions(jsonb,text[]) to zekam_app;
