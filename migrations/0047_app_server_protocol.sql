-- Durable App Server v1 notification stream ve reconnect replay cursor'u.

create schema if not exists app_server;
grant usage on schema app_server to zekam_app;

create table app_server.notification_stream (
  realm_id uuid primary key references core.realm(id) on delete restrict,
  head_sequence bigint not null default 0,
  head_digest text,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  check(head_sequence>=0),
  check((head_sequence=0)=(head_digest is null)),
  check(head_digest is null or head_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table app_server.notification_event (
  id uuid primary key,
  realm_id uuid not null,
  sequence bigint not null,
  previous_digest text,
  event_type text not null,
  payload jsonb not null,
  payload_digest text not null,
  event_body jsonb not null,
  event_digest text not null,
  occurred_at timestamptz not null,
  grants_authority boolean not null default false,
  foreign key(realm_id) references app_server.notification_stream(realm_id) on delete restrict,
  unique(realm_id,id),unique(realm_id,sequence),unique(realm_id,event_digest),
  check(sequence>0),
  check((sequence=1)=(previous_digest is null)),
  check(event_type ~ '^[a-z][a-z0-9_.\-/]{0,127}$'),
  check(jsonb_typeof(payload)='object' and jsonb_typeof(event_body)='object'
    and octet_length(payload::text)<=65536 and octet_length(event_body::text)<=69632),
  check(payload_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(previous_digest is null or previous_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(event_digest ~ '^sha256:[0-9a-f]{64}$'),
  check(not grants_authority)
);

create function app_server.publish_notification(
  p_realm_id uuid,p_event_id uuid,p_event_type text,p_payload jsonb,p_occurred_at timestamptz
) returns boolean language plpgsql security invoker
set search_path=pg_catalog,app_server,models,core as $$
declare existing record;
declare stream record;
declare body jsonb;
declare body_digest text;
declare occurred_text text;
begin
  if p_realm_id is distinct from core.current_realm_id() then
    raise exception 'app notification cross-realm publish denied' using errcode='42501';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_realm_id::text||':'||p_event_id::text,0));
  select event_type,payload into existing from app_server.notification_event
    where realm_id=p_realm_id and id=p_event_id;
  if found then
    if row(existing.event_type,existing.payload) is distinct from row(p_event_type,p_payload) then
      raise exception 'app notification event replay payload drift' using errcode='40001';
    end if;
    return false;
  end if;
  insert into app_server.notification_stream
    (realm_id,head_sequence,head_digest,created_at,updated_at)
    values(p_realm_id,0,null,p_occurred_at,p_occurred_at) on conflict(realm_id) do nothing;
  select head_sequence,head_digest into strict stream from app_server.notification_stream
    where realm_id=p_realm_id for update;
  occurred_text := to_jsonb(p_occurred_at)#>>'{}';
  body := jsonb_build_object(
    'schema','zekam-app-notification/v1','event_id',p_event_id::text,
    'sequence',stream.head_sequence+1,'previous_digest',stream.head_digest,
    'event_type',p_event_type,'payload',p_payload,'occurred_at',occurred_text,
    'grants_authority',false
  );
  body_digest := models.capability_runtime_jsonb_digest(body);
  insert into app_server.notification_event
    (id,realm_id,sequence,previous_digest,event_type,payload,payload_digest,event_body,
     event_digest,occurred_at,grants_authority)
    values(p_event_id,p_realm_id,stream.head_sequence+1,stream.head_digest,p_event_type,p_payload,
      models.capability_runtime_jsonb_digest(p_payload),body,body_digest,p_occurred_at,false);
  return true;
end $$;

create function app_server.enforce_notification_event() returns trigger
language plpgsql security invoker set search_path=pg_catalog,app_server,models,core as $$
declare stream record;
begin
  select head_sequence,head_digest into strict stream
    from app_server.notification_stream where realm_id=new.realm_id for update;
  if row(new.sequence,new.previous_digest) is distinct from
      row(stream.head_sequence+1,stream.head_digest) then
    raise exception 'app notification stream head mismatch' using errcode='40001';
  end if;
  if new.payload_digest is distinct from models.capability_runtime_jsonb_digest(new.payload)
    or new.event_digest is distinct from models.capability_runtime_jsonb_digest(new.event_body)
    or row(new.id,new.sequence,new.event_type,new.payload,new.occurred_at,new.grants_authority)
       is distinct from row(
         (new.event_body->>'event_id')::uuid,
         (new.event_body->>'sequence')::bigint,
         new.event_body->>'event_type',new.event_body->'payload',
         (new.event_body->>'occurred_at')::timestamptz,
         (new.event_body->>'grants_authority')::boolean)
    or new.previous_digest is distinct from new.event_body->>'previous_digest'
    or new.event_body->>'schema'<>'zekam-app-notification/v1' then
    raise exception 'app notification canonical body mismatch' using errcode='23514';
  end if;
  update app_server.notification_stream
     set head_sequence=new.sequence,head_digest=new.event_digest,updated_at=new.occurred_at
   where realm_id=new.realm_id;
  return new;
end $$;

create function app_server.publish_work_item() returns trigger language plpgsql
security invoker set search_path=pg_catalog,app_server as $$
begin
  perform app_server.publish_notification(
    new.realm_id,gen_random_uuid(),
    case when tg_op='INSERT' then 'work.item.created' else 'work.item.updated' end,
    jsonb_build_object('work_item_id',new.id::text,'project_id',new.project_id::text,
      'external_number',new.external_number,'type',new.type,'state',new.state,
      'revision',new.revision,'record_digest',new.record_digest),new.updated_at);
  return new;
end $$;

create function app_server.publish_execution_run() returns trigger language plpgsql
security invoker set search_path=pg_catalog,app_server as $$
begin
  perform app_server.publish_notification(
    new.realm_id,gen_random_uuid(),
    case when tg_op='INSERT' then 'session.run.created' else 'session.run.updated' end,
    jsonb_build_object('run_id',new.id::text,'project_id',new.project_id::text,
      'work_item_id',new.work_item_id::text,'client_id',new.client_id,
      'session_id',new.session_id,'state',new.state,'run_digest',new.run_digest),
    coalesce(new.terminal_at,new.started_at,new.created_at));
  return new;
end $$;

create function app_server.publish_client_lifecycle() returns trigger language plpgsql
security invoker set search_path=pg_catalog,app_server as $$
begin
  perform app_server.publish_notification(
    new.realm_id,gen_random_uuid(),'session.item.observed',
    jsonb_build_object('client_event_id',new.id::text,'stream_id',new.stream_id::text,
      'sequence',new.sequence,'source_event_digest',new.event_digest,
      'event_type',new.payload->>'event_type'),new.ingested_at);
  return new;
end $$;

create trigger notification_event_guard before insert on app_server.notification_event
for each row execute function app_server.enforce_notification_event();
create trigger notification_event_update_guard before update on app_server.notification_event
for each statement execute function core.deny_mutation();
create trigger notification_event_delete_guard before delete on app_server.notification_event
for each statement execute function core.deny_mutation();
create trigger app_server_work_item_publish after insert or update on work.work_item
for each row execute function app_server.publish_work_item();
create trigger app_server_execution_run_publish after insert or update on runtime.execution_run
for each row execute function app_server.publish_execution_run();
create trigger app_server_client_lifecycle_publish after insert on client.lifecycle_event
for each row execute function app_server.publish_client_lifecycle();

alter table app_server.notification_stream enable row level security;
alter table app_server.notification_stream force row level security;
alter table app_server.notification_event enable row level security;
alter table app_server.notification_event force row level security;
create policy scope_select on app_server.notification_stream for select
  using(realm_id=core.current_realm_id());
create policy scope_insert on app_server.notification_stream for insert
  with check(realm_id=core.current_realm_id());
create policy scope_update on app_server.notification_stream for update
  using(realm_id=core.current_realm_id()) with check(realm_id=core.current_realm_id());
create policy scope_select on app_server.notification_event for select
  using(realm_id=core.current_realm_id());
create policy scope_insert on app_server.notification_event for insert
  with check(realm_id=core.current_realm_id());

create index notification_event_replay_idx
  on app_server.notification_event(realm_id,sequence,id);

grant select,insert,update on app_server.notification_stream to zekam_app;
grant select,insert on app_server.notification_event to zekam_app;
revoke all on function app_server.publish_notification(uuid,uuid,text,jsonb,timestamptz),
  app_server.publish_work_item(),app_server.publish_execution_run(),
  app_server.publish_client_lifecycle(),app_server.enforce_notification_event() from public;
grant execute on function app_server.publish_notification(uuid,uuid,text,jsonb,timestamptz),
  app_server.publish_work_item(),app_server.publish_execution_run(),
  app_server.publish_client_lifecycle() to zekam_app;
grant execute on function app_server.enforce_notification_event() to zekam_app;
