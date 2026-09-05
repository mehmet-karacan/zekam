from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from zekam.application import client_lifecycle_spool as lifecycle
from zekam.application.client_lifecycle_spool import (
    CanonicalLifecycleReceipt,
    ClientLifecycleSpool,
    LifecycleSpoolEntry,
    canonical_lifecycle_event,
)
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.codex_lifecycle import parse_codex_hook_input

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
SESSION_ID = "018f0000-0000-7000-8000-0000000000aa"
UUID_VALUES = [str(UUID(f"018f0000-0000-7000-8000-{index:012d}")) for index in range(1, 11)]


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


def _stage(
    spool: ClientLifecycleSpool, label: str = "one", *, at: dt.datetime = NOW
) -> LifecycleSpoolEntry:
    return spool.stage(_observation(), delivery_id=digest(label), occurred_at=at)


def _binding(entry: LifecycleSpoolEntry, event_digest: str) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": lifecycle.CONTINUITY_BINDING_SCHEMA,
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
        "compiler_enqueue": False,
        "status": "completed",
        "grants_authority": False,
    }
    return body | {"binding_digest": digest(body)}


def _receipt(spool: ClientLifecycleSpool, entry: LifecycleSpoolEntry) -> CanonicalLifecycleReceipt:
    event = canonical_lifecycle_event(
        entry,
        client_instance_id=spool.client_instance_id(),
        previous_canonical_event_digest=spool.previous_canonical_event_digest(entry),
    )
    ack = SimpleNamespace(
        event_id=UUID(UUID_VALUES[0]),
        local_event_digest=event["event_digest"],
        canonical_digest=digest("canonical-ack"),
    )
    receipt = CanonicalLifecycleReceipt.verified(entry, event, ack, ack)
    return receipt.bind_continuity(entry, _binding(entry, str(event["event_digest"])))


def _rewrite(path: Path, document: dict[str, object]) -> None:
    path.write_bytes(canonical_bytes(document) + b"\n")


def test_concurrent_identical_stage_is_one_immutable_event(tmp_path: Path) -> None:
    spool = _spool(tmp_path)

    def stage_once(_index: int) -> LifecycleSpoolEntry:
        return _stage(spool)

    with ThreadPoolExecutor(max_workers=8) as executor:
        entries = tuple(executor.map(stage_once, range(16)))
    assert {entry.entry_digest for entry in entries} == {entries[0].entry_digest}
    assert len(tuple(spool.events_directory.glob("*.json"))) == 1
    assert spool.pending() == (entries[0],)
    restarted = _spool(tmp_path)
    assert restarted.pending() == (entries[0],)


def test_queue_pending_partial_write_is_recovered_before_next_stage(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    first = _stage(spool)
    pending = spool._queue_state_document(
        first,
        queue_sequence=1,
        previous_queue_entry_digest=None,
        state="pending",
    )
    lifecycle._write_atomic_json(spool.queue_state_path, pending)
    for path in (
        spool._entry_path(first.entry_digest),
        spool._delivery_path(first.delivery_id),
        spool._queue_path(1),
        spool._session_path(first.client_id, first.session_id),
    ):
        path.unlink()

    restarted = _spool(tmp_path)
    second = _stage(restarted, "two", at=NOW + dt.timedelta(seconds=1))
    assert second.sequence == 2
    assert restarted.pending() == (first, second)
    assert json.loads(restarted.queue_state_path.read_bytes())["state"] == "committed"


def test_attempt_orphan_recovery_and_terminal_replay_are_exact(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    entry = _stage(spool)
    first = spool.record_attempt(
        entry.entry_digest,
        outcome="failed",
        evidence_digest=digest("failure-one"),
        attempted_at=NOW,
    )
    spool._attempt_state_path(entry.entry_digest).unlink()
    recovered = spool.record_attempt(
        entry.entry_digest,
        outcome="failed",
        evidence_digest=digest("failure-one"),
        attempted_at=NOW + dt.timedelta(seconds=1),
    )
    assert recovered == first
    assert spool.status()["page_attempt_count"] == 1

    completed = spool.record_attempt(
        entry.entry_digest,
        outcome="completed",
        evidence_digest=digest("complete"),
        attempted_at=NOW + dt.timedelta(seconds=2),
    )
    assert completed["disposition"] == "completed"
    with pytest.raises(PolicyViolation, match="terminal attempt-state"):
        spool.record_attempt(
            entry.entry_digest,
            outcome="failed",
            evidence_digest=digest("late"),
            attempted_at=NOW + dt.timedelta(seconds=3),
        )


def test_retry_budget_advances_manual_review_cursor_across_restart(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    entry = _stage(spool)
    for index in range(lifecycle.MAX_REPLAY_FAILURES):
        result = spool.record_attempt(
            entry.entry_digest,
            outcome="failed",
            evidence_digest=digest(f"failure-{index}"),
            attempted_at=NOW + dt.timedelta(seconds=index),
        )
    assert result["disposition"] == "manual-review"
    assert spool.pending() == ()
    status = _spool(tmp_path).status()
    assert status["resolved_count"] == 1
    assert status["resolved_manual_review_count"] == 1
    assert status["page_manual_review_count"] == 1


def test_ack_cursor_is_rebuilt_after_pointer_loss(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    entry = _stage(spool)
    receipt = _receipt(spool, entry)
    result = spool.acknowledge_committed_receipt(entry, receipt=receipt, acknowledged_at=NOW)
    assert result.outcome == "completed"
    assert spool.pending() == ()
    spool.drain_cursor_path.unlink()

    restarted = _spool(tmp_path)
    assert restarted.pending() == ()
    assert restarted.drain_cursor_path.is_file()
    status = restarted.status()
    assert status["acked_count"] == 1
    assert status["resolved_count"] == 1


def test_ack_and_cursor_corruption_fail_closed(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    entry = _stage(spool)
    spool.acknowledge_committed_receipt(entry, receipt=_receipt(spool, entry), acknowledged_at=NOW)
    ack_path = spool._ack_path(entry.entry_digest)
    ack_path.write_bytes(b"{")
    with pytest.raises(ValidationFailed, match="document okunamadi"):
        spool._verified_ack_entry_digests(spool._verified_entries())

    other = _spool(tmp_path / "other")
    other_entry = _stage(other)
    other.acknowledge_committed_receipt(
        other_entry, receipt=_receipt(other, other_entry), acknowledged_at=NOW
    )
    pointer = json.loads(other.drain_cursor_path.read_bytes())
    pointer["entry_digest"] = digest("wrong")
    pointer_body = {key: value for key, value in pointer.items() if key != "pointer_digest"}
    pointer["pointer_digest"] = digest(pointer_body)
    _rewrite(other.drain_cursor_path, pointer)
    with pytest.raises(PolicyViolation, match="pointer binding mismatch"):
        other.pending()


def test_queue_delivery_and_attempt_state_corruption_fail_closed(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    _stage(spool)
    queue_path = spool._queue_path(1)
    queue_path.write_bytes(b"not-json")
    with pytest.raises(ValidationFailed, match="document okunamadi"):
        spool.pending()

    other = _spool(tmp_path / "delivery")
    other_entry = _stage(other)
    delivery_path = other._delivery_path(other_entry.delivery_id)
    delivery = json.loads(delivery_path.read_bytes())
    delivery["queue_sequence"] = 2
    body = {key: value for key, value in delivery.items() if key != "ref_digest"}
    delivery["ref_digest"] = digest(body)
    _rewrite(delivery_path, delivery)
    with pytest.raises(PolicyViolation, match="queue/delivery binding mismatch"):
        other.pending()

    third = _spool(tmp_path / "attempt")
    third_entry = _stage(third)
    third.record_attempt(
        third_entry.entry_digest,
        outcome="failed",
        evidence_digest=digest("failure"),
        attempted_at=NOW,
    )
    state_path = third._attempt_state_path(third_entry.entry_digest)
    state = json.loads(state_path.read_bytes())
    state["failure_count"] = 0
    state_body = {key: value for key, value in state.items() if key != "state_digest"}
    state["state_digest"] = digest(state_body)
    _rewrite(state_path, state)
    with pytest.raises(PolicyViolation, match="state/latest receipt parity mismatch"):
        third.status()


def test_symlink_and_unexpected_artifact_are_rejected(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    spool.events_directory.parent.mkdir(parents=True)
    target = tmp_path / "foreign"
    target.mkdir()
    spool.events_directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(PolicyViolation, match="reparse/symlink"):
        spool._verified_entries()

    other = _spool(tmp_path / "artifact")
    _stage(other)
    (other.events_directory / "unexpected.txt").write_text("x")
    with pytest.raises(PolicyViolation, match="beklenmeyen artifact"):
        other._verified_entries()


def test_status_pagination_and_cursor_ahead_are_bounded(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    first = _stage(spool, "one")
    _stage(spool, "two", at=NOW + dt.timedelta(seconds=1))
    page = spool.status(limit=1)
    assert page["page_event_count"] == 1
    assert page["next_after_sequence"] == 1
    second_page = spool.status(limit=1, after_sequence=1)
    assert second_page["page_event_count"] == 1
    assert second_page["next_after_sequence"] is None
    assert spool.previous_canonical_event_digest(first) is None
    with pytest.raises(ValidationFailed, match="pagination"):
        spool.status(after_sequence=-1)

    pointer_body = {
        "schema": lifecycle.SPOOL_DRAIN_CURSOR_POINTER_SCHEMA,
        "queue_sequence": 3,
        "entry_digest": digest("entry"),
        "cursor_digest": digest("cursor"),
        "acknowledged_count": 3,
        "manual_review_count": 0,
        "grants_authority": False,
    }
    _rewrite(spool.drain_cursor_path, pointer_body | {"pointer_digest": digest(pointer_body)})
    with pytest.raises((ValidationFailed, PolicyViolation)):
        spool.pending()


def test_concurrent_distinct_stages_form_one_queue_chain(tmp_path: Path) -> None:
    spool = _spool(tmp_path)

    def stage_index(index: int) -> LifecycleSpoolEntry:
        return _stage(spool, f"delivery-{index}", at=NOW + dt.timedelta(seconds=index))

    with ThreadPoolExecutor(max_workers=4) as executor:
        entries = tuple(executor.map(stage_index, range(8)))
    assert len({entry.entry_digest for entry in entries}) == 8
    pending = spool.pending(limit=8)
    assert tuple(item.sequence for item in pending) == tuple(range(1, 9))
    assert {item.delivery_id for item in pending} == {digest(f"delivery-{i}") for i in range(8)}


def test_atomic_write_collision_and_lock_timeout_are_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "immutable.json"
    lifecycle._write_immutable_json(path, {"value": 1})
    with pytest.raises(PolicyViolation, match="immutable spool collision"):
        lifecycle._write_immutable_json(path, {"value": 2})

    class Expired:
        def remaining_seconds(self) -> float:
            raise TimeoutError

    lock = tmp_path / "lock" / "writer.lock"
    with pytest.raises(TimeoutError), lifecycle._exclusive_lock(lock, deadline=Expired()):
        raise AssertionError("unreachable")
