-- Client lifecycle canonical ingest, stream head ve ACK kayitlari.

create schema if not exists client;
grant usage on schema client to zekam_app;

create table client.lifecycle_stream (
    id uuid primary key,
    realm_id uuid not null references core.realm(id) on delete cascade,
    client_kind text not null,
    client_instance_id text not null,
    session_id text not null,
    head_sequence bigint not null default 0,
    head_digest text,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    unique (realm_id, id),
    unique (realm_id, client_instance_id, session_id),
    check (client_kind ~ '^[a-z][a-z0-9_-]{1,31}$'),
    check (client_instance_id ~ '^[A-Za-z0-9_-]{1,160}$'),
    check (session_id ~ '^[A-Za-z0-9_./:-]{1,160}$'),
    check (head_sequence >= 0),
    check ((head_sequence = 0) = (head_digest is null))
);

create table client.lifecycle_event (
    id uuid primary key,
    realm_id uuid not null,
    stream_id uuid not null,
    sequence bigint not null,
    previous_digest text,
    event_digest text not null,
    payload jsonb not null,
    occurred_at timestamptz not null,
    ingested_at timestamptz not null,
    grants_authority boolean not null default false,
    foreign key (realm_id, stream_id) references client.lifecycle_stream(realm_id, id),
    unique (realm_id, id),
    unique (realm_id, stream_id, sequence),
    unique (realm_id, event_digest),
    check (sequence > 0),
    check ((sequence = 1) = (previous_digest is null)),
    check (event_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (previous_digest is null or previous_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (jsonb_typeof(payload) = 'object'),
    check (grants_authority = false)
);

create table client.lifecycle_ack (
    id uuid primary key,
    realm_id uuid not null,
    event_id uuid not null,
    local_event_digest text not null,
    canonical_digest text not null,
    acknowledged_at timestamptz not null,
    foreign key (realm_id, event_id) references client.lifecycle_event(realm_id, id),
    unique (realm_id, event_id),
    unique (realm_id, local_event_digest),
    check (local_event_digest ~ '^sha256:[0-9a-f]{64}$'),
    check (canonical_digest ~ '^sha256:[0-9a-f]{64}$')
);

create function client.enforce_lifecycle_chain() returns trigger
language plpgsql security invoker set search_path=pg_catalog,client,core as $$
declare current_sequence bigint;
declare current_digest text;
begin
    select head_sequence,head_digest into current_sequence,current_digest
    from client.lifecycle_stream
    where realm_id=new.realm_id and id=new.stream_id
    for update;
    if not found then
        raise exception 'lifecycle stream not found' using errcode='23503';
    end if;
    if new.sequence <> current_sequence + 1
       or new.previous_digest is distinct from current_digest then
        raise exception 'lifecycle stream head/previous mismatch' using errcode='23514';
    end if;
    update client.lifecycle_stream
    set head_sequence=new.sequence,head_digest=new.event_digest,updated_at=new.ingested_at
    where realm_id=new.realm_id and id=new.stream_id;
    return new;
end
$$;

create trigger lifecycle_chain_guard before insert on client.lifecycle_event
    for each row execute function client.enforce_lifecycle_chain();

do $$
declare target text;
begin
    foreach target in array array[
        'client.lifecycle_stream', 'client.lifecycle_event', 'client.lifecycle_ack'
    ] loop
        execute format('alter table %s enable row level security', target);
        execute format('alter table %s force row level security', target);
        execute format(
            'create policy scope_select on %s for select using (realm_id=core.current_realm_id())',
            target
        );
        execute format(
            'create policy scope_insert on %s for insert with check '
            '(realm_id=core.current_realm_id())', target
        );
    end loop;
end
$$;

create policy scope_update on client.lifecycle_stream for update
    using (realm_id=core.current_realm_id())
    with check (realm_id=core.current_realm_id());

create trigger lifecycle_event_deny_update before update on client.lifecycle_event
    for each statement execute function core.deny_mutation();
create trigger lifecycle_event_deny_delete before delete on client.lifecycle_event
    for each statement execute function core.deny_mutation();
create trigger lifecycle_ack_deny_update before update on client.lifecycle_ack
    for each statement execute function core.deny_mutation();
create trigger lifecycle_ack_deny_delete before delete on client.lifecycle_ack
    for each statement execute function core.deny_mutation();

grant select, insert, update on client.lifecycle_stream to zekam_app;
grant select, insert on client.lifecycle_event, client.lifecycle_ack to zekam_app;
grant execute on function client.enforce_lifecycle_chain() to zekam_app;
