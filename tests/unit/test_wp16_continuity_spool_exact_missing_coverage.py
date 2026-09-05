# mypy: disable-error-code="arg-type,assignment,attr-defined,misc,unreachable"
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from tests.integration import test_local_continuity_v4_atomic_close as close_fixture
from tests.integration import test_local_continuity_v4_recovery as recovery_fixture
from tests.unit import test_wp16_runtime_lifecycle_remaining_coverage as bootstrap_fixture
from tests.unit import test_wp16_spool_remaining_coverage as spool_fixture

from zekam.application import client_lifecycle_spool as spool_module
from zekam.application import client_runtime_bootstrap as bootstrap_module
from zekam.application.client_lifecycle_spool import (
    ClientLifecycleSpool,
    LifecycleSpoolEntry,
    canonical_lifecycle_event,
)
from zekam.application.client_runtime_bootstrap import (
    ClaimedLifecycleBootstrapService,
    ClientRuntimeBootstrapService,
)
from zekam.application.local_continuity_v4_recovery import (
    ReceiptlessRecoveryRequest,
    ResolveEffectRecoveryRequest,
    UnknownEffectRequest,
)
from zekam.application.local_continuity_v4_writer import (
    ExactResolvedRecovery,
    FinalizeClosedWriteRequest,
    FrozenSpoolSnapshot,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.domain.realm import ActorKind, LifecycleStatus
from zekam.domain.runtime import JobKind
from zekam.domain.work import WorkState
from zekam.infrastructure.clients.codex_lifecycle import parse_codex_hook_input
from zekam.infrastructure.sqlite import local_continuity_v4_recovery as recovery_module
from zekam.infrastructure.sqlite import local_continuity_v4_writer as writer_module
from zekam.infrastructure.sqlite.local_continuity_v4_recovery import (
    SQLiteDormantV4Recovery,
    _safe_recheck,
    _safe_snapshot,
    verify_selected_b2_graph,
)
from zekam.infrastructure.sqlite.local_continuity_v4_writer import SQLiteDormantV4CloseWriter

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
D = digest("wp16-continuity-spool-exact-missing")
TS0 = "2026-09-04T12:00:00+00:00"
TS1 = "2026-09-04T12:00:01+00:00"
TS2 = "2026-09-04T12:00:30+00:00"


class _Cursor:
    def __init__(self, rows: object) -> None:
        self.rows = rows

    def fetchone(self) -> Any:
        if self.rows is None:
            return None
        if isinstance(self.rows, list):
            return None if not self.rows else self.rows[0]
        return self.rows

    def fetchall(self) -> list[Any]:
        if self.rows is None:
            return []
        return self.rows if isinstance(self.rows, list) else [self.rows]


class _ScriptDB:
    def __init__(self, rules: tuple[tuple[str, object], ...]) -> None:
        self.rules = rules

    def execute(self, sql: str, _values: object = ()) -> _Cursor:
        for marker, rows in self.rules:
            if marker in sql:
                return _Cursor(rows)
        return _Cursor(None)


def _observation(event: str = "SessionStart") -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": close_fixture.SESSION_ID,
        "hook_event_name": event,
    }
    if event == "SessionStart":
        payload["source"] = "startup"
        payload["permission_mode"] = "default"
    else:
        payload["turn_id"] = "018f0000-0000-7000-8000-000000000099"
        payload["trigger"] = "manual"
    return parse_codex_hook_input(json.dumps(payload)).observation_body()


def _spool(tmp_path: Path) -> tuple[ClientLifecycleSpool, LifecycleSpoolEntry]:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    entry = spool.stage(_observation(), delivery_id=digest("delivery"), occurred_at=NOW)
    return spool, entry


def test_spool_exact_public_sequence_path_and_document_guards(tmp_path: Path) -> None:
    spool, entry = _spool(tmp_path)
    invalid = entry.as_dict()
    invalid["sequence"] = True
    with pytest.raises(ValidationFailed, match="sequence"):
        spool_module._entry_from_document(invalid)
    with pytest.raises(ValidationFailed, match="client_instance_id"):
        canonical_lifecycle_event(
            entry,
            client_instance_id="not valid",
            previous_canonical_event_digest=None,
        )
    with pytest.raises(PolicyViolation, match="first canonical"):
        canonical_lifecycle_event(
            entry,
            client_instance_id="codex-instance",
            previous_canonical_event_digest=D,
        )
    for call in (
        lambda: spool._delivery_document(entry, queue_sequence=0),
        lambda: spool._queue_ref_document(
            entry, queue_sequence=0, previous_queue_entry_digest=None
        ),
        lambda: spool._queue_state_document(
            entry, queue_sequence=1, previous_queue_entry_digest=None, state="broken"
        ),
        lambda: spool._checkpoint_document(entry, state="broken"),
        lambda: spool._queue_path(0),
        lambda: spool._drain_cursor_record_path(0),
    ):
        with pytest.raises(ValidationFailed):
            call()
    assert spool_module._safe_json_files(tmp_path / "absent") == ()


def test_spool_bounded_tail_and_canonical_predecessor_guards(tmp_path: Path) -> None:
    spool, first = _spool(tmp_path)
    forged_first = replace(first, previous_entry_digest=D)
    with pytest.raises(PolicyViolation, match="first entry"):
        spool._assert_bounded_tail(forged_first)
    second = spool.stage(
        _observation("PreCompact"), delivery_id=digest("second-delivery"), occurred_at=NOW
    )
    with pytest.raises(PolicyViolation, match="predecessor receipt"):
        canonical_lifecycle_event(
            second,
            client_instance_id=spool.client_instance_id(),
            previous_canonical_event_digest=None,
        )
    predecessor_path = spool._entry_path(first.entry_digest)
    predecessor_path.unlink()
    with pytest.raises(PolicyViolation, match="tail kopuk"):
        spool._assert_bounded_tail(second)


def test_spool_oversize_atomic_and_immutable_writes_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "payload.json"
    huge = {"value": "x" * (spool_module.MAX_SPOOL_DOCUMENT_BYTES + 1)}
    with pytest.raises(PolicyViolation, match="boyut"):
        spool_module._write_immutable_json(target, huge)
    with pytest.raises(PolicyViolation, match="boyut"):
        spool_module._write_atomic_json(target, huge)


def test_spool_existing_link_and_lock_timeout_concurrency_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_text("source")
    target.write_text("target")
    with pytest.raises(FileExistsError):
        spool_module._link_immutable_no_follow(source, target)
    lock = tmp_path / "lock"
    monkeypatch.setattr(spool_module, "LOCK_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(spool_module, "LOCK_RETRY_INTERVAL_SECONDS", 0.0)

    def blocked(_fd: int, _mode: int) -> None:
        raise OSError("busy")

    monkeypatch.setattr(fcntl, "flock", blocked)
    with pytest.raises(ConcurrencyConflict, match="lock alinmadi"):  # noqa: SIM117
        with spool_module._exclusive_lock(lock):
            pass


def test_spool_missing_ack_and_attempt_directories_are_empty_census(tmp_path: Path) -> None:
    spool, entry = _spool(tmp_path)
    spool.acks_directory.rmdir()
    spool.attempts_directory.rmdir()
    assert spool._verified_ack_entry_digests([entry]) == frozenset()
    assert spool._verified_attempt_count([entry]) == 0


def test_spool_verified_entries_reject_filename_duplicate_and_chain_corruption(
    tmp_path: Path,
) -> None:
    for mode in ("filename", "duplicate", "chain"):
        spool, first = _spool(tmp_path / mode)
        second = spool.stage(
            _observation("PreCompact"),
            delivery_id=digest(f"delivery-{mode}"),
            occurred_at=NOW,
        )
        if mode == "filename":
            spool._entry_path(first.entry_digest).rename(spool.events_directory / f"{D[7:]}.json")
        else:
            changed = replace(
                second,
                delivery_id=first.delivery_id if mode == "duplicate" else second.delivery_id,
                previous_entry_digest=(
                    digest("wrong-predecessor") if mode == "chain" else first.entry_digest
                ),
            )
            changed = replace(changed, entry_digest=digest(changed.body()))
            spool._entry_path(second.entry_digest).unlink()
            spool_module._write_immutable_json(
                spool._entry_path(changed.entry_digest), changed.as_dict()
            )
        with pytest.raises(PolicyViolation):
            spool._verified_entries()


def test_spool_ack_and_attempt_census_reject_missing_fields_and_wrong_filenames(
    tmp_path: Path,
) -> None:
    spool, entry = _spool(tmp_path)
    spool_module._write_immutable_json(spool.acks_directory / "bad.json", {"schema": "bad"})
    with pytest.raises(ValidationFailed, match="entry digest eksik"):
        spool._verified_ack_entry_digests([entry])
    (spool.acks_directory / "bad.json").unlink()
    (spool.attempts_directory / "bad.json").write_text('["not","mapping"]')
    with pytest.raises(ValidationFailed, match="attempt schema"):
        spool._verified_attempt_count([entry])


def test_spool_predecessor_entry_delivery_checkpoint_and_queue_integrity_guards(
    tmp_path: Path,
) -> None:
    spool, first = _spool(tmp_path)
    second = spool.stage(_observation("PreCompact"), delivery_id=digest("second"), occurred_at=NOW)
    with pytest.raises(PolicyViolation, match="predecessor receipt"):
        spool.previous_canonical_event_digest(second)
    first_path = spool._entry_path(first.entry_digest)
    first_doc = spool_fixture._read(first_path)
    first_doc["delivery_id"] = digest("changed-delivery")
    spool_fixture._redigest(first_doc, "entry_digest")
    spool_module._write_atomic_json(first_path, first_doc)
    with pytest.raises(PolicyViolation, match="filename digest"):
        spool._read_entry(first.entry_digest)

    spool2, entry = _spool(tmp_path / "delivery")
    spool2._entry_path(entry.entry_digest).unlink()
    with pytest.raises(PolicyViolation, match="source entry eksik"):
        spool2._entry_for_delivery(entry.delivery_id)

    spool3, entry3 = _spool(tmp_path / "checkpoint")
    pending = spool3._checkpoint_document(entry3, state="pending")
    spool_module._write_atomic_json(
        spool3._session_path(entry3.client_id, entry3.session_id), pending
    )
    with pytest.raises(PolicyViolation, match="global queue recovery"):
        spool3._load_session_tail(client_id=entry3.client_id, session_id=entry3.session_id)

    spool4, entry4 = _spool(tmp_path / "queue")
    state = spool4._queue_state_document(
        entry4, queue_sequence=1, previous_queue_entry_digest=None, state="pending"
    )
    spool_module._write_atomic_json(spool4.queue_state_path, state)
    with pytest.raises(PolicyViolation, match="pending recovery"):
        spool4._load_queue_tail(recover=False)
    committed = spool4._queue_state_document(
        entry4, queue_sequence=1, previous_queue_entry_digest=None, state="committed"
    )
    committed["tail_entry_digest"] = digest("wrong-tail")
    spool_fixture._redigest(committed, "state_digest")
    spool_module._write_atomic_json(spool4.queue_state_path, committed)
    with pytest.raises(PolicyViolation, match="tail binding"):
        spool4._load_queue_tail(recover=False)


def test_spool_cursor_deep_semantic_drift_is_rejected_after_valid_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    entry = spool.stage(_observation(), delivery_id=digest("cursor"), occurred_at=NOW)
    receipt = spool_fixture._receipt(spool, entry)
    spool.acknowledge_committed_receipt(entry, receipt=receipt, acknowledged_at=NOW)
    base = spool_fixture._read(spool._drain_cursor_record_path(1))
    monkeypatch.setattr(spool_module, "_validate_drain_cursor_record", lambda *_args, **_kw: None)
    for changes, match in (
        ({"previous_entry_digest": D}, "first cursor previous"),
        ({"acknowledged_count": 0}, "sayac zinciri"),
    ):
        document = deepcopy(base)
        document.update(changes)
        spool_fixture._redigest(document, "cursor_digest")
        with pytest.raises(PolicyViolation, match=match):
            spool._validate_drain_cursor_record(document, expected_sequence=1)
    spool._entry_path(entry.entry_digest).unlink()
    with pytest.raises(PolicyViolation, match="entry eksik"):
        spool._validate_drain_cursor_record(base, expected_sequence=1)


def test_spool_pending_status_missing_source_and_cursor_ahead_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool, entry = _spool(tmp_path)
    assert spool.pending() == (entry,)
    spool._entry_path(entry.entry_digest).unlink()
    with pytest.raises(PolicyViolation, match="queue source entry eksik"):
        spool.pending()
    spool2, _entry2 = _spool(tmp_path / "cursor")
    monkeypatch.setattr(spool2, "_read_drain_cursor", lambda: (2, None, None, 0, 0))
    with pytest.raises(PolicyViolation, match="cursor queue tail ilerisinde"):
        spool2.pending()
    spool3, entry3 = _spool(tmp_path / "status")
    spool3._entry_path(entry3.entry_digest).unlink()
    with pytest.raises(PolicyViolation, match="status queue entry eksik"):
        spool3.status()


def test_spool_attempt_exact_replay_terminal_and_orphan_recovery_matrix(tmp_path: Path) -> None:
    evidence = digest("attempt-evidence")
    spool, entry = _spool(tmp_path / "replay")
    first = spool.record_attempt(
        entry.entry_digest, outcome="failed", evidence_digest=evidence, attempted_at=NOW
    )
    assert (
        spool.record_attempt(
            entry.entry_digest, outcome="failed", evidence_digest=evidence, attempted_at=NOW
        )
        == first
    )

    terminal, terminal_entry = _spool(tmp_path / "terminal")
    terminal.record_attempt(
        terminal_entry.entry_digest,
        outcome="completed",
        evidence_digest=evidence,
        attempted_at=NOW,
    )
    with pytest.raises(PolicyViolation, match="terminal attempt-state"):
        terminal.record_attempt(
            terminal_entry.entry_digest,
            outcome="failed",
            evidence_digest=digest("new-failure"),
            attempted_at=NOW,
        )

    malformed, malformed_entry = _spool(tmp_path / "malformed")
    retry_key = digest(
        {
            "entry_digest": malformed_entry.entry_digest,
            "outcome": "failed",
            "evidence_digest": evidence,
        }
    )
    spool_module._write_immutable_json(malformed._attempt_path(retry_key), {"schema": "partial"})
    with pytest.raises(PolicyViolation, match="orphan attempt retry drift"):
        malformed.record_attempt(
            malformed_entry.entry_digest,
            outcome="failed",
            evidence_digest=evidence,
            attempted_at=NOW,
        )

    for mode in ("recover", "digest", "sequence"):
        orphan, orphan_entry = _spool(tmp_path / mode)
        document = orphan.record_attempt(
            orphan_entry.entry_digest,
            outcome="failed",
            evidence_digest=evidence,
            attempted_at=NOW,
        )
        orphan._attempt_state_path(orphan_entry.entry_digest).unlink()
        path = orphan._attempt_path(str(document["retry_key"]))
        if mode == "digest":
            changed = deepcopy(document)
            changed["attempt_digest"] = D
            spool_module._write_atomic_json(path, changed)
            with pytest.raises(PolicyViolation, match="orphan attempt digest mismatch"):
                orphan.record_attempt(
                    orphan_entry.entry_digest,
                    outcome="failed",
                    evidence_digest=evidence,
                    attempted_at=NOW,
                )
        elif mode == "sequence":
            changed = deepcopy(document)
            changed.update(attempt_number=2, failure_count=2)
            spool_fixture._redigest(changed, "attempt_digest")
            spool_module._write_atomic_json(path, changed)
            with pytest.raises(PolicyViolation, match="orphan attempt sequence drift"):
                orphan.record_attempt(
                    orphan_entry.entry_digest,
                    outcome="failed",
                    evidence_digest=evidence,
                    attempted_at=NOW,
                )
        else:
            assert (
                orphan.record_attempt(
                    orphan_entry.entry_digest,
                    outcome="failed",
                    evidence_digest=evidence,
                    attempted_at=NOW,
                )["attempt_digest"]
                == document["attempt_digest"]
            )
            assert orphan._read_attempt_state(orphan_entry.entry_digest) is not None


def test_spool_acknowledgement_missing_instance_entry_state_and_evidence_guards(
    tmp_path: Path,
) -> None:
    for mode in ("instance", "entry", "state", "evidence"):
        spool, entry = _spool(tmp_path / mode)
        receipt = spool_fixture._receipt(spool, entry)
        if mode == "instance":
            spool.instance_path.unlink()
            expected = "client instance"
        elif mode == "entry":
            spool._entry_path(entry.entry_digest).unlink()
            expected = "source entry"
        elif mode == "state":
            expected = "completed attempt-state"
        else:
            spool.record_attempt(
                entry.entry_digest,
                outcome="completed",
                evidence_digest=digest("wrong-evidence"),
                attempted_at=NOW,
            )
            expected = "lookup evidence mismatch"
        with pytest.raises((PolicyViolation, ValidationFailed), match=expected):
            spool._acknowledge_verified_receipt(entry, receipt=receipt, acknowledged_at=NOW)


def test_spool_acknowledgement_event_and_existing_replay_drift(tmp_path: Path) -> None:
    spool, entry = _spool(tmp_path)
    receipt = spool_fixture._receipt(spool, entry)
    wrong_event = digest("wrong-event")
    forged = replace(
        receipt,
        canonical_event_digest=wrong_event,
        continuity_binding=spool_fixture._binding(entry, wrong_event),
    )
    with pytest.raises(PolicyViolation, match="event/spool binding mismatch"):
        spool._acknowledge_verified_receipt(entry, receipt=forged, acknowledged_at=NOW)
    spool.acknowledge_committed_receipt(entry, receipt=receipt, acknowledged_at=NOW)
    drift = replace(receipt, canonical_ack_digest=digest("changed-ack"))
    with pytest.raises(PolicyViolation, match="replay digest drift"):
        spool._acknowledge_verified_receipt(entry, receipt=drift, acknowledged_at=NOW)


def test_spool_read_session_snapshot_and_tail_parity_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool, entry = _spool(tmp_path)
    original = spool._load_queue_tail
    calls = 0

    def changed(*, recover: bool) -> tuple[int, str | None]:
        nonlocal calls
        calls += 1
        value = original(recover=recover)
        return value if calls == 1 else (value[0] + 1, value[1])

    monkeypatch.setattr(spool, "_load_queue_tail", changed)
    with pytest.raises(ConcurrencyConflict, match="snapshot changed"):
        spool.read_session_entries(client_id=entry.client_id, session_id=entry.session_id)

    spool2, entry2 = _spool(tmp_path / "parity")
    monkeypatch.setattr(spool2, "_verified_entries", lambda: [])
    with pytest.raises(PolicyViolation, match="cursor parity"):
        spool2.read_session_entries(client_id=entry2.client_id, session_id=entry2.session_id)


def test_spool_stage_frozen_detects_held_snapshot_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    original = spool.read_session_entries
    calls = 0

    def changed(*, client_id: str, session_id: str) -> tuple[LifecycleSpoolEntry, ...]:
        nonlocal calls
        calls += 1
        rows = original(client_id=client_id, session_id=session_id)
        return rows if calls == 1 else ()

    monkeypatch.setattr(spool, "read_session_entries", changed)
    deadline = SimpleNamespace(remaining_seconds=lambda: 1.0)
    with (
        pytest.raises(ConcurrencyConflict, match="held spool changed"),
        spool.stage_frozen(
            _observation(), delivery_id=digest("frozen"), occurred_at=NOW, deadline=deadline
        ),
    ):
        pass


def test_spool_manual_review_is_not_pending_and_predecessor_guards(tmp_path: Path) -> None:
    spool, first = _spool(tmp_path)
    for index in range(spool_module.MAX_REPLAY_FAILURES):
        spool.record_attempt(
            first.entry_digest,
            outcome="failed",
            evidence_digest=digest(f"failure-{index}"),
            attempted_at=NOW + dt.timedelta(seconds=index),
        )
    assert spool.pending() == ()
    with pytest.raises(PolicyViolation, match="exact ardil entry"):
        spool.record_predecessor_manual_review(first.entry_digest, attempted_at=NOW)
    second = spool.stage(
        _observation("PreCompact"), delivery_id=digest("predecessor-second"), occurred_at=NOW
    )
    recorded = spool.record_predecessor_manual_review(second.entry_digest, attempted_at=NOW)
    assert recorded["terminal_reason"] == "predecessor-manual-review"
    assert spool.record_predecessor_manual_review(second.entry_digest, attempted_at=NOW) == recorded
    with pytest.raises(PolicyViolation, match="terminal attempt-state"):
        spool.record_attempt(
            second.entry_digest,
            outcome="failed",
            evidence_digest=digest("late"),
            attempted_at=NOW,
        )


def test_spool_ack_census_attempt_census_delivery_and_checkpoint_binding_drift(
    tmp_path: Path,
) -> None:
    spool, entry = _spool(tmp_path / "ack")
    receipt = spool_fixture._receipt(spool, entry)
    spool.acknowledge_committed_receipt(entry, receipt=receipt, acknowledged_at=NOW)
    ack_path = spool._ack_path(entry.entry_digest)
    ack_path.rename(spool.acks_directory / "wrong.json")
    with pytest.raises(PolicyViolation, match="source/filename binding"):
        spool._verified_ack_entry_digests([entry])

    attempts, attempt_entry = _spool(tmp_path / "attempt")
    document = attempts.record_attempt(
        attempt_entry.entry_digest, outcome="failed", evidence_digest=D, attempted_at=NOW
    )
    path = attempts._attempt_path(str(document["retry_key"]))
    path.rename(attempts.attempts_directory / "wrong.json")
    with pytest.raises(PolicyViolation, match="source/digest binding"):
        attempts._verified_attempt_count([attempt_entry])

    delivery, delivery_entry = _spool(tmp_path / "delivery")
    ref_path = delivery._delivery_path(delivery_entry.delivery_id)
    ref = spool_fixture._read(ref_path)
    ref["client_id"] = "other"
    spool_fixture._redigest(ref, "ref_digest")
    spool_module._write_atomic_json(ref_path, ref)
    with pytest.raises(PolicyViolation, match="entry binding mismatch"):
        delivery._entry_for_delivery(delivery_entry.delivery_id)

    checkpoint, checkpoint_entry = _spool(tmp_path / "checkpoint")
    checkpoint_path = checkpoint._session_path(
        checkpoint_entry.client_id, checkpoint_entry.session_id
    )
    value = spool_fixture._read(checkpoint_path)
    value["delivery_id"] = digest("different-delivery")
    spool_fixture._redigest(value, "checkpoint_digest")
    spool_module._write_atomic_json(checkpoint_path, value)
    with pytest.raises(PolicyViolation, match="tail binding mismatch"):
        checkpoint._load_session_tail(
            client_id=checkpoint_entry.client_id, session_id=checkpoint_entry.session_id
        )


def test_spool_ack_validator_canonical_uuid_and_lookup_digest_defenses(tmp_path: Path) -> None:
    spool, entry = _spool(tmp_path)
    receipt = spool_fixture._receipt(spool, entry)
    spool.acknowledge_committed_receipt(entry, receipt=receipt, acknowledged_at=NOW)
    base = spool_fixture._read(spool._ack_path(entry.entry_digest))
    runtime_uuid = "018f0000-0000-7000-8000-000000000077"
    for mode in ("runtime-uppercase", "binding-uppercase", "lookup"):
        value = deepcopy(base)
        if mode == "runtime-uppercase":
            value["runtime_binding_id"] = runtime_uuid.upper()
            value["runtime_binding_digest"] = D
        elif mode == "binding-uppercase":
            binding = value["continuity_binding"]
            assert isinstance(binding, dict)
            binding["realm_id"] = str(binding["realm_id"]).upper()
            spool_fixture._redigest(binding, "binding_digest")
        else:
            value["canonical_lookup_digest"] = D
        spool_fixture._redigest(value, "ack_digest")
        with pytest.raises((ValidationFailed, PolicyViolation)):
            spool_module._validate_ack(value, entry_digest=entry.entry_digest)


def test_spool_queue_pending_entry_and_attempt_retry_reference_defenses(tmp_path: Path) -> None:
    spool, entry = _spool(tmp_path)
    state = spool._queue_state_document(
        entry, queue_sequence=1, previous_queue_entry_digest=None, state="pending"
    )
    pending = deepcopy(state["pending_entry"])
    assert isinstance(pending, dict)
    pending["delivery_id"] = digest("changed-pending-delivery")
    spool_fixture._redigest(pending, "entry_digest")
    state["pending_entry"] = pending
    spool_fixture._redigest(state, "state_digest")
    with pytest.raises(PolicyViolation, match="pending entry binding"):
        spool_module._validate_queue_state(state)
    attempt = spool.record_attempt(
        entry.entry_digest, outcome="failed", evidence_digest=D, attempted_at=NOW
    )
    with pytest.raises(PolicyViolation, match="retry ref mismatch"):
        spool_module._validate_attempt(
            attempt, entry_digest=entry.entry_digest, expected_retry_key=digest("other-retry")
        )


def test_spool_canonical_second_event_accepts_exact_predecessor(tmp_path: Path) -> None:
    spool, _first = _spool(tmp_path)
    second = spool.stage(
        _observation("PreCompact"), delivery_id=digest("canonical-second"), occurred_at=NOW
    )
    event = canonical_lifecycle_event(
        second,
        client_instance_id=spool.client_instance_id(),
        previous_canonical_event_digest=D,
    )
    assert event["previous_digest"] == D


def test_writer_constructor_source_and_evidence_port_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    for changed in (
        {"path": Path("relative")},
        {"busy_timeout_ms": 0},
        {"source": object()},
        {"spool": object()},
        {"projections": object()},
    ):
        values: dict[str, Any] = {
            "path": path,
            "source": SimpleNamespace(
                snapshot=lambda _binding: None,
                resolve_fragment=lambda *_: None,
                assert_current=lambda *_: None,
            ),
            "spool": SimpleNamespace(frozen=lambda _binding: contextlib.nullcontext(None)),
            "projections": SimpleNamespace(frozen=lambda _frozen: contextlib.nullcontext(None)),
            "busy_timeout_ms": 5,
        }
        values.update(changed)
        with pytest.raises(ValidationFailed):
            SQLiteDormantV4CloseWriter(**values)


def test_writer_context_wrappers_map_timeout_and_preserve_policy(tmp_path: Path) -> None:
    writer = object.__new__(SQLiteDormantV4CloseWriter)
    writer.spool = SimpleNamespace(
        frozen=lambda _binding: contextlib.nullcontext(
            cast(Any, (_ for _ in ()).throw(TimeoutError()))
        )
    )
    with pytest.raises(PolicyViolation, match="spool evidence unavailable"):  # noqa: SIM117
        with writer._frozen_spool(close_fixture._binding()):
            pass

    class BrokenProjection:
        @contextlib.contextmanager
        def frozen(self, _frozen: object) -> Any:
            raise OSError("offline")
            yield

    writer.projections = BrokenProjection()
    with pytest.raises(PolicyViolation, match="projection evidence unavailable"):  # noqa: SIM117
        with writer._frozen_projections(cast(Any, object())):
            pass


def test_writer_schema_binding_attachment_capacity_and_spool_static_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(writer_module.operational_schema, "_validate_connection", lambda _db: 3)
    with pytest.raises(ConfigurationError, match="corrected explicit schema"):
        SQLiteDormantV4CloseWriter._schema(cast(Any, object()))
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "create table session(id text,status text,project_id text,work_item_id text,"
        "client_id text,device_id text,close_receipt_digest text,closed_at text);"
        "create table project(id text,slug text);"
        "create table continuity_session_binding(session_id text,external_session_id text,"
        "project_id text,realm_id text,work_item_id text,run_id text,client_id text,"
        "device_id text,source_snapshot_id text,task_digest text,plan_digest text,"
        "policy_digest text,binding_digest text);"
        "create table continuity_hook_attachment(session_id text,attachment_id text);"
        "create table local_runtime_config(singleton integer,max_pending_outbox integer);"
        "create table local_outbox_delivery(state text);"
    )
    with pytest.raises(PolicyViolation, match="binding unavailable"):
        SQLiteDormantV4CloseWriter._binding(db, close_fixture._binding())
    with pytest.raises(PolicyViolation, match="exact attachment"):
        SQLiteDormantV4CloseWriter._attachment(db, close_fixture._binding())
    with pytest.raises(PolicyViolation, match="capacity unavailable"):
        SQLiteDormantV4CloseWriter._capacity(db, 1)
    rows = [{"event_kind": "SESSION_START", "spool_digest": D, "event_digest": D}]
    bad_identity = FrozenSpoolSnapshot("wrong", "external-session", "codex", (D,))
    with pytest.raises(PolicyViolation, match="reviewed SessionStart"):
        SQLiteDormantV4CloseWriter._spool_gate(
            db, cast(Any, rows), bad_identity, close_fixture._binding(), allow_controls=False
        )
    bad_prefix = FrozenSpoolSnapshot(
        close_fixture.SESSION_ID, "external-session", "codex", (digest("other"),)
    )
    with pytest.raises(PolicyViolation, match="ordinary spool prefix"):
        SQLiteDormantV4CloseWriter._spool_gate(
            db, cast(Any, rows), bad_prefix, close_fixture._binding(), allow_controls=False
        )


def test_writer_spool_suffix_summary_and_input_size_guards() -> None:
    binding = close_fixture._binding()
    rows = [{"event_kind": "SESSION_START", "spool_digest": D, "event_digest": D}]
    snapshot = FrozenSpoolSnapshot(
        binding.session_id,
        binding.external_session_id,
        binding.client_id,
        (D, digest("suffix")),
    )
    with pytest.raises(PolicyViolation, match="unpersisted spool delta"):
        SQLiteDormantV4CloseWriter._spool_gate(
            cast(Any, object()),
            cast(Any, rows),
            snapshot,
            binding,
            allow_controls=False,
        )

    class Cursor:
        def fetchall(self) -> list[tuple[str]]:
            return [(digest("wrong-control"),)]

    class DB:
        def execute(self, *_args: object) -> Cursor:
            return Cursor()

    with pytest.raises(PolicyViolation, match="control spool suffix"):
        SQLiteDormantV4CloseWriter._spool_gate(
            cast(Any, DB()), cast(Any, rows), snapshot, binding, allow_controls=True
        )
    bad_request = SimpleNamespace(
        summary=SimpleNamespace(sources=(("outside", D),), evidence=()),
        candidates=None,
        active_manifest_digest=D,
    )
    manifest = SimpleNamespace(selected=())
    with pytest.raises(PolicyViolation, match="summary provenance"):
        SQLiteDormantV4CloseWriter._summary_scope(
            cast(Any, manifest), cast(Any, rows), cast(Any, bad_request)
        )
    request = SimpleNamespace(
        binding=binding,
        active_manifest_digest=D,
        expected_tail=SimpleNamespace(sequence=0),
        observed_at="2026-09-04T12:00:00+00:00",
        candidates=None,
        summary=SimpleNamespace(body=lambda: {"summary": "x" * 70_000}),
    )
    with pytest.raises(ValidationFailed, match="byte bound"):
        SQLiteDormantV4CloseWriter._close_body(
            cast(Any, request), checkpoint_digest=D, preclose_digest=D, project_slug="demo"
        )


def test_writer_front_doors_source_snapshot_and_snapshot_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = close_fixture._seed(tmp_path / "close.db")
    writer = close_fixture._v4_writer(tmp_path / "close.db", seed)
    with pytest.raises(ValidationFailed, match="exact freeze request"):
        writer.freeze_with_preclose(cast(Any, object()))
    with pytest.raises(ValidationFailed, match="exact finalize request"):
        writer.finalize_with_session_closed(cast(Any, object()))
    writer.source = SimpleNamespace(snapshot=lambda _binding: object())
    with pytest.raises(ValidationFailed, match="exact source snapshot"):
        writer._source_snapshot(close_fixture._binding())
    writer.source = SimpleNamespace(snapshot=lambda _binding: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(PolicyViolation, match="source snapshot unavailable"):
        writer._source_snapshot(close_fixture._binding())
    writer.source = SimpleNamespace(assert_current=lambda *_: (_ for _ in ()).throw(OSError()))
    with pytest.raises(PolicyViolation, match="current source unavailable"):
        writer._assert_source_current(close_fixture._binding(), cast(Any, SimpleNamespace()))
    monkeypatch.setattr(
        writer_module.operational_schema,
        "status",
        lambda _path: SimpleNamespace(schema_version=3, schema_ok=True, integrity_ok=True),
    )
    with pytest.raises(ConfigurationError, match="corrected explicit schema"):
        close_fixture._v4_writer(tmp_path / "close.db", seed).freeze_with_preclose(
            close_fixture._request(seed)
        )


def test_writer_event_checkpoint_and_recovery_cardinality_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = close_fixture._binding()
    empty_events = _ScriptDB(
        (
            ("from session_event_detail", []),
            ("from continuity_hook_attachment where", None),
        )
    )
    with pytest.raises(PolicyViolation, match="native attachment missing"):
        SQLiteDormantV4CloseWriter._events(cast(Any, empty_events), binding)

    body = {
        "session_id": binding.session_id,
        "binding_digest": binding.binding_digest,
        "sequence": 1,
        "previous_digest": None,
        "event": {
            "kind": "SESSION_START",
            "idempotency_key": "start",
            "spool_digest": D,
            "occurred_at": TS0,
        },
    }
    event_row = {
        "id": "event",
        "event_kind": "SESSION_START",
        "event_digest": digest(body),
        "created_at": TS0,
        "sequence": 1,
        "previous_digest": None,
        "idempotency_key": "start",
        "spool_digest": D,
        "body_json": canonical_json(body),
    }
    native_missing = _ScriptDB(
        (
            ("from session_event_detail", [event_row]),
            ("from continuity_hook_attachment where", {"attachment_id": "attachment"}),
            ("from continuity_native_event_receipt", None),
        )
    )
    monkeypatch.setattr(writer_module, "verify_reviewed_hook_commands", lambda *_: ())
    with pytest.raises(PolicyViolation, match="native producer integrity"):
        SQLiteDormantV4CloseWriter._events(cast(Any, native_missing), binding)

    with pytest.raises(PolicyViolation, match="checkpoint reference malformed"):
        SQLiteDormantV4CloseWriter._checkpoint_graph(
            cast(Any, _ScriptDB(())), binding, {"checkpoint_digest": None}
        )
    with pytest.raises(PolicyViolation, match="checkpoint missing"):
        SQLiteDormantV4CloseWriter._checkpoint_graph(
            cast(Any, _ScriptDB(())), binding, {"checkpoint_digest": D}
        )
    claim = {
        "id": "claim",
        "claimed_at": TS0,
        "effect_digest": D,
        "fencing_token": 1,
        "job_id": "job",
    }
    duplicate_cases = _ScriptDB((("from local_recovery_case where effect_claim_id", [{}, {}]),))
    with pytest.raises(PolicyViolation, match="duplicate effect recovery"):
        SQLiteDormantV4CloseWriter._recovery_for_effect(
            cast(Any, duplicate_cases), cast(Any, claim), None
        )
    case = {
        "id": "case",
        "created_at": TS1,
        "job_id": "job",
        "case_kind": "effect-unknown",
        "outbox_id": None,
        "evidence_digest": digest(
            {"case_kind": "effect-unknown", "claim_id": "claim", "effect_digest": D}
        ),
        "state": "resolved",
        "resolved_at": TS1,
    }
    duplicate_resolutions = _ScriptDB(
        (
            ("from local_recovery_case where effect_claim_id", [case]),
            ("from local_recovery_resolution where", [{}, {}]),
        )
    )
    with pytest.raises(PolicyViolation, match="duplicate effect resolution"):
        SQLiteDormantV4CloseWriter._recovery_for_effect(
            cast(Any, duplicate_resolutions), cast(Any, claim), None
        )


def _delivery_graph(
    delivery: dict[str, Any],
    *,
    receipts: list[dict[str, Any]] | None = None,
    cases: list[dict[str, Any]] | None = None,
    resolutions: list[dict[str, Any]] | None = None,
) -> _ScriptDB:
    return _ScriptDB(
        (
            ("from local_outbox_delivery", delivery),
            ("from local_outbox_receipt", receipts or []),
            ("from local_recovery_case", cases or []),
            ("from local_recovery_resolution", resolutions or []),
        )
    )


def test_writer_delivery_timestamp_owner_direct_and_status_guards() -> None:
    row = {"id": "outbox", "job_id": "job", "created_at": TS1}
    base = {
        "state": "claimed",
        "claim_id": "018f0000-0000-7000-8000-000000000011",
        "owner_id": "owner",
        "owner_pid": 1,
        "owner_token": "token",
        "expires_at": TS2,
        "fencing_counter": 1,
        "updated_at": TS0,
    }
    with pytest.raises(PolicyViolation, match="preceded outbox"):
        SQLiteDormantV4CloseWriter._delivery_state(
            cast(Any, _delivery_graph(base)), cast(Any, row), trusted_now=NOW
        )
    row["created_at"] = TS0
    invalid_owner = base | {"updated_at": TS1, "owner_pid": 0}
    with pytest.raises(PolicyViolation, match="owner/fence drift"):
        SQLiteDormantV4CloseWriter._delivery_state(
            cast(Any, _delivery_graph(invalid_owner)), cast(Any, row), trusted_now=NOW
        )
    receipt = {
        "id": "018f0000-0000-7000-8000-000000000012",
        "claim_id": base["claim_id"],
        "fencing_token": 1,
        "evidence_digest": D,
        "created_at": TS1,
        "status": "delivered",
    }
    direct_drift = base | {"state": "failed", "updated_at": TS1}
    with pytest.raises(PolicyViolation, match="direct delivery state drift"):
        SQLiteDormantV4CloseWriter._delivery_state(
            cast(Any, _delivery_graph(direct_drift, receipts=[receipt])),
            cast(Any, row),
            trusted_now=NOW,
        )
    invalid_status = base | {"state": "recovery-required", "updated_at": TS1}
    receipt["status"] = "partial"
    with pytest.raises(PolicyViolation, match="receipt status drift"):
        SQLiteDormantV4CloseWriter._delivery_state(
            cast(Any, _delivery_graph(invalid_status, receipts=[receipt])),
            cast(Any, row),
            trusted_now=NOW,
        )


def test_writer_delivery_open_resolved_recovery_guards() -> None:
    row = {"id": "outbox", "job_id": "job", "created_at": TS0}
    delivery = {
        "state": "recovery-required",
        "claim_id": "018f0000-0000-7000-8000-000000000021",
        "owner_id": "owner",
        "owner_pid": 1,
        "owner_token": "token",
        "expires_at": TS2,
        "fencing_counter": 1,
        "updated_at": TS1,
    }
    receipt = {
        "id": "018f0000-0000-7000-8000-000000000022",
        "claim_id": delivery["claim_id"],
        "fencing_token": 1,
        "evidence_digest": D,
        "created_at": TS1,
        "status": "unknown",
    }
    recipe = digest(
        {
            "case_kind": "outbox-delivery-unknown",
            "outbox_id": "outbox",
            "claim_id": delivery["claim_id"],
            "receipt_evidence": D,
        }
    )
    case = {
        "id": "case",
        "job_id": "job",
        "case_kind": "outbox-delivery-unknown",
        "effect_claim_id": None,
        "evidence_digest": recipe,
        "created_at": TS1,
        "resolved_at": None,
        "state": "open",
    }
    with pytest.raises(PolicyViolation, match="open delivery recovery drift"):
        SQLiteDormantV4CloseWriter._delivery_state(
            cast(
                Any,
                _delivery_graph(delivery | {"state": "failed"}, receipts=[receipt], cases=[case]),
            ),
            cast(Any, row),
            trusted_now=NOW,
        )
    case["state"] = "resolved"
    with pytest.raises(PolicyViolation, match="resolved delivery recovery missing"):
        SQLiteDormantV4CloseWriter._delivery_state(
            cast(Any, _delivery_graph(delivery, receipts=[receipt], cases=[case])),
            cast(Any, row),
            trusted_now=NOW,
        )
    case["resolved_at"] = TS1
    resolution = {
        "recovery_case_id": "wrong-case",
        "outcome": "delivered",
        "created_at": TS1,
    }
    with pytest.raises(PolicyViolation, match="resolved delivery outcome drift"):
        SQLiteDormantV4CloseWriter._delivery_state(
            cast(
                Any,
                _delivery_graph(
                    delivery, receipts=[receipt], cases=[case], resolutions=[resolution]
                ),
            ),
            cast(Any, row),
            trusted_now=NOW,
        )


def test_writer_full_freeze_compile_finalize_restart_replay_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "restart-close.db"
    seed = close_fixture._seed(path)
    writer = close_fixture._v4_writer(path, seed)
    request = close_fixture._request(seed)
    frozen = writer.freeze_with_preclose(request)
    assert close_fixture._v4_writer(path, seed).freeze_with_preclose(request) == frozen
    close_fixture._materialize_and_complete(path, frozen, monkeypatch)
    with sqlite3.connect(path) as db:
        frozen_revision = str(
            db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "where state='frozen'"
            ).fetchone()[0]
        )
    final = FinalizeClosedWriteRequest(
        close_fixture._binding(),
        frozen.request_digest,
        frozen_revision,
        "restart-finalize",
        close_fixture.NOW,
    )
    receipt = writer.finalize_with_session_closed(final)
    restarted = close_fixture._v4_writer(path, seed)
    assert restarted.finalize_with_session_closed(final) == receipt


def _unsafe_sql(path: Path, table: str, statement: str, values: tuple[object, ...]) -> None:
    with sqlite3.connect(path) as db:
        triggers = [
            str(row[0])
            for row in db.execute(
                "select sql from sqlite_master where type='trigger' and tbl_name=?", (table,)
            )
            if row[0] is not None
        ]
        names = [
            str(row[0])
            for row in db.execute(
                "select name from sqlite_master where type='trigger' and tbl_name=?", (table,)
            )
        ]
        for name in names:
            db.execute(f'drop trigger "{name}"')
        db.execute("pragma ignore_check_constraints=on")
        db.execute(statement, values)
        for sql in triggers:
            db.execute(sql)
        db.commit()


@pytest.mark.parametrize(
    ("mode", "match"),
    (
        ("missing-job", "job missing"),
        ("link-drift", "immutable job/binding graph drift"),
        ("running-no-lease", "running job lease drift"),
        ("unsupported-state", "job state unsupported"),
        ("missing-terminal-evidence", "terminal job evidence missing"),
        ("outbox-cardinality", "work outbox cardinality drift"),
        ("outbox-identity", "initial outbox identity drift"),
        ("initial-payload", "immutable initial outbox drift"),
    ),
)
def test_writer_ready_runtime_graph_corruption_matrix(
    tmp_path: Path, mode: str, match: str
) -> None:
    path = tmp_path / mode / "runtime.db"
    path.parent.mkdir()
    seed = close_fixture._seed(path)
    writer = close_fixture._v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(close_fixture._request(seed))
    if mode == "missing-job":
        _unsafe_sql(path, "local_job", "delete from local_job where id=?", (frozen.job_id,))
    elif mode == "link-drift":
        _unsafe_sql(
            path,
            "continuity_outbox_binding",
            "update continuity_outbox_binding set purpose='changed' where outbox_id=?",
            (frozen.outbox_id,),
        )
    elif mode == "running-no-lease":
        _unsafe_sql(
            path,
            "local_job",
            "update local_job set state='running',attempt_count=1,fencing_counter=1 where id=?",
            (frozen.job_id,),
        )
    elif mode == "unsupported-state":
        _unsafe_sql(
            path,
            "local_job",
            "update local_job set state='corrupt',terminal_evidence_digest=? where id=?",
            (D, frozen.job_id),
        )
    elif mode == "missing-terminal-evidence":
        _unsafe_sql(
            path,
            "local_job",
            "update local_job set state='completed',terminal_evidence_digest=null where id=?",
            (frozen.job_id,),
        )
    elif mode == "outbox-cardinality":
        _unsafe_sql(
            path, "local_outbox", "delete from local_outbox where id=?", (frozen.outbox_id,)
        )
    elif mode == "outbox-identity":
        _unsafe_sql(
            path,
            "local_outbox",
            "update local_outbox set id=? where id=?",
            ("018f0000-0000-7000-8000-000000000088", frozen.outbox_id),
        )
    else:
        _unsafe_sql(
            path,
            "local_outbox",
            "update local_outbox set payload_json='{}' "
            "where event_kind='job.enqueued' and job_id=?",
            (frozen.job_id,),
        )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match=match):
            writer._runtime_graph(db, close_fixture._binding(), frozen, require_completed=False)


def test_recovery_unknown_restart_resolution_and_exact_terminal_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = recovery_fixture._prepared(tmp_path, monkeypatch)
    request = UnknownEffectRequest(binding, claim_id, recovery_fixture._revision(path))
    entered = store.enter_unknown(request)
    case_id = str(entered.body()["recovery_case_id"])
    restarted = SQLiteDormantV4Recovery(
        path,
        binding,
        unknown_issuer=recovery_fixture._UnknownIssuer(path, binding),
        receiptless_issuer=recovery_fixture._ReceiptlessIssuer(path, binding),
        adjudicator=recovery_fixture._Adjudicator(path, binding),
    )
    assert restarted.enter_unknown(request).body()["status"] == "replayed"
    resolution_request = ResolveEffectRecoveryRequest(
        binding,
        case_id,
        recovery_fixture._revision(path),
        recovery_fixture._tail(path, binding),
    )
    resolved = restarted.resolve(resolution_request)
    assert resolved.body()["job_state"] == "completed"
    assert restarted.resolve(resolution_request).body()["status"] == "replayed"


def test_recovery_request_scope_and_route_snapshot_type_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, binding, _b1, store, claim_id = recovery_fixture._prepared(tmp_path, monkeypatch)
    request = UnknownEffectRequest(binding, claim_id, recovery_fixture._revision(store.path))
    with pytest.raises(ValidationFailed, match="exact public recovery request"):
        store._request(object(), UnknownEffectRequest)
    other_binding = replace(binding, device_id="other-device")
    with pytest.raises(PolicyViolation, match="binding scope drift"):
        store._request(
            UnknownEffectRequest(other_binding, claim_id, recovery_fixture._revision(store.path)),
            UnknownEffectRequest,
        )
    with pytest.raises(ValidationFailed, match="unknown snapshot route drift"):
        store._enter(
            request,
            cast(Any, object()),
            baseline=cast(Any, None),
            route="unknown",
        )
    receiptless = ReceiptlessRecoveryRequest(
        binding, claim_id, recovery_fixture._revision(store.path)
    )
    with pytest.raises(ValidationFailed, match="receiptless snapshot route drift"):
        store._enter(
            receiptless,
            cast(Any, object()),
            baseline=cast(Any, None),
            route="receiptless",
        )


@pytest.mark.parametrize(
    ("mode", "match"),
    (
        ("missing-claim", "selected effect claim missing"),
        ("missing-job", "fresh recovery authority/runtime drift"),
        ("existing-receipt", "fresh recovery receipt already exists"),
        ("missing-config", "persisted outbox config missing"),
    ),
)
def test_recovery_entry_partial_database_graphs_fail_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, match: str
) -> None:
    path, binding, _b1, store, claim_id = recovery_fixture._prepared(tmp_path, monkeypatch)
    request = UnknownEffectRequest(binding, claim_id, recovery_fixture._revision(path))
    snapshot = store.unknown_issuer.snapshot(request)
    if mode == "missing-claim":
        _unsafe_sql(
            path, "local_effect_claim", "delete from local_effect_claim where id=?", (claim_id,)
        )
    elif mode == "missing-job":
        with sqlite3.connect(path) as db:
            job_id = str(
                db.execute(
                    "select job_id from local_effect_claim where id=?", (claim_id,)
                ).fetchone()[0]
            )
        _unsafe_sql(path, "local_job", "delete from local_job where id=?", (job_id,))
    elif mode == "existing-receipt":
        _unsafe_sql(
            path,
            "local_effect_receipt",
            "insert into local_effect_receipt values(?,?,?,?,?)",
            (
                "018f0000-0000-7000-8000-000000000191",
                claim_id,
                "completed",
                D,
                TS1,
            ),
        )
    else:
        _unsafe_sql(path, "local_runtime_config", "delete from local_runtime_config", ())
    case_id = recovery_module._b2_id("effect-case", claim_id)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        baseline = recovery_module._baseline(db, binding, claim_id, case_id)
    monkeypatch.setattr(store, "_schema", lambda: None)
    monkeypatch.setattr(recovery_module, "verify_b1_b2_internal_producers", lambda *_a, **_k: None)
    before = path.read_bytes()
    with pytest.raises((PolicyViolation, ConcurrencyConflict), match=match):
        store._enter(request, snapshot, baseline=baseline, route="unknown")
    assert path.read_bytes() == before


def test_recovery_replay_outbox_census_and_missing_resolution_case_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = recovery_fixture._prepared(tmp_path, monkeypatch)
    request = UnknownEffectRequest(binding, claim_id, recovery_fixture._revision(path))
    entered = store.enter_unknown(request)
    case_id = str(entered.body()["recovery_case_id"])
    with sqlite3.connect(path) as db:
        job_id = str(
            db.execute("select job_id from local_recovery_case where id=?", (case_id,)).fetchone()[
                0
            ]
        )
    _unsafe_sql(path, "local_outbox", "delete from local_outbox where job_id=?", (job_id,))
    monkeypatch.setattr(store, "_schema", lambda: None)
    monkeypatch.setattr(recovery_module, "verify_b1_b2_internal_producers", lambda *_a, **_k: None)
    with pytest.raises(ConcurrencyConflict, match="outbox census drift"):
        store.enter_unknown(request)
    with pytest.raises(PolicyViolation, match="exact recovery case missing"):
        store.resolve(
            ResolveEffectRecoveryRequest(
                binding,
                "018f0000-0000-7000-8000-000000000199",
                recovery_fixture._revision(path),
                recovery_fixture._tail(path, binding),
            )
        )


def test_recovery_authority_snapshot_recheck_and_constructor_guards(tmp_path: Path) -> None:
    class Port:
        def snapshot(self, _request: object) -> object:
            raise TimeoutError

        def recheck(self, _snapshot: object) -> None:
            raise OSError

    with pytest.raises(PolicyViolation, match="snapshot unavailable"):
        _safe_snapshot(Port(), object(), object)
    with pytest.raises(PolicyViolation, match="recheck failed"):
        _safe_recheck(Port(), object())
    issuer = SimpleNamespace(snapshot=lambda _request: object(), recheck=lambda _snapshot: None)
    for path, binding, bad_issuer in (
        (Path("relative"), close_fixture._binding(), issuer),
        (tmp_path / "db", object(), issuer),
        (tmp_path / "db", close_fixture._binding(), object()),
    ):
        with pytest.raises(ValidationFailed):
            SQLiteDormantV4Recovery(
                path,
                cast(Any, binding),
                unknown_issuer=bad_issuer,
                receiptless_issuer=issuer,
                adjudicator=issuer,
            )


def _install_bootstrap_prepare_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    work: Any,
    snapshot: Any,
    actor: Any | None = None,
) -> None:
    graph = SimpleNamespace(items=SimpleNamespace(get=lambda _work_id: work))
    graph.snapshot = lambda _work_id: snapshot
    monkeypatch.setattr(bootstrap_module, "WorkGraphService", lambda *_a, **_k: graph)
    actor = actor or SimpleNamespace(kind=ActorKind.HUMAN, status=LifecycleStatus.ACTIVE)
    template = SimpleNamespace(
        assert_rebootstrap_admissible=lambda _work_id: None,
        assert_legacy_adoption_admissible=lambda *_a, **_k: bootstrap_fixture.IDS[20],
    )
    monkeypatch.setattr(
        bootstrap_module,
        "legacy_repository",
        lambda name, *_a, **_k: (
            SimpleNamespace(get=lambda _actor_id: actor) if name == "actor" else template
        ),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "GovernanceService",
        lambda *_a, **_k: SimpleNamespace(
            policies=SimpleNamespace(
                current=lambda _name: SimpleNamespace(policy_digest=digest("policy"))
            )
        ),
    )


@pytest.mark.parametrize("mode", ("project", "state"))
def test_bootstrap_prepare_exact_work_identity_each_failure_branch(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    work = SimpleNamespace(
        project_id=bootstrap_fixture.IDS[1],
        state=WorkState.PROPOSED,
        revision=1,
        record_digest=D,
        acceptance_criteria=(),
    )
    if mode == "project":
        work.project_id = bootstrap_fixture.IDS[30]
    else:
        work.state = WorkState.ACTIVE
    _install_bootstrap_prepare_fakes(
        monkeypatch,
        work=work,
        snapshot=SimpleNamespace(is_actionable=True, plan=None),
    )
    with pytest.raises(PolicyViolation, match="exact Work state"):
        ClientRuntimeBootstrapService(
            object(), SimpleNamespace(id=bootstrap_fixture.IDS[0])
        ).prepare(
            project_id=bootstrap_fixture.IDS[1],
            work_item_id=bootstrap_fixture.IDS[2],
            actor_id=bootstrap_fixture.IDS[3],
            client_id="codex",
            session_id="session",
            entry_digest=D,
            source_revision="git:source",
            now=NOW,
        )


@pytest.mark.parametrize("mode", ("event", "plan", "criterion"))
def test_bootstrap_prepare_legacy_adoption_each_verified_precondition(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    work = SimpleNamespace(
        project_id=bootstrap_fixture.IDS[1],
        state=WorkState.VERIFICATION,
        revision=1,
        record_digest=D,
        acceptance_criteria=(SimpleNamespace(verified=mode != "criterion"),),
    )
    snapshot = SimpleNamespace(
        is_actionable=True,
        plan=None if mode == "plan" else SimpleNamespace(id=bootstrap_fixture.IDS[4]),
    )
    _install_bootstrap_prepare_fakes(monkeypatch, work=work, snapshot=snapshot)
    with pytest.raises(PolicyViolation, match="verified pre_close"):
        ClientRuntimeBootstrapService(
            object(), SimpleNamespace(id=bootstrap_fixture.IDS[0])
        ).prepare(
            project_id=bootstrap_fixture.IDS[1],
            work_item_id=bootstrap_fixture.IDS[2],
            actor_id=bootstrap_fixture.IDS[3],
            client_id="codex",
            session_id="session",
            entry_digest=D,
            source_revision="git:source",
            event_type="other" if mode == "event" else "pre_close",
            adopt_existing=True,
            now=NOW,
        )


@pytest.mark.parametrize("mode", ("project", "state", "revision", "digest", "actionable"))
def test_bootstrap_apply_revalidates_every_work_graph_binding(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    plan = bootstrap_fixture._bootstrap_plan()
    work = SimpleNamespace(
        id=plan.work_item_id,
        project_id=plan.project_id,
        state=WorkState.PROPOSED,
        revision=plan.work_revision,
        record_digest=plan.work_record_digest,
    )
    if mode == "project":
        work.project_id = bootstrap_fixture.IDS[40]
    elif mode == "state":
        work.state = WorkState.ACTIVE
    elif mode == "revision":
        work.revision += 1
    elif mode == "digest":
        work.record_digest = digest("changed-work")
    graph = SimpleNamespace(items=SimpleNamespace(get=lambda _work_id: work))
    graph.snapshot = lambda _work_id: SimpleNamespace(is_actionable=mode != "actionable", plan=None)
    monkeypatch.setattr(bootstrap_module, "WorkGraphService", lambda *_a, **_k: graph)
    monkeypatch.setattr(
        bootstrap_module,
        "GovernanceService",
        lambda *_a, **_k: SimpleNamespace(
            policies=SimpleNamespace(
                current=lambda _name: SimpleNamespace(policy_digest=plan.policy_digest)
            )
        ),
    )
    with pytest.raises(PolicyViolation, match="state/revision drift"):
        ClientRuntimeBootstrapService(object(), SimpleNamespace(id=plan.realm_id)).apply(
            plan,
            supplied_plan_digest=plan.plan_digest,
            current_entry_digest=plan.entry_digest,
            current_source_revision=plan.source_revision,
            now=NOW,
        )


@pytest.mark.parametrize(
    "mode", ("schema", "kind", "attempts", "capability", "assignment", "run", "work", "plan")
)
def test_claimed_bootstrap_parent_contract_checks_each_field_before_effect(
    tmp_path: Path, mode: str
) -> None:
    work = bootstrap_fixture._claimed_parent()
    if mode == "schema":
        work.job.payload["schema"] = "wrong"
    elif mode == "kind":
        work.job.kind = JobKind.READ_ONLY
    elif mode == "attempts":
        work.job.max_attempts = 2
    elif mode == "capability":
        work.job.required_capabilities = ("wrong",)
    else:
        setattr(
            work.job,
            {
                "assignment": "assignment_id",
                "run": "run_id",
                "work": "work_item_id",
                "plan": "plan_id",
            }[mode],
            None,
        )
    with pytest.raises(PolicyViolation, match=r"contract drift|identity eksik"):
        ClaimedLifecycleBootstrapService(object(), bootstrap_fixture.IDS[0]).materialize(
            work, tmp_path, now=NOW
        )


@pytest.mark.parametrize("mode", ("plan", "effect", "resources"))
def test_claimed_bootstrap_authorization_checks_each_exact_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    work = bootstrap_fixture._claimed_parent()
    entry = SimpleNamespace(entry_digest=digest("entry"))
    authorization = SimpleNamespace(
        work_item_id=work.job.work_item_id,
        plan_id=work.job.plan_id,
        effect_digest=digest("effect"),
        scope=SimpleNamespace(allowed_resources=("runtime-bootstrap:resource",)),
    )
    if mode == "plan":
        authorization.plan_id = bootstrap_fixture.IDS[40]
    elif mode == "effect":
        authorization.effect_digest = digest("other-effect")
    else:
        authorization.scope.allowed_resources = ("different",)
    monkeypatch.setattr(
        bootstrap_module,
        "ClientLifecycleSpool",
        lambda *_a, **_k: SimpleNamespace(pending=lambda **_kw: (entry,)),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "legacy_repository",
        lambda *_a, **_k: SimpleNamespace(get=lambda _id: authorization),
    )
    with pytest.raises(PolicyViolation, match="authorization drift"):
        ClaimedLifecycleBootstrapService(object(), bootstrap_fixture.IDS[0]).materialize(
            work, tmp_path, now=NOW
        )


@pytest.mark.parametrize(
    "mode", ("keys", "schema", "kind", "capability", "assignment", "run", "work", "plan")
)
def test_bind_child_envelope_rejects_each_immutable_contract_drift(mode: str) -> None:
    work = SimpleNamespace(
        job=SimpleNamespace(
            payload={
                "schema": "zekam-codex-lifecycle-job/v1",
                "authorization_id": str(bootstrap_fixture.IDS[1]),
                "lifecycle_plan_body": {},
            },
            kind=JobKind.MUTATION,
            required_capabilities=("client.lifecycle.codex-drain",),
            assignment_id=bootstrap_fixture.IDS[2],
            run_id=bootstrap_fixture.IDS[3],
            work_item_id=bootstrap_fixture.IDS[4],
            plan_id=bootstrap_fixture.IDS[5],
        )
    )
    if mode == "keys":
        work.job.payload["extra"] = True
    elif mode == "schema":
        work.job.payload["schema"] = "wrong"
    elif mode == "kind":
        work.job.kind = JobKind.READ_ONLY
    elif mode == "capability":
        work.job.required_capabilities = ("wrong",)
    else:
        setattr(
            work.job,
            {
                "assignment": "assignment_id",
                "run": "run_id",
                "work": "work_item_id",
                "plan": "plan_id",
            }[mode],
            None,
        )
    with pytest.raises(PolicyViolation, match="immutable materialization payload drift"):
        ClaimedLifecycleBootstrapService(object(), bootstrap_fixture.IDS[0]).bind_child_envelope(
            work, now=NOW
        )


def test_bind_child_envelope_rejects_non_uuid_authorization() -> None:
    work = SimpleNamespace(
        job=SimpleNamespace(
            payload={
                "schema": "zekam-codex-lifecycle-job/v1",
                "authorization_id": "bad",
                "lifecycle_plan_body": {},
            },
            kind=JobKind.MUTATION,
            required_capabilities=("client.lifecycle.codex-drain",),
            assignment_id=bootstrap_fixture.IDS[2],
            run_id=bootstrap_fixture.IDS[3],
            work_item_id=bootstrap_fixture.IDS[4],
            plan_id=bootstrap_fixture.IDS[5],
        )
    )
    with pytest.raises(PolicyViolation, match="materialized UUID drift"):
        ClaimedLifecycleBootstrapService(object(), bootstrap_fixture.IDS[0]).bind_child_envelope(
            work, now=NOW
        )


@pytest.mark.parametrize("missing", ("work_item_id", "plan_id", "run_id"))
def test_prepare_child_plans_requires_each_parent_identity(missing: str) -> None:
    job = SimpleNamespace(
        work_item_id=bootstrap_fixture.IDS[1],
        plan_id=bootstrap_fixture.IDS[2],
        run_id=bootstrap_fixture.IDS[3],
    )
    setattr(job, missing, None)
    with pytest.raises(PolicyViolation, match="parent identity eksik"):
        ClaimedLifecycleBootstrapService(object(), bootstrap_fixture.IDS[0])._prepare_child_plans(
            job=job,
            child_job_id=bootstrap_fixture.IDS[4],
            entry=object(),
            source_revision="git:source",
            source_digest=D,
            migration_digest=D,
            policy_digest=D,
            packet=cast(Any, object()),
            now=NOW,
        )


def test_store_projection_rejects_missing_work_identity() -> None:
    with pytest.raises(PolicyViolation, match="projection Work identity eksik"):
        ClaimedLifecycleBootstrapService(object(), bootstrap_fixture.IDS[0])._store_projection(
            job=SimpleNamespace(project_id=bootstrap_fixture.IDS[1], work_item_id=None),
            facts=(1, "active", D, "git:source", None, 1, D),
            source_digest=D,
            now=NOW,
        )


def test_materialize_claimed_rejects_planned_manifest_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = bootstrap_fixture._claimed_parent()
    template = SimpleNamespace()
    repository = SimpleNamespace(
        projection_facts=lambda *_a: (1, "active", D, "git:source", D, 1, D),
        run_bindings=lambda _run_id: ("git:source", D, 1, 1, 1, NOW),
        current=lambda *_a: template,
    )
    monkeypatch.setattr(bootstrap_module, "legacy_repository", lambda *_a, **_k: repository)
    monkeypatch.setattr(
        bootstrap_module,
        "_materialized_manifest",
        lambda **_k: (object(), SimpleNamespace(manifest_digest=digest("wrong-manifest"))),
    )
    monkeypatch.setattr(ClaimedLifecycleBootstrapService, "_policy", lambda *_a: D)
    with pytest.raises(PolicyViolation, match="planned context manifest drift"):
        ClaimedLifecycleBootstrapService(object(), bootstrap_fixture.IDS[0])._materialize_claimed(
            work=work,
            entry=SimpleNamespace(entry_digest=digest("entry")),
            child_assignment_id=bootstrap_fixture.IDS[2],
            close_assignment_id=None,
            authorizations=object(),
            context_created_at=NOW,
            now=NOW,
        )


def _install_bootstrap_apply_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: Any,
    assignment_replay: bool = False,
    bootstrap_job_replay: bool = False,
    adoption_failure: str | None = None,
    assignment_count: int = 1,
) -> tuple[Any, Any, list[str]]:
    connection = bootstrap_fixture._TransactionConnection()

    @contextlib.contextmanager
    def cursor() -> Any:
        yield SimpleNamespace(
            execute=lambda *_a, **_k: None,
            fetchone=lambda: (assignment_count,),
        )

    connection.cursor = cursor
    prior_plan = SimpleNamespace(
        id=bootstrap_fixture.IDS[10],
        plan_digest=digest("prior-plan"),
        execution_order=("old",),
    )
    adoption_plan = SimpleNamespace(
        id=bootstrap_fixture.IDS[11],
        plan_digest=digest("adoption-plan"),
        execution_order=("client-lifecycle-legacy-adoption",),
    )
    task_plan = SimpleNamespace(
        id=bootstrap_fixture.IDS[12],
        plan_digest=digest("task-plan"),
        execution_order=("client-lifecycle-bootstrap", "client-lifecycle-drain"),
    )
    work = SimpleNamespace(
        id=plan.work_item_id,
        project_id=plan.project_id,
        state=WorkState.VERIFICATION if plan.adopt_existing else WorkState.ACTIVE,
        revision=plan.work_revision,
        record_digest=plan.work_record_digest,
    )
    graph_calls: list[str] = []

    class Graph:
        items = SimpleNamespace(get=lambda _work_id: work)

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        @staticmethod
        def snapshot(_work_id: UUID) -> Any:
            return SimpleNamespace(is_actionable=True, plan=prior_plan)

        @staticmethod
        def set_intent(*_args: object, **_kwargs: object) -> None:
            graph_calls.append("intent")

        @staticmethod
        def create_plan(*_args: object, **_kwargs: object) -> Any:
            if plan.adopt_existing and "adoption-plan" not in graph_calls:
                graph_calls.append("adoption-plan")
                return adoption_plan
            graph_calls.append("task-plan")
            return task_plan

        @staticmethod
        def transition(*_args: object, **_kwargs: object) -> None:
            graph_calls.append("transition")

    assignment_index = 20

    def assignment(**_kwargs: object) -> Any:
        nonlocal assignment_index
        assignment_index += 1
        return SimpleNamespace(id=bootstrap_fixture.IDS[assignment_index])

    class Assignments:
        created = 0

        @classmethod
        def create(cls, row: Any) -> tuple[UUID, bool]:
            cls.created += 1
            return row.id, not (assignment_replay and cls.created == 1)

        @staticmethod
        def complete_terminal_plan(*_args: object, **_kwargs: object) -> None:
            graph_calls.append("complete")

    class Runs:
        @staticmethod
        def create_run(_row: Any) -> None:
            return None

        @staticmethod
        def activate_run(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def finish_run(*_args: object, **_kwargs: object) -> None:
            graph_calls.append("adopted-run-failed")

    class Jobs:
        @staticmethod
        def enqueue(_job: Any) -> tuple[Any, bool]:
            return SimpleNamespace(id=bootstrap_fixture.IDS[45]), not bootstrap_job_replay

    class Authorizations:
        @staticmethod
        def issue(_authorization: Any) -> None:
            return None

        @staticmethod
        def consume(*_args: object, **_kwargs: object) -> Any:
            return SimpleNamespace(consumed=adoption_failure != "consume")

    template = SimpleNamespace(
        assert_rebootstrap_admissible=lambda _work_id: graph_calls.append("rebootstrap"),
        assert_legacy_adoption_admissible=lambda *_a, **_k: plan.adopted_run_id,
    )
    continuity = SimpleNamespace(store_checkpoint=lambda *_a, **_k: None)
    repositories = {
        "agent_assignment": Assignments(),
        "execution_run": Runs(),
        "job": Jobs(),
        "authorization": Authorizations(),
        "lifecycle_runtime_template": template,
        "context_continuity": continuity,
    }
    monkeypatch.setattr(bootstrap_module, "WorkGraphService", Graph)
    monkeypatch.setattr(
        bootstrap_module,
        "GovernanceService",
        lambda *_a, **_k: SimpleNamespace(
            policies=SimpleNamespace(
                current=lambda _name: SimpleNamespace(policy_digest=plan.policy_digest)
            )
        ),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "legacy_repository",
        lambda name, *_a, **_k: repositories[name],
    )
    monkeypatch.setattr(bootstrap_module, "_assignment", assignment)
    monkeypatch.setattr(
        bootstrap_module,
        "ExecutionRun",
        SimpleNamespace(create=lambda **_k: SimpleNamespace(id=bootstrap_fixture.IDS[13])),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "Authorization",
        SimpleNamespace(
            issue=lambda **_k: SimpleNamespace(
                id=bootstrap_fixture.IDS[14],
                authorization_digest=digest("authorization"),
            )
        ),
    )
    monkeypatch.setattr(
        bootstrap_module, "Job", SimpleNamespace(create=lambda **kw: SimpleNamespace(**kw))
    )
    monkeypatch.setattr(
        bootstrap_module,
        "parse_requests",
        lambda **kw: tuple(SimpleNamespace(resource=value) for value in next(iter(kw.values()))),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_planned_manifest",
        lambda _plan: SimpleNamespace(manifest_digest=digest("manifest")),
    )
    monkeypatch.setattr(bootstrap_module, "new_uuid7", lambda **_k: bootstrap_fixture.IDS[13])
    monkeypatch.setattr(
        bootstrap_module,
        "Checkpoint",
        lambda *_a, **_k: SimpleNamespace(checkpoint_id="checkpoint"),
    )

    class AdoptionHost:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.jobs = SimpleNamespace(
                enqueue=lambda _job: (
                    SimpleNamespace(id=bootstrap_fixture.IDS[46]),
                    adoption_failure != "enqueue",
                )
            )

        @staticmethod
        def acquire_work(**_kwargs: object) -> Any:
            if adoption_failure == "missing-work":
                return None
            job_id = (
                bootstrap_fixture.IDS[47]
                if adoption_failure == "wrong-work"
                else bootstrap_fixture.IDS[46]
            )
            return SimpleNamespace(job=SimpleNamespace(id=job_id))

        @staticmethod
        def claim_effect(*_args: object, **_kwargs: object) -> Any:
            return SimpleNamespace(id=bootstrap_fixture.IDS[48])

        @staticmethod
        def record_success(*_args: object, **_kwargs: object) -> Any:
            return SimpleNamespace(id=bootstrap_fixture.IDS[49])

        @staticmethod
        def finish(*_args: object, **_kwargs: object) -> bool:
            return adoption_failure != "finish"

    monkeypatch.setattr(bootstrap_module, "ExecutionHost", AdoptionHost)
    return connection, work, graph_calls


@pytest.mark.parametrize("mode", ("success", "assignment-replay", "job-replay"))
def test_bootstrap_apply_rebootstrap_replay_and_terminal_paths(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    plan = bootstrap_fixture._bootstrap_plan(rebootstrap=True)
    connection, _work, calls = _install_bootstrap_apply_runtime(
        monkeypatch,
        plan=plan,
        assignment_replay=mode == "assignment-replay",
        bootstrap_job_replay=mode == "job-replay",
    )
    if mode == "success":
        result = ClientRuntimeBootstrapService(connection, SimpleNamespace(id=plan.realm_id)).apply(
            plan,
            supplied_plan_digest=plan.plan_digest,
            current_entry_digest=plan.entry_digest,
            current_source_revision=plan.source_revision,
            now=NOW,
        )
        assert result.run_id == bootstrap_fixture.IDS[13]
        assert calls.count("rebootstrap") == 1
        assert connection.rollbacks == 0
    else:
        with pytest.raises(PolicyViolation, match=r"assignment replay|job replay"):
            ClientRuntimeBootstrapService(connection, SimpleNamespace(id=plan.realm_id)).apply(
                plan,
                supplied_plan_digest=plan.plan_digest,
                current_entry_digest=plan.entry_digest,
                current_source_revision=plan.source_revision,
                now=NOW,
            )
        assert connection.rollbacks == 1


def test_bootstrap_apply_adoption_detects_run_binding_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = bootstrap_fixture._bootstrap_plan(
        event_type="pre_close",
        adopt_existing=True,
        adopted_run_id=bootstrap_fixture.IDS[19],
    )
    connection, _work, _calls = _install_bootstrap_apply_runtime(monkeypatch, plan=plan)
    repositories = bootstrap_module.legacy_repository
    original = repositories("lifecycle_runtime_template", object(), plan.realm_id)
    original.assert_legacy_adoption_admissible = lambda *_a, **_k: bootstrap_fixture.IDS[18]
    with pytest.raises(PolicyViolation, match="run binding drift"):
        ClientRuntimeBootstrapService(connection, SimpleNamespace(id=plan.realm_id)).apply(
            plan,
            supplied_plan_digest=plan.plan_digest,
            current_entry_digest=plan.entry_digest,
            current_source_revision=plan.source_revision,
            now=NOW,
        )


def test_bootstrap_apply_adoption_with_no_prior_assignments_skips_terminal_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = bootstrap_fixture._bootstrap_plan(
        event_type="pre_close",
        adopt_existing=True,
        adopted_run_id=bootstrap_fixture.IDS[19],
    )
    connection, _work, calls = _install_bootstrap_apply_runtime(
        monkeypatch, plan=plan, assignment_count=0
    )
    result = ClientRuntimeBootstrapService(connection, SimpleNamespace(id=plan.realm_id)).apply(
        plan,
        supplied_plan_digest=plan.plan_digest,
        current_entry_digest=plan.entry_digest,
        current_source_revision=plan.source_revision,
        now=NOW,
    )
    assert result.adoption_job_id is not None
    assert "complete" in calls  # adoption plan completion only


@pytest.mark.parametrize(
    "failure", (None, "enqueue", "missing-work", "wrong-work", "consume", "finish")
)
def test_bootstrap_apply_legacy_adoption_terminal_chain_and_failures(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    plan = bootstrap_fixture._bootstrap_plan(
        event_type="pre_close",
        adopt_existing=True,
        adopted_run_id=bootstrap_fixture.IDS[19],
    )
    connection, _work, calls = _install_bootstrap_apply_runtime(
        monkeypatch, plan=plan, adoption_failure=failure
    )
    service = ClientRuntimeBootstrapService(connection, SimpleNamespace(id=plan.realm_id))
    if failure is None:
        result = service.apply(
            plan,
            supplied_plan_digest=plan.plan_digest,
            current_entry_digest=plan.entry_digest,
            current_source_revision=plan.source_revision,
            now=NOW,
        )
        assert result.adoption_job_id == bootstrap_fixture.IDS[46]
        assert result.adoption_claim_id == bootstrap_fixture.IDS[48]
        assert result.adoption_receipt_id == bootstrap_fixture.IDS[49]
        assert "adopted-run-failed" in calls
    else:
        with pytest.raises(PolicyViolation):
            service.apply(
                plan,
                supplied_plan_digest=plan.plan_digest,
                current_entry_digest=plan.entry_digest,
                current_source_revision=plan.source_revision,
                now=NOW,
            )
        assert connection.rollbacks == 1


@pytest.mark.parametrize(
    ("mode", "match"),
    (
        ("claim-missing", "claim missing"),
        ("job-missing", "job/case cardinality"),
        ("case-id", "case identity/scope"),
        ("case-job", "case identity/scope"),
        ("case-outbox", "case identity/scope"),
        ("case-kind", "case identity/scope"),
        ("claim-digest", "commitment/time"),
        ("case-time-before-claim", "preceded claim"),
        ("case-evidence", "case evidence drift"),
        ("job-state", "open recovery graph"),
        ("terminal-evidence", "open recovery graph"),
        ("job-updated", "open recovery graph"),
    ),
)
def test_recovery_selected_open_graph_each_low_level_corruption_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    match: str,
) -> None:
    path, binding, _b1, store, claim_id = recovery_fixture._prepared(tmp_path, monkeypatch)
    store.enter_unknown(UnknownEffectRequest(binding, claim_id, recovery_fixture._revision(path)))
    with sqlite3.connect(path) as db:
        case_id, job_id = db.execute(
            "select id,job_id from local_recovery_case where effect_claim_id=?",
            (claim_id,),
        ).fetchone()
    if mode == "claim-missing":
        _unsafe_sql(
            path, "local_effect_claim", "delete from local_effect_claim where id=?", (claim_id,)
        )
    elif mode == "job-missing":
        _unsafe_sql(path, "local_job", "delete from local_job where id=?", (job_id,))
    elif mode == "case-id":
        _unsafe_sql(
            path,
            "local_recovery_case",
            "update local_recovery_case set id='wrong' where id=?",
            (case_id,),
        )
    elif mode == "case-job":
        _unsafe_sql(
            path,
            "local_recovery_case",
            "update local_recovery_case set job_id='wrong' where id=?",
            (case_id,),
        )
    elif mode == "case-outbox":
        _unsafe_sql(
            path,
            "local_recovery_case",
            "update local_recovery_case set outbox_id='wrong' where id=?",
            (case_id,),
        )
    elif mode == "case-kind":
        _unsafe_sql(
            path,
            "local_recovery_case",
            "update local_recovery_case set case_kind='wrong' where id=?",
            (case_id,),
        )
    elif mode == "claim-digest":
        _unsafe_sql(
            path,
            "local_effect_claim",
            "update local_effect_claim set effect_digest='bad' where id=?",
            (claim_id,),
        )
    elif mode == "case-time-before-claim":
        _unsafe_sql(
            path,
            "local_recovery_case",
            "update local_recovery_case set created_at='2020-01-01T00:00:00+00:00' where id=?",
            (case_id,),
        )
    elif mode == "case-evidence":
        _unsafe_sql(
            path,
            "local_recovery_case",
            "update local_recovery_case set evidence_digest=? where id=?",
            (D, case_id),
        )
    elif mode == "job-state":
        _unsafe_sql(path, "local_job", "update local_job set state='failed' where id=?", (job_id,))
    elif mode == "terminal-evidence":
        _unsafe_sql(
            path,
            "local_job",
            "update local_job set terminal_evidence_digest=? where id=?",
            (D, job_id),
        )
    else:
        _unsafe_sql(
            path, "local_job", "update local_job set updated_at=? where id=?", (TS2, job_id)
        )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match=match):
            verify_selected_b2_graph(
                db,
                binding,
                claim_id,
                trusted_now=dt.datetime.fromisoformat(recovery_fixture.UNKNOWN_AT),
            )


@pytest.mark.parametrize(
    ("mode", "match"),
    (
        ("resolution-digest", "commitment/time"),
        ("resolution-id", "resolution parity"),
        ("resolution-outcome", "resolution parity"),
        ("case-state", "resolution parity"),
        ("case-resolved-at", "resolution parity"),
        ("resolution-time", "resolution parity"),
        ("job-state", "reconciled job evidence"),
        ("job-evidence", "reconciled job evidence"),
        ("job-time", "reconciled job evidence"),
        ("restored-missing", "restored revision cardinality"),
    ),
)
def test_recovery_selected_resolved_graph_each_low_level_corruption_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    match: str,
) -> None:
    path, binding, _b1, store, claim_id = recovery_fixture._prepared(tmp_path, monkeypatch)
    entered = store.enter_unknown(
        UnknownEffectRequest(binding, claim_id, recovery_fixture._revision(path))
    )
    case_id = str(entered.body()["recovery_case_id"])
    store.resolve(
        ResolveEffectRecoveryRequest(
            binding,
            case_id,
            recovery_fixture._revision(path),
            recovery_fixture._tail(path, binding),
        )
    )
    with sqlite3.connect(path) as db:
        resolution_id = str(
            db.execute(
                "select id from local_recovery_resolution where recovery_case_id=?",
                (case_id,),
            ).fetchone()[0]
        )
        job_id = str(
            db.execute("select job_id from local_recovery_case where id=?", (case_id,)).fetchone()[
                0
            ]
        )
    if mode == "resolution-digest":
        _unsafe_sql(
            path,
            "local_recovery_resolution",
            "update local_recovery_resolution set evidence_digest='bad' where id=?",
            (resolution_id,),
        )
    elif mode == "resolution-id":
        _unsafe_sql(
            path,
            "local_recovery_resolution",
            "update local_recovery_resolution set id='wrong' where id=?",
            (resolution_id,),
        )
    elif mode == "resolution-outcome":
        _unsafe_sql(
            path,
            "local_recovery_resolution",
            "update local_recovery_resolution set outcome='wrong' where id=?",
            (resolution_id,),
        )
    elif mode == "case-state":
        _unsafe_sql(
            path,
            "local_recovery_case",
            "update local_recovery_case set state='open' where id=?",
            (case_id,),
        )
    elif mode == "case-resolved-at":
        _unsafe_sql(
            path,
            "local_recovery_case",
            "update local_recovery_case set resolved_at=? where id=?",
            (TS2, case_id),
        )
    elif mode == "resolution-time":
        _unsafe_sql(
            path,
            "local_recovery_resolution",
            "update local_recovery_resolution set "
            "created_at='2020-01-01T00:00:00+00:00' where id=?",
            (resolution_id,),
        )
    elif mode == "job-state":
        _unsafe_sql(path, "local_job", "update local_job set state='failed' where id=?", (job_id,))
    elif mode == "job-evidence":
        _unsafe_sql(
            path,
            "local_job",
            "update local_job set terminal_evidence_digest=? where id=?",
            (D, job_id),
        )
    elif mode == "job-time":
        _unsafe_sql(
            path, "local_job", "update local_job set updated_at=? where id=?", (TS2, job_id)
        )
    else:
        _unsafe_sql(
            path,
            "continuity_hook_attachment_revision",
            "delete from continuity_hook_attachment_revision where local_recovery_resolution_id=?",
            (resolution_id,),
        )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match=match):
            verify_selected_b2_graph(
                db,
                binding,
                claim_id,
                trusted_now=dt.datetime.fromisoformat(recovery_fixture.RESOLVED_AT),
            )


def test_recovery_selected_failed_resolution_preserves_attachment_and_has_no_crash_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _b1, store, claim_id = recovery_fixture._prepared(tmp_path, monkeypatch)
    entered = store.enter_unknown(
        UnknownEffectRequest(binding, claim_id, recovery_fixture._revision(path))
    )
    case_id = str(entered.body()["recovery_case_id"])
    store.adjudicator.outcome = "failed"
    result = store.resolve(
        ResolveEffectRecoveryRequest(
            binding,
            case_id,
            recovery_fixture._revision(path),
            recovery_fixture._tail(path, binding),
        )
    )
    assert result.body()["job_state"] == "failed"
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        verify_selected_b2_graph(
            db,
            binding,
            claim_id,
            trusted_now=dt.datetime.fromisoformat(recovery_fixture.RESOLVED_AT),
        )


def _install_materialize_claimed_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    internal_event_type: str,
    hydration: bool,
    child_replay: bool = False,
    close_replay: bool = False,
) -> tuple[Any, Any, Any]:
    work = bootstrap_fixture._claimed_parent()
    work.job.payload["context_manifest_digest"] = digest("manifest")
    entry = SimpleNamespace(
        entry_digest=digest("entry"),
        session_id="session",
        delivery_id="delivery",
        internal_event_type=internal_event_type,
        observation_digest=digest("observation"),
    )
    template = SimpleNamespace(
        execution_environment_snapshot_digest=digest("environment"),
        model_id="model",
        provider_ref="provider",
        route_decision_digest=digest("route"),
        compiled_tool_set_digest=digest("tools"),
        hook_set_digest=digest("hooks"),
        config_effective_digest=digest("config"),
        route_decision_id=bootstrap_fixture.IDS[30],
        route_expires_at=NOW + dt.timedelta(minutes=5),
        provider_binding_id=bootstrap_fixture.IDS[31],
        provider_binding_digest=digest("binding"),
        policy_digest=digest("policy"),
        __dataclass_fields__={},
    )
    candidate = SimpleNamespace(
        candidate_id="candidate",
        authority=SimpleNamespace(),
        source_ref="source",
        content_digest=digest("content"),
    )
    manifest = SimpleNamespace(manifest_digest=digest("manifest"))
    context = SimpleNamespace(
        store_manifest=lambda _manifest: bootstrap_fixture.IDS[32],
        store_fragment_set=lambda *_a, **_k: None,
    )
    packet = SimpleNamespace(id=bootstrap_fixture.IDS[33], packet_digest=digest("packet"))
    execution = SimpleNamespace(
        create_packet=lambda _packet: None,
        bind_assignment_environment=lambda _binding: None,
        create_turn_snapshot=lambda _turn: None,
        create_envelope=lambda _envelope: None,
    )
    hook = SimpleNamespace(start_session=lambda **_k: None)
    enqueue_count = 0

    def enqueue(row: Any) -> tuple[Any, bool]:
        nonlocal enqueue_count
        enqueue_count += 1
        replay = child_replay if enqueue_count == 1 else close_replay
        return SimpleNamespace(id=row.id), not replay

    job_repo = SimpleNamespace(enqueue=enqueue)
    template_repo = SimpleNamespace(
        projection_facts=lambda *_a: (1, "active", D, "git:template", D, 1, D),
        run_bindings=lambda _run_id: (
            "git:source",
            digest("policy"),
            100,
            20,
            1,
            NOW + dt.timedelta(minutes=5),
        ),
        current=lambda *_a: template,
    )
    repositories = {
        "lifecycle_runtime_template": template_repo,
        "context_continuity": context,
        "execution_run": execution,
        "hook_runtime": hook,
        "job": job_repo,
    }
    monkeypatch.setattr(
        bootstrap_module,
        "legacy_repository",
        lambda name, *_a, **_k: repositories[name],
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_materialized_manifest",
        lambda **_k: (candidate, manifest),
    )
    monkeypatch.setattr(bootstrap_module, "ContextFragment", lambda **_k: object())
    monkeypatch.setattr(bootstrap_module, "ContextFragmentSet", lambda *_a, **_k: object())
    monkeypatch.setattr(
        bootstrap_module, "ContextPacket", SimpleNamespace(create=lambda **_k: packet)
    )
    monkeypatch.setattr(
        bootstrap_module,
        "AssignmentEnvironmentBinding",
        SimpleNamespace(create=lambda **_k: object()),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "TurnExecutionSnapshot",
        SimpleNamespace(
            create=lambda **_k: SimpleNamespace(
                id=bootstrap_fixture.IDS[34], turn_snapshot_digest=digest("turn")
            )
        ),
    )
    monkeypatch.setattr(
        bootstrap_module, "ExecutionEnvelope", SimpleNamespace(create=lambda **_k: object())
    )
    monkeypatch.setattr(ClaimedLifecycleBootstrapService, "_policy", lambda *_a: digest("policy"))
    monkeypatch.setattr(
        ClaimedLifecycleBootstrapService, "_store_projection", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        ClaimedLifecycleBootstrapService, "_assert_turn_bindings", lambda *_a, **_k: None
    )
    lifecycle_plan = SimpleNamespace(
        plan_digest=digest("lifecycle-plan"),
        effect_digest=digest("lifecycle-effect"),
        resource="memory:resource",
        body=lambda: {"schema": "plan"},
    )
    hydration_plan = (
        SimpleNamespace(
            plan_digest=digest("hydration-plan"),
            effect_digest=digest("hydration-effect"),
            resource="memory:hydration",
        )
        if hydration
        else None
    )
    monkeypatch.setattr(
        ClaimedLifecycleBootstrapService,
        "_prepare_child_plans",
        lambda *_a, **_k: (lifecycle_plan, hydration_plan),
    )
    auth_ids = iter(bootstrap_fixture.IDS[35:40])
    monkeypatch.setattr(
        bootstrap_module,
        "Authorization",
        SimpleNamespace(issue=lambda **_k: SimpleNamespace(id=next(auth_ids))),
    )
    monkeypatch.setattr(
        bootstrap_module, "Job", SimpleNamespace(create=lambda **kw: SimpleNamespace(**kw))
    )

    def fake_replace(row: Any, **changes: Any) -> Any:
        values = vars(row).copy()
        values.update(changes)
        return SimpleNamespace(**values)

    monkeypatch.setattr(bootstrap_module, "replace", fake_replace)
    ids = iter(bootstrap_fixture.IDS[40:45])
    monkeypatch.setattr(bootstrap_module, "new_uuid7", lambda **_k: next(ids))
    monkeypatch.setattr(
        bootstrap_module,
        "parse_requests",
        lambda **kw: tuple(SimpleNamespace(resource=x) for x in next(iter(kw.values()))),
    )
    parent_auth = SimpleNamespace(
        id=bootstrap_fixture.IDS[1],
        actor_id=bootstrap_fixture.IDS[2],
        scope=SimpleNamespace(body=lambda: {}),
    )
    authorizations = SimpleNamespace(
        get=lambda _id: parent_auth,
        issue=lambda _auth: None,
        list_active=lambda **_k: (),
    )
    return work, entry, authorizations


@pytest.mark.parametrize(
    ("event", "hydration", "close_assignment", "child_replay", "close_replay", "match"),
    (
        ("session_start", True, None, False, False, None),
        ("post_compaction", False, None, False, False, None),
        ("session_start", False, None, True, False, "child job replay"),
        ("pre_close", False, None, False, False, "close assignment"),
        ("pre_close", False, bootstrap_fixture.IDS[50], False, True, "close child job replay"),
        ("pre_close", False, bootstrap_fixture.IDS[50], False, False, None),
        ("session_start", False, bootstrap_fixture.IDS[50], False, False, "Non-close"),
    ),
)
def test_materialize_claimed_child_hydration_close_and_replay_branches(
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    hydration: bool,
    close_assignment: UUID | None,
    child_replay: bool,
    close_replay: bool,
    match: str | None,
) -> None:
    work, entry, authorizations = _install_materialize_claimed_fakes(
        monkeypatch,
        internal_event_type=event,
        hydration=hydration,
        child_replay=child_replay,
        close_replay=close_replay,
    )

    def call() -> Any:
        return ClaimedLifecycleBootstrapService(
            object(), bootstrap_fixture.IDS[0]
        )._materialize_claimed(
            work=work,
            entry=entry,
            child_assignment_id=bootstrap_fixture.IDS[3],
            close_assignment_id=close_assignment,
            authorizations=authorizations,
            context_created_at=NOW,
            now=NOW,
        )

    if match is None:
        assert call().context_manifest_digest == digest("manifest")
    else:
        with pytest.raises(PolicyViolation, match=match):
            call()


@pytest.mark.parametrize(
    ("mode", "match"),
    (
        ("effect-scope", "effect scope drift"),
        ("receipt-evidence", "receipt evidence drift"),
        ("receipt-before-claim", "receipt preceded claim"),
        ("claim-fence", "fencing generation drift"),
        ("claim-before-job", "claim preceded job"),
        ("terminal-lease", "terminal job retained lease"),
        ("terminal-order", "terminal effect timestamp order"),
        ("terminal-history", "terminal outbox history drift"),
        ("recovery-key", "recovery outbox key malformed"),
        ("state-payload", "job-state outbox payload drift"),
        ("completed-no-claim", "completed job evidence drift"),
        ("incomplete-delivery", "pending delivery drift"),
    ),
)
def test_writer_completed_runtime_graph_each_low_level_corruption_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    match: str,
) -> None:
    path = tmp_path / mode / "runtime.db"
    path.parent.mkdir()
    seed = close_fixture._seed(path)
    writer = close_fixture._v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(close_fixture._request(seed))
    close_fixture._materialize_and_complete(path, frozen, monkeypatch)
    with sqlite3.connect(path) as db:
        claim_id, lease_id = db.execute(
            "select id,lease_id from local_effect_claim where job_id=?", (frozen.job_id,)
        ).fetchone()
        receipt_id = db.execute(
            "select id from local_effect_receipt where claim_id=?", (claim_id,)
        ).fetchone()[0]
        terminal_id = db.execute(
            "select id from local_outbox where job_id=? and event_kind='job.completed'",
            (frozen.job_id,),
        ).fetchone()[0]
        delivery_id = db.execute(
            "select outbox_id from local_outbox_delivery where outbox_id=?",
            (terminal_id,),
        ).fetchone()[0]
    if mode == "effect-scope":
        _unsafe_sql(
            path,
            "local_effect_claim",
            "update local_effect_claim set operation='wrong' where id=?",
            (claim_id,),
        )
    elif mode == "receipt-evidence":
        _unsafe_sql(
            path,
            "local_effect_receipt",
            "update local_effect_receipt set evidence_digest=? where id=?",
            (D, receipt_id),
        )
    elif mode == "receipt-before-claim":
        _unsafe_sql(
            path,
            "local_effect_claim",
            "update local_effect_claim set claimed_at=? where id=?",
            (TS2, claim_id),
        )
    elif mode == "claim-fence":
        _unsafe_sql(
            path,
            "local_effect_claim",
            "update local_effect_claim set fencing_token=2 where id=?",
            (claim_id,),
        )
    elif mode == "claim-before-job":
        _unsafe_sql(
            path,
            "local_effect_claim",
            "update local_effect_claim set claimed_at='2020-01-01T00:00:00+00:00' where id=?",
            (claim_id,),
        )
    elif mode == "terminal-lease":
        _unsafe_sql(
            path,
            "local_lease",
            "insert into local_lease values(?,?,?,?,?,?,?,?)",
            (lease_id, frozen.job_id, "owner", 1, "token", 1, TS1, TS2),
        )
    elif mode == "terminal-order":
        _unsafe_sql(
            path,
            "local_effect_receipt",
            "update local_effect_receipt set created_at=? where id=?",
            (TS2, receipt_id),
        )
    elif mode == "terminal-history":
        _unsafe_sql(path, "local_outbox", "delete from local_outbox where id=?", (terminal_id,))
    elif mode == "recovery-key":
        _unsafe_sql(
            path,
            "local_outbox",
            "update local_outbox set idempotency_key=? where id=?",
            (f"job:{frozen.job_id}:recovery:x:completed", terminal_id),
        )
    elif mode == "state-payload":
        _unsafe_sql(
            path,
            "local_outbox",
            "update local_outbox set payload_json='{}' where id=?",
            (terminal_id,),
        )
    elif mode == "completed-no-claim":
        _unsafe_sql(
            path, "local_effect_claim", "delete from local_effect_claim where id=?", (claim_id,)
        )
    else:
        _unsafe_sql(
            path,
            "local_outbox_delivery",
            "update local_outbox_delivery set state='pending',claim_id=null,owner_id=null,"
            "owner_pid=null,owner_token=null,expires_at=null where outbox_id=?",
            (delivery_id,),
        )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match=match):
            writer._runtime_graph(
                db,
                close_fixture._binding(),
                frozen,
                require_completed=mode == "incomplete-delivery",
            )


@pytest.mark.parametrize(
    ("mode", "match"),
    (
        ("predecessor", "predecessor revision drift"),
        ("hook-missing", "hook recovery scope drift"),
        ("local-missing", "local recovery scope drift"),
        ("local-unscoped", "local recovery scope drift"),
        ("resolution", "immutable resolution drift"),
    ),
)
def test_writer_consume_recovery_scope_and_resolution_guards(mode: str, match: str) -> None:
    binding = close_fixture._binding()
    predecessor = {
        "state": "recovery-required",
        "revision_digest": digest("recovery-predecessor"),
        "previous_revision_digest": digest("frozen"),
        "hook_recovery_case_id": str(bootstrap_fixture.IDS[1]),
        "local_recovery_case_id": str(bootstrap_fixture.IDS[1]),
        "attachment_id": "attachment",
        "process_generation_digest": digest("process"),
    }
    request = SimpleNamespace(
        binding=binding,
        expected_frozen_revision_digest=digest("frozen"),
        operation_key="close-operation",
        request_digest=digest("request"),
    )
    kind = "hook" if mode == "hook-missing" else "local"
    recovery = ExactResolvedRecovery(
        predecessor["revision_digest"],
        kind,
        str(bootstrap_fixture.IDS[1]),
        str(bootstrap_fixture.IDS[2]),
        "restored" if kind == "hook" else "completed",
        TS1,
    )
    if mode == "predecessor":
        predecessor["state"] = "frozen"
        db = _ScriptDB(())
    elif mode == "hook-missing" or mode == "local-missing":
        db = _ScriptDB(())
    else:
        row = {
            "job_id": "job",
            "resolution_id": ("wrong" if mode == "resolution" else recovery.recovery_resolution_id),
            "outcome": recovery.outcome,
            "evidence_digest": D,
            "created_at": recovery.recovered_at,
        }
        db = _ScriptDB(
            (
                ("from local_recovery_case", row),
                ("select 1 from local_job", None if mode == "local-unscoped" else (1,)),
            )
        )
    with pytest.raises(PolicyViolation, match=match):
        SQLiteDormantV4CloseWriter._consume_recovery(
            cast(Any, db),
            cast(Any, request),
            cast(Any, predecessor),
            recovery,
            SimpleNamespace(sequence=1, event_digest=D),
        )


@pytest.mark.parametrize(
    ("mode", "match"),
    (
        ("missing", "preflight missing"),
        ("malformed", "malformed frozen preflight"),
        ("drift", "frozen preflight drift"),
    ),
)
def test_writer_frozen_preflight_rejects_partial_malformed_and_drifted_state(
    tmp_path: Path, mode: str, match: str
) -> None:
    path = tmp_path / mode / "runtime.db"
    path.parent.mkdir()
    seed = close_fixture._seed(path)
    writer = close_fixture._v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(close_fixture._request(seed))
    if mode == "missing":
        _unsafe_sql(
            path,
            "continuity_outbox_binding",
            "delete from continuity_outbox_binding where close_request_digest=?",
            (frozen.request_digest,),
        )
    elif mode == "malformed":
        _unsafe_sql(
            path,
            "continuity_close_request",
            "update continuity_close_request set input_json='{' where request_digest=?",
            (frozen.request_digest,),
        )
    else:
        _unsafe_sql(
            path,
            "continuity_close_request",
            "update continuity_close_request set input_json='{}' where request_digest=?",
            (frozen.request_digest,),
        )
    request = FinalizeClosedWriteRequest(
        close_fixture._binding(),
        frozen.request_digest,
        digest("frozen"),
        "finalize",
        close_fixture.NOW,
    )
    with pytest.raises(PolicyViolation, match=match):
        writer._read_frozen_preflight(request)


@pytest.mark.parametrize(
    ("mode", "match"),
    (
        ("checkpoint", "partial frozen graph"),
        ("job", "partial frozen work graph"),
        ("event", "frozen event replay drift"),
    ),
)
def test_writer_freeze_replay_rejects_partial_and_event_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, match: str
) -> None:
    path = tmp_path / mode / "runtime.db"
    path.parent.mkdir()
    seed = close_fixture._seed(path)
    writer = close_fixture._v4_writer(path, seed)
    request = close_fixture._request(seed)
    frozen = writer.freeze_with_preclose(request)
    schema_state = writer_module.operational_schema.status(path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        original_events = writer._events(db, close_fixture._binding())
    if mode == "checkpoint":
        _unsafe_sql(
            path,
            "continuity_checkpoint",
            "delete from continuity_checkpoint where checkpoint_digest=?",
            (frozen.input_body["checkpoint_digest"],),
        )
    elif mode == "job":
        _unsafe_sql(path, "local_job", "delete from local_job where id=?", (frozen.job_id,))
    else:
        _unsafe_sql(
            path,
            "session_event_detail",
            "update session_event_detail set body_json='{}' where session_id=? and sequence>?",
            (close_fixture.SESSION_ID, request.expected_tail.sequence),
        )
        monkeypatch.setattr(writer, "_events", lambda *_a, **_k: original_events)
    monkeypatch.setattr(writer_module.operational_schema, "status", lambda _path: schema_state)
    monkeypatch.setattr(writer, "_schema", lambda _db: None)
    with pytest.raises(PolicyViolation, match=match):
        writer.freeze_with_preclose(request)


@pytest.mark.parametrize("event_type", ("session_start", "post_compaction"))
def test_prepare_child_plans_real_hydration_route_split(
    monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    lifecycle_plan = SimpleNamespace(
        event=SimpleNamespace(
            event_digest=digest("event"),
            event_id=bootstrap_fixture.IDS[8],
            body=lambda: {},
        )
    )
    hydration_plan = SimpleNamespace(plan_digest=digest("hydration"))
    runtime = SimpleNamespace(start_session=lambda: object())
    monkeypatch.setattr(bootstrap_module, "HookRuntime", lambda **_k: runtime)
    monkeypatch.setattr(bootstrap_module, "memory_hook_bundle", lambda _realm: object())
    monkeypatch.setattr(
        bootstrap_module, "_configure_active_memory_hook_runtime", lambda *_a, **_k: None
    )
    continuity = SimpleNamespace(preview_hydration_inventory=lambda **_k: ("inventory",))
    repositories = {
        "client_lifecycle": SimpleNamespace(previous_continuity_digest=lambda **_k: None),
        "memory_continuity": continuity,
        "authorization": object(),
        "hook_runtime": object(),
    }
    monkeypatch.setattr(
        bootstrap_module,
        "legacy_repository",
        lambda name, *_a, **_k: repositories[name],
    )
    monkeypatch.setattr(
        bootstrap_module,
        "ClientLifecycleBridge",
        lambda *_a, **_k: SimpleNamespace(prepare=lambda *_a2, **_k2: lifecycle_plan),
    )
    monkeypatch.setattr(
        bootstrap_module, "load_codex_contract_evidence", lambda _path: {"file_digest": D}
    )
    monkeypatch.setattr(
        bootstrap_module,
        "LifecycleClientContract",
        SimpleNamespace(verified=lambda **_k: object()),
    )
    monkeypatch.setattr(bootstrap_module, "codex_lifecycle_descriptor", lambda *_a, **_k: object())
    monkeypatch.setattr(bootstrap_module, "LifecycleRequest", lambda **_k: object())
    monkeypatch.setattr(
        bootstrap_module,
        "MemoryContinuityService",
        lambda *_a, **_k: SimpleNamespace(
            prepare_from_inventory=lambda *_a2, **_k2: hydration_plan
        ),
    )
    monkeypatch.setattr(bootstrap_module, "HydrationPreparation", lambda **_k: object())
    job = SimpleNamespace(
        project_id=bootstrap_fixture.IDS[1],
        work_item_id=bootstrap_fixture.IDS[2],
        plan_id=bootstrap_fixture.IDS[3],
        run_id=bootstrap_fixture.IDS[4],
    )
    entry = SimpleNamespace(
        session_id="session",
        client_id="codex",
        entry_digest=digest("entry"),
        external_event_type="SessionStart",
        sequence=1,
        delivery_id="delivery",
        observation={},
        occurred_at=NOW,
        internal_event_type=event_type,
    )
    _, hydration = ClaimedLifecycleBootstrapService(
        object(), bootstrap_fixture.IDS[0]
    )._prepare_child_plans(
        job=job,
        child_job_id=bootstrap_fixture.IDS[5],
        entry=entry,
        source_revision="git:source",
        source_digest=D,
        migration_digest=D,
        policy_digest=D,
        packet=SimpleNamespace(id=bootstrap_fixture.IDS[6]),
        now=NOW,
    )
    assert (hydration is not None) is (event_type == "session_start")


@pytest.mark.parametrize(
    ("mode", "match"),
    (
        ("receipt-parity", "unknown receipt parity"),
        ("retained-lease", "retained lease/locks"),
        ("terminal-route", "terminal/recovery outbox route drift"),
    ),
)
def test_recovery_selected_graph_receipt_lease_and_terminal_route_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    match: str,
) -> None:
    path, binding, _b1, store, claim_id = recovery_fixture._prepared(tmp_path, monkeypatch)
    entered = store.enter_unknown(
        UnknownEffectRequest(binding, claim_id, recovery_fixture._revision(path))
    )
    case_id = str(entered.body()["recovery_case_id"])
    with sqlite3.connect(path) as db:
        job_id = str(
            db.execute("select job_id from local_recovery_case where id=?", (case_id,)).fetchone()[
                0
            ]
        )
    if mode == "receipt-parity":
        _unsafe_sql(
            path,
            "local_effect_receipt",
            "update local_effect_receipt set id='wrong' where claim_id=?",
            (claim_id,),
        )
    elif mode == "retained-lease":
        _unsafe_sql(
            path,
            "local_lease",
            "insert into local_lease values(?,?,?,?,?,?,?,?)",
            (str(bootstrap_fixture.IDS[60]), job_id, "owner", 1, "token", 1, TS1, TS2),
        )
    else:
        with sqlite3.connect(path) as db:
            payload = {"job_id": job_id, "state": "failed"}
            db.execute(
                "insert into local_outbox values(?,?,?,?,?,?,?)",
                (
                    str(bootstrap_fixture.IDS[63]),
                    job_id,
                    f"job:{job_id}:terminal-extra",
                    "job.failed",
                    canonical_json(payload),
                    digest(payload),
                    recovery_fixture.UNKNOWN_AT,
                ),
            )
            db.commit()
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match=match):
            verify_selected_b2_graph(
                db,
                binding,
                claim_id,
                trusted_now=dt.datetime.fromisoformat(recovery_fixture.UNKNOWN_AT),
            )


@pytest.mark.parametrize(
    ("mode", "match"),
    (
        ("compile-delivery", "compile delivery preceded"),
        ("quarantine-missing", "quarantine outbox missing"),
        ("premature-state", "premature job-state outbox"),
        ("unexpected-state", "unexpected job-state outbox"),
        ("resolution-evidence", "resolution evidence drift"),
    ),
)
def test_writer_runtime_graph_alternate_terminal_history_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    match: str,
) -> None:
    path = tmp_path / mode / "runtime.db"
    path.parent.mkdir()
    seed = close_fixture._seed(path)
    writer = close_fixture._v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(close_fixture._request(seed))
    if mode in {"compile-delivery", "unexpected-state"}:
        close_fixture._materialize_and_complete(path, frozen, monkeypatch)
    elif mode == "resolution-evidence":
        close_fixture._materialize_runtime_variant(
            path,
            frozen,
            monkeypatch,
            mode="direct-reconciled",
            resolved_unknown_delivery=False,
        )
    if mode == "compile-delivery":
        _unsafe_sql(
            path,
            "local_job",
            "update local_job set state='failed' where id=?",
            (frozen.job_id,),
        )
    elif mode == "quarantine-missing":
        _unsafe_sql(
            path,
            "local_job",
            "update local_job set state='quarantined',attempt_count=1,fencing_counter=1,"
            "terminal_evidence_digest=? where id=?",
            (D, frozen.job_id),
        )
    elif mode == "premature-state":
        with sqlite3.connect(path) as db:
            payload = {"job_id": frozen.job_id, "state": "failed"}
            db.execute(
                "insert into local_outbox values(?,?,?,?,?,?,?)",
                (
                    str(bootstrap_fixture.IDS[61]),
                    frozen.job_id,
                    f"job:{frozen.job_id}:terminal",
                    "job.failed",
                    canonical_json(payload),
                    digest(payload),
                    close_fixture.NOW,
                ),
            )
            db.commit()
    elif mode == "unexpected-state":
        with sqlite3.connect(path) as db:
            extra_payload = {"extra": True}
            db.execute(
                "insert into local_outbox values(?,?,?,?,?,?,?)",
                (
                    str(bootstrap_fixture.IDS[62]),
                    frozen.job_id,
                    "unexpected-state-row",
                    "job.extra",
                    canonical_json(extra_payload),
                    digest(extra_payload),
                    close_fixture.NOW,
                ),
            )
            db.commit()
    else:
        _unsafe_sql(
            path,
            "local_recovery_resolution",
            "update local_recovery_resolution set evidence_digest=?",
            (D,),
        )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match=match):
            writer._runtime_graph(db, close_fixture._binding(), frozen, require_completed=False)


@pytest.mark.parametrize("mode", ("schema", "spool", "projection"))
def test_writer_finalize_front_door_schema_and_snapshot_type_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    path = tmp_path / mode / "runtime.db"
    path.parent.mkdir()
    seed = close_fixture._seed(path)
    writer = close_fixture._v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(close_fixture._request(seed))
    with sqlite3.connect(path) as db:
        frozen_revision = str(
            db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "where state='frozen'"
            ).fetchone()[0]
        )
    request = FinalizeClosedWriteRequest(
        close_fixture._binding(),
        frozen.request_digest,
        frozen_revision,
        "finalize-front-door",
        close_fixture.NOW,
    )
    if mode == "schema":
        monkeypatch.setattr(
            writer_module.operational_schema,
            "status",
            lambda _path: SimpleNamespace(schema_version=3, schema_ok=False, integrity_ok=True),
        )
        with pytest.raises(ConfigurationError, match="explicit schema"):
            writer.finalize_with_session_closed(request)
        return

    with writer._frozen_spool(request.binding) as original:
        spool_snapshot = original.snapshot

    @contextlib.contextmanager
    def spool_handle() -> Any:
        yield SimpleNamespace(
            snapshot=object() if mode == "spool" else spool_snapshot,
            recheck=lambda: None,
        )

    monkeypatch.setattr(writer, "_frozen_spool", lambda _binding: spool_handle())
    if mode == "spool":
        with pytest.raises(ValidationFailed, match="exact spool snapshot"):
            writer.finalize_with_session_closed(request)
        return

    @contextlib.contextmanager
    def projection_handle() -> Any:
        yield SimpleNamespace(snapshot=object(), recheck=lambda: None)

    monkeypatch.setattr(writer, "_frozen_projections", lambda _frozen: projection_handle())
    with pytest.raises(ValidationFailed, match="exact projection snapshot"):
        writer.finalize_with_session_closed(request)


@pytest.mark.parametrize(
    ("mode", "match"),
    (
        ("duplicate-claims", "claim/lease cardinality"),
        ("unknown-without-case", "receipt/recovery state drift"),
        ("quarantine-generation", "quarantine generation drift"),
        ("quarantine-key", "quarantine outbox key drift"),
        ("terminal-kind", "terminal outbox kind drift"),
    ),
)
def test_writer_runtime_graph_final_exact_missing_terminal_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    match: str,
) -> None:
    path = tmp_path / mode / "runtime.db"
    path.parent.mkdir()
    seed = close_fixture._seed(path)
    writer = close_fixture._v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(close_fixture._request(seed))
    if mode in {"unknown-without-case", "terminal-kind"}:
        close_fixture._materialize_and_complete(path, frozen, monkeypatch)
    if mode == "duplicate-claims":
        with sqlite3.connect(path) as db:
            for index in (1, 2):
                db.execute(
                    "insert into local_effect_claim values(?,?,?,?,?,?,?,?)",
                    (
                        str(bootstrap_fixture.IDS[63 + index]),
                        frozen.job_id,
                        str(bootstrap_fixture.IDS[66 + index]),
                        1,
                        "continuity.compile",
                        frozen.compile_evidence(close_fixture._binding()),
                        f"duplicate-claim-{index}",
                        close_fixture.NOW,
                    ),
                )
            db.commit()
    elif mode == "unknown-without-case":
        _unsafe_sql(
            path,
            "local_effect_receipt",
            "update local_effect_receipt set status='unknown'",
            (),
        )
    elif mode == "quarantine-generation":
        _unsafe_sql(
            path,
            "local_job",
            "update local_job set state='quarantined',terminal_evidence_digest=? where id=?",
            (D, frozen.job_id),
        )
    elif mode == "quarantine-key":
        _unsafe_sql(
            path,
            "local_job",
            "update local_job set state='quarantined',attempt_count=1,fencing_counter=1,"
            "terminal_evidence_digest=? where id=?",
            (D, frozen.job_id),
        )
        with sqlite3.connect(path) as db:
            payload = {"job_id": frozen.job_id, "state": "quarantined"}
            db.execute(
                "insert into local_outbox values(?,?,?,?,?,?,?)",
                (
                    str(bootstrap_fixture.IDS[69]),
                    frozen.job_id,
                    "wrong-quarantine-key",
                    "job.quarantined",
                    canonical_json(payload),
                    digest(payload),
                    close_fixture.NOW,
                ),
            )
            db.commit()
    else:
        _unsafe_sql(
            path,
            "local_outbox",
            "update local_outbox set idempotency_key=? "
            "where event_kind='job.completed' and job_id=?",
            (f"job:{frozen.job_id}:recovery:1:failed", frozen.job_id),
        )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match=match):
            writer._runtime_graph(db, close_fixture._binding(), frozen, require_completed=False)
