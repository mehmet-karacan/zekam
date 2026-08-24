-- Context manifest kimligi exact realm/project/work partition'inda tekildir.

alter table work.context_manifest
    drop constraint context_manifest_unique;

alter table work.context_manifest
    add constraint context_manifest_unique
    unique (realm_id, project_id, work_item_id, manifest_digest);
