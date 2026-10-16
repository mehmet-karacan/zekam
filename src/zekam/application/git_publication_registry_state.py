"""Bounded canonical registry effect census; no derived head or authority store."""

from pathlib import Path

from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.operational_schema import _connect

REGISTRY_OPERATIONS = ("publication.trust.enroll", "publication.trust.rotate")
MAX_REGISTRY_LINEAGE = 64


def registry_terminals(home: Path, *, current_job_key: str | None = None) -> list[dict]:
    connection = _connect(home / "state/operational.db", read_only=True)
    try:
        connection.execute("begin")
        unresolved = connection.execute(
            "select 1 from local_effect_claim c join local_job j on j.id=c.job_id "
            "left join local_effect_receipt r on r.claim_id=c.id "
            "where c.operation in (?,?) and (? is null or j.idempotency_key<>?) "
            "and (r.status is null or r.status<>'completed' or j.state<>'completed') limit 1",
            (*REGISTRY_OPERATIONS, current_job_key, current_job_key),
        ).fetchone()
        if unresolved is not None:
            raise PolicyViolation("Publication registry unresolved effect requires recovery")
        rows = connection.execute(
            "select j.id,j.terminal_evidence_digest,c.operation from local_job j "
            "join local_effect_claim c on c.job_id=j.id "
            "join local_effect_receipt r on r.claim_id=c.id "
            "where j.state='completed' and r.status='completed' and c.operation in (?,?) limit ?",
            (*REGISTRY_OPERATIONS, MAX_REGISTRY_LINEAGE + 1),
        ).fetchall()
    finally:
        connection.close()
    if len(rows) > MAX_REGISTRY_LINEAGE or len({row["id"] for row in rows}) != len(rows):
        raise PolicyViolation("Publication registry terminal census invalid or over budget")
    return [
        {
            "job_id": row["id"],
            "evidence_digest": row["terminal_evidence_digest"],
            "operation": row["operation"],
        }
        for row in rows
    ]
