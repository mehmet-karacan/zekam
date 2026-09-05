from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from zekam.application.client_lifecycle_spool import (
    CanonicalLifecycleReceipt,
    ClientLifecycleSpool,
    LifecycleSpoolEntry,
    _entry_from_document,
    _parse_timestamp,
    _safe_text,
    _timestamp,
    _validate_continuity_binding,
    _validate_continuity_preflight,
    canonical_lifecycle_event,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.codex_lifecycle import parse_codex_hook_input

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
SESSION_ID = "018f0000-0000-7000-8000-0000000000aa"
UUID_VALUES = [str(UUID(f"018f0000-0000-7000-8000-{index:012d}")) for index in range(1, 11)]


def _entry(
    tmp_path: Path, *, precompact: bool = False
) -> tuple[ClientLifecycleSpool, LifecycleSpoolEntry]:
    payload: dict[str, object] = {
        "session_id": SESSION_ID,
        "hook_event_name": "PreCompact" if precompact else "SessionStart",
    }
    if precompact:
        payload |= {
            "turn_id": "018f0000-0000-7000-8000-0000000000ab",
            "trigger": "manual",
        }
    else:
        payload |= {"source": "startup", "permission_mode": "default"}
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    observation = parse_codex_hook_input(json.dumps(payload)).observation_body()
    return spool, spool.stage(observation, delivery_id=digest(payload), occurred_at=NOW)


def _event_and_ack(
    spool: ClientLifecycleSpool, entry: LifecycleSpoolEntry
) -> tuple[dict[str, Any], SimpleNamespace]:
    event = canonical_lifecycle_event(
        entry,
        client_instance_id=spool.client_instance_id(),
        previous_canonical_event_digest=None,
    )
    ack = SimpleNamespace(
        event_id=UUID(UUID_VALUES[0]),
        local_event_digest=event["event_digest"],
        canonical_digest=digest("ack"),
    )
    return event, ack


def _binding(entry: LifecycleSpoolEntry, event_digest: str) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": "zekam-client-lifecycle-continuity-binding/v1",
        "entry_digest": entry.entry_digest,
        "canonical_event_digest": event_digest,
        "realm_id": UUID_VALUES[0],
        "project_id": UUID_VALUES[1],
        "work_item_id": UUID_VALUES[2],
        "run_id": UUID_VALUES[3],
        "authorization_id": UUID_VALUES[4],
        "job_id": UUID_VALUES[5],
        "claim_id": UUID_VALUES[6],
        "plan_digest": digest("plan"),
        "effect_digest": digest("effect"),
        "effect_receipt_id": UUID_VALUES[7],
        "effect_receipt_digest": digest("effect-receipt"),
        "continuity_event_id": UUID_VALUES[8],
        "continuity_event_digest": digest("continuity-event"),
        "delivery_outbox_id": UUID_VALUES[9],
        "terminal_receipt_digest": digest("terminal-receipt"),
        "event_type": entry.internal_event_type,
        "session_id": entry.session_id,
        "client_id": entry.client_id,
        "compiler_enqueue": entry.internal_event_type == "pre_compaction",
        "status": "completed",
        "grants_authority": False,
    }
    return body | {"binding_digest": digest(body)}


def _preflight(
    entry: LifecycleSpoolEntry, event_digest: str, instance_id: str
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": "zekam-client-lifecycle-continuity-preflight/v1",
        "entry_digest": entry.entry_digest,
        "canonical_event_digest": event_digest,
        "client_instance_id": instance_id,
        "realm_id": UUID_VALUES[0],
        "project_id": UUID_VALUES[1],
        "work_item_id": UUID_VALUES[2],
        "run_id": UUID_VALUES[3],
        "authorization_id": UUID_VALUES[4],
        "job_id": UUID_VALUES[5],
        "claim_id": UUID_VALUES[6],
        "plan_digest": digest("plan"),
        "effect_digest": digest("effect"),
        "allowed": True,
        "mutation_performed": False,
        "grants_authority": False,
    }
    return body | {"preflight_digest": digest(body)}


def test_spool_timestamp_and_text_helpers_reject_ambiguous_values() -> None:
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        _timestamp(NOW.replace(tzinfo=None), label="time")
    with pytest.raises(ValidationFailed, match="timestamp olmali"):
        _parse_timestamp(1, label="time")
    with pytest.raises(ValidationFailed, match="timestamp gecersiz"):
        _parse_timestamp("not-a-time", label="time")
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        _parse_timestamp("2026-09-04T12:00:00", label="time")
    assert _safe_text(None, label="optional", optional=True) is None
    with pytest.raises(ValidationFailed, match="bounded metin"):
        _safe_text([], label="text")


def test_verified_receipt_rejects_every_cross_record_drift(tmp_path: Path) -> None:
    spool, entry = _entry(tmp_path)
    event, ack = _event_and_ack(spool, entry)
    bad_event = dict(event)
    bad_event["event_digest"] = digest("wrong")
    with pytest.raises(PolicyViolation, match="event digest mismatch"):
        CanonicalLifecycleReceipt.verified(entry, bad_event, ack, ack)

    wrong_local = SimpleNamespace(**(vars(ack) | {"local_event_digest": digest("wrong")}))
    with pytest.raises(PolicyViolation, match="event binding mismatch"):
        CanonicalLifecycleReceipt.verified(entry, event, wrong_local, ack)
    non_uuid = SimpleNamespace(**(vars(ack) | {"event_id": str(ack.event_id)}))
    with pytest.raises(ValidationFailed, match="event_id UUID"):
        CanonicalLifecycleReceipt.verified(entry, event, non_uuid, non_uuid)
    other_id = SimpleNamespace(**(vars(ack) | {"event_id": UUID(UUID_VALUES[1])}))
    with pytest.raises(PolicyViolation, match="lookup drift"):
        CanonicalLifecycleReceipt.verified(entry, event, ack, other_id)
    other_digest = SimpleNamespace(**(vars(ack) | {"canonical_digest": digest("other-ack")}))
    with pytest.raises(PolicyViolation, match="lookup drift"):
        CanonicalLifecycleReceipt.verified(entry, event, ack, other_digest)

    unexpected = SimpleNamespace(
        **vars(ack),
        compaction_outbox_id=UUID(UUID_VALUES[2]),
        compaction_payload_digest=digest("unexpected"),
    )
    with pytest.raises(PolicyViolation, match="unexpected runtime binding"):
        CanonicalLifecycleReceipt.verified(entry, event, unexpected, unexpected)

    compact_spool, compact_entry = _entry(tmp_path / "compact", precompact=True)
    compact_event, compact_ack = _event_and_ack(compact_spool, compact_entry)
    with pytest.raises(PolicyViolation, match="runtime binding outbox"):
        CanonicalLifecycleReceipt.verified(compact_entry, compact_event, compact_ack, compact_ack)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("schema", "wrong", PolicyViolation),
        ("realm_id", 1, ValidationFailed),
        ("realm_id", "018F0000-0000-7000-8000-000000000001", ValidationFailed),
        ("compiler_enqueue", 1, ValidationFailed),
        ("binding_digest", digest("wrong"), PolicyViolation),
    ],
)
def test_continuity_binding_rejects_schema_type_and_digest_drift(
    tmp_path: Path, field: str, value: object, error: type[Exception]
) -> None:
    spool, entry = _entry(tmp_path)
    event, _ = _event_and_ack(spool, entry)
    candidate = _binding(entry, str(event["event_digest"]))
    candidate[field] = value
    with pytest.raises(error):
        _validate_continuity_binding(
            candidate,
            entry=entry,
            canonical_event_digest=str(event["event_digest"]),
        )


def test_continuity_binding_rejects_precompact_without_enqueue(tmp_path: Path) -> None:
    spool, entry = _entry(tmp_path, precompact=True)
    event, _ = _event_and_ack(spool, entry)
    candidate = _binding(entry, str(event["event_digest"]))
    candidate["compiler_enqueue"] = False
    candidate["binding_digest"] = digest(
        {key: value for key, value in candidate.items() if key != "binding_digest"}
    )
    with pytest.raises(PolicyViolation, match="compiler enqueue"):
        _validate_continuity_binding(
            candidate,
            entry=entry,
            canonical_event_digest=str(event["event_digest"]),
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("allowed", False, PolicyViolation),
        ("realm_id", None, ValidationFailed),
        ("realm_id", "018F0000-0000-7000-8000-000000000001", ValidationFailed),
        ("preflight_digest", digest("wrong"), PolicyViolation),
    ],
)
def test_continuity_preflight_rejects_authority_type_and_digest_drift(
    tmp_path: Path, field: str, value: object, error: type[Exception]
) -> None:
    spool, entry = _entry(tmp_path)
    event, _ = _event_and_ack(spool, entry)
    instance_id = spool.client_instance_id()
    candidate = _preflight(entry, str(event["event_digest"]), instance_id)
    candidate[field] = value
    with pytest.raises(error):
        _validate_continuity_preflight(
            candidate,
            entry=entry,
            canonical_event_digest=str(event["event_digest"]),
            client_instance_id=instance_id,
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda value: [], ValidationFailed),
        (lambda value: value | {"schema": "wrong"}, ValidationFailed),
        (lambda value: value | {"grants_authority": True}, PolicyViolation),
        (lambda value: value | {"observation": []}, ValidationFailed),
        (lambda value: value | {"sequence": True}, ValidationFailed),
    ],
)
def test_entry_document_rejects_wrong_shapes_and_authority(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], object],
    error: type[Exception],
) -> None:
    _, entry = _entry(tmp_path)
    with pytest.raises(error):
        _entry_from_document(mutation(entry.as_dict()))


def test_receipt_binding_replay_and_entry_binding_are_fail_closed(tmp_path: Path) -> None:
    spool, entry = _entry(tmp_path)
    event, ack = _event_and_ack(spool, entry)
    receipt = CanonicalLifecycleReceipt.verified(entry, event, ack, ack)
    binding = _binding(entry, receipt.canonical_event_digest)
    bound = receipt.bind_continuity(entry, binding)
    drifted = dict(binding)
    drifted["terminal_receipt_digest"] = digest("other-terminal")
    drifted["binding_digest"] = digest(
        {key: value for key, value in drifted.items() if key != "binding_digest"}
    )
    with pytest.raises(PolicyViolation, match="replay drift"):
        bound.bind_continuity(entry, drifted)
    with pytest.raises(PolicyViolation, match="spool binding mismatch"):
        replace(bound, entry_digest=digest("wrong-entry")).assert_binding(entry)
