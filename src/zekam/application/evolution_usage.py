"""Honest projections of recorded usage; missing observations are never zero."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

from zekam.domain.canonical import digest
from zekam.domain.errors import ZekamError

USAGE_FIELDS = ("provider_calls", "tokens", "cost_micros")
MAX_USAGE_INTEGER = (1 << 63) - 1
MAX_EVALUATION_BODY_BYTES = 1_048_576


def _unique_fields(pairs):
    body = {}
    for key, value in pairs:
        if key in body:
            raise ValueError("duplicate field")
        body[key] = value
    return body


def _nonfinite(_value):
    raise ValueError("nonfinite JSON")


def read_evaluation_body(raw: object, expected_digest: str) -> dict[str, object] | None:
    try:
        if type(raw) is not str or len(raw.encode("utf-8")) > MAX_EVALUATION_BODY_BYTES:
            return None
        body = json.loads(raw, object_pairs_hook=_unique_fields, parse_constant=_nonfinite)
        if not isinstance(body, dict) or digest(body) != expected_digest:
            return None
        return body
    except (ValueError, TypeError, RecursionError, UnicodeError, ZekamError):
        return None


def evaluation_usage(body: Mapping[str, object]) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for name in USAGE_FIELDS:
        value = body.get(f"actual_{name}")
        result[name] = value if type(value) is int and 0 <= value <= MAX_USAGE_INTEGER else None
    return result


def summarize_evaluation_usage(
    pairs: Sequence[Mapping[str, object]], *, total: int, source_available: bool
) -> tuple[dict[str, int | None], dict[str, object]]:
    """Totals cover the entire retained source or explicitly remain unknown.

    The bounded report sample is not the complete ledger. Incomplete per-field
    measurements also cannot be summed into an exact ledger-wide total.
    """
    coverage_complete = source_available and len(pairs) == total
    unknown = {name: 0 for name in USAGE_FIELDS}
    observed = {name: 0 for name in USAGE_FIELDS}
    for pair in pairs:
        usage = pair.get("actual_usage")
        for name in USAGE_FIELDS:
            value = usage.get(name) if isinstance(usage, Mapping) else None
            if type(value) is int and 0 <= value <= MAX_USAGE_INTEGER:
                observed[name] += value
            else:
                unknown[name] += 1
    totals = {
        name: observed[name] if coverage_complete and unknown[name] == 0 else None
        for name in USAGE_FIELDS
    }
    coverage = {
        "scope": "all-retained-evaluations",
        "source_available": source_available,
        "total_records": total if source_available else None,
        "included_records": len(pairs),
        "omitted_or_invalid_records": max(0, total - len(pairs)) if source_available else None,
        "complete": coverage_complete and all(value == 0 for value in unknown.values()),
        "unknown_measurements": unknown,
        "observed_sample_totals": observed,
        "sample_totals_are_ledger_totals": coverage_complete
        and all(value == 0 for value in unknown.values()),
        "grants_authority": False,
    }
    return totals, coverage
