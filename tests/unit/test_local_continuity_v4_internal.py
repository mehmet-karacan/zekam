from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, cast
from uuid import UUID, uuid5

import pytest

from zekam.application.local_continuity import ContinuityBinding, ContinuityTail
from zekam.application.local_continuity_v4_internal import (
    EVENT_NS,
    DirectEffectOutcomeRequest,
    EffectClaimRequest,
    FrozenDirectEffectOutcomeSnapshot,
    FrozenEffectClaimSnapshot,
    FrozenTurnCommitSnapshot,
    InternalProducerResult,
    TurnCommitRequest,
)
from zekam.application.local_continuity_v4_writer import internal_receipt_digest
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.sqlite.local_continuity_v4_internal import (
    SQLiteDormantV4InternalProducer,
)

SESSION = "018f0000-0000-7000-8000-000000000201"
PROJECT = "018f0000-0000-7000-8000-000000000202"
REALM = "018f0000-0000-7000-8000-000000000203"
SNAPSHOT = "018f0000-0000-7000-8000-000000000204"
ITEM = "turn/018f0000-0000-7000-8000-000000000205"
JOB = "018f0000-0000-7000-8000-000000000206"
CLAIM = "018f0000-0000-7000-8000-000000000207"
WORK = "018f0000-0000-7000-8000-000000000208"
RUN = "018f0000-0000-7000-8000-000000000209"
NOW = "2026-09-03T12:00:00+00:00"


def _binding() -> ContinuityBinding:
    return ContinuityBinding(
        session_id=SESSION,
        external_session_id="external",
        project_id=PROJECT,
        realm_id=REALM,
        client_id="codex",
        device_id="macbook",
        source_snapshot_id=SNAPSHOT,
        task_digest=digest("task"),
        plan_digest=digest("plan"),
        policy_digest=digest("policy"),
        work_item_id=WORK,
        run_id=RUN,
    )


def _issue[SnapshotT](kind: type[SnapshotT], **values: object) -> SnapshotT:
    value = object.__new__(kind)
    for name in kind.__dataclass_fields__:  # type: ignore[attr-defined]
        object.__setattr__(value, name, values[name])
    value.__post_init__()  # type: ignore[attr-defined]
    return value


def _turn(**changes: object) -> FrozenTurnCommitSnapshot:
    values: dict[str, object] = {
        "binding_digest": _binding().binding_digest,
        "role": "user",
        "item_ref": ITEM,
        "content_commitment_digest": digest("opaque-content"),
        "store_generation_commitment_digest": digest("opaque-generation"),
        "previous_store_generation_commitment_digest": None,
        "issuer_receipt_id": "issuer-receipt",
        "committed_at": NOW,
    }
    values.update(changes)
    return _issue(FrozenTurnCommitSnapshot, **values)


def _claim(**changes: object) -> FrozenEffectClaimSnapshot:
    values: dict[str, object] = {
        "binding_digest": _binding().binding_digest,
        "job_id": JOB,
        "job_state": "running",
        "job_payload_digest": digest("payload"),
        "job_updated_at": "2026-09-03T11:59:59.123456+00:00",
        "lease_id": "018f0000-0000-7000-8000-000000000208",
        "lease_owner_id": "worker",
        "lease_owner_pid": 123,
        "lease_owner_token": "incarnation",
        "fencing_token": 1,
        "lease_heartbeat_at": "2026-09-03T11:59:59.123456+00:00",
        "lease_expires_at": "2026-09-03T12:01:00.123456+00:00",
        "resource_locks": (("resource/a", "2026-09-03T11:59:59.123456+00:00"),),
        "operation": "test.effect",
        "effect_commitment_digest": digest("opaque-effect"),
        "claimed_at": NOW,
    }
    values.update(changes)
    return _issue(FrozenEffectClaimSnapshot, **values)


def _outcome(**changes: object) -> FrozenDirectEffectOutcomeSnapshot:
    claim = _claim()
    values: dict[str, object] = {
        "binding_digest": claim.binding_digest,
        "job_id": claim.job_id,
        "claim_id": CLAIM,
        "lease_id": claim.lease_id,
        "lease_owner_id": claim.lease_owner_id,
        "lease_owner_pid": claim.lease_owner_pid,
        "lease_owner_token": claim.lease_owner_token,
        "fencing_token": claim.fencing_token,
        "operation": claim.operation,
        "effect_commitment_digest": claim.effect_commitment_digest,
        "claimed_at": claim.claimed_at,
        "status": "completed",
        "outcome_commitment_digest": digest("opaque-outcome"),
        "completed_at": "2026-09-03T12:00:01+00:00",
    }
    values.update(changes)
    return _issue(FrozenDirectEffectOutcomeSnapshot, **values)


def test_literal_namespace_and_deterministic_identities_are_frozen() -> None:
    assert UUID("8f950d20-9db0-5d5e-9f54-99a7987ebf4b") == EVENT_NS
    binding = _binding()
    claim = str(uuid5(EVENT_NS, f"effect-claim|{binding.binding_digest}|{JOB}"))
    receipt = str(uuid5(EVENT_NS, f"effect-receipt|{claim}"))
    assert claim == "c6cb17e3-cc72-5d3e-b4cd-23542e9b3b20"
    assert receipt == "07c31767-8c19-5883-873d-3af07ad9cff9"


@pytest.mark.parametrize(
    "selector_factory",
    (
        lambda: TurnCommitRequest(_binding(), cast(Any, "USER"), ITEM, ContinuityTail(0, None)),
        lambda: TurnCommitRequest(cast(Any, None), "user", ITEM, ContinuityTail(0, None)),
        lambda: TurnCommitRequest(_binding(), "user", "", ContinuityTail(0, None)),
        lambda: TurnCommitRequest(
            _binding(), "user", f"turn/{'a' * 1024}", ContinuityTail(0, None)
        ),
        lambda: TurnCommitRequest(_binding(), "user", "turn/not-a-uuid", ContinuityTail(0, None)),
        lambda: EffectClaimRequest(_binding(), "\x00", ContinuityTail(0, None)),
        lambda: DirectEffectOutcomeRequest(_binding(), "NOT-UUID", ContinuityTail(0, None)),
    ),
)
def test_public_selectors_reject_wrong_blank_control_and_noncanonical_values(
    selector_factory: Callable[[], object],
) -> None:
    with pytest.raises(ValidationFailed):
        selector_factory()


def test_public_selectors_have_only_locator_and_tail_fields() -> None:
    assert tuple(field.name for field in dataclasses.fields(TurnCommitRequest)) == (
        "binding",
        "role",
        "item_ref",
        "expected_tail",
    )
    assert tuple(field.name for field in dataclasses.fields(EffectClaimRequest)) == (
        "binding",
        "job_id",
        "expected_tail",
    )
    assert tuple(field.name for field in dataclasses.fields(DirectEffectOutcomeRequest)) == (
        "binding",
        "claim_id",
        "expected_tail",
    )


@pytest.mark.parametrize(
    ("factory", "changes"),
    (
        (_turn, {"role": 1}),
        (_turn, {"item_ref": None}),
        (_turn, {"committed_at": "2026-09-03T12:00:00.1+00:00"}),
        (_turn, {"committed_at": "2026-09-03T15:00:00+03:00"}),
        (_turn, {"content_commitment_digest": "not-a-digest"}),
        (_turn, {"previous_store_generation_commitment_digest": "not-a-digest"}),
        (_turn, {"issuer_receipt_id": " bad "}),
        (_claim, {"job_state": "ready"}),
        (_claim, {"job_updated_at": None}),
        (_claim, {"job_updated_at": "2026-09-03T15:00:00+03:00"}),
        (_claim, {"lease_owner_pid": True}),
        (_claim, {"fencing_token": 0}),
        (_claim, {"resource_locks": []}),
        (_claim, {"resource_locks": (("resource/a",),)}),
        (_claim, {"resource_locks": (("resource/a", "not-a-time"),)}),
        (_claim, {"resource_locks": (("z", NOW), ("a", NOW))}),
        (_claim, {"resource_locks": (("a", NOW), ("a", NOW))}),
        (_claim, {"resource_locks": tuple((f"r/{n}", NOW) for n in range(65))}),
        (_claim, {"lease_owner_token": " "}),
        (_claim, {"lease_owner_pid": float("nan")}),
        (_claim, {"claimed_at": "not-a-time"}),
        (_claim, {"operation": "bad\noperation"}),
        (_claim, {"effect_commitment_digest": None}),
        (_outcome, {"status": "unknown"}),
        (_outcome, {"status": 1}),
        (_outcome, {"outcome_commitment_digest": None}),
        (_outcome, {"completed_at": "2026-09-03T12:00:01.1+00:00"}),
    ),
)
def test_frozen_snapshots_reject_wrong_types_order_unknown_and_fractional_issued_time(
    factory: Callable[..., object], changes: dict[str, object]
) -> None:
    with pytest.raises(ValidationFailed):
        factory(**changes)


def test_frozen_snapshots_have_no_public_constructor_or_mutable_collections() -> None:
    with pytest.raises(TypeError):
        FrozenTurnCommitSnapshot()
    claim = _claim()
    assert type(claim.resource_locks) is tuple
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.operation = "changed"  # type: ignore[misc]


def test_commitment_and_internal_receipt_known_vectors_are_independent() -> None:
    nonce = "01" * 32
    envelope = {
        "schema": "zekam-turn-content-authority-envelope/v1",
        "nonce_hex": nonce,
        "binding_digest": _binding().binding_digest,
        "role": "user",
        "item_ref": ITEM,
        "internal_content_digest": digest("private-low-entropy-content"),
    }
    assert digest(envelope) == (
        "sha256:750513b743b318f876e0677caffb14a401d758c66ddd899f81649e90462e872e"
    )
    body = {
        "attachment_revision_digest": digest("revision"),
        "binding_digest": _binding().binding_digest,
        "created_at": NOW,
        "event_digest": digest("event"),
        "event_kind": "USER_TURN_COMMITTED",
        "expected_previous_event_digest": digest("previous"),
        "operation_key": "turn-commit:user:018f0000-0000-7000-8000-000000000205",
        "session_id": SESSION,
    }
    assert (
        internal_receipt_digest(
            body, producer_kind="turn_commit_digest", producer_ref=digest("turn")
        )
        == "sha256:1f77c6f19c4602f8a6d1f024390668479d919494d39e58710b7424cc99c1d149"
    )


def test_all_fixed_opaque_envelope_schema_vectors_are_frozen() -> None:
    nonce = "01" * 32
    binding_digest = _binding().binding_digest
    envelopes = (
        {
            "schema": "zekam-turn-generation-authority-envelope/v1",
            "nonce_hex": nonce,
            "binding_digest": binding_digest,
            "role": "user",
            "item_ref": ITEM,
            "store_generation_id": "generation-1",
            "previous_store_generation_id": None,
        },
        {
            "schema": "zekam-effect-plan-authority-envelope/v1",
            "nonce_hex": nonce,
            "binding_digest": binding_digest,
            "job_id": JOB,
            "lease_id": "018f0000-0000-7000-8000-000000000208",
            "fencing_token": 1,
            "operation": "test.effect",
            "internal_effect_digest": digest("private-effect"),
        },
        {
            "schema": "zekam-direct-effect-outcome-authority-envelope/v1",
            "nonce_hex": nonce,
            "binding_digest": binding_digest,
            "job_id": JOB,
            "claim_id": CLAIM,
            "fencing_token": 1,
            "status": "completed",
            "internal_result_digest": digest("private-result"),
        },
    )
    assert tuple(digest(value) for value in envelopes) == (
        "sha256:a13e1ac730fa3cda9ef9f1271cd94980235dff07edea97412b038649144b7402",
        "sha256:fabad9b0a6c19ab603cc982977b1fa04d922c60299bbf6565692bd159e6f0460",
        "sha256:240cb13bd279371f6e2ebe5baa42dafd611b6e0d2c5a0ea5379e11e11cac00e9",
    )


def test_public_result_is_bounded_authority_free_and_contains_no_commitment() -> None:
    result = InternalProducerResult("TOOL_EFFECT_COMPLETED", digest("event"), CLAIM, False)
    assert result.body() == {
        "schema": "zekam-v4-internal-producer-result/v1",
        "event_kind": "TOOL_EFFECT_COMPLETED",
        "event_digest": digest("event"),
        "producer_ref": CLAIM,
        "replay": False,
        "runtime_job_terminal_written": False,
        "terminal_outbox_written": False,
        "grants_authority": False,
    }
    rendered = repr(result.body())
    for secret in ("nonce", "payload", "raw_content", "effect_commitment"):
        assert secret not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("event_kind", "UNKNOWN"),
        ("event_kind", None),
        ("event_digest", None),
        ("producer_ref", "bad producer"),
        ("replay", 0),
    ),
)
def test_public_result_rejects_untyped_or_unknown_fields(field: str, value: object) -> None:
    values: dict[str, object] = {
        "event_kind": "TOOL_EFFECT_COMPLETED",
        "event_digest": digest("event"),
        "producer_ref": CLAIM,
        "replay": False,
    }
    values[field] = value
    with pytest.raises(ValidationFailed):
        InternalProducerResult(**values)  # type: ignore[arg-type]


def test_b1_has_no_unknown_recovery_crash_or_production_issuer_api() -> None:
    names = set(dir(SQLiteDormantV4InternalProducer))
    assert not names.intersection(
        {"unknown", "record_unknown", "recover", "crash_recovered", "finish", "release_lease"}
    )
