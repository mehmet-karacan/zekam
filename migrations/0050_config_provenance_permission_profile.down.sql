drop table if exists security.config_provenance_snapshot;
drop function if exists security.enforce_config_provenance_snapshot();
drop function if exists security.config_leaf_values(jsonb,text);
drop table if exists security.permission_profile_revision;
drop function if exists security.enforce_permission_profile_revision();
