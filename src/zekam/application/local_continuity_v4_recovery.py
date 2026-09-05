from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from zekam.application.local_continuity import ContinuityBinding, ContinuityTail
from zekam.application.local_continuity_v4_internal import (
    _binding,
    _digest,
    _issued_time,
    _key,
    _positive_int,
    _runtime_time,
    _tail,
    _uuid,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ValidationFailed

B2_EVENT_NS = UUID("15a556c8-9258-5fd0-8c67-3051d9e8d75d")

_UNKNOWN_CATEGORIES = (
    "executor-ambiguous",
    "executor-timeout-after-claim",
    "executor-process-lost-after-claim",
    "transport-ack-unknown",
    "sanitized-exception-after-claim",
)


@dataclass(frozen=True, slots=True)
class _ClaimRecoveryRequest:
    binding: ContinuityBinding
    claim_id: str
    expected_revision_digest: str

    def __post_init__(self) -> None:
        _binding(self.binding)
        _uuid(self.claim_id, "B2 claim")
        _digest(self.expected_revision_digest, "B2 expected revision")


@dataclass(frozen=True, slots=True)
class UnknownEffectRequest(_ClaimRecoveryRequest):
    pass


@dataclass(frozen=True, slots=True)
class ReceiptlessRecoveryRequest(_ClaimRecoveryRequest):
    pass


@dataclass(frozen=True, slots=True)
class ResolveEffectRecoveryRequest:
    binding: ContinuityBinding
    recovery_case_id: str
    expected_revision_digest: str
    expected_tail: ContinuityTail

    def __post_init__(self) -> None:
        _binding(self.binding)
        _uuid(self.recovery_case_id, "B2 recovery case")
        _digest(self.expected_revision_digest, "B2 expected revision")
        _tail(self.expected_tail)


@dataclass(frozen=True, slots=True, init=False)
class _FrozenEffectSnapshot:
    binding_digest: str
    job_id: str
    claim_id: str
    lease_id: str
    lease_owner_id: str
    lease_owner_pid: int
    lease_owner_token: str
    fencing_token: int
    operation: str
    effect_commitment_digest: str
    claimed_at: str


@dataclass(frozen=True, slots=True, init=False)
class FrozenUnknownEffectSnapshot(_FrozenEffectSnapshot):
    unknown_category: str
    unknown_commitment_digest: str
    observed_at: str
    issuer_receipt_id: str

    def __new__(cls) -> FrozenUnknownEffectSnapshot:
        raise TypeError("B2 unknown snapshots require a fixed issuer")

    def __post_init__(self) -> None:
        _common_snapshot(self)
        if (
            type(self.unknown_category) is not str
            or self.unknown_category not in _UNKNOWN_CATEGORIES
        ):
            raise ValidationFailed("B2 unknown category unsupported")
        _digest(self.unknown_commitment_digest, "B2 unknown commitment")
        _issued_time(self.observed_at, "B2 unknown observed_at")
        _key(self.issuer_receipt_id, "B2 unknown issuer receipt")


@dataclass(frozen=True, slots=True, init=False)
class FrozenReceiptlessRecoverySnapshot(_FrozenEffectSnapshot):
    recovery_reason: str
    observed_at: str
    issuer_receipt_id: str

    def __new__(cls) -> FrozenReceiptlessRecoverySnapshot:
        raise TypeError("B2 receiptless snapshots require a fixed manager")

    def __post_init__(self) -> None:
        _common_snapshot(self)
        if type(self.recovery_reason) is not str or self.recovery_reason not in {
            "lease-expired",
            "owner-incarnation-lost",
        }:
            raise ValidationFailed("B2 recovery reason unsupported")
        _issued_time(self.observed_at, "B2 recovery observed_at")
        _key(self.issuer_receipt_id, "B2 recovery issuer receipt")


def _common_snapshot(value: _FrozenEffectSnapshot) -> None:
    _digest(value.binding_digest, "B2 binding")
    _uuid(value.job_id, "B2 job")
    _uuid(value.claim_id, "B2 claim")
    _uuid(value.lease_id, "B2 lease")
    _key(value.lease_owner_id, "B2 lease owner")
    _positive_int(value.lease_owner_pid, "B2 lease owner PID")
    _key(value.lease_owner_token, "B2 lease owner token")
    _positive_int(value.fencing_token, "B2 fencing token")
    _key(value.operation, "B2 effect operation")
    _digest(value.effect_commitment_digest, "B2 effect commitment")
    _runtime_time(value.claimed_at, "B2 effect claimed_at")


@dataclass(frozen=True, slots=True, init=False)
class FrozenEffectRecoveryResolutionSnapshot:
    binding_digest: str
    job_id: str
    claim_id: str
    recovery_case_id: str
    outcome: str
    resolution_commitment_digest: str
    resolved_at: str
    issuer_receipt_id: str

    def __new__(cls) -> FrozenEffectRecoveryResolutionSnapshot:
        raise TypeError("B2 resolution snapshots require a fixed adjudicator")

    def __post_init__(self) -> None:
        _digest(self.binding_digest, "B2 resolution binding")
        _uuid(self.job_id, "B2 resolution job")
        _uuid(self.claim_id, "B2 resolution claim")
        _uuid(self.recovery_case_id, "B2 recovery case")
        if type(self.outcome) is not str or self.outcome not in {"completed", "failed"}:
            raise ValidationFailed("B2 recovery outcome unsupported")
        _digest(self.resolution_commitment_digest, "B2 resolution commitment")
        _issued_time(self.resolved_at, "B2 resolved_at")
        _key(self.issuer_receipt_id, "B2 resolution issuer receipt")


class UnknownEffectIssuer(Protocol):
    def snapshot(self, request: UnknownEffectRequest) -> FrozenUnknownEffectSnapshot: ...

    def recheck(self, snapshot: FrozenUnknownEffectSnapshot) -> None: ...


class ReceiptlessRecoveryIssuer(Protocol):
    def snapshot(
        self, request: ReceiptlessRecoveryRequest
    ) -> FrozenReceiptlessRecoverySnapshot: ...

    def recheck(self, snapshot: FrozenReceiptlessRecoverySnapshot) -> None: ...


class EffectRecoveryAdjudicator(Protocol):
    def snapshot(
        self, request: ResolveEffectRecoveryRequest
    ) -> FrozenEffectRecoveryResolutionSnapshot: ...

    def recheck(self, snapshot: FrozenEffectRecoveryResolutionSnapshot) -> None: ...


@dataclass(frozen=True, slots=True, init=False)
class RecoveryResult:
    _body_bytes: bytes

    def __new__(cls, *_args: object, **_kwargs: object) -> RecoveryResult:
        raise TypeError("B2 results require an approved factory")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("B2 recovery results are final")

    def __post_init__(self) -> None:
        if type(self._body_bytes) is not bytes or len(self._body_bytes) > 32_768:
            raise ValidationFailed("B2 exact immutable result body required")
        try:
            encoded = self._body_bytes.decode("utf-8", errors="strict")
            body = json.loads(encoded)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ValidationFailed("B2 exact immutable result body required") from exc
        if type(body) is not dict or canonical_json(body).encode("utf-8") != self._body_bytes:
            raise ValidationFailed("B2 exact immutable result body required")
        _validate_result_body(body)

    def body(self) -> dict[str, object]:
        self.__post_init__()
        return dict(json.loads(self._body_bytes.decode("utf-8")))

    @property
    def result_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class _UnknownEntryResultPayload:
    status: str
    claim_id: str
    recovery_case_id: str
    attachment_revision_digest: str

    def __post_init__(self) -> None:
        _validate_entry_payload(self)

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("B2 result payloads are final")


@dataclass(frozen=True, slots=True)
class _ReceiptlessEntryResultPayload:
    status: str
    claim_id: str
    recovery_case_id: str
    attachment_revision_digest: str

    def __post_init__(self) -> None:
        _validate_entry_payload(self)

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("B2 result payloads are final")


def _validate_entry_payload(
    value: _UnknownEntryResultPayload | _ReceiptlessEntryResultPayload,
) -> None:
    if type(value.status) is not str or value.status not in {"fresh", "replayed"}:
        raise ValidationFailed("B2 entry result status invalid")
    _uuid(value.claim_id, "B2 result claim")
    _uuid(value.recovery_case_id, "B2 result case")
    _digest(value.attachment_revision_digest, "B2 result revision")


@dataclass(frozen=True, slots=True)
class _CompletedRecoveryResultPayload:
    status: str
    claim_id: str
    recovery_case_id: str
    recovery_resolution_id: str
    crash_recovered_event_digest: str
    restored_revision_digest: str

    def __post_init__(self) -> None:
        _validate_resolution_payload(self)
        _digest(self.crash_recovered_event_digest, "B2 result event")
        _digest(self.restored_revision_digest, "B2 result revision")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("B2 result payloads are final")


@dataclass(frozen=True, slots=True)
class _FailedRecoveryAttentionResultPayload:
    status: str
    claim_id: str
    recovery_case_id: str
    recovery_resolution_id: str

    def __post_init__(self) -> None:
        _validate_resolution_payload(self)

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("B2 result payloads are final")


def _validate_resolution_payload(
    value: _CompletedRecoveryResultPayload | _FailedRecoveryAttentionResultPayload,
) -> None:
    if type(value.status) is not str or value.status not in {"fresh", "replayed"}:
        raise ValidationFailed("B2 resolution result status invalid")
    _uuid(value.claim_id, "B2 result claim")
    _uuid(value.recovery_case_id, "B2 result case")
    _uuid(value.recovery_resolution_id, "B2 result resolution")


@dataclass(frozen=True, slots=True)
class _RecoveryCommitOutcomePayload:
    operation: str
    claim_id: str
    recovery_case_id: str

    def __post_init__(self) -> None:
        if type(self.operation) is not str or self.operation not in {
            "unknown-entry",
            "receiptless-entry",
            "resolve",
        }:
            raise ValidationFailed("B2 commit result operation invalid")
        _uuid(self.claim_id, "B2 result claim")
        _uuid(self.recovery_case_id, "B2 result case")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("B2 result payloads are final")


_ResultPayload = (
    _UnknownEntryResultPayload
    | _ReceiptlessEntryResultPayload
    | _CompletedRecoveryResultPayload
    | _FailedRecoveryAttentionResultPayload
    | _RecoveryCommitOutcomePayload
)


def _result(payload: _ResultPayload) -> RecoveryResult:
    body: dict[str, object]
    if type(payload) in {_UnknownEntryResultPayload, _ReceiptlessEntryResultPayload}:
        entry = cast(_UnknownEntryResultPayload | _ReceiptlessEntryResultPayload, payload)
        route = "unknown" if type(payload) is _UnknownEntryResultPayload else "receiptless"
        body = {
            "schema": f"zekam-v4-{route}-recovery-entry-result/v1",
            "status": entry.status,
            "claim_id": entry.claim_id,
            "recovery_case_id": entry.recovery_case_id,
            "attachment_revision_digest": entry.attachment_revision_digest,
            "job_state": "recovery-required",
            "event_written": False,
        }
    elif type(payload) is _CompletedRecoveryResultPayload:
        body = {
            "schema": "zekam-v4-effect-recovery-completed-result/v1",
            "status": payload.status,
            "claim_id": payload.claim_id,
            "recovery_case_id": payload.recovery_case_id,
            "recovery_resolution_id": payload.recovery_resolution_id,
            "job_state": "completed",
            "crash_recovered_event_digest": payload.crash_recovered_event_digest,
            "restored_revision_digest": payload.restored_revision_digest,
            "attention": False,
        }
    elif type(payload) is _FailedRecoveryAttentionResultPayload:
        body = {
            "schema": "zekam-v4-effect-recovery-failed-attention-result/v1",
            "status": payload.status,
            "claim_id": payload.claim_id,
            "recovery_case_id": payload.recovery_case_id,
            "recovery_resolution_id": payload.recovery_resolution_id,
            "job_state": "failed",
            "attachment_state": "recovery-required",
            "event_written": False,
            "attention": True,
        }
    elif (
        isinstance(payload, _RecoveryCommitOutcomePayload)
        and type(payload) is _RecoveryCommitOutcomePayload
    ):
        body = {
            "schema": "zekam-v4-recovery-commit-outcome/v1",
            "status": "not-committed-or-unobservable",
            "operation": payload.operation,
            "claim_id": payload.claim_id,
            "recovery_case_id": payload.recovery_case_id,
            "safe_to_retry": True,
        }
    else:
        raise ValidationFailed("B2 exact typed result payload required")
    body.update(
        grants_authority=False,
        approval_inherited=False,
        production_activated=False,
    )
    _validate_result_body(body)
    value = object.__new__(RecoveryResult)
    encoded = canonical_json(body).encode("utf-8")
    if len(encoded) > 32_768:
        raise ValidationFailed("B2 result outside byte bound")
    object.__setattr__(value, "_body_bytes", encoded)
    value.__post_init__()
    return value


def _validate_result_body(body: dict[str, object]) -> None:
    schema = body.get("schema")
    fresh = type(body.get("status")) is str and body["status"] in {"fresh", "replayed"}
    common = {"schema", "status", "claim_id", "recovery_case_id"}
    flags = {"grants_authority", "approval_inherited", "production_activated"}
    if any(body.get(key) is not False for key in flags):
        raise ValidationFailed("B2 result authority flags must be false")
    entry_schemas = {
        "zekam-v4-unknown-recovery-entry-result/v1",
        "zekam-v4-receiptless-recovery-entry-result/v1",
    }
    rules: dict[str, tuple[set[str], dict[str, object]]] = {
        **{
            value: (
                {"attachment_revision_digest", "job_state", "event_written"},
                {"job_state": "recovery-required", "event_written": False},
            )
            for value in entry_schemas
        },
        "zekam-v4-effect-recovery-completed-result/v1": (
            {
                "recovery_resolution_id",
                "job_state",
                "crash_recovered_event_digest",
                "restored_revision_digest",
                "attention",
            },
            {"job_state": "completed", "attention": False},
        ),
        "zekam-v4-effect-recovery-failed-attention-result/v1": (
            {
                "recovery_resolution_id",
                "job_state",
                "attachment_state",
                "event_written",
                "attention",
            },
            {
                "job_state": "failed",
                "attachment_state": "recovery-required",
                "event_written": False,
                "attention": True,
            },
        ),
    }
    if schema in rules:
        extra, fixed = rules[str(schema)]
        if (
            set(body) != common | flags | extra
            or not fresh
            or any(
                (type(expected) is bool and body.get(key) is not expected)
                or (type(expected) is not bool and body.get(key) != expected)
                for key, expected in fixed.items()
            )
        ):
            raise ValidationFailed("B2 result contract invalid")
        if schema in entry_schemas:
            _digest(body.get("attachment_revision_digest"), "B2 result revision")
        elif schema == "zekam-v4-effect-recovery-completed-result/v1":
            _digest(body.get("crash_recovered_event_digest"), "B2 result event")
            _digest(body.get("restored_revision_digest"), "B2 result revision")
    elif schema == "zekam-v4-recovery-commit-outcome/v1":
        expected = common | flags | {"operation", "safe_to_retry"}
        if (
            set(body) != expected
            or body.get("status") != "not-committed-or-unobservable"
            or type(body.get("operation")) is not str
            or body["operation"] not in {"unknown-entry", "receiptless-entry", "resolve"}
            or body.get("safe_to_retry") is not True
        ):
            raise ValidationFailed("B2 commit result contract invalid")
    else:
        raise ValidationFailed("B2 result schema unsupported")
    _uuid(body.get("claim_id"), "B2 result claim")
    _uuid(body.get("recovery_case_id"), "B2 result case")
    if "recovery_resolution_id" in body:
        _uuid(body.get("recovery_resolution_id"), "B2 result resolution")


def entry_result(
    *,
    route: object,
    status: object,
    claim_id: str,
    recovery_case_id: str,
    attachment_revision_digest: str,
) -> RecoveryResult:
    if type(route) is not str or route not in {"unknown", "receiptless"}:
        raise ValidationFailed("B2 entry result route invalid")
    if type(status) is not str or status not in {"fresh", "replayed"}:
        raise ValidationFailed("B2 entry result status invalid")
    payload_type = (
        _UnknownEntryResultPayload if route == "unknown" else _ReceiptlessEntryResultPayload
    )
    return _result(payload_type(status, claim_id, recovery_case_id, attachment_revision_digest))


def resolution_result(
    *,
    status: object,
    outcome: object,
    claim_id: str,
    recovery_case_id: str,
    recovery_resolution_id: str,
    crash_recovered_event_digest: str | None = None,
    restored_revision_digest: str | None = None,
) -> RecoveryResult:
    if type(outcome) is not str or outcome not in {"completed", "failed"}:
        raise ValidationFailed("B2 resolution result outcome invalid")
    if type(status) is not str or status not in {"fresh", "replayed"}:
        raise ValidationFailed("B2 resolution result status invalid")
    if outcome == "failed":
        if crash_recovered_event_digest is not None or restored_revision_digest is not None:
            raise ValidationFailed("B2 failed result rejects restored evidence")
        return _result(
            _FailedRecoveryAttentionResultPayload(
                status, claim_id, recovery_case_id, recovery_resolution_id
            )
        )
    if crash_recovered_event_digest is None or restored_revision_digest is None:
        raise ValidationFailed("B2 completed result requires restored evidence")
    return _result(
        _CompletedRecoveryResultPayload(
            status,
            claim_id,
            recovery_case_id,
            recovery_resolution_id,
            crash_recovered_event_digest,
            restored_revision_digest,
        )
    )


def commit_outcome(
    *,
    operation: object,
    claim_id: str,
    recovery_case_id: str,
) -> RecoveryResult:
    if type(operation) is not str or operation not in {
        "unknown-entry",
        "receiptless-entry",
        "resolve",
    }:
        raise ValidationFailed("B2 commit result operation invalid")
    return _result(_RecoveryCommitOutcomePayload(operation, claim_id, recovery_case_id))
