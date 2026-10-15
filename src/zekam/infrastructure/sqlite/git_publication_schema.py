"""Additive operational v9 publication authority; no implicit grants or migration."""

MIGRATION_NAME = "operational-git-publication-v9"

GIT_PUBLICATION_DDL = r"""
create table git_publication_evidence (
    evidence_digest text primary key,
    body_json text not null check(json_valid(body_json) and length(body_json)<=16384),
    source_job_id text not null references local_job(id),
    source_evidence_digest text not null references artifact_ref(digest),
    producer_id text not null,
    attestation text not null check(length(attestation) between 1 and 512)
) strict;
create table git_publication_authorization (
    id text primary key,
    authorization_digest text not null unique,
    project_id text not null references project(id),
    work_item_id text not null references work_item(id),
    work_revision integer not null check(work_revision>0),
    binding_digest text not null,
    effect_digest text not null,
    input_digest text not null,
    job_key text not null unique,
    body_json text not null check(json_valid(body_json) and length(body_json)<=16384),
    plan_json text not null check(json_valid(plan_json) and length(plan_json)<=16384),
    producer_id text not null,
    attestation text not null check(length(attestation) between 1 and 512),
    foreign key(work_item_id,work_revision) references work_revision(work_item_id,revision)
) strict;
create table git_publication_consumption (
    authorization_id text primary key references git_publication_authorization(id),
    claim_id text not null unique references local_effect_claim(id),
    effect_digest text not null,
    consumed_at text not null,
    consumed_by text not null
) strict;
create table git_publication_revocation (
    authorization_id text primary key references git_publication_authorization(id),
    revoked_at text not null,
    producer_id text not null,
    attestation text not null check(length(attestation) between 1 and 512)
) strict;
create trigger git_publication_consume_guard before insert on git_publication_consumption begin
    select case when not exists (
        select 1 from git_publication_authorization a
        join local_job j on j.idempotency_key=a.job_key and j.state='running'
        join local_job_binding b on b.local_job_id=j.id and b.binding_digest=a.binding_digest
        join local_effect_claim c on c.job_id=j.id and c.id=new.claim_id and c.operation='git.push'
        join local_lease l on l.id=c.lease_id and l.job_id=j.id and l.fencing_token=c.fencing_token
        where a.id=new.authorization_id and a.effect_digest=new.effect_digest
        and l.expires_at>new.consumed_at
        and not exists(select 1 from local_effect_receipt r where r.claim_id=c.id)
        and not exists(select 1 from git_publication_revocation r where r.authorization_id=a.id)
    ) then raise(abort,'git publication live claim required') end;
end;
"""

for _table in (
    "git_publication_evidence",
    "git_publication_authorization",
    "git_publication_consumption",
    "git_publication_revocation",
):
    for _operation in ("update", "delete"):
        GIT_PUBLICATION_DDL += (
            f"create trigger {_table}_no_{_operation} before {_operation} on {_table} begin\n"
            " select raise(abort,'git publication ledger append-only');\nend;\n"
        )
