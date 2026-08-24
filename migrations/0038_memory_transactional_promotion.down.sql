do $$
begin
    if exists (
        select 1 from memory.record group by realm_id,logical_memory_id having count(*) > 1
    ) then
        raise exception 'memory migration 38 downgrade requires forward-fix: multi-revision family exists'
            using errcode = '55000';
    end if;
end
$$;

drop trigger if exists promotion_receipt_integrity on memory.promotion_receipt;
drop function if exists memory.enforce_promotion_receipt();
drop trigger if exists promotion_plan_digest_guard on memory.promotion_plan;
drop function if exists memory.enforce_promotion_plan();
drop trigger if exists candidate_promotion_integrity on memory.candidate;
drop function if exists memory.enforce_candidate_promotion();
drop trigger if exists promotion_outbox_update_guard on memory.promotion_outbox;
drop function if exists memory.enforce_outbox_update();

drop table if exists memory.promotion_receipt;
drop table if exists memory.promotion_outbox;
drop table if exists memory.evidence_link;
drop table if exists memory.revision;
drop table if exists memory.review;
drop table if exists memory.promotion_plan;

drop index if exists memory.record_active_logical_family_idx;

alter table memory.record
    drop constraint if exists record_predecessor_not_self,
    drop constraint if exists record_revision_identity_unique,
    drop constraint if exists record_predecessor_same_realm,
    drop column if exists predecessor_id,
    add constraint record_logical_id_unique unique (realm_id, logical_memory_id);

alter table memory.candidate
    drop constraint if exists candidate_promoted_record_same_realm,
    drop constraint if exists candidate_promotion_digest_format,
    drop constraint if exists candidate_promotion_pair,
    drop column if exists promotion_plan_digest,
    drop column if exists promoted_record_id;
