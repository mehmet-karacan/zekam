"""Canonical, bounded Run readback shared by local Work surfaces."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def execution_guard(bound_jobs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize lease/recovery readback without exposing owner or grant data.

    Resume surfaces must not turn a stale or receiptless bound job into a safe
    continuation hint.  This projection is deliberately read-only: a caller
    still has to reacquire the current lease/grant through the canonical worker
    before any effect is admitted.
    """

    jobs = tuple(bound_jobs)
    if not jobs:
        status = "no-bound-job"
        next_safe_action = "Kanonik Work/Run bagini yeniden dogrula; yeni lease ve grant edin."
    else:
        states = {str(item.get("job_state", "")) for item in jobs}
        missing_receipts = any(
            isinstance(item.get("missing_receipt_count"), int)
            and item.get("missing_receipt_count", 0) > 0
            for item in jobs
        )
        unknown_shape = any(
            not isinstance(item.get("job_state"), str)
            or not isinstance(item.get("canonical_inputs_current"), bool)
            for item in jobs
        )
        stale_binding = any(item.get("canonical_inputs_current") is False for item in jobs)
        if unknown_shape or missing_receipts or "recovery-required" in states:
            status = "recovery-required"
            next_safe_action = (
                "Receipt/lease zincirini kanonik recovery ile uzlastir; effect tekrarlama."
            )
        elif stale_binding:
            status = "revalidation-required"
            next_safe_action = "Work, source snapshot ve Run binding'ini yeniden dogrula."
        elif "running" in states:
            status = "lease-active"
            next_safe_action = "Aktif lease'i devralma; mevcut owner terminal readback'ini bekle."
        elif "ready" in states:
            status = "lease-reacquisition-required"
            next_safe_action = "Guncel lease, fencing ve grant kosullarini yeniden edin."
        elif states.issubset({"completed", "failed", "cancelled", "quarantined"}):
            status = "terminal-readback"
            next_safe_action = "Yeni effect baslatmadan terminal receipt ve Work durumunu eslestir."
        else:
            status = "manual-review-required"
            next_safe_action = "Bagli job durumunu canonical runtime readback ile incele."
    return {
        "schema": "zekam-execution-guard/v1",
        "status": status,
        "bound_job_count": len(jobs),
        "revalidation_required": status
        in {"recovery-required", "revalidation-required", "manual-review-required"},
        "lease_reacquisition_required": status in {"no-bound-job", "lease-reacquisition-required"},
        "next_safe_action": next_safe_action,
        "read_only": True,
        "grants_authority": False,
    }


def run_document(run: Any, *, include_work_item_id: bool = False) -> dict[str, Any]:
    """Project one canonical Run and its source/config revalidation signal."""

    source_snapshot_id = run.source_snapshot_id
    source_fingerprint = {
        "snapshot_id": source_snapshot_id,
        "binding_id": getattr(run, "source_binding_id", None),
        "revision_ref": getattr(run, "source_revision_ref", None),
        "tree_digest": getattr(run, "source_tree_digest", None),
        "content_digest": getattr(run, "source_content_digest", None),
        "config_digest": getattr(run, "source_config_digest", None),
    }
    source_bound = source_snapshot_id is not None
    source_complete = not source_bound or all(
        source_fingerprint[key] is not None
        for key in ("binding_id", "revision_ref", "tree_digest", "content_digest", "config_digest")
    )
    config_fingerprint = {
        "revision_id": run.config_revision_id,
        "digest": getattr(run, "config_digest", None),
    }
    config_complete = (
        run.config_revision_id is not None and config_fingerprint["digest"] is not None
    )
    fingerprints_complete = source_complete and config_complete
    fingerprints_consistent = fingerprints_complete and (
        not source_bound or source_fingerprint["config_digest"] == config_fingerprint["digest"]
    )
    document: dict[str, Any] = {
        "id": run.id,
        "status": run.status,
        "plan_digest": run.plan_digest,
        "terminal_receipt_digest": run.terminal_receipt_digest,
        "source_snapshot_id": source_snapshot_id,
        "config_revision_id": run.config_revision_id,
        "fingerprints": {
            "source": source_fingerprint,
            "config": config_fingerprint,
            "complete": fingerprints_complete,
            "consistent": fingerprints_consistent,
            "revalidation_required": not fingerprints_consistent,
        },
    }
    if include_work_item_id:
        document["work_item_id"] = run.work_item_id
    return document
