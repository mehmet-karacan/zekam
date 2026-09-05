-- Cross-client handoff capability ve fresh route provenance'i.

alter table work.finalized_handoff
    add column source_client_capability_digest text,
    add column target_client_capability_digest text,
    add column source_client_permission_digest text,
    add column target_client_permission_digest text,
    add column unsupported_capabilities text[] not null default '{}',
    add column unsupported_permissions text[] not null default '{}',
    add column required_replan_items text[] not null default '{}',
    add column target_route_decision_id uuid,
    add column target_route_decision_digest text,
    add column target_route_valid_until timestamptz,
    add column target_route_fresh boolean not null default false;

alter table work.finalized_handoff
    add constraint handoff_source_capability_digest_format check (
        source_client_capability_digest is null
        or source_client_capability_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    add constraint handoff_target_capability_digest_format check (
        target_client_capability_digest is null
        or target_client_capability_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    add constraint handoff_source_permission_digest_format check (
        source_client_permission_digest is null
        or source_client_permission_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    add constraint handoff_target_permission_digest_format check (
        target_client_permission_digest is null
        or target_client_permission_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    add constraint handoff_target_route_digest_format check (
        target_route_decision_digest is null
        or target_route_decision_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    add constraint handoff_cross_client_evidence_complete check (
        from_client = to_client
        or not target_route_fresh
        or (
            source_client_capability_digest is not null
            and target_client_capability_digest is not null
            and source_client_permission_digest is not null
            and target_client_permission_digest is not null
            and target_route_decision_id is not null
            and target_route_decision_digest is not null
            and target_route_valid_until is not null
            and created_at < target_route_valid_until
        )
    ),
    add constraint handoff_target_route_same_realm
        foreign key (realm_id,target_route_decision_id)
        references models.model_route_decision (realm_id,id) on delete restrict;

create function work.enforce_cross_client_handoff_route() returns trigger
language plpgsql security invoker set search_path=pg_catalog,work,models,core as $$
declare
    route_digest text;
    route_model text;
    route_valid_until timestamptz;
begin
    if new.from_client = new.to_client or not new.target_route_fresh then
        return new;
    end if;
    select d.evidence_digest,d.primary_model_id,
           least(et.expires_at,coalesce(rp.expires_at,et.expires_at))
      into route_digest,route_model,route_valid_until
      from models.model_route_decision d
      join models.execution_target_snapshot et
        on et.realm_id=d.realm_id and et.id=d.execution_target_id
      join models.routing_role_policy rp
        on rp.realm_id=d.realm_id and rp.id=d.role_policy_id
      join models.model_route_candidate c
        on c.realm_id=d.realm_id and c.decision_id=d.id
       and c.model_id=d.primary_model_id and c.disposition='primary'
     where d.realm_id=new.realm_id and d.id=new.target_route_decision_id
       and d.status='selected' and d.decided_at<=new.created_at
       and d.primary_model_id=new.to_model_ref
       and et.client_id=new.to_client
       and (d.project_id is null or d.project_id=new.project_id)
       and et.captured_at<=new.created_at and et.expires_at>new.created_at
       and rp.effective_from<=new.created_at
       and (rp.expires_at is null or rp.expires_at>new.created_at)
       and d.id=(select latest.id from models.model_route_decision latest
                  where latest.realm_id=d.realm_id and latest.role=d.role
                    and latest.decided_at<=new.created_at
                    and latest.target_layer=d.target_layer
                    and latest.project_id is not distinct from d.project_id
                    and latest.workload is not distinct from d.workload
                    and latest.technology is not distinct from d.technology
                  order by latest.decided_at desc,latest.id desc limit 1)
     group by d.evidence_digest,d.primary_model_id,et.expires_at,rp.expires_at;
    if route_digest is null
       or route_digest is distinct from new.target_route_decision_digest
       or route_model is distinct from new.to_model_ref
       or route_valid_until is distinct from new.target_route_valid_until then
        raise exception 'cross-client handoff canonical route drift' using errcode='42501';
    end if;
    return new;
end
$$;

create trigger finalized_handoff_route_guard
before insert on work.finalized_handoff
for each row execute function work.enforce_cross_client_handoff_route();

create index finalized_handoff_cross_client_ready_idx
    on work.finalized_handoff (realm_id, work_item_id, created_at desc)
    where from_client <> to_client and target_route_fresh;
