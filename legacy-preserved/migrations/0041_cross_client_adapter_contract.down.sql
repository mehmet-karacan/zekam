drop index if exists work.finalized_handoff_cross_client_ready_idx;
drop trigger if exists finalized_handoff_route_guard on work.finalized_handoff;
drop function if exists work.enforce_cross_client_handoff_route();

alter table work.finalized_handoff
    drop constraint if exists handoff_cross_client_evidence_complete,
    drop constraint if exists handoff_target_route_same_realm,
    drop constraint if exists handoff_target_route_digest_format,
    drop constraint if exists handoff_target_permission_digest_format,
    drop constraint if exists handoff_source_permission_digest_format,
    drop constraint if exists handoff_target_capability_digest_format,
    drop constraint if exists handoff_source_capability_digest_format,
    drop column if exists target_route_fresh,
    drop column if exists target_route_valid_until,
    drop column if exists target_route_decision_digest,
    drop column if exists required_replan_items,
    drop column if exists target_route_decision_id,
    drop column if exists unsupported_permissions,
    drop column if exists unsupported_capabilities,
    drop column if exists target_client_permission_digest,
    drop column if exists source_client_permission_digest,
    drop column if exists target_client_capability_digest,
    drop column if exists source_client_capability_digest;
