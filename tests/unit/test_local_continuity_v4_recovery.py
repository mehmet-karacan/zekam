from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, cast
from uuid import UUID, uuid5

import pytest

import zekam.application.local_continuity_v4_recovery as recovery_module
from zekam.application.local_continuity import ContinuityBinding, ContinuityTail
from zekam.application.local_continuity_v4_recovery import (
    B2_EVENT_NS,
    FrozenEffectRecoveryResolutionSnapshot,
    FrozenReceiptlessRecoverySnapshot,
    FrozenUnknownEffectSnapshot,
    ReceiptlessRecoveryRequest,
    RecoveryResult,
    ResolveEffectRecoveryRequest,
    UnknownEffectRequest,
    commit_outcome,
    entry_result,
    resolution_result,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed

SESSION = "018f0000-0000-7000-8000-000000000701"
CLAIM = "018f0000-0000-7000-8000-000000000702"
JOB = "018f0000-0000-7000-8000-000000000703"
LEASE = "018f0000-0000-7000-8000-000000000704"
CASE = "018f0000-0000-7000-8000-000000000705"
NOW = "2026-09-03T12:00:00+00:00"


def _binding() -> ContinuityBinding:
    return ContinuityBinding(
        session_id=SESSION,
        external_session_id="external",
        project_id="018f0000-0000-7000-8000-000000000706",
        realm_id="018f0000-0000-7000-8000-000000000707",
        client_id="codex",
        device_id="macbook",
        source_snapshot_id="018f0000-0000-7000-8000-000000000708",
        task_digest=digest("task"),
        plan_digest=digest("plan"),
        policy_digest=digest("policy"),
        work_item_id="018f0000-0000-7000-8000-000000000709",
        run_id="018f0000-0000-7000-8000-000000000710",
    )


def _issue[SnapshotT](kind: type[SnapshotT], **values: object) -> SnapshotT:
    value = object.__new__(kind)
    for name in kind.__dataclass_fields__:  # type: ignore[attr-defined]
        object.__setattr__(value, name, values[name])
    value.__post_init__()  # type: ignore[attr-defined]
    return value


def _unknown(**changes: object) -> FrozenUnknownEffectSnapshot:
    values: dict[str, object] = {
        "binding_digest": _binding().binding_digest,
        "job_id": JOB,
        "claim_id": CLAIM,
        "lease_id": LEASE,
        "lease_owner_id": "owner",
        "lease_owner_pid": 10,
        "lease_owner_token": "token",
        "fencing_token": 1,
        "operation": "test.effect",
        "effect_commitment_digest": digest("effect"),
        "claimed_at": NOW,
        "unknown_category": "executor-ambiguous",
        "unknown_commitment_digest": digest("unknown-envelope"),
        "observed_at": "2026-09-03T12:00:01+00:00",
        "issuer_receipt_id": "private-issuer",
    }
    values.update(changes)
    return _issue(FrozenUnknownEffectSnapshot, **values)


def _receiptless(**changes: object) -> FrozenReceiptlessRecoverySnapshot:
    values = {
        name: getattr(_unknown(), name)
        for name in (
            "binding_digest",
            "job_id",
            "claim_id",
            "lease_id",
            "lease_owner_id",
            "lease_owner_pid",
            "lease_owner_token",
            "fencing_token",
            "operation",
            "effect_commitment_digest",
            "claimed_at",
            "observed_at",
            "issuer_receipt_id",
        )
    }
    values["recovery_reason"] = "lease-expired"
    values.update(changes)
    return _issue(FrozenReceiptlessRecoverySnapshot, **values)


def _resolution(**changes: object) -> FrozenEffectRecoveryResolutionSnapshot:
    values: dict[str, object] = {
        "binding_digest": _binding().binding_digest,
        "job_id": JOB,
        "claim_id": CLAIM,
        "recovery_case_id": CASE,
        "outcome": "completed",
        "resolution_commitment_digest": digest("resolution"),
        "resolved_at": NOW,
        "issuer_receipt_id": "private-adjudicator",
    }
    values.update(changes)
    return _issue(FrozenEffectRecoveryResolutionSnapshot, **values)


def test_namespace_request_fields_and_deterministic_ids_are_frozen() -> None:
    assert UUID("15a556c8-9258-5fd0-8c67-3051d9e8d75d") == B2_EVENT_NS
    assert str(uuid5(B2_EVENT_NS, f"effect-case|{CLAIM}")) == (
        "452bf8cb-a509-5e97-86cf-2ab92530c52f"
    )
    assert tuple(field.name for field in dataclasses.fields(UnknownEffectRequest)) == (
        "binding",
        "claim_id",
        "expected_revision_digest",
    )
    assert tuple(field.name for field in dataclasses.fields(ResolveEffectRecoveryRequest)) == (
        "binding",
        "recovery_case_id",
        "expected_revision_digest",
        "expected_tail",
    )


@pytest.mark.parametrize(
    "call",
    (
        lambda: UnknownEffectRequest(cast(Any, None), CLAIM, digest("revision")),
        lambda: UnknownEffectRequest(_binding(), "", digest("revision")),
        lambda: ReceiptlessRecoveryRequest(_binding(), CLAIM.upper(), digest("revision")),
        lambda: ResolveEffectRecoveryRequest(_binding(), CASE, "bad", ContinuityTail(0, None)),
        lambda: ResolveEffectRecoveryRequest(_binding(), CASE, digest("revision"), cast(Any, None)),
    ),
)
def test_public_requests_reject_wrong_null_blank_and_noncanonical(call: Any) -> None:
    with pytest.raises(ValidationFailed):
        call()


@pytest.mark.parametrize(
    ("factory", "changes"),
    (
        (_unknown, {"lease_owner_pid": True}),
        (_unknown, {"unknown_category": "raw-exception"}),
        (_unknown, {"observed_at": "2026-09-03T12:00:00.1+00:00"}),
        (_unknown, {"unknown_commitment_digest": "bad"}),
        (_unknown, {"lease_owner_token": "secret\x00"}),
        (_receiptless, {"recovery_reason": "missing-hook"}),
        (_receiptless, {"fencing_token": 0}),
        (_resolution, {"outcome": "delivered"}),
        (_resolution, {"resolved_at": "not-a-time"}),
        (_resolution, {"issuer_receipt_id": "x" * 513}),
    ),
)
def test_sealed_snapshots_reject_wrong_boundary_and_unapproved_values(
    factory: Any, changes: dict[str, object]
) -> None:
    with pytest.raises(ValidationFailed):
        factory(**changes)


def test_snapshots_have_no_public_constructor_or_mutable_state() -> None:
    with pytest.raises(TypeError):
        FrozenUnknownEffectSnapshot()
    value = _unknown()
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.operation = "changed"  # type: ignore[misc]


def test_snapshot_preserves_valid_fractional_runtime_claim_timestamp() -> None:
    assert _unknown(claimed_at="2026-09-03T12:00:00.123456+00:00").claimed_at.endswith(
        ".123456+00:00"
    )


def test_result_contracts_are_exact_authority_free_and_digest_bound() -> None:
    entries = tuple(
        entry_result(
            route=route,
            status="fresh",
            claim_id=CLAIM,
            recovery_case_id=CASE,
            attachment_revision_digest=digest("revision"),
        )
        for route in ("unknown", "receiptless")
    )
    failed = resolution_result(
        status="replayed",
        outcome="failed",
        claim_id=CLAIM,
        recovery_case_id=CASE,
        recovery_resolution_id=LEASE,
    )
    completed = resolution_result(
        status="fresh",
        outcome="completed",
        claim_id=CLAIM,
        recovery_case_id=CASE,
        recovery_resolution_id=LEASE,
        crash_recovered_event_digest=digest("event"),
        restored_revision_digest=digest("restored"),
    )
    uncertain = commit_outcome(operation="resolve", claim_id=CLAIM, recovery_case_id=CASE)
    for value in (*entries, failed, completed, uncertain):
        assert value.result_digest == digest(value.body())
        assert value.body()["grants_authority"] is False
        assert value.body()["approval_inherited"] is False
        assert value.body()["production_activated"] is False
    assert failed.body()["attention"] is True
    assert uncertain.body()["recovery_case_id"] == CASE
    assert [value.body()["schema"] for value in entries] == [
        "zekam-v4-unknown-recovery-entry-result/v1",
        "zekam-v4-receiptless-recovery-entry-result/v1",
    ]


def test_result_contract_cannot_be_constructed_with_malformed_or_extra_fields() -> None:
    constructor = cast(Any, RecoveryResult)
    with pytest.raises(TypeError):
        constructor()
    with pytest.raises(TypeError):
        constructor(
            {
                "schema": "wrong",
                "extra": "forged",
                "grants_authority": False,
                "approval_inherited": False,
                "production_activated": False,
            }
        )
    with pytest.raises(TypeError):
        constructor(_body_bytes=b"{}")


@pytest.mark.parametrize(
    "raw",
    (None, b"x" * 32_769, b"\xff", b"[]", b'{"b":1,"a":2}'),
)
def test_forged_result_bytes_fail_exact_type_size_utf8_and_canonical_checks(
    raw: object,
) -> None:
    forged = object.__new__(RecoveryResult)
    object.__setattr__(forged, "_body_bytes", raw)
    with pytest.raises(ValidationFailed):
        forged.__post_init__()


def test_private_result_builder_rejects_unrecognized_payload_type() -> None:
    with pytest.raises(ValidationFailed, match="typed result payload"):
        recovery_module._result(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "body",
    (
        {},
        {
            "schema": "zekam-v4-recovery-commit-outcome/v1",
            "status": "not-committed-or-unobservable",
            "operation": "resolve",
            "claim_id": CLAIM,
            "recovery_case_id": CASE,
            "safe_to_retry": True,
            "grants_authority": True,
            "approval_inherited": False,
            "production_activated": False,
        },
        {
            "schema": "zekam-v4-recovery-commit-outcome/v1",
            "status": "wrong",
            "operation": "resolve",
            "claim_id": CLAIM,
            "recovery_case_id": CASE,
            "safe_to_retry": True,
            "grants_authority": False,
            "approval_inherited": False,
            "production_activated": False,
        },
    ),
)
def test_result_body_validator_rejects_unknown_schema_authority_and_fixed_drift(
    body: dict[str, object],
) -> None:
    with pytest.raises(ValidationFailed):
        recovery_module._validate_result_body(body)


def test_result_is_factory_only_non_subclassable_and_immutably_byte_backed() -> None:
    value = entry_result(
        route="receiptless",
        status="replayed",
        claim_id=CLAIM,
        recovery_case_id=CASE,
        attachment_revision_digest=digest("revision"),
    )
    original = value.result_digest
    value.body()["grants_authority"] = True
    assert value.result_digest == original
    with pytest.raises(TypeError):

        class ForgedResult(RecoveryResult):
            pass

    with pytest.raises(dataclasses.FrozenInstanceError):
        value._body_bytes = b"{}"  # type: ignore[misc]
    with pytest.raises(TypeError):
        dataclasses.replace(value)
    for removed in (
        "EntryResultPayload",
        "ResolutionResultPayload",
        "RecoveryCommitOutcomePayload",
    ):
        assert not hasattr(recovery_module, removed)
    for private_name in (
        "_UnknownEntryResultPayload",
        "_ReceiptlessEntryResultPayload",
        "_CompletedRecoveryResultPayload",
        "_FailedRecoveryAttentionResultPayload",
        "_RecoveryCommitOutcomePayload",
    ):
        with pytest.raises(TypeError):
            type("ForgedPayload", (getattr(recovery_module, private_name),), {})


def test_result_factories_reject_subclasses_and_cross_field_drift() -> None:
    class Text(str):
        pass

    calls: tuple[Callable[[], object], ...] = (
        lambda: entry_result(
            route=cast(Any, Text("unknown")),
            status="fresh",
            claim_id=CLAIM,
            recovery_case_id=CASE,
            attachment_revision_digest=digest("revision"),
        ),
        lambda: entry_result(
            route="unknown",
            status=cast(Any, False),
            claim_id=CLAIM,
            recovery_case_id=CASE,
            attachment_revision_digest=digest("revision"),
        ),
        lambda: resolution_result(
            status="fresh",
            outcome="failed",
            claim_id=CLAIM,
            recovery_case_id=CASE,
            recovery_resolution_id=LEASE,
            restored_revision_digest=digest("forged"),
        ),
        lambda: resolution_result(
            status="fresh",
            outcome="completed",
            claim_id=CLAIM,
            recovery_case_id=CASE,
            recovery_resolution_id=LEASE,
        ),
        lambda: commit_outcome(
            operation=cast(Any, Text("resolve")), claim_id=CLAIM, recovery_case_id=CASE
        ),
    )
    for call in calls:
        with pytest.raises(ValidationFailed):
            call()


def test_private_envelope_known_vector_does_not_expose_raw_canary() -> None:
    body = {
        "schema": "zekam-effect-unknown-authority-envelope/v1",
        "nonce_hex": "01" * 32,
        "binding_digest": _binding().binding_digest,
        "job_id": JOB,
        "claim_id": CLAIM,
        "fencing_token": 1,
        "unknown_category": "executor-ambiguous",
        "internal_observation_digest": digest("PRIVATE-CANARY"),
    }
    commitment = digest(body)
    snapshot = _unknown(unknown_commitment_digest=commitment)
    assert "PRIVATE-CANARY" not in repr(snapshot)
    assert "PRIVATE-CANARY" not in commitment
