"""WP-08 additive local continuity schema (approved plan revision 2).

No historical database is a data source. This SQL is applied only after exact
local operational-v1 admission, or after constructing v1 in an empty database.
"""

SCHEMA_V2_SQL = """
create table continuity_session_binding (
    session_id text primary key references session(id),
    external_session_id text not null,
    project_id text not null references project(id),
    realm_id text not null,
    work_item_id text references work_item(id),
    run_id text references run(id),
    client_id text not null,
    device_id text not null,
    source_snapshot_id text not null references source_snapshot(id),
    task_digest text not null,
    plan_digest text not null,
    policy_digest text not null,
    binding_digest text not null unique,
    created_at text not null,
    check((work_item_id is null) = (run_id is null)),
    unique(client_id,device_id,external_session_id)
) strict;
create table session_event_detail (
    event_id text primary key references session_event(id),
    session_id text not null references continuity_session_binding(session_id),
    sequence integer not null check(sequence>0),
    previous_digest text,
    idempotency_key text not null,
    event_digest text not null unique,
    spool_digest text,
    body_json text not null check(json_valid(body_json) and length(body_json)<=16384),
    unique(session_id,sequence),
    unique(session_id,idempotency_key)
) strict;
create table continuity_effect_binding (
    claim_id text primary key references local_effect_claim(id),
    session_id text not null references continuity_session_binding(session_id),
    job_id text not null references local_job(id),
    binding_digest text not null unique
) strict;
create table continuity_checkpoint (
    checkpoint_digest text primary key,
    session_id text not null references continuity_session_binding(session_id),
    idempotency_key text not null,
    covered_sequence integer not null check(covered_sequence>0),
    covered_event_digest text not null references session_event_detail(event_digest),
    source_snapshot_id text not null references source_snapshot(id),
    context_digest text not null,
    spool_digest text,
    body_json text not null check(json_valid(body_json) and length(body_json)<=65536),
    created_at text not null,
    unique(session_id,idempotency_key)
) strict;
create table context_manifest (
    manifest_digest text primary key,
    session_id text not null references continuity_session_binding(session_id),
    checkpoint_digest text references continuity_checkpoint(checkpoint_digest),
    token_budget integer not null check(token_budget between 1 and 131072),
    token_count integer not null check(token_count>=0 and token_count<=token_budget),
    body_json text not null check(json_valid(body_json) and length(body_json)<=1048576),
    created_at text not null
) strict;
create table hydration_receipt (
    receipt_digest text primary key,
    session_id text not null references continuity_session_binding(session_id),
    manifest_digest text not null references context_manifest(manifest_digest),
    idempotency_key text not null,
    created_at text not null,
    unique(session_id,idempotency_key)
) strict;
create table continuity_close_request (
    request_digest text primary key,
    session_id text not null unique references continuity_session_binding(session_id),
    checkpoint_digest text not null references continuity_checkpoint(checkpoint_digest),
    covered_sequence integer not null check(covered_sequence>0),
    input_json text not null check(json_valid(input_json) and length(input_json)<=65536),
    created_at text not null
) strict;
create table continuity_outbox_binding (
    outbox_id text primary key references local_outbox(id),
    session_id text not null references continuity_session_binding(session_id),
    job_id text not null references local_job(id),
    purpose text not null check(purpose in ('close','checkpoint')),
    input_digest text not null,
    close_request_digest text references continuity_close_request(request_digest),
    unique(session_id,purpose,input_digest),
    check((purpose='close') = (close_request_digest is not null))
) strict;
create table close_receipt (
    receipt_digest text primary key,
    request_digest text not null unique references continuity_close_request(request_digest),
    session_id text not null unique references continuity_session_binding(session_id),
    checkpoint_digest text not null references continuity_checkpoint(checkpoint_digest),
    manifest_digest text not null references context_manifest(manifest_digest),
    outbox_id text not null unique references local_outbox_receipt(outbox_id),
    projections_json text not null check(json_valid(projections_json)),
    created_at text not null
) strict;
create trigger continuity_session_scope_guard before insert on continuity_session_binding
when not exists(
    select 1 from session s join source_snapshot ss on ss.id=new.source_snapshot_id
    join source_binding sb on sb.id=ss.source_binding_id
    join project_knowledge_realm pr on pr.project_id=new.project_id
    where s.id=new.session_id and s.status='open'
      and s.project_id=new.project_id and s.work_item_id is new.work_item_id
      and s.client_id=new.client_id and s.device_id=new.device_id
      and sb.project_id=new.project_id and sb.active=1 and pr.realm_id=new.realm_id
) or (new.work_item_id is not null and not exists(
    select 1 from work_item w join run r on r.work_item_id=w.id
    join config_revision c on c.id=r.config_revision_id
    where w.id=new.work_item_id and w.project_id=new.project_id and r.id=new.run_id
      and r.source_snapshot_id=new.source_snapshot_id and r.plan_digest=new.plan_digest
      and c.task_digest=new.task_digest
))
begin select raise(abort,'continuity exact owner/source scope required'); end;
create trigger continuity_session_owner_guard before insert on session
when new.work_item_id is not null and not exists(
    select 1 from work_item w where w.id=new.work_item_id and w.project_id=new.project_id
)
begin select raise(abort,'session exact work project required'); end;
create trigger continuity_session_owner_update_guard before update on session
when exists(select 1 from continuity_session_binding b where b.session_id=old.id)
 and (new.id is not old.id or new.project_id is not old.project_id
      or new.work_item_id is not old.work_item_id or new.client_id is not old.client_id
      or new.device_id is not old.device_id or new.opened_at is not old.opened_at)
begin select raise(abort,'continuity session owner immutable'); end;
create trigger continuity_session_close_update_guard before update on session
when exists(select 1 from continuity_session_binding b where b.session_id=old.id)
 and new.status='closed' and not exists(
     select 1 from close_receipt r where r.session_id=old.id
       and r.receipt_digest=new.close_receipt_digest
 )
begin select raise(abort,'continuity terminal close receipt required'); end;
create trigger continuity_event_chain_guard before insert on session_event_detail
when new.sequence <> coalesce((select max(sequence)+1 from session_event_detail
                              where session_id=new.session_id),1)
  or new.previous_digest is not (select event_digest from session_event_detail
       where session_id=new.session_id order by sequence desc limit 1)
  or not exists(select 1 from session_event e join session s on s.id=e.session_id
                where e.id=new.event_id and e.session_id=new.session_id
                  and e.event_digest=new.event_digest and s.status='open')
begin select raise(abort,'continuity event chain or session state mismatch'); end;
create trigger continuity_closed_event_guard before insert on session_event
when exists(select 1 from continuity_session_binding b where b.session_id=new.session_id)
 and (select status from session where id=new.session_id)<>'open'
begin select raise(abort,'continuity session delta frozen'); end;
create trigger continuity_effect_scope_guard before insert on continuity_effect_binding
when not exists(
    select 1 from local_effect_claim c join local_job j on j.id=c.job_id
    join continuity_session_binding b on b.session_id=new.session_id
    join session s on s.id=b.session_id
    join local_lease l on l.id=c.lease_id and l.job_id=c.job_id
    where c.id=new.claim_id and c.job_id=new.job_id and l.fencing_token=c.fencing_token
      and (b.run_id is not null or c.operation='continuity.compile')
      and json_extract(j.payload_json,'$.session_id')=b.session_id
      and json_extract(j.payload_json,'$.run_id') is b.run_id
      and json_extract(j.payload_json,'$.binding_digest')=b.binding_digest
      and (s.status='open' or (s.status='closing' and c.operation='continuity.compile'
        and exists(select 1 from continuity_close_request cr
          join continuity_outbox_binding ob on ob.close_request_digest=cr.request_digest
          where cr.session_id=b.session_id
            and cr.request_digest=json_extract(j.payload_json,'$.request_digest')
            and (ob.job_id=j.id or (
              json_extract(j.payload_json,'$.purpose')='repair-generated-candidates'
              and json_extract(j.payload_json,'$.original_job_id')=ob.job_id
            )))))
)
begin select raise(abort,'continuity exact effect scope required'); end;
create trigger continuity_closed_job_guard before insert on local_job
when exists(select 1 from continuity_session_binding b join session s on s.id=b.session_id
            where b.session_id=json_extract(new.payload_json,'$.session_id')
              and (s.status='closed' or (s.status='closing' and not (
                json_extract(new.payload_json,'$.operation') is 'continuity.compile'
                and json_extract(new.payload_json,'$.purpose') is 'repair-generated-candidates'
                and exists(select 1 from continuity_close_request cr
                  join continuity_outbox_binding ob on ob.close_request_digest=cr.request_digest
                  where cr.session_id=s.id
                    and cr.request_digest=json_extract(new.payload_json,'$.request_digest')
                    and ob.job_id=json_extract(new.payload_json,'$.original_job_id')
                    and b.binding_digest=json_extract(new.payload_json,'$.binding_digest'))
              ))))
begin select raise(abort,'continuity session job admission frozen'); end;
create trigger continuity_outbox_scope_guard before insert on continuity_outbox_binding
when not exists(
    select 1 from local_outbox o join local_job j on j.id=o.job_id
    join continuity_session_binding b on b.session_id=new.session_id
    where o.id=new.outbox_id and o.job_id=new.job_id
      and json_extract(j.payload_json,'$.session_id')=b.session_id
      and json_extract(j.payload_json,'$.binding_digest')=b.binding_digest
) or (new.close_request_digest is not null and not exists(
    select 1 from continuity_close_request c where c.request_digest=new.close_request_digest
      and c.session_id=new.session_id and c.request_digest=new.input_digest
))
begin select raise(abort,'continuity exact outbox scope required'); end;
create trigger continuity_checkpoint_scope_guard before insert on continuity_checkpoint
when not exists(
    select 1 from session_event_detail e join continuity_session_binding b
      on b.session_id=e.session_id
    where e.session_id=new.session_id and e.sequence=new.covered_sequence
      and e.event_digest=new.covered_event_digest
      and b.source_snapshot_id=new.source_snapshot_id
)
begin select raise(abort,'continuity checkpoint scope mismatch'); end;
create trigger continuity_manifest_scope_guard before insert on context_manifest
when new.checkpoint_digest is not null and not exists(
    select 1 from continuity_checkpoint c where c.checkpoint_digest=new.checkpoint_digest
      and c.session_id=new.session_id
)
begin select raise(abort,'continuity context checkpoint scope mismatch'); end;
create trigger continuity_hydration_scope_guard before insert on hydration_receipt
when not exists(select 1 from context_manifest m where m.manifest_digest=new.manifest_digest
                and m.session_id=new.session_id)
begin select raise(abort,'continuity hydration scope mismatch'); end;
create trigger continuity_close_scope_guard before insert on continuity_close_request
when not exists(select 1 from continuity_checkpoint c
    where c.checkpoint_digest=new.checkpoint_digest and c.session_id=new.session_id
      and c.covered_sequence=new.covered_sequence)
begin select raise(abort,'continuity close checkpoint scope mismatch'); end;
create trigger continuity_close_receipt_guard before insert on close_receipt
when not exists(
    select 1 from continuity_close_request c join context_manifest m
      on m.session_id=c.session_id
    join continuity_outbox_binding b on b.close_request_digest=c.request_digest
    join local_outbox_receipt r on r.outbox_id=b.outbox_id
    where c.request_digest=new.request_digest and c.session_id=new.session_id
      and c.checkpoint_digest=new.checkpoint_digest and m.manifest_digest=new.manifest_digest
      and b.outbox_id=new.outbox_id
      and exists(select 1 from local_outbox_delivery d
                 where d.outbox_id=r.outbox_id and d.state='delivered')
      and (r.status='delivered' or (r.status='unknown' and exists(
          select 1 from local_recovery_case rc join local_recovery_resolution rr
            on rr.recovery_case_id=rc.id
          where rc.outbox_id=r.outbox_id and rc.state='resolved' and rr.outcome='delivered'
      )))
)
begin select raise(abort,'continuity terminal close evidence required'); end;
"""

# Admission must precede execution, not merely reject a later optional effect binding.
# Session-free runtime jobs are unaffected; a frozen session only admits its exact
# original close compile or an explicitly created repair for that same request.
for _table in ("local_lease", "local_effect_claim"):
    SCHEMA_V2_SQL += f"""
create trigger continuity_{_table}_admission before insert on {_table}
when exists(select 1 from local_job j
    join continuity_session_binding b on b.session_id=json_extract(j.payload_json,'$.session_id')
    join session s on s.id=b.session_id
    where j.id=new.job_id and (s.status='closed' or (s.status='closing' and not (
        json_extract(j.payload_json,'$.operation') is 'continuity.compile'
        and json_extract(j.payload_json,'$.binding_digest') is b.binding_digest
        and exists(select 1 from continuity_close_request cr
          join continuity_outbox_binding ob on ob.close_request_digest=cr.request_digest
          where cr.session_id=s.id
            and cr.request_digest=json_extract(j.payload_json,'$.request_digest')
            and (ob.job_id=j.id or (
              json_extract(j.payload_json,'$.purpose')='repair-generated-candidates'
              and json_extract(j.payload_json,'$.original_job_id')=ob.job_id
            )))
    ))))
begin select raise(abort,'continuity execution admission frozen'); end;
"""

for _table in (
    "continuity_session_binding",
    "session_event_detail",
    "continuity_effect_binding",
    "continuity_checkpoint",
    "context_manifest",
    "hydration_receipt",
    "continuity_close_request",
    "continuity_outbox_binding",
    "close_receipt",
):
    for _action in ("update", "delete"):
        SCHEMA_V2_SQL += (
            f"create trigger {_table}_immutable_{_action} before {_action} on {_table}\n"
            f"begin select raise(abort,'{_table} immutable'); end;\n"
        )
