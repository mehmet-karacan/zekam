from __future__ import annotations

import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

import zekam.application.client_lifecycle_composition as lifecycle_composition
from zekam.application.client_lifecycle_bridge import LifecycleClientContract
from zekam.application.client_lifecycle_composition import (
    LifecyclePlanInputs,
    drain_claimed_codex_delivery,
)
from zekam.application.client_lifecycle_continuity import (
    LIFECYCLE_ADAPTER_DIGEST,
    LIFECYCLE_EFFECT_OPERATION,
)
from zekam.application.client_lifecycle_spool import (
    MAX_SPOOL_DOCUMENT_BYTES,
    CanonicalLifecycleReceipt,
    ClientLifecycleSpool,
    LifecycleReplayResult,
    LifecycleSpoolEntry,
    _fsync_parent_directory,
    canonical_lifecycle_event,
    drain_to_postgres,
    replay_pending,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.hook_runtime import HookEventType
from zekam.domain.runtime import AttemptOutcome
from zekam.infrastructure.clients.codex_lifecycle import (
    CODEX_EVENT_MAPPING,
    CODEX_REVIEWED_CLIENT_CONTRACT_DIGEST,
    CODEX_REVIEWED_VERSION,
    CODEX_REVIEWED_WINDOWS_SHA256,
    CodexHookEnvelope,
    assert_reviewed_codex_version,
    codex_lifecycle_descriptor,
    load_codex_contract_evidence,
    parse_codex_hook_input,
    parse_codex_version_output,
)
from zekam.interfaces.cli.client import app as client_app

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 8, 28, 9, 0, tzinfo=dt.UTC)
SESSION_ID = "0198f2ad-3d10-7a11-b515-4c5c1733f7b1"
TURN_ID = "0198f2ad-3d10-7a11-b515-4c5c1733f7b2"
OCCURRENCE_ID = "0198f2ad-3d10-7a11-b515-4c5c1733f7b3"
CANONICAL_EVENT_ID = UUID("0198f2ad-3d10-7a11-b515-4c5c1733f7b4")
REALM_ID = UUID("0198f2ad-3d10-7a11-b515-4c5c1733f7b5")
PROJECT_ID = UUID("0198f2ad-3d10-7a11-b515-4c5c1733f7b6")
WORK_ITEM_ID = UUID("0198f2ad-3d10-7a11-b515-4c5c1733f7b7")
RUN_ID = UUID("0198f2ad-3d10-7a11-b515-4c5c1733f7b8")
CONTINUITY_EVENT_ID = UUID("0198f2ad-3d10-7a11-b515-4c5c1733f7b9")
DELIVERY_OUTBOX_ID = UUID("0198f2ad-3d10-7a11-b515-4c5c1733f7ba")
AUTHORIZATION_ID = UUID("0198f2ad-3d10-7a11-b515-4c5c1733f7bb")
JOB_ID = UUID("0198f2ad-3d10-7a11-b515-4c5c1733f7bc")
CLAIM_ID = UUID("0198f2ad-3d10-7a11-b515-4c5c1733f7bd")
EFFECT_RECEIPT_ID = UUID("0198f2ad-3d10-7a11-b515-4c5c1733f7be")
PLAN_DIGEST = digest("codex-governed-drain-plan")
EFFECT_DIGEST = digest("codex-governed-drain-effect")


def test_reviewed_camelcase_mapping_binds_exact_tracked_contract_digest() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    evidence = load_codex_contract_evidence(
        repository_root / "config" / "client-lifecycle" / "codex-0.150.1.json"
    )
    contract = LifecycleClientContract.verified(
        descriptor=codex_lifecycle_descriptor("codex", installed_version=CODEX_REVIEWED_VERSION),
        installed_version=CODEX_REVIEWED_VERSION,
        event_mapping=CODEX_EVENT_MAPPING,
        contract_evidence_digest=str(evidence["file_digest"]),
    )
    assert contract.contract_digest == CODEX_REVIEWED_CLIENT_CONTRACT_DIGEST


def _session_start(**extra: object) -> dict[str, object]:
    return {
        "session_id": SESSION_ID,
        "hook_event_name": "SessionStart",
        "source": "startup",
        "permission_mode": "default",
        **extra,
    }


def _delivery(
    envelope: CodexHookEnvelope,
    occurrence_id: str = OCCURRENCE_ID,
) -> str:
    return envelope.delivery_id(occurrence_id=occurrence_id)


def _canonical_receipt(
    spool: ClientLifecycleSpool,
    entry: LifecycleSpoolEntry,
    *,
    previous_canonical_event_digest: str | None = None,
) -> CanonicalLifecycleReceipt:
    event = canonical_lifecycle_event(
        entry,
        client_instance_id=spool.client_instance_id(),
        previous_canonical_event_digest=previous_canonical_event_digest,
    )
    ack = SimpleNamespace(
        event_id=CANONICAL_EVENT_ID,
        local_event_digest=event["event_digest"],
        canonical_digest=digest({"canonical_receipt": entry.entry_digest}),
    )
    receipt = CanonicalLifecycleReceipt.verified(entry, event, ack, ack)
    return receipt.bind_continuity(entry, _continuity_binding(entry, receipt))


def _continuity_binding(
    entry: LifecycleSpoolEntry,
    receipt: CanonicalLifecycleReceipt,
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": "zekam-client-lifecycle-continuity-binding/v1",
        "entry_digest": entry.entry_digest,
        "canonical_event_digest": receipt.canonical_event_digest,
        "realm_id": str(REALM_ID),
        "project_id": str(PROJECT_ID),
        "work_item_id": str(WORK_ITEM_ID),
        "run_id": str(RUN_ID),
        "authorization_id": str(AUTHORIZATION_ID),
        "job_id": str(JOB_ID),
        "claim_id": str(CLAIM_ID),
        "plan_digest": PLAN_DIGEST,
        "effect_digest": EFFECT_DIGEST,
        "effect_receipt_id": str(EFFECT_RECEIPT_ID),
        "effect_receipt_digest": digest({"claim_id": str(CLAIM_ID), "outcome": "completed"}),
        "continuity_event_id": str(CONTINUITY_EVENT_ID),
        "continuity_event_digest": digest(
            {"entry_digest": entry.entry_digest, "ledger": "continuity"}
        ),
        "delivery_outbox_id": str(DELIVERY_OUTBOX_ID),
        "terminal_receipt_digest": digest(
            {"entry_digest": entry.entry_digest, "status": "completed"}
        ),
        "event_type": entry.internal_event_type,
        "session_id": entry.session_id,
        "client_id": entry.client_id,
        "compiler_enqueue": entry.internal_event_type == "pre_compaction",
        "status": "completed",
        "grants_authority": False,
    }
    return body | {"binding_digest": digest(body)}


def _continuity_preflight(
    entry: LifecycleSpoolEntry,
    canonical_event: dict[str, object],
    *,
    client_instance_id: str,
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": "zekam-client-lifecycle-continuity-preflight/v1",
        "entry_digest": entry.entry_digest,
        "canonical_event_digest": canonical_event["event_digest"],
        "client_instance_id": client_instance_id,
        "realm_id": str(REALM_ID),
        "project_id": str(PROJECT_ID),
        "work_item_id": str(WORK_ITEM_ID),
        "run_id": str(RUN_ID),
        "authorization_id": str(AUTHORIZATION_ID),
        "job_id": str(JOB_ID),
        "claim_id": str(CLAIM_ID),
        "plan_digest": PLAN_DIGEST,
        "effect_digest": EFFECT_DIGEST,
        "allowed": True,
        "mutation_performed": False,
        "grants_authority": False,
    }
    return body | {"preflight_digest": digest(body)}


@pytest.mark.parametrize(
    ("document", "expected"),
    (
        (_session_start(), HookEventType.CONTINUITY_SESSION_START),
        (
            {
                "session_id": SESSION_ID,
                "hook_event_name": "PreCompact",
                "turn_id": TURN_ID,
                "trigger": "manual",
            },
            HookEventType.PRE_COMPACTION,
        ),
        (
            {
                "session_id": SESSION_ID,
                "hook_event_name": "PostCompact",
                "turn_id": TURN_ID,
                "trigger": "manual",
            },
            HookEventType.POST_COMPACTION,
        ),
        (
            {
                "session_id": SESSION_ID,
                "hook_event_name": "Stop",
                "turn_id": TURN_ID,
                "stop_hook_active": False,
                "permission_mode": "default",
            },
            HookEventType.PRE_CLOSE,
        ),
        (
            {
                "session_id": SESSION_ID,
                "hook_event_name": "SessionEnd",
                "reason": "other",
            },
            HookEventType.POST_CLOSE,
        ),
    ),
)
def test_exact_codex_events_map_to_canonical_continuity(
    document: dict[str, object], expected: HookEventType
) -> None:
    envelope = parse_codex_hook_input(json.dumps(document))

    assert envelope.internal_event_type is expected
    observation = envelope.observation_body()
    assert observation["external_event_type"] == document["hook_event_name"]
    assert observation["internal_event_type"] == expected.value
    assert observation["grants_authority"] is False


@pytest.mark.parametrize("reason", ("clear", "logout", "prompt_input_exit", "other"))
def test_official_session_end_reasons_are_exactly_allowlisted(reason: str) -> None:
    envelope = parse_codex_hook_input(
        json.dumps(
            {
                "session_id": SESSION_ID,
                "hook_event_name": "SessionEnd",
                "reason": reason,
            }
        )
    )
    assert envelope.reason == reason


def test_session_turn_and_occurrence_ids_require_lowercase_uuid_shape() -> None:
    with pytest.raises(ValidationFailed, match=r"session_id.*UUID"):
        parse_codex_hook_input(json.dumps(_session_start(session_id="session-123")))
    with pytest.raises(ValidationFailed, match=r"turn_id.*UUID"):
        parse_codex_hook_input(
            json.dumps(
                {
                    "session_id": SESSION_ID,
                    "hook_event_name": "Stop",
                    "turn_id": "turn-1",
                    "permission_mode": "default",
                }
            )
        )
    envelope = parse_codex_hook_input(json.dumps(_session_start()))
    with pytest.raises(ValidationFailed, match=r"occurrence_id.*UUID"):
        envelope.delivery_id(occurrence_id="occurrence-1")


def test_content_fields_are_neither_retained_nor_hashed() -> None:
    first = parse_codex_hook_input(
        json.dumps(
            _session_start(
                cwd="C:/private/first",
                transcript_path="C:/private/transcript-one.jsonl",
                model="private-model-one",
                prompt="DO-NOT-PERSIST-FIRST",
                response="DO-NOT-PERSIST-FIRST-RESPONSE",
                last_assistant_message="DO-NOT-PERSIST-FIRST-ANSWER",
            )
        )
    )
    second = parse_codex_hook_input(
        json.dumps(
            _session_start(
                cwd="D:/different/path",
                transcript_path="D:/different/transcript-two.jsonl",
                model="different-model",
                prompt="DO-NOT-PERSIST-SECOND",
                response="DO-NOT-PERSIST-SECOND-RESPONSE",
                last_assistant_message="DO-NOT-PERSIST-SECOND-ANSWER",
            )
        )
    )

    assert first.wire_digest == second.wire_digest
    assert _delivery(first) == _delivery(second)
    assert _delivery(first, "0198f2ad-3d10-7a11-b515-4c5c1733f7b5") != _delivery(first)
    rendered = json.dumps(first.observation_body())
    assert "DO-NOT-PERSIST" not in rendered
    assert "private/first" not in rendered
    assert "transcript_path" not in rendered


def test_unknown_or_drifted_codex_contract_fails_closed() -> None:
    with pytest.raises(PolicyViolation, match="reviewed contract disinda"):
        parse_codex_hook_input(
            json.dumps(
                {
                    "session_id": SESSION_ID,
                    "hook_event_name": "BeforeRelease",
                }
            )
        )
    with pytest.raises(ValidationFailed, match="permission_mode reviewed enum disinda"):
        parse_codex_hook_input(json.dumps(_session_start(permission_mode="custom-mode")))
    with pytest.raises(ValidationFailed, match="trigger reviewed enum disinda"):
        parse_codex_hook_input(
            json.dumps(
                {
                    "session_id": SESSION_ID,
                    "hook_event_name": "PreCompact",
                    "turn_id": TURN_ID,
                    "trigger": "background",
                }
            )
        )
    with pytest.raises(PolicyViolation, match="version drift"):
        assert_reviewed_codex_version("0.150.2")
    with pytest.raises(ValidationFailed, match="version output"):
        parse_codex_version_output("codex 0.150.1 extra")


def test_exact_version_descriptor_and_tracked_evidence() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    evidence = load_codex_contract_evidence(
        repository_root / "config" / "client-lifecycle" / "codex-0.150.1.json"
    )
    descriptor = codex_lifecycle_descriptor(
        "codex",
        installed_version=parse_codex_version_output("codex-cli 0.150.1"),
    )

    assert descriptor.version == CODEX_REVIEWED_VERSION
    assert descriptor.supports("lifecycle-events-v2")
    assert evidence["reviewed_executable"]["sha256"] == CODEX_REVIEWED_WINDOWS_SHA256
    assert evidence["event_mapping"] == [
        {"external": raw, "internal": canonical.value} for raw, canonical in CODEX_EVENT_MAPPING
    ]
    assert str(evidence["file_digest"]).startswith("sha256:")


def _claimed_lifecycle_boundary() -> tuple[SimpleNamespace, SimpleNamespace]:
    job_id, attempt_id = uuid4(), uuid4()
    work = SimpleNamespace(
        job=SimpleNamespace(
            id=job_id,
            project_id=uuid4(),
            work_item_id=uuid4(),
            plan_id=uuid4(),
            run_id=uuid4(),
        ),
        attempt_id=attempt_id,
        lease=SimpleNamespace(
            id=uuid4(),
            owner_digest=digest("owner"),
            worker_label="codex-lifecycle-worker",
            fencing_token=7,
        ),
    )
    claim = SimpleNamespace(
        id=uuid4(),
        job_id=job_id,
        attempt_id=attempt_id,
        fencing_token=7,
        operation=LIFECYCLE_EFFECT_OPERATION,
        adapter_digest=LIFECYCLE_ADAPTER_DIGEST,
        effect_digest=digest("effect"),
    )
    return work, claim


def test_claimed_composition_accepts_only_canonical_completed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, claim = _claimed_lifecycle_boundary()
    entry = SimpleNamespace(
        entry_digest=digest("entry"),
        delivery_id=digest("delivery"),
        client_id="codex",
        session_id=str(uuid4()),
        external_event_type="SessionStart",
        internal_event_type="session_start",
        sequence=1,
        observation={},
        occurred_at=NOW,
    )
    spool = SimpleNamespace(
        pending=lambda *, limit: (entry,), client_instance_id=lambda: "codex-instance"
    )
    repository = SimpleNamespace(
        realm_id=uuid4(),
        connection=object(),
        previous_continuity_digest=lambda **_: None,
        current_work_plan_digest=lambda **_: digest("work-plan"),
    )
    expected = LifecycleReplayResult(
        entry.entry_digest,
        "completed",
        digest("ack"),
        digest("attempt"),
    )
    monkeypatch.setattr(
        lifecycle_composition, "drain_to_postgres", lambda *args, **kwargs: (expected,)
    )
    result = drain_claimed_codex_delivery(
        spool=spool,
        bridge=SimpleNamespace(
            repository=object(),
            authorizations=object(),
            prepare=lambda *args, **kwargs: SimpleNamespace(effect_digest=claim.effect_digest),
        ),
        repository=repository,
        work=work,
        claim=claim,
        authorization_id=uuid4(),
        contract=object(),
        hook_session=object(),
        session_binding_id=uuid4(),
        inputs=LifecyclePlanInputs(
            "git:source",
            digest("source"),
            digest("policy"),
            digest("migration"),
            digest("work-plan"),
            None,
            None,
        ),
    )
    assert result == (expected,)


def test_receiptless_claim_without_pending_entry_enters_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, claim = _claimed_lifecycle_boundary()
    outcomes: list[AttemptOutcome] = []
    state_calls: list[dict[str, object]] = []

    class Host:
        ledger = SimpleNamespace(receipt_for_claim=lambda claim_id: None)

        def __init__(self, *args, **kwargs) -> None:
            pass

        @staticmethod
        def finish(claimed, *, outcome, result_digest):  # type: ignore[no-untyped-def]
            outcomes.append(outcome)
            return True

    monkeypatch.setattr(lifecycle_composition, "ExecutionHost", Host)
    with pytest.raises(PolicyViolation, match="Receiptless claimed Codex delivery"):
        drain_claimed_codex_delivery(
            spool=SimpleNamespace(pending=lambda *, limit: ()),
            bridge=object(),
            repository=SimpleNamespace(
                realm_id=uuid4(),
                connection=object(),
                recovery_finish_state=lambda **values: (
                    state_calls.append(values) or "running-exact"
                ),
            ),
            work=work,
            claim=claim,
            authorization_id=uuid4(),
            contract=object(),
            hook_session=object(),
            session_binding_id=uuid4(),
            inputs=LifecyclePlanInputs(
                "git:source",
                digest("source"),
                digest("policy"),
                digest("migration"),
                digest("work-plan"),
                None,
                None,
            ),
        )
    assert outcomes == [AttemptOutcome.RECOVERY_REQUIRED]
    assert state_calls == [
        {
            "job_id": work.job.id,
            "attempt_id": work.attempt_id,
            "lease_id": work.lease.id,
            "owner_digest": work.lease.owner_digest,
            "fencing_token": work.lease.fencing_token,
        }
    ]


@pytest.mark.parametrize(
    ("recovery_state", "expected_message", "expected_outcomes"),
    (
        (
            "recovery-required-closed",
            "Codex drain exact delivery terminal ACK uretmedi",
            (),
        ),
        (
            "running-exact",
            "Codex drain exact delivery terminal ACK uretmedi",
            (AttemptOutcome.RECOVERY_REQUIRED,),
        ),
        ("attempt-drift", "Codex lifecycle recovery finish state drift", ()),
    ),
)
def test_outer_drain_reconciles_only_exact_receiptless_recovery_state(
    monkeypatch: pytest.MonkeyPatch,
    recovery_state: str,
    expected_message: str,
    expected_outcomes: tuple[AttemptOutcome, ...],
) -> None:
    work, claim = _claimed_lifecycle_boundary()
    outcomes: list[AttemptOutcome] = []
    state_calls: list[dict[str, object]] = []
    entry = SimpleNamespace(
        entry_digest=digest("incomplete-entry"),
        delivery_id=digest("incomplete-delivery"),
        client_id="codex",
        session_id=str(uuid4()),
        external_event_type="SessionStart",
        internal_event_type="session_start",
        sequence=1,
        observation={},
        occurred_at=NOW,
    )
    spool = SimpleNamespace(
        pending=lambda *, limit: (entry,),
        client_instance_id=lambda: "codex-instance",
    )
    repository = SimpleNamespace(
        realm_id=uuid4(),
        connection=object(),
        previous_continuity_digest=lambda **_: None,
        current_work_plan_digest=lambda **_: digest("work-plan"),
        recovery_finish_state=lambda **values: state_calls.append(values) or recovery_state,
    )
    bridge = SimpleNamespace(
        repository=object(),
        authorizations=object(),
        prepare=lambda *args, **kwargs: SimpleNamespace(effect_digest=claim.effect_digest),
    )

    class Host:
        ledger = SimpleNamespace(receipt_for_claim=lambda claim_id: None)

        def __init__(self, *args, **kwargs) -> None:
            pass

        @staticmethod
        def finish(claimed, *, outcome, result_digest):  # type: ignore[no-untyped-def]
            outcomes.append(outcome)
            return True

    monkeypatch.setattr(lifecycle_composition, "ExecutionHost", Host)
    monkeypatch.setattr(
        lifecycle_composition,
        "drain_to_postgres",
        lambda *args, **kwargs: (
            LifecycleReplayResult(
                entry.entry_digest,
                "failed",
                None,
                digest("failed-attempt"),
            ),
        ),
    )

    with pytest.raises(PolicyViolation, match=expected_message):
        drain_claimed_codex_delivery(
            spool=spool,
            bridge=bridge,
            repository=repository,
            work=work,
            claim=claim,
            authorization_id=uuid4(),
            contract=object(),
            hook_session=object(),
            session_binding_id=uuid4(),
            inputs=LifecyclePlanInputs(
                "git:source",
                digest("source"),
                digest("policy"),
                digest("migration"),
                digest("work-plan"),
                None,
                None,
            ),
        )

    assert tuple(outcomes) == expected_outcomes
    assert state_calls == [
        {
            "job_id": work.job.id,
            "attempt_id": work.attempt_id,
            "lease_id": work.lease.id,
            "owner_digest": work.lease.owner_digest,
            "fencing_token": work.lease.fencing_token,
        }
    ]


@pytest.mark.parametrize(
    ("field", "drifted"),
    (
        ("per_session_hash_chain", False),
        ("idempotent_delivery_replay", False),
        ("bounded_pending_batch", 255),
        ("predecessor_manual_review_cascade", False),
        ("caller_controlled_mutation_skip", True),
        ("drain_cursor_semantics", "ack-prefix-v1"),
        ("continuity_adapter_composed", False),
        ("generic_repository_direct_drain", True),
    ),
)
def test_tracked_contract_rejects_durability_semantic_drift(
    tmp_path: Path,
    field: str,
    drifted: object,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    source = repository_root / "config" / "client-lifecycle" / "codex-0.150.1.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    document["durability"][field] = drifted
    candidate = tmp_path / "codex-contract.json"
    candidate.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PolicyViolation, match="semantic drift"):
        load_codex_contract_evidence(candidate)


@pytest.mark.parametrize(
    ("section", "field", "drifted"),
    (
        ("hook_command", "external_provider_required", True),
        ("hook_command", "command", "codex unsafe-hook"),
        ("offline_e2e", "real_binary", False),
        ("offline_e2e", "saved_auth_loaded", True),
        ("offline_e2e", "hook_trust_bypass_for_test_only", False),
        ("reviewed_executable", "path_recorded", True),
    ),
)
def test_tracked_contract_rejects_critical_remote_and_hook_drift(
    tmp_path: Path,
    section: str,
    field: str,
    drifted: object,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    source = repository_root / "config" / "client-lifecycle" / "codex-0.150.1.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    document[section][field] = drifted
    candidate = tmp_path / "codex-critical-contract.json"
    candidate.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PolicyViolation, match="semantic drift"):
        load_codex_contract_evidence(candidate)


def test_spool_is_immutable_idempotent_and_ack_driven(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    start = parse_codex_hook_input(
        json.dumps(
            _session_start(
                transcript_path="C:/private/never-persist-this.jsonl",
                prompt="NEVER-PERSIST-THIS-PROMPT",
            )
        )
    )
    observation = start.observation_body()
    entry = spool.stage(observation, delivery_id=_delivery(start), occurred_at=NOW)
    replay = spool.stage(
        observation,
        delivery_id=_delivery(start),
        occurred_at=NOW + dt.timedelta(minutes=1),
    )

    assert replay == entry
    assert spool.pending() == (entry,)
    assert spool.status()["page_pending_count"] == 1
    source_text = next(spool.events_directory.glob("*.json")).read_text(encoding="utf-8")
    assert "never-persist-this" not in source_text.lower()
    assert "C:/private" not in source_text

    receipt = _canonical_receipt(spool, entry)
    spool.record_attempt(
        entry.entry_digest,
        outcome="completed",
        evidence_digest=receipt.canonical_lookup_digest,
        attempted_at=NOW + dt.timedelta(minutes=2),
    )
    first_ack = spool._acknowledge_verified_receipt(
        entry,
        receipt=receipt,
        acknowledged_at=NOW + dt.timedelta(minutes=2),
    )
    replay_ack = spool._acknowledge_verified_receipt(
        entry,
        receipt=receipt,
        acknowledged_at=NOW + dt.timedelta(minutes=3),
    )
    assert replay_ack == first_ack
    assert spool.pending() == ()
    assert spool.status()["acked_count"] == 1
    cursor_records = list(spool.drain_cursors_directory.glob("*.json"))
    assert len(cursor_records) == 1
    cursor = json.loads(cursor_records[0].read_text(encoding="utf-8"))
    assert cursor["entry_digest"] == entry.entry_digest
    assert cursor["ack_digest"] == first_ack["ack_digest"]
    assert cursor["continuity_binding_digest"] == first_ack["continuity_binding"]["binding_digest"]

    attempt = spool.record_attempt(
        entry.entry_digest,
        outcome="completed",
        evidence_digest=receipt.canonical_lookup_digest,
        attempted_at=NOW + dt.timedelta(minutes=4),
    )
    assert attempt["grants_authority"] is False
    assert len(list(spool.attempts_directory.glob("*.json"))) == 1
    assert spool.status()["page_attempt_count"] == 1


def test_delivery_replay_with_different_observation_is_rejected(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    start = parse_codex_hook_input(json.dumps(_session_start()))
    spool.stage(start.observation_body(), delivery_id=_delivery(start), occurred_at=NOW)
    drifted = parse_codex_hook_input(
        json.dumps(_session_start(permission_mode="plan"))
    ).observation_body()

    with pytest.raises(PolicyViolation, match="replay payload drift"):
        spool.stage(drifted, delivery_id=_delivery(start), occurred_at=NOW)


def test_cursor_refuses_delivery_ack_continuity_parity_drift(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    envelope = parse_codex_hook_input(json.dumps(_session_start()))
    entry = spool.stage(
        envelope.observation_body(), delivery_id=_delivery(envelope), occurred_at=NOW
    )
    receipt = _canonical_receipt(spool, entry)
    spool.record_attempt(
        entry.entry_digest,
        outcome="completed",
        evidence_digest=receipt.canonical_lookup_digest,
        attempted_at=NOW + dt.timedelta(seconds=1),
    )
    spool._acknowledge_verified_receipt(
        entry,
        receipt=receipt,
        acknowledged_at=NOW + dt.timedelta(seconds=1),
    )
    delivery_path = next(spool.deliveries_directory.glob("*.json"))
    delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    delivery["ref_digest"] = digest("tampered-delivery")
    delivery_path.write_text(json.dumps(delivery), encoding="utf-8")

    with pytest.raises(PolicyViolation, match="delivery ref digest mismatch"):
        spool.pending()


def test_hook_stage_uses_bounded_checkpoint_not_history_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")

    def forbidden_history_scan(_: ClientLifecycleSpool) -> list[LifecycleSpoolEntry]:
        raise AssertionError("hook fast path scanned full history")

    monkeypatch.setattr(ClientLifecycleSpool, "_verified_entries", forbidden_history_scan)
    start = parse_codex_hook_input(json.dumps(_session_start()))
    first = spool.stage(start.observation_body(), delivery_id=_delivery(start), occurred_at=NOW)
    compact = parse_codex_hook_input(
        json.dumps(
            {
                "session_id": SESSION_ID,
                "hook_event_name": "PreCompact",
                "turn_id": TURN_ID,
                "trigger": "auto",
            }
        )
    )
    second = spool.stage(
        compact.observation_body(),
        delivery_id=_delivery(compact, "0198f2ad-3d10-7a11-b515-4c5c1733f7b6"),
        occurred_at=NOW + dt.timedelta(seconds=1),
    )

    assert second.sequence == 2
    assert second.previous_entry_digest == first.entry_digest
    assert spool.pending(limit=2) == (first, second)
    assert len(list(spool.sessions_directory.glob("*.json"))) == 1
    assert len(list(spool.deliveries_directory.glob("*.json"))) == 2
    assert len(list(spool.queue_directory.glob("*.json"))) == 2


def test_status_uses_bounded_queue_pagination(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    envelope = parse_codex_hook_input(json.dumps(_session_start()))
    occurrences = (
        "0198f2ad-3d10-7a11-b515-4c5c1733f7c4",
        "0198f2ad-3d10-7a11-b515-4c5c1733f7c5",
        "0198f2ad-3d10-7a11-b515-4c5c1733f7c6",
    )
    for index, occurrence in enumerate(occurrences):
        spool.stage(
            envelope.observation_body(),
            delivery_id=_delivery(envelope, occurrence),
            occurred_at=NOW + dt.timedelta(seconds=index),
        )

    first = spool.status(limit=2)
    second = spool.status(limit=2, after_sequence=2)
    assert first["event_count"] == 3
    assert first["page_event_count"] == 2
    assert first["next_after_sequence"] == 2
    assert first["history_complete"] is False
    assert second["page_event_count"] == 1
    assert second["next_after_sequence"] is None


def test_explicit_bounded_replay_acks_only_terminal_canonical_receipt(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    envelope = parse_codex_hook_input(json.dumps(_session_start()))
    entry = spool.stage(
        envelope.observation_body(),
        delivery_id=_delivery(envelope),
        occurred_at=NOW,
    )
    calls: list[str] = []
    canonical_receipt = _canonical_receipt(spool, entry)

    def deliver(current: LifecycleSpoolEntry) -> CanonicalLifecycleReceipt:
        calls.append(current.entry_digest)
        return canonical_receipt

    result = replay_pending(
        spool,
        deliver=deliver,
        limit=1,
        attempted_at=NOW + dt.timedelta(minutes=1),
    )
    replay = replay_pending(
        spool,
        deliver=deliver,
        limit=1,
        attempted_at=NOW + dt.timedelta(minutes=2),
    )

    assert calls == [entry.entry_digest]
    assert result[0].outcome == "completed"
    assert result[0].canonical_ack_digest == canonical_receipt.canonical_ack_digest
    assert replay == ()
    assert spool.pending() == ()


def test_replay_failure_is_receipted_without_ack_or_silent_retry(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    envelope = parse_codex_hook_input(json.dumps(_session_start()))
    entry = spool.stage(
        envelope.observation_body(),
        delivery_id=_delivery(envelope),
        occurred_at=NOW,
    )

    def reject(current: LifecycleSpoolEntry) -> CanonicalLifecycleReceipt:
        assert current.entry_digest == entry.entry_digest
        raise ValidationFailed("canonical admission rejected")

    result = replay_pending(
        spool,
        deliver=reject,
        limit=1,
        attempted_at=NOW + dt.timedelta(minutes=1),
    )

    assert result[0].outcome == "rejected"
    assert result[0].canonical_ack_digest is None
    assert spool.pending() == (entry,)
    assert spool.status()["page_attempt_count"] == 1
    assert spool.status()["acked_count"] == 0


def test_poison_attempt_is_idempotent_then_bounded_to_manual_review(
    tmp_path: Path,
) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    envelope = parse_codex_hook_input(json.dumps(_session_start()))
    entry = spool.stage(
        envelope.observation_body(), delivery_id=_delivery(envelope), occurred_at=NOW
    )
    first = spool.record_attempt(
        entry.entry_digest,
        outcome="failed",
        evidence_digest=digest("poison-1"),
        attempted_at=NOW + dt.timedelta(seconds=1),
    )
    replay = spool.record_attempt(
        entry.entry_digest,
        outcome="failed",
        evidence_digest=digest("poison-1"),
        attempted_at=NOW + dt.timedelta(seconds=2),
    )
    assert replay == first
    assert len(list(spool.attempts_directory.glob("*.json"))) == 1

    spool.record_attempt(
        entry.entry_digest,
        outcome="failed",
        evidence_digest=digest("poison-2"),
        attempted_at=NOW + dt.timedelta(seconds=3),
    )
    terminal = spool.record_attempt(
        entry.entry_digest,
        outcome="failed",
        evidence_digest=digest("poison-3"),
        attempted_at=NOW + dt.timedelta(seconds=4),
    )
    assert terminal["disposition"] == "manual-review"
    assert terminal["failure_count"] == 3
    assert terminal["terminal_reason"] == "retry-budget-exhausted"
    assert spool.pending() == ()
    assert spool.status()["page_manual_review_count"] == 1
    with pytest.raises(PolicyViolation, match="terminal attempt-state"):
        spool.record_attempt(
            entry.entry_digest,
            outcome="completed",
            evidence_digest=digest("late-recovery"),
            attempted_at=NOW + dt.timedelta(seconds=5),
        )


def test_manual_review_resolved_prefix_does_not_skip_retryable_entries(
    tmp_path: Path,
) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    entries: list[LifecycleSpoolEntry] = []
    for offset in range(3):
        session_id = f"0198f2ad-3d10-7a11-b515-4c5c1733f7c{offset}"
        occurrence_id = f"0198f2ad-3d10-7a11-b515-4c5c1733f7d{offset}"
        envelope = parse_codex_hook_input(json.dumps(_session_start(session_id=session_id)))
        entries.append(
            spool.stage(
                envelope.observation_body(),
                delivery_id=_delivery(envelope, occurrence_id),
                occurred_at=NOW + dt.timedelta(seconds=offset),
            )
        )
    manual_terminal: dict[str, object] | None = None
    for offset in range(3):
        manual_terminal = spool.record_attempt(
            entries[0].entry_digest,
            outcome="failed",
            evidence_digest=digest(f"manual-review-{offset}"),
            attempted_at=NOW + dt.timedelta(minutes=1, seconds=offset),
        )
    receipt = _canonical_receipt(spool, entries[1])
    spool.record_attempt(
        entries[1].entry_digest,
        outcome="completed",
        evidence_digest=receipt.canonical_lookup_digest,
        attempted_at=NOW + dt.timedelta(minutes=2),
    )
    spool._acknowledge_verified_receipt(
        entries[1],
        receipt=receipt,
        acknowledged_at=NOW + dt.timedelta(minutes=2),
    )

    assert spool.pending(limit=2) == (entries[2],)
    status = spool.status()
    assert status["acked_count"] == 1
    assert status["resolved_manual_review_count"] == 1
    cursors = sorted(spool.drain_cursors_directory.glob("*.json"))
    manual_cursor = json.loads(cursors[0].read_text(encoding="utf-8"))
    assert manual_cursor["terminal_disposition"] == "manual-review"
    assert manual_cursor["ack_digest"] is None
    assert manual_cursor["canonical_event_digest"] is None
    assert manual_cursor["continuity_binding_digest"] is None
    assert manual_terminal is not None
    assert manual_cursor["attempt_digest"] == manual_terminal["attempt_digest"]


def test_manual_review_same_session_descendant_does_not_starve_other_sessions(
    tmp_path: Path,
) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    first_envelope = parse_codex_hook_input(json.dumps(_session_start()))
    first = spool.stage(
        first_envelope.observation_body(),
        delivery_id=_delivery(first_envelope),
        occurred_at=NOW,
    )
    compact_envelope = parse_codex_hook_input(
        json.dumps(
            {
                "session_id": SESSION_ID,
                "hook_event_name": "PreCompact",
                "turn_id": TURN_ID,
                "trigger": "manual",
            }
        )
    )
    same_session_second = spool.stage(
        compact_envelope.observation_body(),
        delivery_id=_delivery(
            compact_envelope,
            "0198f2ad-3d10-7a11-b515-4c5c1733f7c0",
        ),
        occurred_at=NOW + dt.timedelta(seconds=1),
    )
    other_entries: list[LifecycleSpoolEntry] = []
    for offset in range(2):
        envelope = parse_codex_hook_input(
            json.dumps(_session_start(session_id=f"0198f2ad-3d10-7a11-b515-4c5c1733f7d{offset}"))
        )
        other_entries.append(
            spool.stage(
                envelope.observation_body(),
                delivery_id=_delivery(
                    envelope,
                    f"0198f2ad-3d10-7a11-b515-4c5c1733f7e{offset}",
                ),
                occurred_at=NOW + dt.timedelta(seconds=offset + 2),
            )
        )
    for offset in range(3):
        spool.record_attempt(
            first.entry_digest,
            outcome="failed",
            evidence_digest=digest(f"first-manual-{offset}"),
            attempted_at=NOW + dt.timedelta(minutes=1, seconds=offset),
        )

    def deliver(entry: LifecycleSpoolEntry) -> CanonicalLifecycleReceipt:
        return _canonical_receipt(spool, entry)

    first_page = replay_pending(
        spool,
        deliver=deliver,
        limit=2,
        attempted_at=NOW + dt.timedelta(minutes=2),
    )
    descendant_state = spool._read_attempt_state(same_session_second.entry_digest)
    predecessor_state = spool._read_attempt_state(first.entry_digest)
    assert descendant_state is not None
    assert predecessor_state is not None
    assert descendant_state["disposition"] == "manual-review"
    assert descendant_state["terminal_reason"] == "predecessor-manual-review"
    assert descendant_state["predecessor_entry_digest"] == first.entry_digest
    assert descendant_state["predecessor_attempt_state_digest"] == predecessor_state["state_digest"]
    assert [result.outcome for result in first_page] == [
        "recovery-required",
        "completed",
    ]
    assert spool.pending(limit=1) == (other_entries[1],)


def test_unexpected_replay_exception_is_sanitized_failed_attempt(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    envelope = parse_codex_hook_input(json.dumps(_session_start()))
    entry = spool.stage(
        envelope.observation_body(), delivery_id=_delivery(envelope), occurred_at=NOW
    )

    def explode(_: LifecycleSpoolEntry) -> CanonicalLifecycleReceipt:
        raise RuntimeError("SECRET=C:/private/raw-transcript.jsonl")

    result = replay_pending(
        spool,
        deliver=explode,
        limit=1,
        attempted_at=NOW + dt.timedelta(minutes=1),
    )

    assert result[0].outcome == "failed"
    assert spool.pending() == (entry,)
    persisted = next(spool.attempts_directory.glob("*.json")).read_text(encoding="utf-8")
    assert "SECRET" not in persisted
    assert "private" not in persisted
    assert "unexpected-exception" not in persisted  # category is digest-bound, not copied


def test_production_drain_requires_preflight_apply_and_read_only_lookup(
    tmp_path: Path,
) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    envelope = parse_codex_hook_input(json.dumps(_session_start()))
    entry = spool.stage(
        envelope.observation_body(), delivery_id=_delivery(envelope), occurred_at=NOW
    )

    class Admission:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.receipt: CanonicalLifecycleReceipt | None = None

        def preflight(
            self,
            source: LifecycleSpoolEntry,
            document: dict[str, object],
            *,
            client_instance_id: str,
        ) -> dict[str, object]:
            self.calls.append("preflight")
            return _continuity_preflight(source, document, client_instance_id=client_instance_id)

        def apply(
            self,
            source: LifecycleSpoolEntry,
            document: dict[str, object],
            **_: object,
        ) -> CanonicalLifecycleReceipt:
            self.calls.append("apply")
            ack = SimpleNamespace(
                event_id=CANONICAL_EVENT_ID,
                local_event_digest=document["event_digest"],
                canonical_digest=digest(
                    {"event_id": str(CANONICAL_EVENT_ID), "event": document["event_digest"]}
                ),
            )
            base = CanonicalLifecycleReceipt.verified(source, document, ack, ack)
            self.receipt = base.bind_continuity(source, _continuity_binding(source, base))
            return self.receipt

        def lookup(self, *_: object, **__: object) -> CanonicalLifecycleReceipt:
            self.calls.append("lookup")
            assert self.receipt is not None
            return self.receipt

    admission = Admission()
    result = drain_to_postgres(
        spool,
        client_instance_id=spool.client_instance_id(),
        continuity_admission=admission,
        limit=1,
        attempted_at=NOW + dt.timedelta(minutes=1),
    )

    assert admission.calls == ["preflight", "apply", "lookup"]
    assert result[0].outcome == "completed"
    assert spool.pending() == ()
    ack = json.loads(next(spool.acks_directory.glob("*.json")).read_text(encoding="utf-8"))
    assert admission.receipt is not None
    assert ack["entry_digest"] == entry.entry_digest
    assert ack["canonical_event_digest"] == admission.receipt.canonical_event_digest
    assert ack["continuity_binding"]["status"] == "completed"


def test_production_drain_rejects_missing_adapter_without_local_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "never-created"
    spool = ClientLifecycleSpool(home, client_id="codex")
    with pytest.raises(PolicyViolation, match="continuity admission adapter"):
        drain_to_postgres(
            spool,
            client_instance_id="codex-static-instance",
            limit=1,
            attempted_at=NOW + dt.timedelta(minutes=1),
        )
    assert not home.exists()


def test_cli_apply_without_composition_has_no_local_mutation(tmp_path: Path) -> None:
    home = tmp_path / "never-created"
    result = CliRunner().invoke(
        client_app,
        ["drain", "--uygula", "--home", str(home)],
    )
    assert result.exit_code == 6
    normalized_output = " ".join(result.output.split())
    assert (
        "Lifecycle apply public CLI'dan yapilamaz; exact ClaimedWork worker gerekir"
        in normalized_output
    )
    assert not home.exists()


@pytest.mark.parametrize("command", ("pending", "drain"))
def test_mutating_selection_cli_has_no_caller_controlled_sequence_skip(
    tmp_path: Path,
    command: str,
) -> None:
    result = CliRunner().invoke(
        client_app,
        [command, "--after-sequence", "1", "--home", str(tmp_path / "home")],
    )

    assert result.exit_code == 2
    assert "No such option" in result.output


def test_precompact_receipt_requires_exact_runtime_binding_outbox(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    envelope = parse_codex_hook_input(
        json.dumps(
            {
                "session_id": SESSION_ID,
                "hook_event_name": "PreCompact",
                "turn_id": TURN_ID,
                "trigger": "manual",
            }
        )
    )
    entry = spool.stage(
        envelope.observation_body(), delivery_id=_delivery(envelope), occurred_at=NOW
    )
    event = canonical_lifecycle_event(
        entry,
        client_instance_id=spool.client_instance_id(),
        previous_canonical_event_digest=None,
    )
    unbound = SimpleNamespace(
        event_id=CANONICAL_EVENT_ID,
        local_event_digest=event["event_digest"],
        canonical_digest=digest("canonical-ack"),
        compaction_outbox_id=None,
        compaction_payload_digest=None,
    )
    with pytest.raises(PolicyViolation, match="runtime binding outbox"):
        CanonicalLifecycleReceipt.verified(entry, event, unbound, unbound)

    bound = SimpleNamespace(
        event_id=CANONICAL_EVENT_ID,
        local_event_digest=event["event_digest"],
        canonical_digest=digest("canonical-ack"),
        compaction_outbox_id=UUID("0198f2ad-3d10-7a11-b515-4c5c1733f7b7"),
        compaction_payload_digest=digest("runtime-binding-payload"),
    )
    receipt = CanonicalLifecycleReceipt.verified(entry, event, bound, bound)
    assert receipt.runtime_binding_id == bound.compaction_outbox_id
    assert receipt.runtime_binding_digest == bound.compaction_payload_digest
    with pytest.raises(PolicyViolation, match="terminal continuity binding"):
        receipt.assert_binding(entry)
    continuity = _continuity_binding(entry, receipt)
    continuity["compiler_enqueue"] = False
    continuity_body = {key: value for key, value in continuity.items() if key != "binding_digest"}
    continuity["binding_digest"] = digest(continuity_body)
    with pytest.raises(PolicyViolation, match="compiler enqueue"):
        receipt.bind_continuity(entry, continuity)


def test_public_arbitrary_ack_command_is_not_exposed() -> None:
    result = CliRunner().invoke(client_app, ["ack", "--entry-digest", digest("x")])
    assert result.exit_code != 0


def test_spool_parent_symlink_or_reparse_fails_closed(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    try:
        os.symlink(real_home, linked_home, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("platform test symlink olusturma yetkisi vermiyor")
    spool = ClientLifecycleSpool(linked_home, client_id="codex")
    envelope = parse_codex_hook_input(json.dumps(_session_start()))
    with pytest.raises(PolicyViolation, match="reparse/symlink"):
        spool.stage(
            envelope.observation_body(),
            delivery_id=_delivery(envelope),
            occurred_at=NOW,
        )


def test_ack_target_symlink_or_reparse_fails_closed(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    envelope = parse_codex_hook_input(json.dumps(_session_start()))
    entry = spool.stage(
        envelope.observation_body(), delivery_id=_delivery(envelope), occurred_at=NOW
    )
    external = tmp_path / "external-ack.json"
    external.write_text("{}", encoding="utf-8")
    ack_path = spool.acks_directory / f"{entry.entry_digest.removeprefix('sha256:')}.json"
    try:
        os.symlink(external, ack_path)
    except (OSError, NotImplementedError):
        pytest.skip("platform test symlink olusturma yetkisi vermiyor")
    receipt = _canonical_receipt(spool, entry)
    spool.record_attempt(
        entry.entry_digest,
        outcome="completed",
        evidence_digest=receipt.canonical_lookup_digest,
        attempted_at=NOW + dt.timedelta(seconds=1),
    )
    with pytest.raises(PolicyViolation, match="regular file"):
        spool._acknowledge_verified_receipt(
            entry,
            receipt=receipt,
            acknowledged_at=NOW + dt.timedelta(seconds=1),
        )


def test_oversized_spool_target_fails_closed(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    envelope = parse_codex_hook_input(json.dumps(_session_start()))
    spool.stage(envelope.observation_body(), delivery_id=_delivery(envelope), occurred_at=NOW)
    spool.queue_state_path.write_bytes(b"x" * (MAX_SPOOL_DOCUMENT_BYTES + 1))
    with pytest.raises(PolicyViolation, match="boyut sinirini asti"):
        spool.pending()


def test_parent_directory_fsync_platform_boundary_is_explicit(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    assert _fsync_parent_directory(target) is (os.name != "nt")


def test_concurrent_identical_delivery_creates_one_event(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    envelope = parse_codex_hook_input(json.dumps(_session_start()))

    def stage() -> str:
        return spool.stage(
            envelope.observation_body(),
            delivery_id=_delivery(envelope),
            occurred_at=NOW,
        ).entry_digest

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(stage) for _ in range(32)]
        results = [future.result() for future in futures]

    assert len(set(results)) == 1
    assert len(list(spool.events_directory.glob("*.json"))) == 1


def test_corrupted_spool_chain_is_never_silently_replayed(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    envelope = parse_codex_hook_input(json.dumps(_session_start()))
    spool.stage(
        envelope.observation_body(),
        delivery_id=_delivery(envelope),
        occurred_at=NOW,
    )
    source = next(spool.events_directory.glob("*.json"))
    document = json.loads(source.read_text(encoding="utf-8"))
    document["sequence"] = 2
    source.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PolicyViolation, match=r"sequence|digest"):
        spool.pending()


def test_hook_cli_spools_empty_json_and_version_drift_rejects(tmp_path: Path) -> None:
    runner = CliRunner()
    payload = json.dumps(_session_start())
    success = runner.invoke(
        client_app,
        [
            "hook",
            "--client",
            "codex",
            "--client-version",
            CODEX_REVIEWED_VERSION,
            "--home",
            str(tmp_path / "home"),
        ],
        input=payload,
    )
    drift = runner.invoke(
        client_app,
        [
            "hook",
            "--client",
            "codex",
            "--client-version",
            "0.150.2",
            "--home",
            str(tmp_path / "drift-home"),
        ],
        input=payload,
    )

    assert success.exit_code == 0, success.output
    assert json.loads(success.stdout) == {}
    assert drift.exit_code == 2
    assert not (tmp_path / "drift-home").exists()


def test_precompact_hook_failure_returns_documented_fail_closed_output(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        client_app,
        ["hook", "--home", str(tmp_path / "home")],
        input=json.dumps(
            {
                "session_id": SESSION_ID,
                "hook_event_name": "PreCompact",
                "trigger": "manual",
            }
        ),
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"continue": False}
    assert not (tmp_path / "home").exists()
