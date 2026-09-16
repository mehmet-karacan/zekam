"""Shared claim-before-effect runner for bounded local mutations."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from zekam.application.local_runtime_service import (
    LocalEffectDispatcher,
    LocalEffectRequest,
    LocalEffectResult,
    LocalRuntimeService,
)
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.local_runtime_effects import LocalJournalOutboxPublisher
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore


def run_claimed_local_effect(
    operational_path: Path,
    home: Path,
    *,
    operation: str,
    effect: dict[str, object],
    idempotency_key: str,
    apply_effect: Callable[[], dict[str, object]],
    replay_effect: Callable[[], dict[str, object]],
    logical_resources: tuple[str, ...] = (),
) -> dict[str, object]:
    """Run one exact local mutation and verify its terminal claim/receipt chain."""

    normalized = json.loads(canonical_json(effect))
    if not isinstance(normalized, dict):
        raise PolicyViolation("Local effect canonical body invalid")
    store = SQLiteLocalRuntimeStore(operational_path, existing_only=True)
    before = store.status()
    if (
        before.pending_outbox
        or before.claimed_outbox
        or before.recovery_outbox
        or before.recovery_jobs
        or before.open_recovery_cases
    ):
        raise PolicyViolation("Local effect refuses unrelated unresolved runtime work")
    completed: dict[str, dict[str, object]] = {}
    failed: dict[str, Exception] = {}

    def evidence(document: dict[str, object]) -> str:
        value = document.get("receipt_digest")
        if not isinstance(value, str):
            raise PolicyViolation("Local effect receipt digest missing")
        parse_digest(value)
        return value

    def execute(request: LocalEffectRequest) -> LocalEffectResult:
        if request.operation != operation or request.payload != normalized:
            raise PolicyViolation("Local runtime effect binding drift")
        try:
            document = apply_effect()
        except Exception as exc:
            failed["error"] = exc
            return LocalEffectResult("failed", digest({"operation": operation, "state": "failed"}))
        completed["document"] = document
        return LocalEffectResult("completed", evidence(document))

    service = LocalRuntimeService(
        store,
        effect_dispatcher=LocalEffectDispatcher(((operation, execute),)),
        outbox_publisher=LocalJournalOutboxPublisher(home / "runtime" / "local-effects"),
    )
    job, _created = store.enqueue(
        idempotency_key=idempotency_key,
        payload={"operation": operation, "effect": normalized},
    )
    if job.state == "ready":
        service.run_worker_once(
            owner_id=f"local-effect-{os.getpid()}",
            owner_pid=os.getpid(),
            owner_token=str(uuid4()),
            lease_seconds=30,
            job_id=job.id,
            resources=logical_resources,
        )
        if "error" in failed:
            raise failed["error"]
    elif job.state != "completed":
        raise PolicyViolation("Existing local effect job needs recovery")
    document = completed.get("document") or replay_effect()
    terminal = evidence(document)
    snapshot = store.job_snapshot(job.id)
    if (
        snapshot is None
        or snapshot.get("state") != "completed"
        or snapshot.get("terminal_evidence_digest") != terminal
        or not isinstance(snapshot.get("effects"), list)
        or len(snapshot["effects"]) != 1
        or snapshot["effects"][0].get("operation") != operation
        or snapshot["effects"][0].get("receipt_status") != "completed"
        or snapshot["effects"][0].get("evidence_digest") != terminal
    ):
        raise PolicyViolation("Local effect terminal receipt readback failed")
    publisher_token = str(uuid4())
    for _index in range(3):
        if store.status().pending_outbox == 0:
            break
        service.publish_outbox_once(
            owner_id=f"local-effect-outbox-{os.getpid()}",
            owner_pid=os.getpid(),
            owner_token=publisher_token,
            lease_seconds=30,
        )
    after = store.status()
    if after.pending_outbox or after.claimed_outbox or after.recovery_outbox:
        raise PolicyViolation("Local effect outbox terminal readback failed")
    return document | {
        "operational_job_id": job.id,
        "operational_terminal_evidence_digest": terminal,
    }
