"""Canonical, bounded Run readback shared by local Work surfaces."""

from __future__ import annotations

from typing import Any


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
