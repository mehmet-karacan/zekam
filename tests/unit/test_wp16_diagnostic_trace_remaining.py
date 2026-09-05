from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.diagnostic_trace import (
    DiagnosticTracePolicy,
    ReducedTrace,
    TraceBundle,
    TraceEventRecord,
    TraceEventType,
    TraceGraphNode,
    TracePurgeCandidate,
    TraceVisibility,
    trace_event_body,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
DIGEST = digest("trace")


def _bundle(**changes: Any) -> TraceBundle:
    policy = DiagnosticTracePolicy(enabled=False)
    values: dict[str, Any] = {
        "id": uuid4(),
        "realm_id": uuid4(),
        "trace_ref": "trace:test",
        "project_id": None,
        "work_item_id": None,
        "run_id": None,
        "root_assignment_id": None,
        "root_client_session_id": "session:test",
        "policy": policy,
        "created_at": NOW,
        "expires_at": NOW + dt.timedelta(days=policy.retention_days),
    }
    values.update(changes)
    return TraceBundle(**values)


def _event(**changes: Any) -> TraceEventRecord:
    values: dict[str, Any] = {
        "id": uuid4(),
        "realm_id": uuid4(),
        "bundle_id": uuid4(),
        "sequence": 1,
        "event_type": TraceEventType.RUNTIME_STATE,
        "visibility": TraceVisibility.RUNTIME_ONLY,
        "occurred_at": NOW,
        "correlation": {"session": "one"},
        "payload_ref": DIGEST,
        "payload_cipher_digest": DIGEST,
        "payload_plain_digest": DIGEST,
        "payload_size_bytes": 1,
        "encryption_key_ref": "secretref:key",
        "redaction_digest": DIGEST,
        "previous_event_digest": None,
    }
    values.update(changes)
    grants_authority = bool(values.pop("grants_authority", False))
    body = trace_event_body(**values)
    return TraceEventRecord(
        **values,
        event_digest=digest(body),
        grants_authority=grants_authority,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"retention_days": 0},
        {"retention_days": 31},
        {"max_payload_bytes": 0},
        {"max_payload_bytes": 8_388_609},
        {"max_events": 0},
        {"max_events": 100_001},
        {"max_total_bytes": 0},
        {"max_total_bytes": 512 * 1_048_576 + 1},
        {"enabled": True},
        {"redaction_profile": " "},
        {"grants_authority": True},
    ],
)
def test_trace_policy_rejects_invalid_limits_missing_key_profile_and_authority(
    changes: dict[str, Any],
) -> None:
    values: dict[str, Any] = {"enabled": False}
    values.update(changes)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        DiagnosticTracePolicy(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"trace_ref": " "},
        {"root_client_session_id": " "},
        {"created_at": NOW.replace(tzinfo=None)},
        {"expires_at": NOW.replace(tzinfo=None)},
        {"expires_at": NOW + dt.timedelta(days=6)},
        {"state": "invalid"},
        {"grants_authority": True},
    ],
)
def test_trace_bundle_rejects_invalid_identity_time_retention_state_and_authority(
    changes: dict[str, Any],
) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _bundle(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"sequence": 0},
        {"occurred_at": NOW.replace(tzinfo=None)},
        {"correlation": {}},
        {"correlation": {" ": "value"}},
        {"correlation": {str(index): "value" for index in range(17)}},
        {"payload_ref": "bad"},
        {"previous_event_digest": "bad"},
        {"payload_size_bytes": 0},
        {"encryption_key_ref": " "},
        {"grants_authority": True},
        {"visibility": TraceVisibility.MODEL_VISIBLE},
    ],
)
def test_trace_event_rejects_invalid_sequence_correlation_digest_payload_and_visibility(
    changes: dict[str, Any],
) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _event(**changes)


def test_trace_event_rejects_semantic_digest_mismatch() -> None:
    event = _event()
    with pytest.raises(ValidationFailed, match="digest mismatch"):
        replace(event, payload_size_bytes=2)


def _reduced(**changes: Any) -> ReducedTrace:
    node = TraceGraphNode("node", "Event", TraceVisibility.RUNTIME_ONLY, 1, DIGEST)
    values: dict[str, Any] = {
        "bundle_id": uuid4(),
        "event_count": 1,
        "nodes": (node,),
        "edges": (),
        "first_event_digest": DIGEST,
        "last_event_digest": DIGEST,
        "reduced_at": NOW,
    }
    values.update(changes)
    return ReducedTrace(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"event_count": 0},
        {"event_count": 2},
        {"reduced_at": NOW.replace(tzinfo=None)},
        {"grants_authority": True},
    ],
)
def test_reduced_trace_rejects_invalid_count_time_and_authority(changes: dict[str, Any]) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _reduced(**changes)


def test_trace_purge_candidate_rejects_naive_expiry_and_invalid_payload_ref() -> None:
    with pytest.raises(ValidationFailed):
        TracePurgeCandidate(uuid4(), (), NOW.replace(tzinfo=None))
    with pytest.raises(ValidationFailed):
        TracePurgeCandidate(uuid4(), ("bad",), NOW)
