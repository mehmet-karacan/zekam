"""Model qualification snapshot built only from measured inventory evidence.

This surface deliberately reports ``unknown`` when a catalog revision, quota
observation, or performance trial was not supplied.  Inventory declarations
are useful identity and provenance input, but they do not become qualification
evidence by themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.model_inventory import BenchmarkState, HealthState, ModelRecord

QUALIFICATION_SCHEMA = "zekam-model-qualification/v1"
_VERIFIED_CAPABILITIES = {
    "tools": "tool-call",
    "structured_output": "json-schema",
    "context": "context-limit",
}


def _signal(record: ModelRecord, capability: str) -> str:
    return "verified" if record.verified(capability) else "unknown"


def _qualification(record: ModelRecord) -> str:
    if (
        not record.enabled
        or record.health_state is HealthState.QUARANTINED
        or record.benchmark_state is BenchmarkState.FAILED
    ):
        return "not-qualified"
    if (
        record.health_state
        in {HealthState.PROJECT_QUALIFIED, HealthState.ACTIVE_CANDIDATE}
        and record.benchmark_state is BenchmarkState.PASSED
    ):
        return "qualified"
    return "unknown"


def build_qualification_report(
    records: Sequence[ModelRecord],
    *,
    catalog_model_ids: Sequence[str] | None = None,
    catalog_revision: str | None = None,
) -> dict[str, Any]:
    """Return a secret-free, non-authoritative qualification snapshot.

    ``catalog_model_ids`` must already be resolved to canonical Model IDs.  A
    missing list means the catalog was not observed, so the report keeps the
    catalog and configured state unknown.  Quota is likewise always unknown on
    this inventory-only path; a percentage is never inferred from model rows.
    """

    if any(type(record) is not ModelRecord for record in records):
        raise TypeError("Model qualification exact ModelRecord ister")
    canonical_ids = tuple(record.model_id for record in records)
    if len(set(canonical_ids)) != len(canonical_ids):
        raise ValueError("Model qualification duplicate Model ID")
    catalog_ids: frozenset[str] | None
    if catalog_model_ids is None:
        catalog_ids = None
    else:
        if any(type(value) is not str or not value.strip() for value in catalog_model_ids):
            raise ValueError("Catalog model ID metin olmali")
        catalog_ids = frozenset(catalog_model_ids)
        if len(catalog_ids) != len(tuple(catalog_model_ids)):
            raise ValueError("Catalog model ID duplicate")
        if catalog_revision is None:
            raise ValueError("Catalog revision digest ister")
    if catalog_revision is not None:
        parse_digest(catalog_revision)

    rows: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda item: item.inventory_index):
        configured: str
        if catalog_ids is None:
            configured = "unknown"
        else:
            configured = "configured" if record.model_id in catalog_ids else "not-configured"
        signals = {
            "quality": (
                "observed"
                if record.benchmark_state is BenchmarkState.PASSED
                else "failed"
                if record.benchmark_state is BenchmarkState.FAILED
                else "unknown"
            ),
            "tools": _signal(record, _VERIFIED_CAPABILITIES["tools"]),
            "structured_output": _signal(record, _VERIFIED_CAPABILITIES["structured_output"]),
            "context": _signal(record, _VERIFIED_CAPABILITIES["context"]),
            "latency": "unknown",
            "failure": (
                "observed-failure"
                if record.health_state is HealthState.QUARANTINED
                else "unknown"
            ),
            "cost": "observed" if record.cost.has_evidence else "unknown",
        }
        rows.append(
            {
                "model_id": record.model_id,
                "access_name": record.access_name,
                "provider_protocol": record.provider_protocol.value,
                "configured": configured,
                "health_state": record.health_state.value,
                "benchmark_state": record.benchmark_state.value,
                "qualification": _qualification(record),
                "signals": signals,
            }
        )

    body: dict[str, Any] = {
        "schema": QUALIFICATION_SCHEMA,
        "models": rows,
        "qualification_counts": {
            state: sum(row["qualification"] == state for row in rows)
            for state in ("qualified", "not-qualified", "unknown")
            if any(row["qualification"] == state for row in rows)
        },
        "catalog": {
            "status": "observed" if catalog_ids is not None else "unknown",
            "model_count": None if catalog_ids is None else len(catalog_ids),
        },
        "catalog_revision": catalog_revision,
        "quota": {"status": "unknown", "remaining_ratio": None},
        "read_only": True,
        "grants_authority": False,
    }
    return body | {"evidence_digest": digest(body)}
