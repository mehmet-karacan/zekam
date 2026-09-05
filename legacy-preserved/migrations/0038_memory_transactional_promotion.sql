-- Memory v2 promotion zincirini tek transaction ve normalize provenance ile zorlar.

alter table memory.candidate
    add column promoted_record_id uuid,
    add column promotion_plan_digest text,
    add constraint candidate_promotion_pair check (
        (promoted_record_id is null) = (promotion_plan_digest is null)
    ),
    add constraint candidate_promotion_digest_format check (
        promotion_plan_digest is null or promotion_plan_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    add constraint candidate_promoted_record_same_realm
        foreign key (realm_id, promoted_record_id)
        references memory.record (realm_id, id) deferrable initially deferred;

alter table memory.record
    drop constraint record_logical_id_unique,
    add column predecessor_id uuid,
    add constraint record_predecessor_same_realm
        foreign key (realm_id, predecessor_id)
        references memory.record (realm_id, id) deferrable initially deferred,
    add constraint record_revision_identity_unique
        unique (realm_id, logical_memory_id, revision),
    add constraint record_predecessor_not_self check (predecessor_id is null or predecessor_id <> id);

create unique index record_active_logical_family_idx
    on memory.record (realm_id, logical_memory_id) where state = 'active';

create table memory.promotion_plan (
    id uuid primary key,
    realm_id uuid not null,
    candidate_id uuid not null,
    plan_body jsonb not null,
    plan_digest text not null,
    created_at timestamptz not null,
    constraint promotion_plan_candidate_same_realm
        foreign key (realm_id, candidate_id) references memory.candidate (realm_id, id),
    constraint promotion_plan_realm_key unique (realm_id, id),
    constraint promotion_plan_digest_unique unique (realm_id, plan_digest),
    constraint promotion_plan_body_object check (jsonb_typeof(plan_body) = 'object'),
    constraint promotion_plan_digest_format check (plan_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table memory.review (
    id uuid primary key,
    realm_id uuid not null,
    candidate_id uuid not null,
    reviewer_ref text not null,
    decision text not null,
    reason_digest text not null,
    policy_digest text,
    review_digest text not null,
    decided_at timestamptz not null,
    grants_authority boolean not null default false,
    constraint review_candidate_same_realm
        foreign key (realm_id, candidate_id) references memory.candidate (realm_id, id),
    constraint review_realm_key unique (realm_id, id),
    constraint review_candidate_unique unique (realm_id, candidate_id),
    constraint review_decision check (decision in ('approved', 'rejected')),
    constraint review_reason_digest_format check (reason_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint review_policy_digest_format check (
        policy_digest is null or policy_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    constraint review_digest_format check (review_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint review_no_authority check (grants_authority = false)
);

create table memory.revision (
    id uuid primary key,
    realm_id uuid not null,
    record_id uuid not null,
    logical_memory_id text not null,
    revision integer not null,
    predecessor_id uuid,
    review_id uuid not null,
    record_digest text not null,
    plan_digest text not null,
    created_at timestamptz not null,
    constraint revision_record_same_realm
        foreign key (realm_id, record_id) references memory.record (realm_id, id),
    constraint revision_predecessor_same_realm
        foreign key (realm_id, predecessor_id) references memory.record (realm_id, id),
    constraint revision_review_same_realm
        foreign key (realm_id, review_id) references memory.review (realm_id, id),
    constraint revision_realm_key unique (realm_id, id),
    constraint revision_record_unique unique (realm_id, record_id),
    constraint revision_family_unique unique (realm_id, logical_memory_id, revision),
    constraint revision_positive check (revision > 0),
    constraint revision_record_digest_format check (record_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint revision_plan_digest_format check (plan_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table memory.evidence_link (
    id uuid primary key,
    realm_id uuid not null,
    record_id uuid not null,
    ordinal integer not null,
    evidence_kind text not null,
    evidence_ref text not null,
    evidence_digest text not null,
    created_at timestamptz not null,
    constraint evidence_record_same_realm
        foreign key (realm_id, record_id) references memory.record (realm_id, id),
    constraint evidence_realm_key unique (realm_id, id),
    constraint evidence_ordinal_unique unique (realm_id, record_id, ordinal),
    constraint evidence_identity_unique unique (
        realm_id, record_id, evidence_kind, evidence_ref, evidence_digest
    ),
    constraint evidence_ordinal_positive check (ordinal > 0),
    constraint evidence_digest_format check (evidence_digest ~ '^sha256:[0-9a-f]{64}$')
);

create table memory.promotion_outbox (
    id uuid primary key,
    realm_id uuid not null,
    record_id uuid not null,
    kind text not null,
    target_ref text not null,
    payload_digest text not null,
    state text not null default 'pending',
    attempt_count integer not null default 0,
    last_error_digest text,
    created_at timestamptz not null,
    completed_at timestamptz,
    constraint promotion_outbox_record_same_realm
        foreign key (realm_id, record_id) references memory.record (realm_id, id),
    constraint promotion_outbox_realm_key unique (realm_id, id),
    constraint promotion_outbox_identity_unique unique (realm_id, record_id, kind, target_ref),
    constraint promotion_outbox_kind check (kind in ('embedding', 'external-sync')),
    constraint promotion_outbox_state check (state in ('pending', 'processing', 'completed', 'failed')),
    constraint promotion_outbox_attempt check (attempt_count >= 0),
    constraint promotion_outbox_payload_digest_format check (
        payload_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    constraint promotion_outbox_error_digest_format check (
        last_error_digest is null or last_error_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    constraint promotion_outbox_completed_pair check (
        (state = 'completed') = (completed_at is not null)
    )
);

create table memory.promotion_receipt (
    id uuid primary key,
    realm_id uuid not null,
    candidate_id uuid not null,
    record_id uuid not null,
    predecessor_id uuid,
    review_id uuid not null,
    authorization_id uuid not null references security.authorization (id),
    plan_digest text not null,
    effect_digest text not null,
    result_digest text not null,
    created_at timestamptz not null,
    constraint promotion_candidate_same_realm
        foreign key (realm_id, candidate_id) references memory.candidate (realm_id, id),
    constraint promotion_record_same_realm
        foreign key (realm_id, record_id) references memory.record (realm_id, id),
    constraint promotion_predecessor_same_realm
        foreign key (realm_id, predecessor_id) references memory.record (realm_id, id),
    constraint promotion_review_same_realm
        foreign key (realm_id, review_id) references memory.review (realm_id, id),
    constraint promotion_plan_same_realm
        foreign key (realm_id, plan_digest)
        references memory.promotion_plan (realm_id, plan_digest),
    constraint promotion_realm_key unique (realm_id, id),
    constraint promotion_candidate_unique unique (realm_id, candidate_id),
    constraint promotion_plan_unique unique (realm_id, plan_digest),
    constraint promotion_record_unique unique (realm_id, record_id),
    constraint promotion_plan_digest_format check (plan_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint promotion_effect_digest_format check (effect_digest ~ '^sha256:[0-9a-f]{64}$'),
    constraint promotion_result_digest_format check (result_digest ~ '^sha256:[0-9a-f]{64}$')
);

create function memory.enforce_promotion_receipt() returns trigger
language plpgsql security invoker set search_path = pg_catalog, memory, security, core, models as $$
declare
    candidate_row memory.candidate%rowtype;
    record_row memory.record%rowtype;
    predecessor_row memory.record%rowtype;
    authorization_row security.authorization%rowtype;
    plan_row memory.promotion_plan%rowtype;
    review_row memory.review%rowtype;
    revision_row memory.revision%rowtype;
    expected_candidate_digest text;
    expected_review_digest text;
    expected_record_digest text;
    candidate_observed_at text;
    review_decided_at text;
begin
    select * into strict candidate_row from memory.candidate
    where realm_id = new.realm_id and id = new.candidate_id;
    select * into strict record_row from memory.record
    where realm_id = new.realm_id and id = new.record_id;
    select * into strict authorization_row from security.authorization
    where realm_id = new.realm_id and id = new.authorization_id;
    select * into strict plan_row from memory.promotion_plan
    where realm_id = new.realm_id and plan_digest = new.plan_digest;
    select * into strict review_row from memory.review
    where realm_id = new.realm_id and id = new.review_id;
    select * into strict revision_row from memory.revision
    where realm_id = new.realm_id and record_id = new.record_id;

    candidate_observed_at := to_char(candidate_row.created_at at time zone 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS') ||
        case when extract(microseconds from candidate_row.created_at)::integer % 1000000 = 0
             then '' else '.' || to_char(candidate_row.created_at at time zone 'UTC','US') end ||
        'Z';
    review_decided_at := to_char(review_row.decided_at at time zone 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS') ||
        case when extract(microseconds from review_row.decided_at)::integer % 1000000 = 0
             then '' else '.' || to_char(review_row.decided_at at time zone 'UTC','US') end ||
        'Z';
    expected_candidate_digest := models.capability_runtime_jsonb_digest(jsonb_build_object(
        'schema','zekam-memory-candidate-snapshot/v1',
        'candidate_id',candidate_row.logical_candidate_id,
        'key',jsonb_build_object(
            'scope',candidate_row.scope,
            'realm_ref',(select slug from core.realm where id = new.realm_id),
            'project_ref',candidate_row.project_ref,
            'work_ref',candidate_row.work_ref,
            'run_ref',null,
            'agent_ref',null
        ),
        'memory_class',candidate_row.memory_class,
        'content',candidate_row.content,
        'author_ref',candidate_row.author_ref,
        'evidence',candidate_row.evidence,
        'occurrence_key',candidate_row.occurrence_key,
        'observation_count',candidate_row.observation_count,
        'observed_at',candidate_observed_at
    ));
    expected_review_digest := models.capability_runtime_jsonb_digest(jsonb_build_object(
        'schema','zekam-memory-review/v1',
        'approved',(review_row.decision = 'approved'),
        'reviewer_ref',review_row.reviewer_ref,
        'reason_digest',review_row.reason_digest,
        'policy_digest',review_row.policy_digest,
        'decided_at',review_decided_at,
        'grants_authority',false
    ));
    expected_record_digest := models.capability_runtime_jsonb_digest(jsonb_build_object(
        'schema','zekam-memory-record/v1',
        'memory_id',record_row.logical_memory_id,
        'key',jsonb_build_object(
            'scope',record_row.scope,
            'realm_ref',(select slug from core.realm where id = new.realm_id),
            'project_ref',record_row.project_ref,
            'work_ref',record_row.work_ref,
            'run_ref',null,
            'agent_ref',null
        ),
        'memory_class',record_row.memory_class,
        'content',record_row.content,
        'state',record_row.state,
        'revision',record_row.revision,
        'evidence',record_row.evidence,
        'entities',to_jsonb(record_row.entities),
        'reviewed_by',record_row.reviewed_by,
        'author_ref',record_row.author_ref,
        'superseded_by',null,
        'grants_authority',false
    ));
    if plan_row.plan_digest <> models.capability_runtime_jsonb_digest(plan_row.plan_body)
       or plan_row.plan_body->>'realm_id' <> new.realm_id::text
       or plan_row.candidate_id <> new.candidate_id
       or plan_row.plan_body->>'candidate_storage_id' <> new.candidate_id::text
       or plan_row.plan_body->>'candidate_id' <> candidate_row.logical_candidate_id
       or plan_row.plan_body->>'candidate_digest' <> expected_candidate_digest
       or plan_row.plan_body->>'logical_memory_id' <> record_row.logical_memory_id
       or (plan_row.plan_body->>'next_revision')::integer <> record_row.revision
       or plan_row.plan_body->>'review_digest' <> review_row.review_digest
       or plan_row.plan_body->>'evidence_digest' <>
          models.capability_runtime_jsonb_digest(candidate_row.evidence) then
        raise exception 'memory promotion canonical plan mismatch' using errcode = '23514';
    end if;
    if record_row.scope <> candidate_row.scope
       or record_row.project_id is distinct from candidate_row.project_id
       or record_row.work_item_id is distinct from candidate_row.work_item_id
       or record_row.project_ref is distinct from candidate_row.project_ref
       or record_row.work_ref is distinct from candidate_row.work_ref
       or record_row.memory_class <> candidate_row.memory_class
       or record_row.content <> candidate_row.content
       or record_row.evidence <> candidate_row.evidence
       or record_row.author_ref is distinct from candidate_row.author_ref
       or record_row.reviewed_by is distinct from review_row.reviewer_ref
       or record_row.state <> 'active'
       or cardinality(record_row.entities) <> 0
       or record_row.created_at is distinct from new.created_at
       or record_row.valid_from is distinct from record_row.created_at
       or record_row.valid_until is not null
       or record_row.record_digest <> expected_record_digest then
        raise exception 'memory promotion candidate/record canonical mismatch'
            using errcode = '23514';
    end if;

    if candidate_row.promoted_record_id is distinct from new.record_id
       or candidate_row.promotion_plan_digest is distinct from new.plan_digest
       or candidate_row.reviewed is not true then
        raise exception 'memory promotion candidate binding mismatch' using errcode = '23514';
    end if;
    if review_row.candidate_id <> new.candidate_id
       or review_row.decision <> 'approved'
       or review_row.reviewer_ref <> candidate_row.reviewer_ref
       or review_row.reviewer_ref = candidate_row.author_ref
       or review_row.review_digest <> expected_review_digest
       or review_row.review_digest <> plan_row.plan_body->>'review_digest' then
        raise exception 'memory promotion review provenance mismatch' using errcode = '23514';
    end if;
    if authorization_row.state <> 'consumed'
       or authorization_row.plan_digest <> new.plan_digest
       or authorization_row.effect_digest <> new.effect_digest then
        raise exception 'memory promotion authorization binding mismatch' using errcode = '23514';
    end if;
    if revision_row.review_id <> new.review_id
       or revision_row.plan_digest <> new.plan_digest
       or revision_row.record_digest <> record_row.record_digest
       or revision_row.logical_memory_id <> record_row.logical_memory_id
       or revision_row.revision <> record_row.revision
       or revision_row.predecessor_id is distinct from record_row.predecessor_id then
        raise exception 'memory promotion revision evidence missing' using errcode = '23514';
    end if;
    if (select count(*) from memory.evidence_link e
        where e.realm_id = new.realm_id and e.record_id = new.record_id)
       <> jsonb_array_length(record_row.evidence) then
        raise exception 'memory promotion normalized evidence mismatch' using errcode = '23514';
    end if;
    if exists (
        select 1
        from jsonb_array_elements(record_row.evidence) with ordinality source(item,ordinal)
        left join memory.evidence_link link
          on link.realm_id = new.realm_id and link.record_id = new.record_id
         and link.ordinal = source.ordinal
        where link.id is null
           or link.evidence_kind <> source.item->>'kind'
           or link.evidence_ref <> source.item->>'reference'
           or link.evidence_digest <> source.item->>'digest'
    ) then
        raise exception 'memory promotion normalized evidence content mismatch'
            using errcode = '23514';
    end if;
    if (select count(*) from memory.promotion_outbox o
        where o.realm_id = new.realm_id and o.record_id = new.record_id
        and o.kind = 'embedding') <> 1
       or (select count(*) from memory.promotion_outbox o
        where o.realm_id = new.realm_id and o.record_id = new.record_id
        and o.kind = 'external-sync') <> 1 then
        raise exception 'memory promotion outbox pair missing' using errcode = '23514';
    end if;
    if exists (
        select 1 from memory.promotion_outbox o
        where o.realm_id = new.realm_id and o.record_id = new.record_id and (
            (o.kind = 'embedding' and o.target_ref <>
                plan_row.plan_body->>'embedding_profile_digest')
            or (o.kind = 'external-sync' and o.target_ref <>
                plan_row.plan_body->>'external_target_ref')
            or o.payload_digest <> models.capability_runtime_jsonb_digest(jsonb_build_object(
                'record_digest',record_row.record_digest,
                'kind',o.kind,
                'target_ref',o.target_ref
            ))
        )
    ) then
        raise exception 'memory promotion outbox payload mismatch' using errcode = '23514';
    end if;
    if authorization_row.allowed_effects <> array['database-write']::text[]
       or cardinality(authorization_row.allowed_resources) <> 2
       or not authorization_row.allowed_resources @> array[
           'memory:candidate:' || new.candidate_id::text,
           'memory:logical:' || record_row.logical_memory_id
       ]::text[] then
        raise exception 'memory promotion authorization scope mismatch' using errcode = '23514';
    end if;
    if new.predecessor_id is null then
        if record_row.revision <> 1 or record_row.predecessor_id is not null
           or plan_row.plan_body->>'predecessor_storage_id' is not null
           or plan_row.plan_body->>'predecessor_digest' is not null then
            raise exception 'memory initial revision binding mismatch' using errcode = '23514';
        end if;
    else
        select * into strict predecessor_row from memory.record
        where realm_id = new.realm_id and id = new.predecessor_id;
        if record_row.predecessor_id is distinct from new.predecessor_id
           or record_row.logical_memory_id <> predecessor_row.logical_memory_id
           or record_row.revision <> predecessor_row.revision + 1
           or plan_row.plan_body->>'predecessor_storage_id' <> new.predecessor_id::text
           or plan_row.plan_body->>'predecessor_digest' <> predecessor_row.record_digest
           or predecessor_row.state <> 'superseded'
           or predecessor_row.superseded_by is distinct from new.record_id
           or not exists (
               select 1 from memory.relation rel where rel.realm_id = new.realm_id
               and rel.from_id = new.record_id and rel.to_id = new.predecessor_id
               and rel.kind = 'supersedes'
           ) then
            raise exception 'memory predecessor/relation binding mismatch' using errcode = '23514';
        end if;
    end if;
    if not exists (
        select 1 from security.audit_event a where a.realm_id = new.realm_id
        and a.authorization_id = new.authorization_id
        and a.action = 'memory.promotion.applied'
        and a.subject_type = 'memory-promotion' and a.subject_id = new.plan_digest
        and a.decision = 'allow'
    ) then
        raise exception 'memory promotion audit evidence missing' using errcode = '23514';
    end if;
    return new;
end
$$;

create function memory.enforce_promotion_plan() returns trigger
language plpgsql security invoker set search_path = pg_catalog, memory, models as $$
begin
    if new.plan_digest <> models.capability_runtime_jsonb_digest(new.plan_body)
       or new.plan_body->>'realm_id' <> new.realm_id::text
       or new.plan_body->>'candidate_storage_id' <> new.candidate_id::text then
        raise exception 'memory promotion plan canonical digest mismatch' using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger promotion_plan_digest_guard before insert on memory.promotion_plan
    for each row execute function memory.enforce_promotion_plan();

create constraint trigger promotion_receipt_integrity
    after insert on memory.promotion_receipt deferrable initially deferred
    for each row execute function memory.enforce_promotion_receipt();

create function memory.enforce_candidate_promotion() returns trigger
language plpgsql security invoker set search_path = pg_catalog, memory as $$
begin
    if new.promoted_record_id is not null and (
        new.promoted_record_id is distinct from old.promoted_record_id
        or new.promotion_plan_digest is distinct from old.promotion_plan_digest
    ) and not exists (
        select 1 from memory.promotion_receipt p where p.realm_id = new.realm_id
        and p.candidate_id = new.id and p.record_id = new.promoted_record_id
        and p.plan_digest = new.promotion_plan_digest
    ) then
        raise exception 'memory candidate promotion receipt missing' using errcode = '23514';
    end if;
    return new;
end
$$;

create constraint trigger candidate_promotion_integrity
    after update of promoted_record_id,promotion_plan_digest on memory.candidate
    deferrable initially deferred for each row execute function memory.enforce_candidate_promotion();

create function memory.enforce_outbox_update() returns trigger
language plpgsql security invoker set search_path = pg_catalog, memory as $$
begin
    if new.realm_id is distinct from old.realm_id
       or new.record_id is distinct from old.record_id
       or new.kind is distinct from old.kind
       or new.target_ref is distinct from old.target_ref
       or new.payload_digest is distinct from old.payload_digest
       or new.created_at is distinct from old.created_at then
        raise exception 'memory outbox payload identity degistirilemez' using errcode = '23514';
    end if;
    return new;
end
$$;

create trigger promotion_outbox_update_guard before update on memory.promotion_outbox
    for each row execute function memory.enforce_outbox_update();

do $$
declare target text;
begin
    foreach target in array array[
        'memory.promotion_plan', 'memory.review', 'memory.revision', 'memory.evidence_link',
        'memory.promotion_outbox', 'memory.promotion_receipt'
    ] loop
        execute format('alter table %s enable row level security', target);
        execute format('alter table %s force row level security', target);
        execute format(
            'create policy scope_select on %s for select using (realm_id = core.current_realm_id())',
            target
        );
        execute format(
            'create policy scope_insert on %s for insert with check (realm_id = core.current_realm_id())',
            target
        );
    end loop;
end
$$;

create policy scope_update on memory.promotion_outbox for update
    using (realm_id = core.current_realm_id()) with check (realm_id = core.current_realm_id());

do $$
declare target text;
begin
    foreach target in array array[
        'memory.promotion_plan', 'memory.review', 'memory.revision',
        'memory.evidence_link', 'memory.promotion_receipt'
    ] loop
        execute format(
            'create trigger deny_update before update on %s for each statement '
            'execute function core.deny_mutation()', target
        );
        execute format(
            'create trigger deny_delete before delete on %s for each statement '
            'execute function core.deny_mutation()', target
        );
    end loop;
end
$$;

create trigger deny_delete before delete on memory.promotion_outbox
    for each statement execute function core.deny_mutation();

create index promotion_outbox_pending_idx
    on memory.promotion_outbox (realm_id, created_at) where state = 'pending';
create index evidence_link_record_idx on memory.evidence_link (realm_id, record_id, ordinal);
create index revision_family_idx on memory.revision (realm_id, logical_memory_id, revision desc);

grant select, insert on memory.promotion_plan, memory.review, memory.revision, memory.evidence_link,
    memory.promotion_outbox, memory.promotion_receipt to zekam_app;
grant update (state, attempt_count, last_error_digest, completed_at)
    on memory.promotion_outbox to zekam_app;
grant update (reviewed, reviewer_ref, review_reason, promoted_record_id, promotion_plan_digest)
    on memory.candidate to zekam_app;
grant execute on function memory.enforce_promotion_receipt() to zekam_app;
grant execute on function memory.enforce_promotion_plan() to zekam_app;
grant execute on function memory.enforce_candidate_promotion() to zekam_app;
grant execute on function memory.enforce_outbox_update() to zekam_app;
