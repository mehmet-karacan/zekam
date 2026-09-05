-- Realm-scoped, read-only causal projection and operational orphan detector.
-- Canonical rows stay in their owning bounded contexts; these views only derive
-- sanitized identities, states and timestamps for diagnosis.

create view ops.causal_node with (security_invoker=true) as
select realm_id, 'work:'||id::text node_id, 'work' kind, state,
       created_at occurred_at, 'db:work.work_item/'||id::text canonical_ref,
       id work_item_id, null::uuid job_id
from work.work_item
union all
select realm_id, 'run:'||id::text, 'run', state, created_at,
       'db:runtime.execution_run/'||id::text, work_item_id, null::uuid
from runtime.execution_run
union all
select p.realm_id, 'plan-step:'||p.id::text||':'||(s.value->>'step_id'),
       'plan-step', coalesce(s.value->>'state','defined'), p.created_at,
       'db:work.task_plan/'||p.id::text||'#step='||(s.value->>'step_id'),
       p.work_item_id, null::uuid
from work.task_plan p cross join lateral jsonb_array_elements(p.steps) s(value)
where nullif(btrim(s.value->>'step_id'),'') is not null
union all
select realm_id, 'job:'||id::text, 'job', state, created_at,
       'db:runtime.job/'||id::text, work_item_id, id
from runtime.job
union all
select a.realm_id, 'job-attempt:'||a.id::text, 'job-attempt',
       coalesce(a.outcome,'running'), a.started_at,
       'db:runtime.job_attempt/'||a.id::text, j.work_item_id, a.job_id
from runtime.job_attempt a join runtime.job j
  on j.realm_id=a.realm_id and j.id=a.job_id
union all
select a.realm_id, 'assignment:'||a.id::text, 'assignment', a.status, a.created_at,
       'db:agents.assignment/'||a.id::text, a.work_item_id, null::uuid
from agents.assignment a
union all
select i.realm_id, 'invocation:'||i.id::text, 'agent-invocation',
       case when rr.invocation_id is null then 'dispatched' else 'received' end,
       i.created_at, 'db:agents.invocation/'||i.id::text, a.work_item_id, null::uuid
from agents.invocation i
join agents.assignment a on a.realm_id=i.realm_id and a.id=i.assignment_id
left join agents.result_receipt rr on rr.realm_id=i.realm_id and rr.invocation_id=i.id
union all
select rr.realm_id, 'agent-result:'||rr.invocation_id::text,
       case when a.role='verifier' then 'verifier-result' else 'agent-result' end, 'received',
       rr.created_at, 'db:agents.result_receipt/'||rr.invocation_id::text,
       a.work_item_id, null::uuid
from agents.result_receipt rr join agents.assignment a
  on a.realm_id=rr.realm_id and a.id=rr.assignment_id
union all
select c.realm_id, 'context:'||c.id::text, 'context',
       case when jsonb_array_length(c.omitted)>0 then 'compiled-with-omissions' else 'compiled' end,
       c.created_at, 'db:work.context_manifest/'||c.id::text, c.work_item_id, null::uuid
from work.context_manifest c
union all
select m.realm_id, 'route:'||m.id::text, 'route',
       case when m.route_decision_digest is null then 'missing' else 'bound' end,
       m.created_at, 'db:models.model_route_decision?evidence_digest='||
         coalesce(m.route_decision_digest,'missing'), m.work_item_id, m.job_id
from models.request_manifest m
union all
select m.realm_id, 'model-manifest:'||m.id::text, 'model-request', m.binding_status,
       m.created_at, 'db:models.request_manifest/'||m.id::text, m.work_item_id, m.job_id
from models.request_manifest m
union all
select a.realm_id, 'model-attempt:'||a.id::text, 'model-attempt', a.state, a.created_at,
       'db:models.invocation_attempt/'||a.id::text, m.work_item_id, m.job_id
from models.invocation_attempt a join models.request_manifest m
  on m.realm_id=a.realm_id and m.id=a.manifest_id
union all
select r.realm_id, 'model-result:'||r.id::text, 'model-result', r.state, r.created_at,
       'db:models.invocation_result/'||r.id::text, m.work_item_id, m.job_id
from models.invocation_result r join models.request_manifest m
  on m.realm_id=r.realm_id and m.id=r.manifest_id
union all
select c.realm_id, 'effect-claim:'||c.id::text, 'effect-claim',
       case when r.id is null then 'open' else r.status end, c.claimed_at,
       'db:runtime.effect_claim/'||c.id::text, j.work_item_id, c.job_id
from runtime.effect_claim c join runtime.job j
  on j.realm_id=c.realm_id and j.id=c.job_id
left join runtime.effect_receipt r on r.realm_id=c.realm_id and r.claim_id=c.id
union all
select r.realm_id, 'effect-receipt:'||r.id::text, 'effect-receipt', r.status,
       r.completed_at, 'db:runtime.effect_receipt/'||r.id::text, j.work_item_id, c.job_id
from runtime.effect_receipt r join runtime.effect_claim c
  on c.realm_id=r.realm_id and c.id=r.claim_id
join runtime.job j on j.realm_id=c.realm_id and j.id=c.job_id
union all
select c.realm_id, 'checkpoint:'||c.id::text, 'checkpoint', c.resumability,
       c.created_at, 'db:work.checkpoint_v2/'||c.id::text, c.work_item_id, c.job_id
from work.checkpoint_v2 c
union all
select e.realm_id, 'execution-event:'||e.id::text, 'execution-event', e.event_type,
       e.occurred_at, 'db:runtime.execution_event/'||e.id::text,
       j.work_item_id, e.job_id
from runtime.execution_event e left join runtime.job j
  on j.realm_id=e.realm_id and j.id=e.job_id
union all
select o.realm_id, 'outbox:'||o.id::text, 'outbox-event',
       case when o.published_at is null then 'pending' else 'published' end,
       o.created_at, 'db:runtime.outbox_event/'||o.id::text,
       j.work_item_id, o.job_id
from runtime.outbox_event o left join runtime.job j
  on j.realm_id=o.realm_id and j.id=o.job_id
union all
select c.realm_id, 'memory-candidate:'||c.id::text, 'memory-candidate',
       case when c.reviewed then 'reviewed' else 'candidate' end, c.created_at,
       'db:memory.candidate/'||c.id::text, c.work_item_id, null::uuid
from memory.candidate c;

create view ops.causal_edge with (security_invoker=true) as
select realm_id, 'work:'||work_item_id::text source_node_id,
       'run:'||id::text target_node_id, 'started-run' kind
from runtime.execution_run
union all
select p.realm_id, 'work:'||p.work_item_id::text,
       'plan-step:'||p.id::text||':'||(s.value->>'step_id'), 'planned-step'
from work.task_plan p cross join lateral jsonb_array_elements(p.steps) s(value)
where nullif(btrim(s.value->>'step_id'),'') is not null
union all
select a.realm_id, 'plan-step:'||a.plan_id::text||':'||a.step_id,
       'assignment:'||a.id::text, 'assigned-step'
from agents.assignment a where a.plan_id is not null and a.step_id is not null
union all
select j.realm_id, 'plan-step:'||j.plan_id::text||':'||j.step_id,
       'job:'||j.id::text, 'scheduled-step-job'
from runtime.job j where j.plan_id is not null and j.step_id is not null
union all
select realm_id, 'work:'||work_item_id::text, 'job:'||id::text, 'scheduled-job'
from runtime.job where work_item_id is not null
union all
select realm_id, 'run:'||run_id::text, 'job:'||id::text, 'owns-job'
from runtime.job where run_id is not null
union all
select realm_id, 'job:'||job_id::text, 'job-attempt:'||id::text, 'started-attempt'
from runtime.job_attempt
union all
select realm_id, 'assignment:'||assignment_id::text, 'job:'||id::text, 'assigned-job'
from runtime.job where assignment_id is not null
union all
select realm_id, 'assignment:'||assignment_id::text, 'invocation:'||id::text,
       'dispatched-agent' from agents.invocation
union all
select realm_id, 'invocation:'||invocation_id::text,
       'agent-result:'||invocation_id::text, 'received-agent-result'
from agents.result_receipt
union all
select i.realm_id, 'invocation:'||i.id::text,
       'assignment:'||a.parent_assignment_id::text, 'reports-to-coordinator'
from agents.invocation i join agents.assignment a
  on a.realm_id=i.realm_id and a.id=i.assignment_id
where a.parent_assignment_id is not null
union all
select realm_id, 'job:'||job_id::text, 'model-manifest:'||id::text, 'requested-model'
from models.request_manifest
union all
select m.realm_id, 'plan-step:'||m.plan_id::text||':'||m.step_id,
       'context:'||c.id::text, 'compiled-context'
from models.request_manifest m join work.context_manifest c
  on c.realm_id=m.realm_id and c.manifest_digest=m.context_manifest_digest
where m.context_manifest_digest is not null
union all
select m.realm_id, 'context:'||c.id::text, 'route:'||m.id::text, 'informed-route'
from models.request_manifest m join work.context_manifest c
  on c.realm_id=m.realm_id and c.manifest_digest=m.context_manifest_digest
where m.context_manifest_digest is not null
union all
select realm_id, 'route:'||id::text, 'model-manifest:'||id::text, 'selected-invocation'
from models.request_manifest
union all
select realm_id, 'model-manifest:'||manifest_id::text, 'model-attempt:'||id::text,
       'attempted-model' from models.invocation_attempt
union all
select realm_id, 'model-attempt:'||attempt_id::text, 'model-result:'||id::text,
       'received-model-result' from models.invocation_result
union all
select a.realm_id, 'model-attempt:'||a.id::text, 'effect-claim:'||a.effect_claim_id::text,
       'claimed-provider-effect' from models.invocation_attempt a
where a.effect_claim_id is not null
union all
select realm_id, 'job:'||job_id::text, 'effect-claim:'||id::text, 'claimed-effect'
from runtime.effect_claim
union all
select r.realm_id, 'effect-claim:'||r.claim_id::text, 'effect-receipt:'||r.id::text,
       'completed-effect' from runtime.effect_receipt r
union all
select realm_id, 'job:'||job_id::text, 'checkpoint:'||id::text, 'checkpointed-job'
from work.checkpoint_v2
union all
select v.realm_id, 'agent-result:'||v.verifier_invocation_id::text,
       'checkpoint:'||v.checkpoint_id::text, 'verified-checkpoint-step'
from work.checkpoint_v2_step_verification v
union all
select r.realm_id, 'effect-receipt:'||r.receipt_id::text,
       'agent-result:'||v.verifier_invocation_id::text, 'verified-step-effect'
from work.checkpoint_v2_step_receipt r
join work.checkpoint_v2_step_verification v
  on v.realm_id=r.realm_id and v.checkpoint_id=r.checkpoint_id and v.step_id=r.step_id
union all
select c.realm_id, 'checkpoint:'||checkpoint.id::text, 'memory-candidate:'||c.id::text,
       'evidenced-memory-candidate'
from memory.candidate c cross join lateral jsonb_array_elements(c.evidence) evidence(value)
join work.checkpoint_v2 checkpoint
  on checkpoint.realm_id=c.realm_id
 and evidence.value->>'reference'='db:work.checkpoint_v2/'||checkpoint.id::text
union all
select c.realm_id, 'effect-receipt:'||receipt.id::text, 'memory-candidate:'||c.id::text,
       'evidenced-memory-candidate'
from memory.candidate c cross join lateral jsonb_array_elements(c.evidence) evidence(value)
join runtime.effect_receipt receipt
  on receipt.realm_id=c.realm_id
 and evidence.value->>'reference'='db:runtime.effect_receipt/'||receipt.id::text
union all
select realm_id, 'job:'||job_id::text, 'execution-event:'||id::text, 'emitted-event'
from runtime.execution_event where job_id is not null
union all
select realm_id, 'job:'||job_id::text, 'outbox:'||id::text, 'queued-outbox-event'
from runtime.outbox_event where job_id is not null;

create view ops.causal_orphan with (security_invoker=true) as
select j.realm_id, 'running-job-without-live-lease' orphan_kind, 'critical' severity,
       'job:'||j.id::text node_id, 'db:runtime.job/'||j.id::text canonical_ref,
       j.work_item_id, j.id job_id, j.updated_at observed_at,
       'running job has no unexpired exact-fence lease' reason
from runtime.job j left join runtime.lease l
  on l.realm_id=j.realm_id and l.job_id=j.id and l.fencing_token=j.fencing_token
     and l.expires_at>statement_timestamp()
where j.state='running' and l.id is null
union all
select c.realm_id, 'claim-without-terminal-receipt', 'high',
       'effect-claim:'||c.id::text, 'db:runtime.effect_claim/'||c.id::text,
       j.work_item_id, c.job_id, c.claimed_at,
       'effect claim exceeded grace period without terminal receipt'
from runtime.effect_claim c join runtime.job j
  on j.realm_id=c.realm_id and j.id=c.job_id
left join runtime.effect_receipt r on r.realm_id=c.realm_id and r.claim_id=c.id
where r.id is null and c.claimed_at<statement_timestamp()-interval '2 minutes'
union all
select i.realm_id, 'agent-invocation-without-result', 'high',
       'invocation:'||i.id::text, 'db:agents.invocation/'||i.id::text,
       a.work_item_id, j.id, i.created_at,
       'agent invocation exceeded grace period without structural result receipt'
from agents.invocation i join agents.assignment a
  on a.realm_id=i.realm_id and a.id=i.assignment_id
left join agents.result_receipt rr on rr.realm_id=i.realm_id and rr.invocation_id=i.id
left join runtime.job j on j.realm_id=a.realm_id and j.assignment_id=a.id
where rr.invocation_id is null and i.created_at<statement_timestamp()-interval '5 minutes'
union all
select ma.realm_id, 'model-attempt-without-result', 'high',
       'model-attempt:'||ma.id::text, 'db:models.invocation_attempt/'||ma.id::text,
       m.work_item_id, m.job_id, ma.created_at,
       'sent model attempt exceeded grace period without canonical invocation result'
from models.invocation_attempt ma join models.request_manifest m
  on m.realm_id=ma.realm_id and m.id=ma.manifest_id
left join models.invocation_result mr on mr.realm_id=ma.realm_id and mr.attempt_id=ma.id
where ma.state in ('sent','response-received','parsed') and mr.id is null
  and ma.created_at<statement_timestamp()-interval '5 minutes'
union all
select e.realm_id, 'lifecycle-event-without-ack', 'medium',
       'lifecycle-event:'||e.id::text, 'db:client.lifecycle_event/'||e.id::text,
       null::uuid, null::uuid, e.ingested_at,
       'ingested client lifecycle event exceeded grace period without canonical ack'
from client.lifecycle_event e left join client.lifecycle_ack a
  on a.realm_id=e.realm_id and a.event_id=e.id
where a.id is null and e.ingested_at<statement_timestamp()-interval '2 minutes'
union all
select la.realm_id, 'loop-attempt-without-outcome', 'high',
       'loop-attempt:'||la.id::text, 'db:runtime.loop_attempt/'||la.id::text,
       lp.work_item_id, null::uuid, la.admitted_at,
       'admitted bounded-loop attempt exceeded grace period without outcome or terminal'
from runtime.loop_attempt la join runtime.loop_policy lp
  on lp.realm_id=la.realm_id and lp.id=la.loop_id
left join runtime.loop_attempt_outcome lo on lo.realm_id=la.realm_id and lo.attempt_id=la.id
left join runtime.loop_terminal lt on lt.realm_id=la.realm_id and lt.loop_id=la.loop_id
where lo.id is null and lt.id is null
  and la.admitted_at<least(lp.deadline,statement_timestamp()-interval '5 minutes')
union all
select o.realm_id, 'outbox-event-not-published', 'medium',
       'outbox:'||o.id::text, 'db:runtime.outbox_event/'||o.id::text,
       j.work_item_id, o.job_id, o.created_at,
       'transactional outbox event exceeded grace period without publication'
from runtime.outbox_event o left join runtime.job j
  on j.realm_id=o.realm_id and j.id=o.job_id
where o.published_at is null and o.created_at<statement_timestamp()-interval '2 minutes'
union all
select r.realm_id, 'execution-run-deadline-exceeded', 'critical',
       'run:'||r.id::text, 'db:runtime.execution_run/'||r.id::text,
       r.work_item_id, j.id, r.deadline,
       'active execution run exceeded its canonical deadline without terminal transition'
from runtime.execution_run r left join runtime.job j
  on j.realm_id=r.realm_id and j.run_id=r.id and j.state='running'
where r.state='active' and r.deadline<statement_timestamp()
union all
select e.realm_id, 'resume-apply-without-terminal', 'high',
       'resume-apply:'||e.resume_apply_id::text,
       'db:runtime.resume_apply_event/'||e.id::text,
       a.work_item_id, a.job_id, e.occurred_at,
       'latest resume apply transition exceeded grace period without terminal receipt state'
from runtime.resume_apply_event e join runtime.resume_apply a
  on a.realm_id=e.realm_id and a.id=e.resume_apply_id
where e.sequence=(select max(x.sequence) from runtime.resume_apply_event x
                  where x.realm_id=e.realm_id and x.resume_apply_id=e.resume_apply_id)
  and e.state in ('claimed','dispatched')
  and e.occurred_at<statement_timestamp()-interval '2 minutes'
union all
select j.realm_id, 'completed-agentic-job-without-checkpoint', 'critical',
       'job:'||j.id::text, 'db:runtime.job/'||j.id::text,
       j.work_item_id, j.id, j.updated_at,
       'completed assignment-bound job has no structural checkpoint v2'
from runtime.job j
where j.state='completed' and j.assignment_id is not null
  and not exists(select 1 from work.checkpoint_v2 c
                 where c.realm_id=j.realm_id and c.job_id=j.id)
union all
select j.realm_id, 'completed-agentic-job-without-verified-result', 'critical',
       'job:'||j.id::text, 'db:runtime.job/'||j.id::text,
       j.work_item_id, j.id, j.updated_at,
       'completed assignment-bound job has no canonical independent verified result'
from runtime.job j
where j.state='completed' and j.assignment_id is not null
  and not exists(
    select 1 from models.request_manifest m
    join models.invocation_result r on r.realm_id=m.realm_id and r.manifest_id=m.id
    join agents.assignment verifier
      on verifier.realm_id=m.realm_id and verifier.id=m.assignment_id
    join agents.assignment assigned
      on assigned.realm_id=j.realm_id and assigned.id=j.assignment_id
    where m.realm_id=j.realm_id and m.job_id=j.id and r.state='verified'
      and r.envelope_digest is not null and m.role='verifier'
      and verifier.role='verifier' and verifier.agent_ref<>assigned.agent_ref
      and row(verifier.project_id,verifier.work_item_id,verifier.plan_id,verifier.step_id)
        is not distinct from row(assigned.project_id,assigned.work_item_id,
                                 assigned.plan_id,assigned.step_id)
  )
  and not exists(
    select 1 from work.checkpoint_v2 c
    join work.checkpoint_v2_step_verification v
      on v.realm_id=c.realm_id and v.checkpoint_id=c.id
    join agents.invocation i
      on i.realm_id=v.realm_id and i.id=v.verifier_invocation_id
    join agents.result_receipt rr
      on rr.realm_id=i.realm_id and rr.invocation_id=i.id
    where c.realm_id=j.realm_id and c.job_id=j.id
  );

create function ops.causal_chain(p_work_item_id uuid, p_limit integer default 256)
returns table(record_type text,node_id text,source_node_id text,target_node_id text,
              kind text,state text,occurred_at timestamptz,canonical_ref text,
              truncated boolean)
language plpgsql stable security invoker
set search_path=pg_catalog,ops,core as $$
begin
  if p_limit<1 or p_limit>512 then
    raise exception 'causal chain limit 1..512 olmali' using errcode='22023';
  end if;
  if not exists(select 1 from ops.causal_node n where n.work_item_id=p_work_item_id) then
    return;
  end if;
  return query
    with work_nodes as materialized (
      select n.* from ops.causal_node n where n.work_item_id=p_work_item_id
    ), selected_nodes as materialized (
      select n.* from work_nodes n order by n.occurred_at,n.node_id
      limit greatest(1,(p_limit+1)/2)
    ), eligible_edges as materialized (
      select e.* from ops.causal_edge e
      where e.source_node_id in (select n.node_id from selected_nodes n)
        and e.target_node_id in (select n.node_id from selected_nodes n)
    ), combined as materialized (
      select 0 sort_group,n.node_id sort_key,'node' record_type,n.node_id,
             null::text source_node_id,null::text target_node_id,n.kind,n.state,
             n.occurred_at,n.canonical_ref
      from selected_nodes n
      union all
      select 1,e.source_node_id||':'||e.target_node_id||':'||e.kind,'edge',null::text,
             e.source_node_id,e.target_node_id,e.kind,null::text,null::timestamptz,null::text
      from eligible_edges e
    ), bounds as (
      select (select count(*) from work_nodes)>(select count(*) from selected_nodes)
          or (select count(*) from combined)>p_limit is_truncated
    )
    select c.record_type,c.node_id,c.source_node_id,c.target_node_id,c.kind,c.state,
           c.occurred_at,c.canonical_ref,b.is_truncated
    from combined c cross join bounds b
    order by c.sort_group,c.sort_key
    limit p_limit;
end $$;

comment on view ops.causal_node is 'Kanonik kimliklerden turetilen realm-scoped nedensellik dugumleri; authority degildir.';
comment on view ops.causal_edge is 'Exact FK baglarindan turetilen nedensellik kenarlari; authority degildir.';
comment on view ops.causal_orphan is 'Gecikme esigini asmis yapisal kanit bosluklari; salt okunur operasyon alarmidir.';

grant select on ops.causal_node,ops.causal_edge,ops.causal_orphan to zekam_app;
grant execute on function ops.causal_chain(uuid,integer) to zekam_app;
