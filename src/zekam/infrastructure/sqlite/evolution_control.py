"""One bounded, read-only integrity reader for evolution control history."""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ZekamError

MAX_CONTROL_EVENTS = 10_000
MAX_CONTROL_BODY_BYTES = 1_048_576
MAX_CONTROL_HISTORY_BYTES = 8 * 1_048_576
_REASON = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,255}\Z")
_SECRET = re.compile(r"(?i)(?:bearer\s+|password|credential|api[-_]?key|\bsk-[A-Za-z0-9]{8,})")
_REQUIRED = {
    "schema",
    "ordinal",
    "state",
    "previous_event_digest",
    "reason",
    "enable_plan_digest",
    "admission_evidence",
    "occurred_at",
    "grants_authority",
}
_ADMISSION = {
    "evidence_digest",
    "task_scope_digest",
    "config_digest",
    "implementation_digest",
    "supervisor_status_digest",
    "grant_binding_digest",
    "grant_digests",
    "recovery_clear",
}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyViolation("Evolution control duplicate JSON field")
        result[key] = value
    return result


def _constant(_value):
    raise PolicyViolation("Evolution control nonfinite JSON")


def _digest(value):
    if type(value) is not str:
        raise PolicyViolation("Evolution control digest must be text")
    try:
        parse_digest(value)
    except ZekamError as exc:
        raise PolicyViolation("Evolution control invalid digest") from exc


def validate_control_body(body, *, ordinal, previous_state, previous_digest):
    if (
        not isinstance(body, dict)
        or not _REQUIRED <= set(body)
        or set(body) - _REQUIRED - {"bootstrap_settlement"}
        or body["schema"] != "zekam-evolution-control-event/v1"
        or type(body["ordinal"]) is not int
        or body["ordinal"] != ordinal
        or body["previous_event_digest"] != previous_digest
        or type(body["state"]) is not str
        or body["state"] not in {"observing", "paused", "disabled"}
        or type(body["reason"]) is not str
        or _REASON.fullmatch(body["reason"]) is None
        or _SECRET.search(body["reason"])
        or body["grants_authority"] is not False
        or type(body["occurred_at"]) is not str
    ):
        raise PolicyViolation("Evolution control event body/chain drift")
    try:
        moment = dt.datetime.fromisoformat(body["occurred_at"])
    except ValueError as exc:
        raise PolicyViolation("Evolution control invalid timestamp") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise PolicyViolation("Evolution control timestamp timezone missing")
    enable = body["enable_plan_digest"]
    if enable is not None:
        _digest(enable)
    if previous_state == "disabled" and body["state"] != "disabled" and enable is None:
        raise PolicyViolation("Evolution control disabled transition lacks enable plan")
    admission = body["admission_evidence"]
    if previous_state == "paused" and body["state"] == "observing" and admission is None:
        raise PolicyViolation("Evolution control resume admission missing")
    if admission is not None:
        if (
            not isinstance(admission, dict)
            or set(admission) != _ADMISSION
            or admission["recovery_clear"] is not True
            or not isinstance(admission["grant_digests"], (list, tuple))
            or not admission["grant_digests"]
            or len(admission["grant_digests"]) > 1024
        ):
            raise PolicyViolation("Evolution control admission schema drift")
        for key in _ADMISSION - {"grant_digests", "recovery_clear"}:
            _digest(admission[key])
        for grant in admission["grant_digests"]:
            _digest(grant)
        if (
            list(admission["grant_digests"]) != sorted(set(admission["grant_digests"]))
            or digest({k: v for k, v in admission.items() if k != "evidence_digest"})
            != admission["evidence_digest"]
        ):
            raise PolicyViolation("Evolution control admission digest/grant drift")
    settlement = body.get("bootstrap_settlement")
    if settlement is not None:
        if (
            not isinstance(settlement, dict)
            or body["state"] != "observing"
            or body["reason"] != "bootstrap-complete"
            or enable is None
            or "receipt_digest" not in settlement
            or digest({k: v for k, v in settlement.items() if k != "receipt_digest"})
            != settlement["receipt_digest"]
        ):
            raise PolicyViolation("Evolution control bootstrap settlement drift")


def read_control_chain(
    db: sqlite3.Connection,
    *,
    max_events: int = MAX_CONTROL_EVENTS,
    max_history_bytes: int = MAX_CONTROL_HISTORY_BYTES,
    include_bootstrap: bool = True,
) -> dict[str, object]:
    """Verify every retained event, including predecessors, with constant row memory."""
    if (
        type(max_events) is not int
        or not 1 <= max_events <= MAX_CONTROL_EVENTS
        or type(max_history_bytes) is not int
        or not 1 <= max_history_bytes <= MAX_CONTROL_HISTORY_BYTES
    ):
        raise PolicyViolation("Evolution control invalid read budget")
    result = {
        "state": "observing",
        "ordinal": 0,
        "event_digest": None,
        "reason": "default-local-observation",
        "occurred_at": None,
    }
    previous = None
    total = 0
    cursor = db.execute(
        "select event_digest,ordinal,state,previous_event_digest,reason,occurred_at,"
        "case when length(cast(body_json as blob))<=? then body_json else null end "
        "from evolution_control_event order by ordinal",
        (MAX_CONTROL_BODY_BYTES,),
    )
    for ordinal, row in enumerate(cursor, start=1):
        if ordinal > max_events or type(row[6]) is not str:
            raise PolicyViolation("Evolution control history read budget exceeded")
        total += len(row[6].encode("utf-8"))
        if total > max_history_bytes:
            raise PolicyViolation("Evolution control history byte budget exceeded")
        try:
            body = json.loads(row[6], object_pairs_hook=_pairs, parse_constant=_constant)
        except (ValueError, RecursionError) as exc:
            raise PolicyViolation("Evolution control strict JSON required") from exc
        validate_control_body(
            body, ordinal=ordinal, previous_state=result["state"], previous_digest=previous
        )
        if (
            type(row[1]) is not int
            or row[1] != ordinal
            or row[0] != digest(body)
            or (row[2], row[3], row[4], row[5])
            != (body["state"], previous, body["reason"], body["occurred_at"])
        ):
            raise PolicyViolation("Evolution control row/hash/chain drift")
        _digest(row[0])
        previous = row[0]
        result = {
            "event_digest": row[0],
            "ordinal": ordinal,
            "state": body["state"],
            "reason": body["reason"],
            "occurred_at": body["occurred_at"],
        }
        for key in ("enable_plan_digest", "admission_evidence", "bootstrap_settlement"):
            if key == "bootstrap_settlement" and not include_bootstrap:
                continue
            if body.get(key) is not None:
                result[key] = body[key]
    return result
