from __future__ import annotations

import datetime as dt
import json
import multiprocessing
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from zekam.application.client_lifecycle_bridge import (
    LifecycleBridgePlan,
    LifecycleClientContract,
)
from zekam.application.client_lifecycle_spool import (
    CanonicalLifecycleReceipt,
    ClientLifecycleSpool,
    LifecycleSpoolEntry,
    canonical_lifecycle_event,
    replay_pending,
)
from zekam.application.evolution_capture import (
    MAX_REPLAY_EVENTS,
    EvolutionCaptureEnvelope,
    EvolutionCaptureReceipt,
    inspect_capture_replay,
    persist_capture_gap,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.session_continuity import DataClassification, SessionLifecycleEvent
from zekam.infrastructure.clients.codex_lifecycle import (
    CODEX_EVENT_MAPPING,
    CODEX_REVIEWED_VERSION,
    codex_lifecycle_descriptor,
    load_codex_contract_evidence,
    parse_codex_hook_input,
)

NOW = dt.datetime(2026, 9, 6, 12, tzinfo=dt.UTC)
SESSION_ID = "018f0000-0000-7000-8000-0000000000aa"


def _contract() -> LifecycleClientContract:
    evidence = load_codex_contract_evidence(
        Path(__file__).parents[2] / "config/client-lifecycle/codex-0.153.1.json"
    )
    return LifecycleClientContract.verified(
        descriptor=codex_lifecycle_descriptor("codex", installed_version=CODEX_REVIEWED_VERSION),
        installed_version=CODEX_REVIEWED_VERSION,
        event_mapping=CODEX_EVENT_MAPPING,
        contract_evidence_digest=str(evidence["file_digest"]),
    )


def _spooled(
    tmp_path: Path, *, count: int = 1, precompact: bool = False
) -> tuple[ClientLifecycleSpool, LifecycleSpoolEntry]:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    entry: LifecycleSpoolEntry | None = None
    for index in range(count):
        payload = (
            {
                "session_id": SESSION_ID,
                "hook_event_name": "PreCompact",
                "turn_id": f"018f0000-0000-7000-8000-{index + 100:012d}",
                "trigger": "manual",
            }
            if precompact
            else {
                "session_id": SESSION_ID,
                "hook_event_name": "SessionStart",
                "source": "startup",
                "permission_mode": "default",
            }
        )
        observation = parse_codex_hook_input(json.dumps(payload)).observation_body()
        entry = spool.stage(observation, delivery_id=digest({"event": index}), occurred_at=NOW)
    assert entry is not None
    return spool, entry


def _receipt(
    spool: ClientLifecycleSpool, entry: LifecycleSpoolEntry
) -> CanonicalLifecycleReceipt:
    previous = None
    if entry.sequence > 1:
        previous = digest("canonical-predecessor")
    event = canonical_lifecycle_event(
        entry,
        client_instance_id=spool.client_instance_id(),
        previous_canonical_event_digest=previous,
    )
    from types import SimpleNamespace

    ack = SimpleNamespace(
        event_id=UUID("018f0000-0000-7000-8000-000000000099"),
        local_event_digest=event["event_digest"],
        canonical_digest=digest("ack"),
        compaction_outbox_id=(
            UUID("018f0000-0000-7000-8000-000000000098")
            if entry.internal_event_type == "pre_compaction"
            else None
        ),
        compaction_payload_digest=(
            digest("compaction-payload")
            if entry.internal_event_type == "pre_compaction"
            else None
        ),
    )
    generic = CanonicalLifecycleReceipt.verified(entry, event, ack, ack)
    ids = [str(UUID(f"018f0000-0000-7000-8000-{index:012d}")) for index in range(10, 20)]
    body = {
        "schema": "zekam-client-lifecycle-continuity-binding/v1",
        "entry_digest": entry.entry_digest,
        "canonical_event_digest": generic.canonical_event_digest,
        "realm_id": ids[0], "project_id": ids[1], "work_item_id": ids[2],
        "run_id": ids[3], "authorization_id": ids[4], "job_id": ids[5],
        "claim_id": ids[6], "plan_digest": digest("plan"),
        "effect_digest": digest("effect"), "effect_receipt_id": ids[7],
        "effect_receipt_digest": digest("effect-receipt"),
        "continuity_event_id": ids[8], "continuity_event_digest": digest("continuity"),
        "delivery_outbox_id": ids[9], "terminal_receipt_digest": digest("terminal"),
        "event_type": entry.internal_event_type, "session_id": entry.session_id,
        "client_id": entry.client_id,
        "compiler_enqueue": entry.internal_event_type == "pre_compaction",
        "status": "completed", "grants_authority": False,
    }
    return generic.bind_continuity(entry, body | {"binding_digest": digest(body)})


def _plan(contract: LifecycleClientContract, entry: LifecycleSpoolEntry) -> LifecycleBridgePlan:
    event = SessionLifecycleEvent(
        UUID("018f0000-0000-7000-8000-000000000001"),
        UUID("018f0000-0000-7000-8000-000000000002"),
        UUID("018f0000-0000-7000-8000-000000000003"),
        UUID("018f0000-0000-7000-8000-000000000004"),
        entry.session_id,
        "codex",
        UUID("018f0000-0000-7000-8000-000000000005"),
        entry.internal_event_type,
        entry.sequence,
        entry.previous_entry_digest,
        "client:codex",
        "delivery:one",
        "job:one",
        0,
        "git:" + "a" * 40,
        "work-plan:one",
        None,
        None,
        entry.observation_digest,
        (),
        DataClassification.INTERNAL,
        NOW,
        NOW + dt.timedelta(seconds=1),
    )
    return LifecycleBridgePlan(
        event,
        {"lifecycle": event.body(), "data": {"grants_authority": False}},
        contract.contract_digest,
        1,
        digest("hook-set"),
        ("capture-hook",),
        entry.delivery_id,
        "realm:one",
        digest("source"),
        digest("policy"),
        digest("migration"),
        digest("effect"),
        digest("plan"),
    )


def _hard_kill_after_durable_commit(home: str, database: str) -> None:
    spool = ClientLifecycleSpool(Path(home), client_id="codex")
    entry = spool.pending(limit=1)[0]
    with sqlite3.connect(database) as connection:
        connection.execute("create table if not exists terminal(entry_digest text primary key)")
        connection.execute("insert into terminal(entry_digest) values (?)", (entry.entry_digest,))
        connection.commit()
    receipt = _receipt(spool, entry)

    def kill_before_ack(
        _entry: LifecycleSpoolEntry, _receipt: CanonicalLifecycleReceipt
    ) -> dict[str, object]:
        os._exit(91)

    replay_pending(
        spool,
        deliver=lambda _entry: receipt,
        capture=kill_before_ack,
        attempted_at=NOW,
    )


def test_capture_projection_maps_existing_canonical_identities_without_raw_content(
    tmp_path: Path,
) -> None:
    contract = _contract()
    spool, entry = _spooled(tmp_path)
    receipt = _receipt(spool, entry)
    capture = EvolutionCaptureEnvelope.from_terminal(
        _plan(contract, entry),
        contract,
        spool=spool,
        entry=entry,
        receipt=receipt,
    )
    envelope = capture.as_dict()

    assert envelope["client_version"] == "0.153.1"
    assert envelope["event_type"] == "session_start"
    assert envelope["privacy_class"] == "internal"
    assert envelope["sequence_or_cursor"] == 1
    assert envelope["contains_prompt"] is False
    assert envelope["contains_response"] is False
    assert envelope["contains_transcript"] is False
    rendered = json.dumps(envelope)
    assert "transcript_path" not in rendered
    assert "raw-secret-prompt-value" not in rendered
    verified = EvolutionCaptureReceipt.verified(capture, entry, receipt).as_dict()
    assert verified["status"] == "completed"


def test_capture_projection_rejects_contract_plan_drift(tmp_path: Path) -> None:
    contract = _contract()
    spool, entry = _spooled(tmp_path)
    plan = _plan(contract, entry)
    drifted = LifecycleBridgePlan(
        plan.event,
        plan.hook_payload,
        digest("wrong-contract"),
        plan.hook_generation,
        plan.hook_set_digest,
        plan.hook_ids,
        plan.idempotency_key,
        plan.resource,
        plan.source_digest,
        plan.policy_digest,
        plan.migration_digest,
        plan.effect_digest,
        plan.plan_digest,
    )
    with pytest.raises(PolicyViolation, match="contract/plan"):
        EvolutionCaptureEnvelope.from_terminal(
            drifted, contract, spool=spool, entry=entry, receipt=_receipt(spool, entry)
        )


def test_capture_projection_rejects_path_like_or_secret_assignment_refs(
    tmp_path: Path,
) -> None:
    contract = _contract()
    spool, entry = _spooled(tmp_path)
    plan = _plan(contract, entry)
    hostile = replace(plan, event=replace(plan.event, source_revision="SuperSecret123456"))
    with pytest.raises(PolicyViolation, match="binding drift"):
        EvolutionCaptureEnvelope.from_terminal(
            hostile, contract, spool=spool, entry=entry, receipt=_receipt(spool, entry)
        )


def test_capture_projection_body_is_immutable_and_digest_bound(tmp_path: Path) -> None:
    contract = _contract()
    spool, entry = _spooled(tmp_path)
    envelope = EvolutionCaptureEnvelope.from_terminal(
        _plan(contract, entry), contract, spool=spool, entry=entry, receipt=_receipt(spool, entry)
    )
    with pytest.raises(TypeError):
        envelope.body["event_type"] = "post_close"  # type: ignore[index]
    with pytest.raises(PolicyViolation, match="digest mismatch"):
        EvolutionCaptureEnvelope({"schema": "drift"}, digest("other"))


def test_capture_receipt_is_persisted_inside_canonical_spool_ack(tmp_path: Path) -> None:
    contract = _contract()
    spool, entry = _spooled(tmp_path)
    receipt = _receipt(spool, entry)
    plan = _plan(contract, entry)

    result = replay_pending(
        spool,
        deliver=lambda _entry: receipt,
        capture=lambda captured, terminal: EvolutionCaptureReceipt.verified(
            EvolutionCaptureEnvelope.from_terminal(
                plan,
                contract,
                spool=spool,
                entry=captured,
                receipt=terminal,
            ),
            captured,
            terminal,
        ).as_dict(),
        attempted_at=NOW,
    )

    assert result[0].outcome == "completed"
    ack = json.loads(spool._ack_path(entry.entry_digest).read_text(encoding="utf-8"))
    assert ack["evolution_capture"]["status"] == "completed"
    assert ack["evolution_capture"]["capture_digest"]

    assert replay_pending(
        spool,
        deliver=lambda _entry: receipt,
        capture=lambda _entry, _receipt: pytest.fail("duplicate capture ran"),
        attempted_at=NOW,
    ) == ()


def test_precompaction_capture_is_bound_to_runtime_receipt(tmp_path: Path) -> None:
    contract = _contract()
    spool, entry = _spooled(tmp_path, precompact=True)
    receipt = _receipt(spool, entry)
    capture = EvolutionCaptureEnvelope.from_terminal(
        _plan(contract, entry), contract, spool=spool, entry=entry, receipt=receipt
    )
    assert capture.body["event_type"] == "pre_compaction"
    assert EvolutionCaptureReceipt.verified(capture, entry, receipt).as_dict()[
        "status"
    ] == "completed"


def test_post_commit_capture_failure_replays_without_blind_effect_retry(tmp_path: Path) -> None:
    contract = _contract()
    spool, entry = _spooled(tmp_path)
    receipt = _receipt(spool, entry)
    plan = _plan(contract, entry)
    first = replay_pending(
        spool,
        deliver=lambda _entry: receipt,
        capture=lambda _entry, _receipt: (_ for _ in ()).throw(OSError("process-kill")),
        attempted_at=NOW,
    )
    assert first[0].outcome == "failed"
    assert not spool._ack_path(entry.entry_digest).exists()

    second = replay_pending(
        spool,
        deliver=lambda _entry: receipt,
        capture=lambda captured, terminal: EvolutionCaptureReceipt.verified(
            EvolutionCaptureEnvelope.from_terminal(
                plan,
                contract,
                spool=spool,
                entry=captured,
                receipt=terminal,
            ),
            captured,
            terminal,
        ).as_dict(),
        attempted_at=NOW + dt.timedelta(seconds=1),
    )
    assert second[0].outcome == "completed"


def test_real_child_hard_exit_after_commit_recovers_capture_and_ack(tmp_path: Path) -> None:
    contract = _contract()
    spool, entry = _spooled(tmp_path)
    database = tmp_path / "terminal.sqlite3"
    process = multiprocessing.get_context("spawn").Process(
        target=_hard_kill_after_durable_commit,
        args=(str(tmp_path / "home"), str(database)),
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        pytest.fail("hard-kill child zamaninda cikmadi")
    assert process.exitcode == 91
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "select entry_digest from terminal"
        ).fetchone() == (entry.entry_digest,)
    assert not spool._ack_path(entry.entry_digest).exists()

    receipt = _receipt(spool, entry)
    plan = _plan(contract, entry)
    recovered = replay_pending(
        spool,
        deliver=lambda candidate: (
            receipt
            if candidate.entry_digest == entry.entry_digest
            else pytest.fail("unexpected replay entry")
        ),
        capture=lambda captured, terminal: EvolutionCaptureReceipt.verified(
            EvolutionCaptureEnvelope.from_terminal(
                plan,
                contract,
                spool=spool,
                entry=captured,
                receipt=terminal,
            ),
            captured,
            terminal,
        ).as_dict(),
        attempted_at=NOW + dt.timedelta(seconds=2),
    )
    assert recovered[0].outcome == "completed"
    ack = json.loads(spool._ack_path(entry.entry_digest).read_text(encoding="utf-8"))
    assert ack["evolution_capture"]["status"] == "completed"


def test_approved_spool_source_replay_is_bounded_and_idempotent(tmp_path: Path) -> None:
    spool, _ = _spooled(tmp_path, count=MAX_REPLAY_EVENTS + 3)
    first = inspect_capture_replay(
        spool=spool, client_id="codex", session_id=SESSION_ID, last_delivered_sequence=3
    )
    replay = inspect_capture_replay(
        spool=spool, client_id="codex", session_id=SESSION_ID, last_delivered_sequence=3
    )

    assert first == replay
    assert first.state == "replay"
    assert first.limit == MAX_REPLAY_EVENTS
    assert first.through_sequence == 3 + MAX_REPLAY_EVENTS


def test_expired_spool_window_creates_gap_without_inference(tmp_path: Path) -> None:
    spool, _ = _spooled(tmp_path)
    decision = inspect_capture_replay(
        spool=spool, client_id="codex", session_id=SESSION_ID, last_delivered_sequence=7
    ).as_dict()

    assert decision["state"] == "capture-gap"
    assert decision["reason"] == "source-window-expired"
    assert decision["inferred_content"] is False
    assert decision["limit"] == 0
    persisted = persist_capture_gap(
        spool=spool,
        decision=inspect_capture_replay(
            spool=spool,
            client_id="codex",
            session_id=SESSION_ID,
            last_delivered_sequence=7,
        ),
    )
    assert persisted == decision
    assert len(tuple(spool.capture_gaps_directory.glob("*.json"))) == 1


def test_wrong_spool_client_is_not_caller_authorized(tmp_path: Path) -> None:
    spool, _ = _spooled(tmp_path)
    decision = inspect_capture_replay(
        spool=spool, client_id="other", session_id=SESSION_ID, last_delivered_sequence=0
    )
    assert decision.state == "capture-gap"
    assert decision.reason == "source-unavailable"
