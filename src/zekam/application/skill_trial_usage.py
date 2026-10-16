"""Read receipt-verified trial usage without changing frozen aggregate digests."""

from __future__ import annotations

from decimal import Decimal, localcontext
from pathlib import Path

from zekam.application.config import load_settings
from zekam.application.skill_trial_aggregation import _object, aggregate_skill_trials
from zekam.domain.canonical import digest
from zekam.infrastructure.clients.opencode_usage import validate_native_usage
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


def _totals(records):
    count = len(records)
    tokens = [item["native_reported_tokens"] if item is not None else None for item in records]
    costs = [item["host_estimated_cost_usd"] if item is not None else None for item in records]
    token_count = sum(value is not None for value in tokens)
    cost_count = sum(value is not None for value in costs)
    with localcontext() as arithmetic:
        arithmetic.prec = 400
        cost = sum((Decimal(value) for value in costs), Decimal(0)) if cost_count == count else None
    return {
        "agent_dispatches": count,
        "artifacts_with_native_usage": sum(item is not None for item in records),
        "complete_token_measurements": token_count,
        "missing_or_partial_token_measurements": count - token_count,
        "complete_cost_estimates": cost_count,
        "missing_or_partial_cost_estimates": count - cost_count,
        "native_reported_tokens": sum(tokens) if token_count == count else None,
        "host_estimated_cost_usd": format(cost, "f") if cost is not None else None,
        "estimated_cost_micros_sum": sum(item["estimated_cost_micros"] for item in records)
        if cost_count == count
        else None,
        "provider_requests": None,
        "billing_verified": False,
        "grants_authority": False,
    }


def read_skill_trial_usage(
    home: Path,
    *,
    references: tuple[str, ...],
    cases: tuple[dict, ...],
    project_id: str,
    revision_digest: str,
    package_digest: str,
) -> dict:
    # The existing aggregate validates every receipt, assignment, native result,
    # model/scope/sample/session and locally recomputed grade. Do not consume a
    # caller-supplied aggregate or a loose list of artifact digests instead.
    aggregate = aggregate_skill_trials(
        home,
        references=references,
        cases=cases,
        project_id=project_id,
        revision_digest=revision_digest,
        package_digest=package_digest,
    )
    objects = LocalContentAddressedStore(home / load_settings(home=home).object_store_relative)
    trials = []
    buckets = {condition: [] for condition in ("baseline", "candidate")}
    for trial in aggregate["trials"]:
        observations = {}
        for condition in ("baseline", "candidate"):
            item = trial["observations"][condition]
            usage = None
            if item["outcome"] == "success":
                artifact = _object(
                    objects, item["artifact_digest"], maximum=64_000, public_safe=True
                )
                usage = validate_native_usage(artifact)
            observations[condition] = {
                "artifact_digest": item["artifact_digest"],
                "outcome": item["outcome"],
                "elapsed_ns": item["elapsed_ns"],
                "native_usage": usage,
            }
            buckets[condition].append(usage)
        trials.append(
            {
                "job_id": trial["job_id"],
                "receipt_evidence_digest": trial["receipt_evidence_digest"],
                "observations": observations,
            }
        )
    body = {
        "schema": "zekam-skill-trial-native-usage-report/v1",
        "scope": "included-trial-dispatches",
        "project_id": project_id,
        "model_id": aggregate["conditions"]["model_id"],
        "aggregate_digest": aggregate["aggregate_digest"],
        "trials": trials,
        "by_condition": {condition: _totals(records) for condition, records in buckets.items()},
        "new_agent_calls": 0,
        "new_provider_calls": 0,
        "billing_verified": False,
        "grants_authority": False,
    }
    return {
        "schema": "zekam-skill-trial-aggregate-with-native-usage/v1",
        "aggregate": aggregate,
        "native_usage_report": body | {"report_digest": digest(body)},
        "grants_authority": False,
    }
