-- Context compiler v2 scoring policy ve canonical efficiency metrics persistence.

create function core.canonical_jsonb(value jsonb) returns text
language sql immutable strict parallel safe set search_path = pg_catalog as $$
select case jsonb_typeof(value)
    when 'object' then '{' || coalesce((
        select string_agg(to_jsonb(key)::text || ':' || core.canonical_jsonb(item), ',' order by key)
        from jsonb_each(value) entry(key, item)
    ), '') || '}'
    when 'array' then '[' || coalesce((
        select string_agg(core.canonical_jsonb(item), ',' order by ordinal)
        from jsonb_array_elements(value) with ordinality entry(item, ordinal)
    ), '') || ']'
    else value::text
end
$$;

create function work.current_context_ranking_projection(
    requested_realm uuid, requested_project uuid, requested_work uuid, requested_assignment uuid
) returns table (
    role text, assignment_digest text, step_id text, plan_id uuid,
    source_revision text, task_terms text[], database_now timestamptz
)
language sql security definer set search_path = pg_catalog, work, agents, core as $$
select a.role,a.assignment_digest,a.step_id,a.plan_id,p.source_revision,
       array(select distinct matches[1]
             from regexp_matches(lower(w.title || ' ' || a.step_id),
                 '[[:alnum:]_.-]+','g') matches order by matches[1]),
       clock_timestamp()
from agents.assignment a
join work.task_plan p on p.realm_id=a.realm_id and p.id=a.plan_id
    and p.project_id=a.project_id and p.work_item_id=a.work_item_id
join work.work_item w on w.realm_id=a.realm_id and w.id=a.work_item_id
where requested_realm=core.current_realm_id()
  and a.realm_id=requested_realm and a.project_id=requested_project
  and a.work_item_id=requested_work and a.id=requested_assignment
  and a.status in ('ready','active')
  and exists (select 1 from jsonb_array_elements(p.steps) step
              where step->>'step_id'=a.step_id)
for share of a,p,w
$$;

revoke all on function work.current_context_ranking_projection(uuid,uuid,uuid,uuid) from public;
grant execute on function work.current_context_ranking_projection(uuid,uuid,uuid,uuid) to zekam_app;

create function work.lock_context_source_revision_v2() returns trigger
language plpgsql security definer set search_path = pg_catalog as $$
begin
    if new.entity_type in (
        'context.system_policy','work.item','context.run_status','context.architecture_rule',
        'context.dependency_manifest','context.source_slice','context.source_diff',
        'context.effect_receipt','context.test_evidence'
    ) then
        perform pg_advisory_xact_lock(hashtextextended(
            'context-source:' || new.realm_id::text || ':' || new.entity_id::text, 0
        ));
    end if;
    return new;
end
$$;

create trigger context_source_revision_serialization_v2
before insert on core.revision
for each row execute function work.lock_context_source_revision_v2();

create function work.current_context_source_revisions(
    requested_realm uuid, requested_work uuid
) returns table (
    id uuid, entity_type text, candidate_kind text, payload jsonb,
    payload_digest text, recorded_at timestamptz
)
language plpgsql security definer set search_path = pg_catalog, core as $$
begin
perform pg_advisory_xact_lock(hashtextextended(
    'context-source:' || requested_realm::text || ':' || requested_work::text, 0
));
return query
with latest as (
    select candidate.id,
           row_number() over (
               partition by candidate.entity_type order by candidate.revision desc
           ) as position
    from core.revision candidate
    where candidate.realm_id=requested_realm and candidate.entity_id=requested_work
      and candidate.entity_type in (
        'context.system_policy','work.item','context.run_status','context.architecture_rule',
        'context.dependency_manifest','context.source_slice','context.source_diff',
        'context.effect_receipt','context.test_evidence'
      )
)
select revision.id,revision.entity_type,
       case revision.entity_type
         when 'context.system_policy' then 'system-policy'
         when 'work.item' then 'work-contract'
         when 'context.run_status' then 'run-status'
         when 'context.architecture_rule' then 'architecture-rule'
         when 'context.dependency_manifest' then 'dependency-manifest'
         when 'context.source_slice' then 'source-slice'
         when 'context.source_diff' then 'source-diff'
         when 'context.effect_receipt' then 'effect-receipt'
         when 'context.test_evidence' then 'test-evidence'
       end,
       revision.payload,revision.payload_digest,revision.recorded_at
from core.revision revision
join latest on latest.id=revision.id and latest.position=1
where requested_realm=core.current_realm_id()
  and revision.realm_id=requested_realm and revision.entity_id=requested_work
order by revision.entity_type
for share of revision
;
end
$$;

revoke all on function work.current_context_source_revisions(uuid,uuid) from public;
grant execute on function work.current_context_source_revisions(uuid,uuid) to zekam_app;
revoke all on function work.lock_context_source_revision_v2() from public;
grant execute on function work.lock_context_source_revision_v2() to zekam_app;

create table work.context_ranking_snapshot (
    realm_id uuid not null,
    project_id uuid not null,
    work_item_id uuid not null,
    assignment_id uuid not null,
    assignment_digest text not null check (assignment_digest ~ '^sha256:[0-9a-f]{64}$'),
    source_snapshot_digest text not null check (source_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    snapshot_digest text not null check (snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    canonical_body text not null,
    captured_at timestamptz not null,
    expires_at timestamptz not null check (expires_at > captured_at),
    primary key (realm_id, snapshot_digest),
    foreign key (realm_id, project_id) references projects.project (realm_id, id),
    foreign key (realm_id, work_item_id) references work.work_item (realm_id, id),
    foreign key (realm_id, assignment_id) references agents.assignment (realm_id, id),
    check (canonical_body = core.canonical_jsonb(canonical_body::jsonb)),
    check ('sha256:' || encode(public.digest(convert_to(canonical_body, 'UTF8'), 'sha256'), 'hex') = snapshot_digest)
);

create table work.context_candidate_set (
    realm_id uuid not null,
    project_id uuid not null,
    work_item_id uuid not null,
    ranking_snapshot_digest text not null,
    candidate_set_digest text not null check (candidate_set_digest ~ '^sha256:[0-9a-f]{64}$'),
    candidate_fingerprint text not null check (candidate_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    candidate_count integer not null check (candidate_count > 0),
    candidate_tokens integer not null check (candidate_tokens >= candidate_count),
    canonical_body text not null,
    captured_at timestamptz not null,
    expires_at timestamptz not null check (expires_at > captured_at),
    primary key (realm_id, candidate_set_digest),
    unique (realm_id, project_id, work_item_id, candidate_set_digest),
    foreign key (realm_id, ranking_snapshot_digest)
        references work.context_ranking_snapshot (realm_id, snapshot_digest),
    check (canonical_body = core.canonical_jsonb(canonical_body::jsonb)),
    check ('sha256:' || encode(public.digest(convert_to(canonical_body, 'UTF8'), 'sha256'), 'hex') = candidate_set_digest),
    check ((canonical_body::jsonb->>'ranking_snapshot_digest') = ranking_snapshot_digest),
    check ((canonical_body::jsonb->>'candidate_fingerprint') = candidate_fingerprint),
    check (jsonb_array_length(canonical_body::jsonb->'candidates') = candidate_count)
);

create function work.enforce_context_ranking_snapshot_v2() returns trigger
language plpgsql security invoker set search_path = pg_catalog, work, core as $$
begin
    if new.canonical_body <> core.canonical_jsonb(new.canonical_body::jsonb)
       or (select count(*) from jsonb_object_keys(new.canonical_body::jsonb)) <> 11
       or (select count(*) from jsonb_object_keys(new.canonical_body::jsonb->'request')) <> 10
       or new.canonical_body::jsonb->>'schema' <> 'zekam-context-ranking-snapshot/v1'
       or new.canonical_body::jsonb->>'assignment_id' <> new.assignment_id::text
       or new.canonical_body::jsonb->>'assignment_digest' <> new.assignment_digest
       or new.canonical_body::jsonb->>'source_snapshot_digest' <> new.source_snapshot_digest
       or (new.canonical_body::jsonb->>'captured_at')::timestamptz <> new.captured_at
       or (new.canonical_body::jsonb->>'expires_at')::timestamptz <> new.expires_at
       or new.captured_at > clock_timestamp()
       or new.expires_at > new.captured_at + interval '5 minutes'
       or not exists (
            select 1 from agents.assignment assignment
            join work.task_plan plan on plan.realm_id=assignment.realm_id
              and plan.id=assignment.plan_id and plan.project_id=assignment.project_id
              and plan.work_item_id=assignment.work_item_id
            join work.work_item item on item.realm_id=assignment.realm_id
              and item.id=assignment.work_item_id
            where assignment.realm_id=new.realm_id and assignment.id=new.assignment_id
              and assignment.project_id=new.project_id
              and assignment.work_item_id=new.work_item_id
              and assignment.status in ('ready','active')
              and assignment.assignment_digest=new.assignment_digest
              and assignment.role=new.canonical_body::jsonb->'request'->>'role'
              and new.canonical_body::jsonb->>'realm_ref'='realm/' || new.realm_id::text
              and new.canonical_body::jsonb->>'project_ref'='project/' || new.project_id::text
              and new.canonical_body::jsonb->>'work_ref'='work/' || new.work_item_id::text
              and new.canonical_body::jsonb->>'step_ref'='step/' || assignment.step_id
              and new.canonical_body::jsonb->'request'->>'realm_scope_ref'=
                    new.canonical_body::jsonb->>'realm_ref'
              and new.canonical_body::jsonb->'request'->>'project_scope_ref'=
                    new.canonical_body::jsonb->>'project_ref'
              and new.canonical_body::jsonb->'request'->>'work_scope_ref'=
                    new.canonical_body::jsonb->>'work_ref'
              and new.canonical_body::jsonb->'request'->>'step_scope_ref'=
                    new.canonical_body::jsonb->>'step_ref'
              and new.canonical_body::jsonb->'request'->>'current_source_revision'=plan.source_revision
              and new.canonical_body::jsonb->'request'->'compatible_source_revisions'='[]'::jsonb
              and new.canonical_body::jsonb->'request'->>'tokenizer_profile_digest'=
                    'sha256:0c6514c76589ae135055aefb11d84e396b3e78e7159af4c8157d1bd691a10ef8'
              and new.canonical_body::jsonb->'request'->'target_identity_refs' = to_jsonb(array[
                    'step/' || assignment.step_id, 'work/' || new.work_item_id::text])
              and new.canonical_body::jsonb->'request'->'task_terms' = (
                    select coalesce(jsonb_agg(term order by term),'[]'::jsonb)
                    from (select distinct matches[1] term
                          from regexp_matches(lower(item.title || ' ' || assignment.step_id),
                              '[[:alnum:]_.-]+','g') matches) terms)
              and new.source_snapshot_digest='sha256:' || encode(public.digest(convert_to(
                    core.canonical_jsonb(jsonb_build_object(
                        'schema','zekam-context-ranking-source/v1',
                        'realm_id',new.realm_id::text,'project_id',new.project_id::text,
                        'work_item_id',new.work_item_id::text,
                        'assignment_id',new.assignment_id::text,
                        'assignment_digest',new.assignment_digest,
                        'plan_id',assignment.plan_id::text,
                        'step_id',assignment.step_id,
                        'source_revision',plan.source_revision)), 'UTF8'), 'sha256'), 'hex')
       )
       or 'sha256:' || encode(public.digest(convert_to(new.canonical_body, 'UTF8'), 'sha256'), 'hex') <> new.snapshot_digest then
        raise exception 'context ranking snapshot canonical provenance drift' using errcode='23514';
    end if;
    return new;
end
$$;

create function work.enforce_context_candidate_set_v2() returns trigger
language plpgsql security invoker set search_path = pg_catalog, work, core as $$
declare expected_fingerprint text;
begin
    select 'sha256:' || encode(public.digest(convert_to(
        core.canonical_jsonb(jsonb_agg(to_jsonb(entry->>'candidate_digest') order by entry->>'candidate_id')),
        'UTF8'), 'sha256'), 'hex') into expected_fingerprint
      from jsonb_array_elements(new.canonical_body::jsonb->'candidates') entry;
    if new.canonical_body <> core.canonical_jsonb(new.canonical_body::jsonb)
       or (select count(*) from jsonb_object_keys(new.canonical_body::jsonb)) <> 6
       or new.canonical_body::jsonb->>'schema' <> 'zekam-context-candidate-set/v1'
       or (new.canonical_body::jsonb->>'captured_at')::timestamptz <> new.captured_at
       or (new.canonical_body::jsonb->>'expires_at')::timestamptz <> new.expires_at
       or jsonb_array_length(new.canonical_body::jsonb->'candidates') <> new.candidate_count
       or coalesce((select sum((entry->>'token_count')::integer)
            from jsonb_array_elements(new.canonical_body::jsonb->'candidates') entry),0) <> new.candidate_tokens
       or expected_fingerprint <> new.candidate_fingerprint
       or exists (
            select 1 from jsonb_array_elements(new.canonical_body::jsonb->'candidates') entry
            where (select count(*) from jsonb_object_keys(entry)) <> 6
               or (entry->>'required')::boolean
               or (select count(*) from jsonb_object_keys(entry->'provenance')) <> 19
               or entry->>'candidate_id' <> entry->'provenance'->>'id'
               or entry->>'content_digest' <> entry->'provenance'->>'digest'
               or (entry->>'token_count')::integer <> (entry->'provenance'->>'tokens')::integer
               or entry->>'candidate_digest' <> 'sha256:' || encode(public.digest(
                    convert_to(core.canonical_jsonb(entry->'provenance'),'UTF8'),'sha256'),'hex')
               or entry->'provenance'->>'canonical_revision_id' is null
               or (entry->'provenance'->>'authority')::integer <> 3
                or entry->'provenance'->>'kind' not in (
                     'system-policy','work-contract','run-status','architecture-rule',
                     'dependency-manifest','source-slice','source-diff','effect-receipt',
                     'test-evidence')
               or entry->'provenance'->>'scope_ref' <> 'work/' || new.work_item_id::text
               or entry->'provenance'->>'tokenizer_profile_digest' <>
                    'sha256:0c6514c76589ae135055aefb11d84e396b3e78e7159af4c8157d1bd691a10ef8'
               or entry->'provenance'->'valid_until' <> 'null'::jsonb
               or (entry->'provenance'->>'superseded')::boolean
               or entry->'provenance'->'task_terms' <> '[]'::jsonb
               or entry->'provenance'->'compatible_source_revisions' <> '[]'::jsonb
               or entry->'provenance'->'conflict_refs' <> '[]'::jsonb
               or entry->'provenance'->'applicable_roles' <> jsonb_build_array(
                    (select canonical_body::jsonb->'request'->>'role'
                     from work.context_ranking_snapshot
                     where realm_id=new.realm_id and snapshot_digest=new.ranking_snapshot_digest))
               or entry->'provenance'->'identity_refs' <> (
                    select canonical_body::jsonb->'request'->'target_identity_refs'
                    from work.context_ranking_snapshot
                    where realm_id=new.realm_id and snapshot_digest=new.ranking_snapshot_digest)
               or entry->'provenance'->>'source_revision' <> (
                    select canonical_body::jsonb->'request'->>'current_source_revision'
                    from work.context_ranking_snapshot
                    where realm_id=new.realm_id and snapshot_digest=new.ranking_snapshot_digest)
               or jsonb_array_length(entry->'provenance'->'evidence_refs') <> 1
               or not exists (
                    select 1 from core.revision revision
                    where revision.realm_id=new.realm_id
                      and revision.id=(entry->'provenance'->>'canonical_revision_id')::uuid
                       and revision.entity_id=new.work_item_id
                       and entry->'provenance'->>'kind'=case revision.entity_type
                         when 'context.system_policy' then 'system-policy'
                         when 'work.item' then 'work-contract'
                         when 'context.run_status' then 'run-status'
                         when 'context.architecture_rule' then 'architecture-rule'
                         when 'context.dependency_manifest' then 'dependency-manifest'
                         when 'context.source_slice' then 'source-slice'
                         when 'context.source_diff' then 'source-diff'
                         when 'context.effect_receipt' then 'effect-receipt'
                         when 'context.test_evidence' then 'test-evidence'
                       end
                      and (entry->'provenance'->>'observed_at')::timestamptz=revision.recorded_at
                      and (entry->>'token_count')::integer=
                          octet_length(core.canonical_jsonb(revision.payload))
                      and entry->>'content_digest'='sha256:' || encode(public.digest(
                          convert_to(to_jsonb(core.canonical_jsonb(revision.payload))::text,
                                     'UTF8'),'sha256'),'hex')
                      and entry->'provenance'->>'source_ref'='revision/' || revision.id::text
                      and entry->>'candidate_id'='revision/' || revision.id::text || '/' ||
                          (entry->'provenance'->>'kind')
                      and entry->'provenance'->'evidence_refs'->0->>'kind'='work'
                      and entry->'provenance'->'evidence_refs'->0->>'ref'='revision/' || revision.id::text
                      and entry->'provenance'->'evidence_refs'->0->>'digest'=revision.payload_digest
                      and entry->'provenance'->'evidence_refs'->0->'revision'='null'::jsonb
                      and (select count(*) from jsonb_object_keys(
                          entry->'provenance'->'evidence_refs'->0))=4
               )
       )
       or 'sha256:' || encode(public.digest(convert_to(new.canonical_body, 'UTF8'), 'sha256'), 'hex') <> new.candidate_set_digest then
        raise exception 'context candidate set canonical provenance drift' using errcode='23514';
    end if;
    return new;
end
$$;

create trigger context_ranking_snapshot_v2_integrity before insert on work.context_ranking_snapshot
    for each row execute function work.enforce_context_ranking_snapshot_v2();
create trigger context_candidate_set_v2_integrity before insert on work.context_candidate_set
    for each row execute function work.enforce_context_candidate_set_v2();

do $$
declare target text;
begin
    foreach target in array array['work.context_ranking_snapshot','work.context_candidate_set'] loop
        execute format('alter table %s enable row level security', target);
        execute format('alter table %s force row level security', target);
        execute format('create policy scope_select on %s for select using (realm_id=core.current_realm_id())', target);
        execute format('create policy scope_insert on %s for insert with check (realm_id=core.current_realm_id())', target);
        execute format('create trigger deny_update before update on %s for each statement execute function core.deny_mutation()', target);
        execute format('create trigger deny_delete before delete on %s for each statement execute function core.deny_mutation()', target);
    end loop;
end
$$;

grant select, insert on work.context_ranking_snapshot, work.context_candidate_set to zekam_app;
grant execute on function core.canonical_jsonb(jsonb) to zekam_app;
grant execute on function work.enforce_context_ranking_snapshot_v2() to zekam_app;
grant execute on function work.enforce_context_candidate_set_v2() to zekam_app;

alter table work.context_manifest
    add column compiler_version smallint not null default 1,
    add column scoring_policy_digest text,
    add column compiler_metrics jsonb,
    add column compiler_metrics_digest text,
    add column compiler_metrics_canonical text,
    add column manifest_canonical text,
    add column ranking_snapshot_digest text,
    add column candidate_set_digest text,
    add constraint context_manifest_compiler_version check (compiler_version in (1, 2)),
    add constraint context_manifest_v2_binding check (
        (compiler_version = 1 and scoring_policy_digest is null
            and compiler_metrics is null and compiler_metrics_digest is null
            and compiler_metrics_canonical is null and manifest_canonical is null
            and ranking_snapshot_digest is null
            and candidate_set_digest is null)
        or
        (compiler_version = 2 and scoring_policy_digest like 'sha256:%'
            and jsonb_typeof(compiler_metrics) = 'object'
            and compiler_metrics_digest like 'sha256:%'
            and compiler_metrics_canonical is not null and manifest_canonical is not null
            and ranking_snapshot_digest like 'sha256:%'
            and candidate_set_digest like 'sha256:%')
    );

alter table work.context_manifest
    add constraint context_manifest_candidate_set_fk foreign key
        (realm_id,project_id,work_item_id,candidate_set_digest)
        references work.context_candidate_set
            (realm_id,project_id,work_item_id,candidate_set_digest);

create function work.enforce_context_manifest_v2() returns trigger
language plpgsql security invoker set search_path = pg_catalog, work, core as $$
declare
    selected_count integer;
    omitted_count integer;
    selected_tokens integer;
    omitted_tokens integer;
    duplicate_count integer;
    duplicate_tokens integer;
    eligible_count integer;
    eligible_tokens integer;
    required_count integer;
    selected_relevance_units integer;
    omission_counts jsonb;
    candidate_set_body jsonb;
begin
    if new.compiler_version <> 2 then
        return new;
    end if;
    select count(*), coalesce(sum((entry->>'token_count')::integer), 0)
      into selected_count, selected_tokens
      from jsonb_array_elements(new.selected) entry;
    select count(*), coalesce(sum((entry->>'token_count')::integer), 0),
           count(*) filter (where entry->>'reason' = 'duplicate'),
           coalesce(sum((entry->>'token_count')::integer)
               filter (where entry->>'reason' = 'duplicate'), 0)
      into omitted_count, omitted_tokens, duplicate_count, duplicate_tokens
      from jsonb_array_elements(new.omitted) entry;
    select count(*), coalesce(sum((entry->>'token_count')::integer), 0)
      into eligible_count, eligible_tokens
      from (
        select entry from jsonb_array_elements(new.selected) entry
        union all
        select entry from jsonb_array_elements(new.omitted) entry
          where entry->>'reason' in ('duplicate', 'budget-exhausted')
      ) eligible;
    select count(*) into required_count
      from jsonb_array_elements(new.selected) entry
      where entry->'reason_codes' ? 'required';
    select coalesce(sum(((entry->'score'->>6)::integer +
                         (entry->'score'->>7)::integer) *
                        (entry->>'token_count')::integer),0)
      into selected_relevance_units from jsonb_array_elements(new.selected) entry;
    select coalesce(jsonb_object_agg(reason, reason_count), '{}'::jsonb)
      into omission_counts
      from (
        select entry->>'reason' reason, count(*) reason_count
        from jsonb_array_elements(new.omitted) entry group by entry->>'reason'
      ) reasons;
    select canonical_body::jsonb into candidate_set_body
      from work.context_candidate_set
      where realm_id=new.realm_id and project_id=new.project_id
        and work_item_id=new.work_item_id and candidate_set_digest=new.candidate_set_digest
        and ranking_snapshot_digest=new.ranking_snapshot_digest
        and candidate_fingerprint=new.candidate_fingerprint
        and expires_at>clock_timestamp();
    if exists (
        select 1 from (
            select entry->>'candidate_id' candidate_id from jsonb_array_elements(new.selected) entry
            union all
            select entry->>'candidate_id' candidate_id from jsonb_array_elements(new.omitted) entry
        ) all_candidates
        group by candidate_id having count(*) <> 1
    )
    or candidate_set_body is null
    or selected_count + omitted_count <> jsonb_array_length(candidate_set_body->'candidates')
    or exists (
        select 1 from jsonb_array_elements(candidate_set_body->'candidates') candidate
        where not exists (
            select 1 from jsonb_array_elements(new.selected) selected_entry
            where selected_entry->>'candidate_id'=candidate->>'candidate_id'
              and selected_entry->>'candidate_digest'=candidate->>'candidate_digest'
              and selected_entry->>'content_digest'=candidate->>'content_digest'
              and (selected_entry->>'token_count')::integer=(candidate->>'token_count')::integer
              and selected_entry->>'kind'=candidate->'provenance'->>'kind'
              and selected_entry->>'source_ref'=candidate->'provenance'->>'source_ref'
              and selected_entry->>'source_revision'=candidate->'provenance'->>'revision'
              and (selected_entry->>'authority')::integer=
                  (candidate->'provenance'->>'authority')::integer
        ) and not exists (
            select 1 from jsonb_array_elements(new.omitted) omitted_entry
            where omitted_entry->>'candidate_id'=candidate->>'candidate_id'
              and (omitted_entry->>'token_count')::integer=(candidate->>'token_count')::integer
        )
    )
    or exists (
        select 1 from jsonb_array_elements(new.selected) entry
        where (select count(*) from jsonb_object_keys(entry)) <> 11
    )
    or exists (
        select 1 from jsonb_array_elements(new.omitted) entry
        where (select count(*) from jsonb_object_keys(entry)) <> 5
    )
    or selected_count <> (new.compiler_metrics->>'selected_count')::integer
    or omitted_count <> (new.compiler_metrics->>'omitted_count')::integer
    or selected_count + omitted_count <> (new.compiler_metrics->>'input_count')::integer
    or selected_tokens <> (new.compiler_metrics->>'selected_tokens')::integer
    or omitted_tokens <> (new.compiler_metrics->>'omitted_tokens')::integer
    or selected_tokens + omitted_tokens <> (new.compiler_metrics->>'input_tokens')::integer
    or duplicate_count <> (new.compiler_metrics->>'duplicate_suppressed_count')::integer
    or duplicate_tokens <> (new.compiler_metrics->>'duplicate_suppressed_tokens')::integer
    or eligible_count <> (new.compiler_metrics->>'eligible_count')::integer
    or eligible_tokens <> (new.compiler_metrics->>'eligible_tokens')::integer
    or required_count <> (new.compiler_metrics->>'required_total')::integer
    or required_count <> (new.compiler_metrics->>'required_selected')::integer
    or omission_counts <> new.compiler_metrics->'omission_counts'
    or selected_tokens > new.token_budget
    or (new.compiler_metrics->>'token_budget')::integer <> new.token_budget
    or (new.compiler_metrics->>'token_utilization_ppm')::integer <>
       least(1000000, selected_tokens::bigint * 1000000 / new.token_budget)
    or (new.compiler_metrics->>'token_efficiency_ppm')::integer <>
       (case when selected_tokens=0 then 0 else
        least(1000000, selected_relevance_units::bigint * 1000000 /
             (selected_tokens::bigint * 8)) end)
    or (new.compiler_metrics->>'duplicate_token_ratio_ppm')::integer <>
       (case when eligible_tokens = 0 then 0
             else least(1000000, duplicate_tokens::bigint * 1000000 / eligible_tokens) end) then
        raise exception 'context compiler v2 exact partition/metrics drift' using errcode = '23514';
    end if;
    if new.scoring_policy_digest <>
       'sha256:2869ebd2250cd8bcde5073afd1896dae743a05ecccc5e20ddcf29f97fac27213'
    or (select count(*) from jsonb_object_keys(new.compiler_metrics)) <> 18
    or new.compiler_metrics->>'schema' <> 'zekam-context-compiler-metrics/v2'
    or new.compiler_metrics_canonical <> core.canonical_jsonb(new.compiler_metrics)
    or 'sha256:' || encode(public.digest(convert_to(new.compiler_metrics_canonical, 'UTF8'), 'sha256'), 'hex')
       <> new.compiler_metrics_digest
    or new.manifest_canonical::jsonb->'selected' <> new.selected
    or new.manifest_canonical::jsonb->'omitted' <> new.omitted
    or new.manifest_canonical::jsonb->'compiler_metrics' <> new.compiler_metrics
    or (new.manifest_canonical::jsonb->>'token_budget')::integer <> new.token_budget
    or (new.manifest_canonical::jsonb->>'compiler_version')::integer <> new.compiler_version
    or new.manifest_canonical::jsonb->>'candidate_fingerprint' <> new.candidate_fingerprint
    or new.manifest_canonical::jsonb->>'scoring_policy_digest' <> new.scoring_policy_digest
    or new.manifest_canonical::jsonb->>'ranking_snapshot_digest' <> new.ranking_snapshot_digest
    or new.manifest_canonical::jsonb->>'candidate_set_digest' <> new.candidate_set_digest
    or (new.manifest_canonical::jsonb->>'created_at')::timestamptz <> new.created_at
    or new.manifest_canonical <> core.canonical_jsonb(new.manifest_canonical::jsonb)
    or (select count(*) from jsonb_object_keys(new.manifest_canonical::jsonb)) <> 15
    or 'sha256:' || encode(public.digest(convert_to(new.manifest_canonical, 'UTF8'), 'sha256'), 'hex')
       <> new.manifest_digest then
        raise exception 'context compiler v2 canonical digest/policy drift' using errcode = '23514';
    end if;
    if exists (
        select 1 from jsonb_array_elements(new.omitted) entry
        where entry->>'reason' not in (
            'budget-exhausted','stale','insufficient-authority','superseded',
            'recipe-excluded','duplicate','identity-mismatch','scope-mismatch',
            'source-revision-mismatch','conflict','role-mismatch','low-relevance'
        )
    ) then
        raise exception 'context omission reason registry disinda' using errcode = '23514';
    end if;
    if exists (
        select 1 from jsonb_array_elements(new.omitted) entry
        where entry->>'reason' = 'duplicate'
          and (entry->>'canonical_candidate_id' is null
               or entry->>'group_digest' not like 'sha256:%'
               or not exists (
                   select 1 from jsonb_array_elements(new.selected) selected_entry
                   where selected_entry->>'candidate_id' = entry->>'canonical_candidate_id'
               ))
    ) then
        raise exception 'context duplicate omission provenance eksik' using errcode = '23514';
    end if;
    if exists (
        select 1 from jsonb_array_elements(new.selected) entry
        where jsonb_typeof(entry->'score') <> 'array'
           or jsonb_array_length(entry->'score') <> 13
           or entry->'score'->>12 <> entry->>'candidate_id'
           or not (entry->'reason_codes' ? 'token-efficiency')
           or not (entry->'reason_codes' ? 'stable-id')
    ) or exists (
        select 1
        from jsonb_array_elements(new.selected) entry,
             jsonb_array_elements_text(entry->'reason_codes') reason
        where reason not in (
            'required','authority','exact-identity','scope-proximity','source-compatible',
            'evidence-strength','role-relevance','task-relevance','freshness',
            'conflict-penalty','duplicate-penalty','token-efficiency','stable-id'
        )
    ) then
        raise exception 'context selection score/reason provenance gecersiz' using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger context_manifest_v2_integrity
    before insert on work.context_manifest
    for each row execute function work.enforce_context_manifest_v2();

grant execute on function work.enforce_context_manifest_v2() to zekam_app;
