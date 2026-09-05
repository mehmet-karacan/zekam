drop trigger if exists context_manifest_v2_integrity on work.context_manifest;
drop function if exists work.enforce_context_manifest_v2();
alter table work.context_manifest
    drop constraint if exists context_manifest_candidate_set_fk,
    drop constraint if exists context_manifest_v2_binding,
    drop constraint if exists context_manifest_compiler_version,
    drop column if exists compiler_metrics_digest,
    drop column if exists compiler_metrics_canonical,
    drop column if exists manifest_canonical,
    drop column if exists ranking_snapshot_digest,
    drop column if exists candidate_set_digest,
    drop column if exists compiler_metrics,
    drop column if exists scoring_policy_digest,
    drop column if exists compiler_version;
drop trigger if exists context_candidate_set_v2_integrity on work.context_candidate_set;
drop trigger if exists context_ranking_snapshot_v2_integrity on work.context_ranking_snapshot;
drop function if exists work.enforce_context_candidate_set_v2();
drop function if exists work.enforce_context_ranking_snapshot_v2();
drop function if exists work.current_context_ranking_projection(uuid,uuid,uuid,uuid);
drop function if exists work.current_context_source_revisions(uuid,uuid);
drop trigger if exists context_source_revision_serialization_v2 on core.revision;
drop function if exists work.lock_context_source_revision_v2();
drop table if exists work.context_candidate_set;
drop table if exists work.context_ranking_snapshot;
drop function if exists core.canonical_jsonb(jsonb);
