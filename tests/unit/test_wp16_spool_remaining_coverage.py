from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from zekam.application import client_lifecycle_spool as lifecycle
from zekam.application.client_lifecycle_spool import (
    CanonicalLifecycleReceipt,
    ClientLifecycleSpool,
    LifecycleSpoolEntry,
    canonical_lifecycle_event,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.codex_lifecycle import parse_codex_hook_input

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
SESSION_ID = "018f0000-0000-7000-8000-0000000000aa"
UUIDS = tuple(str(UUID(f"018f0000-0000-7000-8000-{index:012d}")) for index in range(1, 11))


def _observation() -> dict[str, object]:
    return parse_codex_hook_input(
        json.dumps(
            {
                "session_id": SESSION_ID,
                "hook_event_name": "SessionStart",
                "source": "startup",
                "permission_mode": "default",
            }
        )
    ).observation_body()


def _spool(tmp_path: Path) -> ClientLifecycleSpool:
    return ClientLifecycleSpool(tmp_path / "home", client_id="codex")


def _stage(spool: ClientLifecycleSpool) -> LifecycleSpoolEntry:
    return spool.stage(_observation(), delivery_id=digest("delivery"), occurred_at=NOW)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    assert type(value) is dict
    return value


def _redigest(document: dict[str, Any], field: str) -> dict[str, Any]:
    body = {key: value for key, value in document.items() if key != field}
    document[field] = digest(body)
    return document


def _binding(entry: LifecycleSpoolEntry, event_digest: str) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": lifecycle.CONTINUITY_BINDING_SCHEMA,
        "entry_digest": entry.entry_digest,
        "canonical_event_digest": event_digest,
        "realm_id": UUIDS[0],
        "project_id": UUIDS[1],
        "work_item_id": UUIDS[2],
        "run_id": UUIDS[3],
        "authorization_id": UUIDS[4],
        "job_id": UUIDS[5],
        "claim_id": UUIDS[6],
        "plan_digest": digest("plan"),
        "effect_digest": digest("effect"),
        "effect_receipt_id": UUIDS[7],
        "effect_receipt_digest": digest("effect-receipt"),
        "continuity_event_id": UUIDS[8],
        "continuity_event_digest": digest("continuity-event"),
        "delivery_outbox_id": UUIDS[9],
        "terminal_receipt_digest": digest("terminal-receipt"),
        "event_type": entry.internal_event_type,
        "session_id": entry.session_id,
        "client_id": entry.client_id,
        "compiler_enqueue": False,
        "status": "completed",
        "grants_authority": False,
    }
    return body | {"binding_digest": digest(body)}


def _preflight(
    entry: LifecycleSpoolEntry, event_digest: str, instance_id: str
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": lifecycle.CONTINUITY_PREFLIGHT_SCHEMA,
        "entry_digest": entry.entry_digest,
        "canonical_event_digest": event_digest,
        "client_instance_id": instance_id,
        "realm_id": UUIDS[0],
        "project_id": UUIDS[1],
        "work_item_id": UUIDS[2],
        "run_id": UUIDS[3],
        "authorization_id": UUIDS[4],
        "job_id": UUIDS[5],
        "claim_id": UUIDS[6],
        "plan_digest": digest("plan"),
        "effect_digest": digest("effect"),
        "allowed": True,
        "mutation_performed": False,
        "grants_authority": False,
    }
    return body | {"preflight_digest": digest(body)}


def _receipt(spool: ClientLifecycleSpool, entry: LifecycleSpoolEntry) -> CanonicalLifecycleReceipt:
    event = canonical_lifecycle_event(
        entry,
        client_instance_id=spool.client_instance_id(),
        previous_canonical_event_digest=None,
    )
    ack = SimpleNamespace(
        event_id=UUID(UUIDS[0]),
        local_event_digest=event["event_digest"],
        canonical_digest=digest("canonical-ack"),
    )
    return CanonicalLifecycleReceipt.verified(entry, event, ack, ack).bind_continuity(
        entry, _binding(entry, str(event["event_digest"]))
    )


def _artifact_set(
    tmp_path: Path,
) -> tuple[ClientLifecycleSpool, LifecycleSpoolEntry, dict[str, dict[str, Any]]]:
    spool = _spool(tmp_path)
    entry = _stage(spool)
    spool.client_instance_id()
    attempt = spool.record_attempt(
        entry.entry_digest,
        outcome="failed",
        evidence_digest=digest("failure"),
        attempted_at=NOW,
    )
    return (
        spool,
        entry,
        {
            "delivery": _read(spool._delivery_path(entry.delivery_id)),
            "queue_ref": _read(spool._queue_path(1)),
            "queue_state": _read(spool.queue_state_path),
            "checkpoint": _read(spool._session_path(entry.client_id, entry.session_id)),
            "attempt": _read(spool._attempt_path(str(attempt["retry_key"]))),
            "attempt_state": _read(spool._attempt_state_path(entry.entry_digest)),
            "instance": _read(spool.instance_path),
        },
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.pop("schema"),
        lambda value: value.__setitem__("grants_authority", True),
        lambda value: value.__setitem__("client_id", []),
        lambda value: value.__setitem__("stop_hook_active", 1),
        lambda value: value.__setitem__("wire_digest", 1),
    ),
)
def test_observation_validator_rejects_schema_authority_and_types(
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    observation = dict(_observation())
    mutate(observation)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        lifecycle._validate_observation(observation)


def test_entry_integrity_rejects_sequence_digest_and_envelope_drift(tmp_path: Path) -> None:
    _, entry = _artifact_set(tmp_path)[:2]
    for changed in (
        replace(entry, sequence=0),
        replace(entry, previous_entry_digest=digest("unexpected")),
        replace(entry, observation_digest=digest("wrong")),
        replace(entry, entry_digest=digest("wrong")),
        replace(entry, client_id="other"),
    ):
        with pytest.raises(PolicyViolation):
            changed.assert_integrity()


def test_preflight_and_terminal_receipt_binding_fail_closed(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    entry = _stage(spool)
    instance = spool.client_instance_id()
    event = canonical_lifecycle_event(
        entry,
        client_instance_id=instance,
        previous_canonical_event_digest=None,
    )
    preflight = _preflight(entry, str(event["event_digest"]), instance)
    lifecycle._validate_continuity_preflight(
        preflight,
        entry=entry,
        canonical_event_digest=str(event["event_digest"]),
        client_instance_id=instance,
    )
    with pytest.raises(ValidationFailed, match="client instance"):
        invalid = dict(preflight)
        invalid["client_instance_id"] = "not valid"
        _redigest(invalid, "preflight_digest")
        lifecycle._validate_continuity_preflight(
            invalid,
            entry=entry,
            canonical_event_digest=str(event["event_digest"]),
            client_instance_id="not valid",
        )
    receipt = _receipt(spool, entry)
    lifecycle._assert_preflight_receipt_binding(preflight, receipt)
    with pytest.raises(PolicyViolation, match="binding eksik"):
        lifecycle._assert_preflight_receipt_binding(
            preflight, replace(receipt, continuity_binding=None)
        )
    drift = dict(preflight)
    drift["job_id"] = UUIDS[9]
    with pytest.raises(PolicyViolation, match="binding drift"):
        lifecycle._assert_preflight_receipt_binding(drift, receipt)


def test_entry_document_rejects_authority_even_with_valid_shape(tmp_path: Path) -> None:
    entry = _stage(_spool(tmp_path))
    document = entry.as_dict()
    document["grants_authority"] = True
    with pytest.raises(PolicyViolation, match="authority"):
        lifecycle._entry_from_document(document)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.__setitem__("schema", "wrong"),
        lambda value: value.__setitem__("grants_authority", True),
        lambda value: value.__setitem__("entry_digest", 1),
        lambda value: value.__setitem__("client_id", []),
        lambda value: value.__setitem__("queue_sequence", True),
        lambda value: value.__setitem__("queue_sequence", 0),
        lambda value: value.__setitem__("ref_digest", digest("wrong")),
    ),
)
def test_delivery_validator_rejects_each_contract_class(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], object]
) -> None:
    _, entry, artifacts = _artifact_set(tmp_path)
    value = deepcopy(artifacts["delivery"])
    mutate(value)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        lifecycle._validate_delivery_ref(value, delivery_id=entry.delivery_id)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.__setitem__("schema", "wrong"),
        lambda value: value.__setitem__("queue_sequence", 2),
        lambda value: value.__setitem__("previous_queue_entry_digest", digest("unexpected")),
        lambda value: value.__setitem__("entry_digest", 1),
        lambda value: value.__setitem__("ref_digest", digest("wrong")),
    ),
)
def test_queue_ref_validator_rejects_binding_and_digest_drift(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], object]
) -> None:
    _, _, artifacts = _artifact_set(tmp_path)
    value = deepcopy(artifacts["queue_ref"])
    mutate(value)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        lifecycle._validate_queue_ref(value, queue_sequence=1, previous_queue_entry_digest=None)


def test_queue_ref_rejects_impossible_sequence_previous_pairs(tmp_path: Path) -> None:
    _, _, artifacts = _artifact_set(tmp_path)
    value = deepcopy(artifacts["queue_ref"])
    value["queue_sequence"] = 0
    _redigest(value, "ref_digest")
    with pytest.raises(ValidationFailed, match="sequence/previous"):
        lifecycle._validate_queue_ref(value, queue_sequence=0, previous_queue_entry_digest=None)
    candidate = deepcopy(artifacts["queue_ref"])
    candidate["queue_sequence"] = 1
    candidate["previous_queue_entry_digest"] = digest("unexpected")
    _redigest(candidate, "ref_digest")
    with pytest.raises(ValidationFailed, match="sequence/previous"):
        lifecycle._validate_queue_ref(
            candidate,
            queue_sequence=1,
            previous_queue_entry_digest=digest("unexpected"),
        )


@pytest.mark.parametrize(
    "change",
    (
        {"schema": "wrong"},
        {"state": "unknown"},
        {"grants_authority": True},
        {"tail_sequence": True},
        {"previous_tail_sequence": 7},
        {"tail_entry_digest": 1},
        {"previous_tail_entry_digest": digest("unexpected")},
        {"pending_entry": {}},
        {"state_digest": digest("wrong")},
    ),
)
def test_queue_state_validator_rejects_corrupt_contract(
    tmp_path: Path, change: dict[str, object]
) -> None:
    _, _, artifacts = _artifact_set(tmp_path)
    value = deepcopy(artifacts["queue_state"])
    value.update(change)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        lifecycle._validate_queue_state(value)


def test_queue_state_rejects_pending_entry_binding(tmp_path: Path) -> None:
    _, entry, artifacts = _artifact_set(tmp_path)
    value = deepcopy(artifacts["queue_state"])
    value["state"] = "pending"
    value["pending_entry"] = entry.as_dict() | {"entry_digest": digest("wrong")}
    _redigest(value, "state_digest")
    with pytest.raises(PolicyViolation):
        lifecycle._validate_queue_state(value)


@pytest.mark.parametrize(
    "change",
    (
        {"schema": "wrong"},
        {"state": "unknown"},
        {"grants_authority": True},
        {"sequence": True},
        {"previous_sequence": 4},
        {"entry_digest": 1},
        {"previous_entry_digest": digest("unexpected")},
        {"pending_entry": {}},
        {"checkpoint_digest": digest("wrong")},
    ),
)
def test_checkpoint_validator_rejects_corrupt_contract(
    tmp_path: Path, change: dict[str, object]
) -> None:
    _, entry, artifacts = _artifact_set(tmp_path)
    value = deepcopy(artifacts["checkpoint"])
    value.update(change)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        lifecycle._validate_checkpoint(
            value, client_id=entry.client_id, session_id=entry.session_id
        )


@pytest.mark.parametrize(
    "change",
    (
        {"schema": "wrong"},
        {"grants_authority": True},
        {"outcome": "unknown"},
        {"attempt_number": True},
        {"failure_count": -1},
        {"failure_count": 2},
        {"disposition": "unknown"},
        {"terminal_reason": "other"},
        {"predecessor_entry_digest": digest("unexpected")},
        {"retry_key": digest("wrong")},
        {"attempt_digest": digest("wrong")},
    ),
)
def test_attempt_validator_rejects_corrupt_state(tmp_path: Path, change: dict[str, object]) -> None:
    _, entry, artifacts = _artifact_set(tmp_path)
    value = deepcopy(artifacts["attempt"])
    value.update(change)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        lifecycle._validate_attempt(value, entry_digest=entry.entry_digest)


def test_attempt_validator_rejects_semantic_terminal_relations(tmp_path: Path) -> None:
    _, entry, artifacts = _artifact_set(tmp_path)
    base = artifacts["attempt"]
    cases = (
        {"outcome": "completed", "disposition": "retryable"},
        {"disposition": "manual-review", "terminal_reason": "retry-budget-exhausted"},
        {
            "disposition": "retryable",
            "attempt_number": lifecycle.MAX_REPLAY_FAILURES,
            "failure_count": lifecycle.MAX_REPLAY_FAILURES,
        },
    )
    for changes in cases:
        value = deepcopy(base)
        value.update(changes)
        _redigest(value, "attempt_digest")
        with pytest.raises(PolicyViolation):
            lifecycle._validate_attempt(value, entry_digest=entry.entry_digest)


@pytest.mark.parametrize(
    "change",
    (
        {"schema": "wrong"},
        {"grants_authority": True},
        {"attempt_count": True},
        {"failure_count": -1},
        {"failure_count": 2},
        {"disposition": "unknown"},
        {"terminal_reason": "other"},
        {"predecessor_entry_digest": digest("unexpected")},
        {"latest_attempt_ref": digest("wrong")},
        {"state_digest": digest("wrong")},
    ),
)
def test_attempt_state_validator_rejects_corrupt_contract(
    tmp_path: Path, change: dict[str, object]
) -> None:
    _, entry, artifacts = _artifact_set(tmp_path)
    value = deepcopy(artifacts["attempt_state"])
    value.update(change)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        lifecycle._validate_attempt_state(value, entry_digest=entry.entry_digest)


def test_attempt_state_rejects_retry_limit_and_terminal_reason(tmp_path: Path) -> None:
    _, entry, artifacts = _artifact_set(tmp_path)
    for changes in (
        {
            "disposition": "retryable",
            "attempt_count": lifecycle.MAX_REPLAY_FAILURES,
            "failure_count": lifecycle.MAX_REPLAY_FAILURES,
        },
        {"disposition": "manual-review", "terminal_reason": "retry-budget-exhausted"},
    ):
        value = deepcopy(artifacts["attempt_state"])
        value.update(changes)
        _redigest(value, "state_digest")
        with pytest.raises(PolicyViolation):
            lifecycle._validate_attempt_state(value, entry_digest=entry.entry_digest)


@pytest.mark.parametrize(
    "change",
    (
        {"schema": "wrong"},
        {"client_id": "other"},
        {"grants_authority": True},
        {"client_instance_id": "bad"},
        {"instance_digest": digest("wrong")},
    ),
)
def test_instance_validator_rejects_identity_and_digest_drift(
    tmp_path: Path, change: dict[str, object]
) -> None:
    spool, _, artifacts = _artifact_set(tmp_path)
    value = deepcopy(artifacts["instance"])
    value.update(change)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        lifecycle._validate_instance(value, spool.root.name)


def test_instance_rejects_noncanonical_identity_before_digest(tmp_path: Path) -> None:
    spool, _, artifacts = _artifact_set(tmp_path)
    value = deepcopy(artifacts["instance"])
    value["client_instance_id"] = "not valid"
    _redigest(value, "instance_digest")
    with pytest.raises(ValidationFailed, match="kimligi"):
        lifecycle._validate_instance(value, spool.root.name)


def test_ack_and_cursor_validators_reject_cross_record_tamper(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    entry = _stage(spool)
    receipt = _receipt(spool, entry)
    spool.acknowledge_committed_receipt(entry, receipt=receipt, acknowledged_at=NOW)
    ack = _read(spool._ack_path(entry.entry_digest))
    pointer = _read(spool.drain_cursor_path)
    record = _read(spool._drain_cursor_record_path(1))
    ack_changes: tuple[dict[str, Any], ...] = (
        {"schema": "wrong"},
        {"grants_authority": True},
        {"canonical_event_id": "bad"},
        {"runtime_binding_id": UUIDS[0]},
        {"continuity_binding": []},
        {"ack_digest": digest("wrong")},
        {"canonical_lookup_digest": digest("wrong")},
    )
    for change in ack_changes:
        value = deepcopy(ack)
        value.update(change)
        with pytest.raises((ValidationFailed, PolicyViolation)):
            lifecycle._validate_ack(value, entry_digest=entry.entry_digest)
    pointer_changes: tuple[dict[str, Any], ...] = (
        {"schema": "wrong"},
        {"queue_sequence": True},
        {"acknowledged_count": -1},
        {"pointer_digest": digest("wrong")},
    )
    for change in pointer_changes:
        value = deepcopy(pointer)
        value.update(change)
        with pytest.raises((ValidationFailed, PolicyViolation)):
            lifecycle._validate_drain_cursor_pointer(value)
    record_changes: tuple[dict[str, Any], ...] = (
        {"schema": "wrong"},
        {"queue_sequence": True},
        {"terminal_disposition": "other"},
        {"acknowledged_count": 0},
        {"previous_entry_digest": digest("unexpected")},
        {"cursor_digest": digest("wrong")},
    )
    for change in record_changes:
        value = deepcopy(record)
        value.update(change)
        with pytest.raises((ValidationFailed, PolicyViolation)):
            lifecycle._validate_drain_cursor_record(value, expected_sequence=1)


def test_ack_validator_rejects_deep_runtime_and_continuity_relations(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    entry = _stage(spool)
    spool.acknowledge_committed_receipt(entry, receipt=_receipt(spool, entry), acknowledged_at=NOW)
    ack = _read(spool._ack_path(entry.entry_digest))
    cases: tuple[Callable[[dict[str, Any]], None], ...] = (
        lambda value: value.__setitem__(
            "canonical_event_id", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()
        ),
        lambda value: value.update(runtime_binding_id=1, runtime_binding_digest=digest("runtime")),
        lambda value: value.update(
            runtime_binding_id="not-a-uuid", runtime_binding_digest=digest("runtime")
        ),
        lambda value: value.__setitem__("continuity_binding", None),
        lambda value: value["continuity_binding"].__setitem__("entry_digest", digest("wrong")),
        lambda value: value["continuity_binding"].__setitem__("binding_digest", 1),
        lambda value: value["continuity_binding"].__setitem__("realm_id", "not-a-uuid"),
        lambda value: value["continuity_binding"].__setitem__("compiler_enqueue", 1),
        lambda value: value.__setitem__("acknowledged_at", "not-a-time"),
    )
    for mutate in cases:
        value = deepcopy(ack)
        mutate(value)
        with pytest.raises((ValidationFailed, PolicyViolation, TypeError)):
            lifecycle._validate_ack(value, entry_digest=entry.entry_digest)

    with pytest.raises(ValidationFailed, match="digest alanlari"):
        value = deepcopy(ack)
        value["canonical_ack_digest"] = 1
        lifecycle._validate_ack(value, entry_digest=entry.entry_digest)
    with pytest.raises(ValidationFailed, match="runtime binding"):
        value = deepcopy(ack)
        value.update(runtime_binding_id=1, runtime_binding_digest=digest("runtime"))
        lifecycle._validate_ack(value, entry_digest=entry.entry_digest)
    with pytest.raises(ValidationFailed, match="runtime binding UUID"):
        value = deepcopy(ack)
        value.update(runtime_binding_id="not-a-uuid", runtime_binding_digest=digest("runtime"))
        lifecycle._validate_ack(value, entry_digest=entry.entry_digest)
    for field, changed in (("compiler_enqueue", 1), ("realm_id", "not-a-uuid")):
        value = deepcopy(ack)
        continuity = value["continuity_binding"]
        assert type(continuity) is dict
        continuity[field] = changed
        _redigest(continuity, "binding_digest")
        with pytest.raises(ValidationFailed):
            lifecycle._validate_ack(value, entry_digest=entry.entry_digest)
    value = deepcopy(ack)
    continuity = value["continuity_binding"]
    assert type(continuity) is dict
    continuity["event_type"] = "pre_compaction"
    continuity["compiler_enqueue"] = False
    _redigest(continuity, "binding_digest")
    with pytest.raises(PolicyViolation, match="compiler enqueue"):
        lifecycle._validate_ack(value, entry_digest=entry.entry_digest)
    value = deepcopy(ack)
    continuity = value["continuity_binding"]
    assert type(continuity) is dict
    continuity["realm_id"] = 1
    _redigest(continuity, "binding_digest")
    with pytest.raises(ValidationFailed, match="realm_id UUID"):
        lifecycle._validate_ack(value, entry_digest=entry.entry_digest)


def test_checkpoint_pending_entry_must_match_outer_record(tmp_path: Path) -> None:
    _, entry, artifacts = _artifact_set(tmp_path)
    checkpoint = deepcopy(artifacts["checkpoint"])
    pending = entry.as_dict()
    pending["delivery_id"] = digest("different-delivery")
    pending["entry_digest"] = digest(
        {key: value for key, value in pending.items() if key != "entry_digest"}
    )
    checkpoint["state"] = "pending"
    checkpoint["pending_entry"] = pending
    _redigest(checkpoint, "checkpoint_digest")
    with pytest.raises(PolicyViolation, match="entry binding mismatch"):
        lifecycle._validate_checkpoint(
            checkpoint, client_id=entry.client_id, session_id=entry.session_id
        )


def test_attempt_predecessor_manual_review_and_retry_budget_contracts(tmp_path: Path) -> None:
    _, entry, artifacts = _artifact_set(tmp_path)
    predecessor_entry = digest("predecessor-entry")
    predecessor_state = digest("predecessor-state")
    evidence = digest(
        {
            "schema": "zekam-client-lifecycle-predecessor-block/v1",
            "entry_digest": entry.entry_digest,
            "predecessor_entry_digest": predecessor_entry,
            "predecessor_attempt_state_digest": predecessor_state,
            "grants_authority": False,
        }
    )
    attempt = deepcopy(artifacts["attempt"])
    attempt.update(
        outcome="rejected",
        evidence_digest=evidence,
        attempt_number=2,
        failure_count=2,
        disposition="manual-review",
        terminal_reason="predecessor-manual-review",
        predecessor_entry_digest=predecessor_entry,
        predecessor_attempt_state_digest=predecessor_state,
    )
    attempt["retry_key"] = digest(
        {
            "entry_digest": entry.entry_digest,
            "outcome": "rejected",
            "evidence_digest": evidence,
            "terminal_reason": "predecessor-manual-review",
        }
    )
    _redigest(attempt, "attempt_digest")
    lifecycle._validate_attempt(attempt, entry_digest=entry.entry_digest)
    wrong = deepcopy(attempt)
    wrong["evidence_digest"] = digest("wrong")
    _redigest(wrong, "attempt_digest")
    with pytest.raises(PolicyViolation, match="evidence mismatch"):
        lifecycle._validate_attempt(wrong, entry_digest=entry.entry_digest)

    exhausted = deepcopy(artifacts["attempt"])
    exhausted.update(
        attempt_number=lifecycle.MAX_REPLAY_FAILURES,
        failure_count=lifecycle.MAX_REPLAY_FAILURES,
        disposition="manual-review",
        terminal_reason="retry-budget-exhausted",
    )
    _redigest(exhausted, "attempt_digest")
    lifecycle._validate_attempt(exhausted, entry_digest=entry.entry_digest)


def test_attempt_state_accepts_exact_predecessor_terminal_relation(tmp_path: Path) -> None:
    _, entry, artifacts = _artifact_set(tmp_path)
    state = deepcopy(artifacts["attempt_state"])
    state.update(
        attempt_count=2,
        failure_count=2,
        disposition="manual-review",
        terminal_reason="predecessor-manual-review",
        predecessor_entry_digest=digest("predecessor-entry"),
        predecessor_attempt_state_digest=digest("predecessor-state"),
    )
    _redigest(state, "state_digest")
    lifecycle._validate_attempt_state(state, entry_digest=entry.entry_digest)


def test_manual_review_cursor_rejects_ack_fields(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    entry = _stage(spool)
    for index in range(lifecycle.MAX_REPLAY_FAILURES):
        spool.record_attempt(
            entry.entry_digest,
            outcome="failed",
            evidence_digest=digest(f"failure-{index}"),
            attempted_at=NOW + dt.timedelta(seconds=index),
        )
    assert spool.pending() == ()
    record = _read(spool._drain_cursor_record_path(1))
    lifecycle._validate_drain_cursor_record(record, expected_sequence=1)
    record["ack_digest"] = digest("forged")
    _redigest(record, "cursor_digest")
    with pytest.raises(PolicyViolation, match="ACK alani"):
        lifecycle._validate_drain_cursor_record(record, expected_sequence=1)


def test_filesystem_helpers_reject_partial_oversize_and_unexpected_entries(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    assert lifecycle._safe_directory_exists(missing) is False
    assert lifecycle._safe_regular_file_exists(missing) is False
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(PolicyViolation):
        lifecycle._safe_regular_file_exists(directory)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (lifecycle.MAX_SPOOL_DOCUMENT_BYTES + 1))
    with pytest.raises(PolicyViolation, match="siniri"):
        lifecycle._read_bounded_bytes(oversized)
    (directory / "unexpected.txt").write_text("x")
    with pytest.raises(PolicyViolation, match="beklenmeyen artifact"):
        lifecycle._safe_json_files(directory)


def test_corrupt_json_and_unsafe_parent_chain_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolation, match="spool dizini"):
        lifecycle._assert_json_directory(tmp_path / "absent", label="coverage")
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_bytes(b"{")
    with pytest.raises(ValidationFailed, match="okunamadi"):
        lifecycle._read_json(corrupt)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PolicyViolation, match="reparse/symlink"):
        lifecycle._assert_safe_parent_chain(link / "child")
