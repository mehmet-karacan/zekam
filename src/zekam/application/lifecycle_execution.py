"""Database-neutral lifecycle execution evidence DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ActiveLifecycleExecution:
    """Read-only proof that one delivery is attached to the live worker envelope."""

    project_id: UUID
    work_item_id: UUID
    plan_id: UUID
    run_id: UUID
    attempt_id: UUID
    assignment_id: UUID
    lease_id: UUID
    fencing_token: int
    envelope_id: UUID
    envelope_digest: str
    source_revision: str
    source_digest: str
    policy_digest: str
    migration_digest: str
    context_manifest_digest: str
    journal_head_digest: str
    work_plan_digest: str
