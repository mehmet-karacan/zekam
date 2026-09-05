"""Additive v3 control observations; the v2 ordinary event chain stays immutable."""

SCHEMA_V3_SQL = """
create table continuity_control_event (
    control_digest text primary key not null,
    session_id text not null references continuity_session_binding(session_id),
    binding_digest text not null,
    request_digest text not null references continuity_close_request(request_digest),
    client_id text not null,
    device_id text not null,
    external_session_id text not null,
    spool_digest text not null,
    observation_digest text not null,
    delivery_id text not null,
    spool_sequence integer not null check(spool_sequence>0),
    previous_spool_digest text,
    external_event_type text not null,
    internal_event_type text not null,
    disposition text not null check(disposition in ('advisory-post-close','rejected-after-freeze')),
    body_json text not null check(json_valid(body_json) and json_type(body_json)='object'
                                  and length(cast(body_json as blob))<=32768),
    created_at text not null,
    unique(session_id,spool_digest),
    unique(session_id,delivery_id),
    unique(session_id,spool_sequence),
    check((disposition='advisory-post-close')=(internal_event_type='post_close'))
) strict;
create trigger continuity_control_scope_guard before insert on continuity_control_event
when not exists(
    select 1 from continuity_session_binding b join session s on s.id=b.session_id
    join continuity_close_request c on c.session_id=s.id
    join continuity_checkpoint p on p.checkpoint_digest=c.checkpoint_digest
    where b.session_id=new.session_id and b.binding_digest=new.binding_digest
      and b.client_id=new.client_id and b.device_id=new.device_id
      and b.external_session_id=new.external_session_id
      and s.status in ('closing','closed') and c.request_digest=new.request_digest
      and p.session_id=b.session_id and p.covered_sequence=c.covered_sequence
      and new.spool_sequence > (select count(*) from session_event_detail e
          where e.session_id=b.session_id and e.spool_digest is not null)
)
or exists(select 1 from session_event_detail e where e.session_id=new.session_id
           and (e.spool_digest=new.spool_digest or e.idempotency_key=new.delivery_id))
begin select raise(abort,'continuity control exact frozen scope required'); end;
create trigger continuity_control_chain_guard before insert on continuity_control_event
when new.spool_sequence <> (
    (select count(*) from session_event_detail where session_id=new.session_id
       and spool_digest is not null)
    + (select count(*) from continuity_control_event where session_id=new.session_id) + 1)
or new.previous_spool_digest is not coalesce(
    (select spool_digest from continuity_control_event where session_id=new.session_id
      order by spool_sequence desc limit 1),
    (select spool_digest from session_event_detail where session_id=new.session_id
      and spool_digest is not null order by sequence desc limit 1))
begin select raise(abort,'continuity control contiguous source chain required'); end;
create trigger continuity_control_body_guard before insert on continuity_control_event
when json_extract(new.body_json,'$.session_id') is not new.session_id
  or json_extract(new.body_json,'$.binding_digest') is not new.binding_digest
  or json_extract(new.body_json,'$.request_digest') is not new.request_digest
  or json_extract(new.body_json,'$.client_id') is not new.client_id
  or json_extract(new.body_json,'$.device_id') is not new.device_id
  or json_extract(new.body_json,'$.external_session_id') is not new.external_session_id
  or json_extract(new.body_json,'$.spool_digest') is not new.spool_digest
  or json_extract(new.body_json,'$.observation_digest') is not new.observation_digest
  or json_extract(new.body_json,'$.delivery_id') is not new.delivery_id
  or json_extract(new.body_json,'$.spool_sequence') is not new.spool_sequence
  or json_extract(new.body_json,'$.previous_spool_digest') is not new.previous_spool_digest
  or json_extract(new.body_json,'$.external_event_type') is not new.external_event_type
  or json_extract(new.body_json,'$.internal_event_type') is not new.internal_event_type
  or json_extract(new.body_json,'$.disposition') is not new.disposition
  or json_extract(new.body_json,'$.created_at') is not new.created_at
  or json_type(new.body_json,'$.grants_authority') is not 'false'
  or json_type(new.body_json,'$.approval_inherited') is not 'false'
begin select raise(abort,'continuity control body/column parity required'); end;
create trigger continuity_control_event_no_update before update on continuity_control_event
begin select raise(abort,'continuity control evidence immutable'); end;
create trigger continuity_control_event_no_delete before delete on continuity_control_event
begin select raise(abort,'continuity control evidence immutable'); end;
"""
